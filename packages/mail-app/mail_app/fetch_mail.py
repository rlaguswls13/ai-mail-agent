"""
Gmail / Naver / Outlook 메일함을 IMAP으로 조회해, 계정별로 app.db에 마지막으로
저장된 메일 이후(신규 계정은 어제부터)를 가져와 data/app.db(SQLite)에 원본을 쌓고,
카테고리별 action(trash/save/read)에 매칭된 메일을 처리한다.

외부 패키지 불필요 (표준 라이브러리만 사용) - LLM 토큰을 전혀 쓰지 않는다.
매일 새벽 Windows 작업 스케줄러로 이 스크립트를 실행하도록 등록해서 쓴다.

실제 조회/분류/액션 로직은 mail_core(accounts / mail_fetch / classify / actions)와
mail_app(config_store / mail_log_store)에 나눠져 있고, 이 파일은 그것들을 엮어서
실행하는 CLI 진입점 역할만 한다(`python -m mail_app.fetch_mail`). 대시보드는
report_*.json이 아니라 data/app.db를 직접 쿼리해서 generate_html.py가 만든다.

data/app.db는 카테고리 설정(categories 테이블)과 원본 메일 로그(messages/action_runs
테이블)를 파일 하나로 합친 것이다(예전엔 categories.db + mail_log.db로 나뉘어
있었음) - 테이블 이름이 겹치지 않아서 병합에 문제가 없었다.
"""
import argparse
import imaplib
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from mail_core.accounts import IMAP_SERVERS, load_accounts
from mail_core.imap_auth import authenticate
from mail_core.actions import (
    decode_mailbox_name,
    find_archive_folder,
    find_trash_folder,
    group_uids_by_action,
    mark_as_read,
    move_to_folder,
)
from mail_core.classify import classify, needs_contents
from mail_core.mail_fetch import (
    MAX_CONNECTIONS_PER_ACCOUNT,
    MAX_WORKERS,
    build_date_chunks,
    fetch_account_headers,
    fetch_body_texts,
)

from mail_app import app_paths
from mail_app.config_store import load_categories
from mail_app.mail_log_store import (
    last_message_date,
    log_action_run,
    mark_message_status,
    reconcile_missing,
    upsert_messages,
)

DATA_DIR = app_paths.data_dir()  # 기본: 저장소의 data/. 데스크톱 앱은 MAIL_AGENT_DATA_DIR.
DB_PATH = app_paths.db_path()
ACCOUNTS_PATH = app_paths.config_dir() / "accounts.yaml"

# action 이름 -> 대상 폴더를 찾는 함수. "read"는 폴더 이동이 아니라서 여기 없음.
ACTION_FOLDER_FINDERS = {
    "trash": find_trash_folder,
    "save": find_archive_folder,
}

# action 이름 -> messages.status에 기록할 값 (3b: 액션 후 상태 동기화). "read"는 상태가
# 아니라 is_read 컬럼을 따로 쓰므로 여기 없음.
ACTION_STATUS = {
    "trash": "trashed",
    "save": "archived",
}


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help=(
            "YYYY-MM-DD. 이 날짜 0시 이후 메일을 조회한다 (기본값: 계정별 마지막 갱신"
            "(app.db에 저장된 마지막 message_date) 이후부터. 아직 저장된 메일이 없는 "
            "신규 계정은 어제부터)."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help=(
            "카테고리 action이 keep이 아닌 것에 매칭된 메일을 실제로 처리한다"
            "(trash=휴지통 이동, save=보관 이동, read=읽음 표시). 기본값은 "
            "dry-run(실제로 처리하지 않고 몇 건이 대상인지만 로그/DB에 남김)."
        ),
    )
    return parser.parse_args()


def classify_with_contents(msgs: list[dict], account: dict, categories: dict) -> dict:
    """헤더만으로 1차 분류하고, contents 키워드를 쓰는 카테고리가 있으면 아직 분류
    안 된 메일만 골라 본문을 추가로 가져와서 다시 분류한다.

    본문 조회는 비용이 커서(건당 IMAP 왕복 + 전체 메시지 다운로드), 정말 필요한
    경우(어떤 카테고리든 contents 키워드가 있고, 그걸로도 못 걸러낸 메일이 있을 때)
    에만, 그 남은 메일에 대해서만 수행한다.
    """
    classified = classify(msgs, categories)
    if not needs_contents(categories):
        return classified

    matched_uids = {
        m["uid"]
        for matches in classified["category_matches"].values()
        for m in matches
        if m.get("uid")
    }
    pending = [m for m in msgs if m.get("uid") and m["uid"] not in matched_uids]
    if not pending:
        return classified

    bodies = fetch_body_texts(account, [m["uid"] for m in pending])
    if not bodies:
        return classified

    for m in pending:
        if m["uid"] in bodies:
            m["contents"] = bodies[m["uid"]]
    return classify(msgs, categories)


def _apply_account_actions(account: dict, grouped: dict[str, list[str]]) -> list[dict]:
    """한 계정에 대해 IMAP 액션만 수행하고 결과 레코드 리스트를 반환한다 (DB 미접근).

    스레드에서 병렬로 돌리기 위해 순수 IMAP 작업만 담당한다 - app.db 쓰기(action_runs
    기록, messages 상태 동기화)는 호출부가 메인 스레드에서 몰아서 처리한다(SQLite
    동시 쓰기 회피). IMAP 커넥션 오류는 예외로 전파해서 호출부가 계정 전체를 실패로
    기록하게 한다.
    """
    results: list[dict] = []
    imap = imaplib.IMAP4_SSL(IMAP_SERVERS[account["type"]], 993)
    try:
        authenticate(imap, account)
        imap.select("INBOX", readonly=False)
        for action, uids in grouped.items():
            if action == "read":
                succeeded, failed_uids = mark_as_read(imap, uids)
                results.append({
                    "action": action, "candidates": len(uids), "succeeded": succeeded,
                    "failed": len(failed_uids), "folder": None, "note": None,
                })
                continue

            finder = ACTION_FOLDER_FINDERS.get(action)
            folder = finder(imap, account["type"]) if finder else None
            if not folder:
                results.append({
                    "action": action, "candidates": len(uids), "succeeded": [],
                    "failed": 0, "folder": None, "note": "대상 폴더를 찾지 못함",
                })
                continue

            succeeded, failed_uids = move_to_folder(imap, uids, folder)
            results.append({
                "action": action, "candidates": len(uids), "succeeded": succeeded,
                "failed": len(failed_uids), "folder": decode_mailbox_name(folder), "note": None,
            })
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return results


def run_actions(
    accounts: list[dict], per_account: dict, categories: dict, apply: bool, run_at: str
) -> None:
    """categories의 action(keep이 아닌 것)별로 메일을 처리하고, 결과를
    app.db의 action_runs에 기록한다.

    apply가 False면(기본값) 아무것도 옮기거나 표시하지 않고, 몇 건이 대상인지만
    계산해서 dry-run으로 기록한다. apply면 계정별 IMAP 작업을 조회 경로와 동일하게
    스레드 풀로 병렬 실행한다(계정마다 connect+login+LIST 고정 비용이 순차로 쌓이던
    것을 제거) - DB 쓰기는 결과를 받아 메인 스레드에서 몰아 한다.
    """
    work: list[tuple[dict, dict[str, list[str]]]] = []
    for account in accounts:
        user = account["user"]
        if user not in per_account:
            continue
        grouped = group_uids_by_action(per_account[user], categories)
        if grouped:
            work.append((account, grouped))

    if not apply:
        for account, grouped in work:
            for action, uids in grouped.items():
                log_action_run(
                    DB_PATH, run_at, True, account["user"], action, len(uids), 0, 0, None, None
                )
            parts = ", ".join(f"{action} {len(uids)}건" for action, uids in grouped.items())
            print(f"[dry-run] [{account['user']}] {parts} 예정 (--apply로 실행)")
        return

    if not work:
        return

    def worker(account: dict, grouped: dict[str, list[str]]):
        # OSError까지 잡는다 - 한 계정의 연결 실패(DNS/타임아웃 등)가 다른 계정의
        # 액션까지 죽이지 않도록. IMAP4.error는 로그인/명령 거부.
        try:
            return account, grouped, _apply_account_actions(account, grouped), None
        except (imaplib.IMAP4.error, OSError) as e:
            return account, grouped, None, e

    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(work))) as executor:
        futures = [executor.submit(worker, account, grouped) for account, grouped in work]
        for future in as_completed(futures):
            account, grouped, results, err = future.result()
            user = account["user"]

            if err is not None:
                print(f"[{user}] 액션 실행 중 IMAP 오류: {err}", file=sys.stderr)
                for action, uids in grouped.items():
                    log_action_run(
                        DB_PATH, run_at, False, user, action,
                        len(uids), 0, len(uids), None, str(err),
                    )
                continue

            for r in results:
                action, succeeded = r["action"], r["succeeded"]
                if r["note"] == "대상 폴더를 찾지 못함":
                    print(
                        f"[{user}] {action} 대상 폴더를 찾지 못해 건너뜀 ({r['candidates']}건 유지)",
                        file=sys.stderr,
                    )
                    log_action_run(
                        DB_PATH, run_at, False, user, action,
                        r["candidates"], 0, 0, None, r["note"],
                    )
                    continue

                if action == "read":
                    mark_message_status(DB_PATH, user, succeeded, is_read=True)
                else:
                    mark_message_status(DB_PATH, user, succeeded, status=ACTION_STATUS.get(action))
                log_action_run(
                    DB_PATH, run_at, False, user, action,
                    r["candidates"], len(succeeded), r["failed"], r["folder"], None,
                )
                fail_note = f", {r['failed']}건 실패" if r["failed"] else ""
                if action == "read":
                    print(f"[{user}] 읽음 표시 {len(succeeded)}건 완료{fail_note}")
                else:
                    print(f"[{user}] {action} {len(succeeded)}건을 '{r['folder']}'(으)로 이동 완료{fail_note}")


def main():
    args = parse_args()

    if not DB_PATH.exists():
        print(f"DB가 없습니다: {DB_PATH} (관리 화면 `python -m admin_ui`를 먼저 한 번 실행하면 생성됩니다)", file=sys.stderr)
        sys.exit(1)
    categories = load_categories(DB_PATH)
    if not categories:
        print(f"카테고리가 하나도 없습니다: {DB_PATH}", file=sys.stderr)
        sys.exit(1)

    accounts = load_accounts(ACCOUNTS_PATH)
    if not accounts:
        print(
            f"연동된 계정이 없습니다. {ACCOUNTS_PATH} 파일에 계정 정보를 채워주세요.",
            file=sys.stderr,
        )
        sys.exit(1)

    since_override: datetime | None = None
    if args.since:
        try:
            since_override = datetime.strptime(args.since, "%Y-%m-%d")
        except ValueError:
            print(f"--since 형식이 올바르지 않습니다 (YYYY-MM-DD): {args.since}", file=sys.stderr)
            sys.exit(1)

    fallback_since = datetime.now() - timedelta(days=1)

    def account_since_date(account: dict) -> datetime:
        # --since가 명시되면 모든 계정에 그대로 적용(과거 백필 등 의도적인 재조회용).
        # 아니면 계정별로 app.db에 저장된 마지막 message_date부터 이어서 조회한다 -
        # 매일 실행을 며칠 건너뛰어도(예: PC를 꺼뒀다 켬) 그 사이 메일을 놓치지 않는다.
        # 아직 저장된 메일이 없는 신규 계정은 기존과 같이 어제부터 조회한다.
        if since_override is not None:
            return since_override
        return last_message_date(DB_PATH, account["user"]) or fallback_since

    # 계정마다 청크(주 단위, 매일 실행이면 청크 1개)를 만들고, 계정별 커넥션 수를
    # MAX_CONNECTIONS_PER_ACCOUNT로 제한한 채 전체를 스레드 풀에서 병렬 조회한다.
    # 청크 경계가 SINCE/BEFORE 반개구간이라 겹치는 메일이 없으므로 그냥 합치면 된다.
    account_semaphores = {
        account["user"]: threading.Semaphore(MAX_CONNECTIONS_PER_ACCOUNT)
        for account in accounts
    }
    work_items = [
        (account, chunk_since, chunk_until)
        for account in accounts
        for chunk_since, chunk_until in build_date_chunks(account_since_date(account))
    ]

    def run_chunk(account: dict, chunk_since: datetime, chunk_until: datetime | None):
        with account_semaphores[account["user"]]:
            try:
                msgs = fetch_account_headers(account, chunk_since, chunk_until)
                return account["user"], chunk_since, chunk_until, msgs, None
            except imaplib.IMAP4.error as e:
                return account["user"], chunk_since, chunk_until, [], e

    per_account_messages: dict[str, list[dict]] = {a["user"]: [] for a in accounts}
    account_had_success: dict[str, bool] = {a["user"]: False for a in accounts}
    successful_chunks: list[tuple[str, datetime, datetime | None, list[dict]]] = []

    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, len(work_items))) as executor:
        futures = [executor.submit(run_chunk, *item) for item in work_items]
        for future in as_completed(futures):
            user, chunk_since, chunk_until, msgs, err = future.result()
            if err:
                print(f"[{user}] IMAP 오류(해당 구간만 누락, 나머지는 계속 진행): {err}", file=sys.stderr)
                continue
            account_had_success[user] = True
            per_account_messages[user].extend(msgs)
            successful_chunks.append((user, chunk_since, chunk_until, msgs))

    # 원본 메일을 그대로 app.db에 쌓는다 - 분류 결과가 아니라 raw 데이터라서,
    # 나중에 카테고리 규칙이 바뀌어도 재조회 없이 다시 분류할 수 있다.
    DATA_DIR.mkdir(exist_ok=True)
    for user, msgs in per_account_messages.items():
        upsert_messages(DB_PATH, msgs)

    # 3c: 이번에 실제로 조회에 성공한 구간마다, 그 구간에서 방금 확인한 "지금 살아있는
    # uid 목록"과 app.db에 저장된 값을 대조해서 사용자가 다른 클라이언트(웹메일 등)에서
    # 직접 지운 메일을 정리한다. run_actions()보다 반드시 먼저 실행해야 한다 - 이번
    # 실행에서 막 trash/save 처리될 메일은 아직 status='active'인 채 live_uids 안에
    # 있으므로 여기서는 지워지지 않고, 그 다음 run_actions()가 정상적으로 상태를 바꾼다.
    reconciled_total = 0
    for user, chunk_since, chunk_until, msgs in successful_chunks:
        live_uids = [m["uid"] for m in msgs if m.get("uid")]
        reconciled_total += reconcile_missing(DB_PATH, user, chunk_since, chunk_until, live_uids)
    if reconciled_total:
        print(f"메일함에서 직접 삭제된 것으로 확인된 {reconciled_total}건을 app.db에서 정리했습니다.")

    # 헤더 기준(senders -> title) 1차 분류 후, contents 키워드를 쓰는 카테고리가 있으면
    # 아직 분류 안 된 메일만 본문을 추가로 가져와서 2차 분류한다 (계정별로 독립적).
    # 이 분류 결과는 저장하지 않는다 - 이번 실행에서 액션(trash/save/read) 대상을
    # 정하는 데만 쓰고, 대시보드는 generate_html.py가 app.db를 다시 쿼리해서
    # 현재 카테고리 규칙으로 새로 분류한다.
    per_account = {}
    total = 0
    for account in accounts:
        user = account["user"]
        if not account_had_success[user]:
            continue
        msgs = per_account_messages[user]
        classified = classify_with_contents(msgs, account, categories)
        per_account[user] = classified
        total += len(msgs)

    run_at = datetime.now().isoformat(timespec="seconds")
    run_actions(accounts, per_account, categories, args.apply, run_at)

    print(f"조회 완료: {total}건을 {DB_PATH}에 저장했습니다.")
    print("대시보드를 보려면: python -m mail_app.generate_html")


if __name__ == "__main__":
    main()
