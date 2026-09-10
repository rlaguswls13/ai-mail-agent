"""app.db 에 쌓인, 아직 메일함에 반영 안 된 카테고리 액션을 실제로 적용한다.

`fetch_mail --apply` 는 "이번에 새로 조회한" 메일에만 액션을 건다. 데스크톱 스케줄러가
dry-run(`/sync`)만 자동 실행하는 사이, 처리 대상(save/trash/read)인데 아직 INBOX 에
그대로인 메일이 계속 쌓인다. 이 CLI 는 DB 를 스캔해 **현재 카테고리 규칙상 처리
대상인 active 메일 전부**에 IMAP 액션을 건다(메일 재조회 없음).

    python -m mail_app.apply_pending           # 실제 적용
    python -m mail_app.apply_pending --count   # 건수만 (JSON 한 줄)
    python -m mail_app.apply_pending --json    # 적용 + NDJSON 결과 (데스크톱 셸용)

본문 키워드(categories.contents) 규칙은 DB 에 본문이 없어 여기서 매칭되지 않는다 -
발신인/도메인/제목 규칙 기준이다(대시보드 재분류와 동일한 한계).
"""
import argparse
import json
import sys
from datetime import datetime, timedelta

from mail_core.accounts import load_accounts, resolve_accounts_from_stdin
from mail_core.actions import group_uids_by_action
from mail_core.classify import classify

from mail_app import app_paths
from mail_app.config_store import load_categories
from mail_app.fetch_mail import ACTION_STATUS, _apply_account_actions
from mail_app.mail_log_store import (
    log_action_run,
    mark_message_status,
    query_messages,
)

DB_PATH = app_paths.db_path()
ACCOUNTS_PATH = app_paths.config_dir() / "accounts.yaml"

# 미적용 액션을 훑는 기본 기간(일). 스케줄러가 dry-run 만 자동 실행하는 사이 쌓인
# "최근" 미적용분을 잡는 게 목적이라 한 달로 좁게 잡는다. 더 옛날 메일(그동안 규칙이
# 바뀌어 소급 매칭되는 것 포함)까지 일괄 이동하는 건 위험하므로 `--days` 로 명시할 때만.
DEFAULT_DAYS = 30


def pending_by_account(db_path, categories: dict, days: int = DEFAULT_DAYS) -> dict[str, dict[str, list[str]]]:
    """{user: {action: [uid, ...]}} - 아직 메일함에 반영 안 된 액션 대상.

    action=read 는 이미 읽은 메일을 제외한다(is_read).
    """
    since = datetime.now() - timedelta(days=days)
    rows = query_messages(db_path, since, None, status="active")

    by_user: dict[str, list[dict]] = {}
    for m in rows:
        by_user.setdefault(m["account"], []).append(m)

    out: dict[str, dict[str, list[str]]] = {}
    for user, msgs in by_user.items():
        grouped = group_uids_by_action(classify(msgs, categories), categories)
        if not grouped:
            continue
        read_uids = {m["uid"] for m in msgs if m.get("is_read")}
        cleaned: dict[str, list[str]] = {}
        for action, uids in grouped.items():
            uids = [u for u in uids if action != "read" or u not in read_uids]
            if uids:
                cleaned[action] = uids
        if cleaned:
            out[user] = cleaned
    return out


def count_pending(db_path, categories: dict, days: int = DEFAULT_DAYS) -> dict:
    pend = pending_by_account(db_path, categories, days)
    by_action: dict[str, int] = {}
    total = 0
    for actions in pend.values():
        for action, uids in actions.items():
            by_action[action] = by_action.get(action, 0) + len(uids)
            total += len(uids)
    return {
        "total": total,
        "by_action": by_action,
        "by_account": {u: sum(len(x) for x in a.values()) for u, a in pend.items()},
    }


def apply_pending(db_path, accounts_path, days: int = DEFAULT_DAYS, *, emit=None) -> dict:
    """미적용 액션을 실제로 적용한다. emit(dict) 가 주어지면 NDJSON 이벤트를 낸다."""
    categories = load_categories(db_path)
    pend = pending_by_account(db_path, categories, days)
    accounts_cfg = {a["user"]: a for a in load_accounts(accounts_path)}
    run_at = datetime.now().isoformat(timespec="seconds")

    summary = {"applied": 0, "failed": 0, "accounts": []}
    for user, grouped in pend.items():
        account = accounts_cfg.get(user)
        candidates = sum(len(u) for u in grouped.values())
        if not account:
            summary["failed"] += candidates
            rec = {"account": user, "applied": 0, "failed": candidates, "note": "계정 설정 없음"}
            summary["accounts"].append(rec)
            emit and emit({"event": "account", **rec})
            continue
        try:
            results = _apply_account_actions(account, grouped)
        except Exception as e:  # noqa: BLE001 - 한 계정 실패가 다른 계정을 막지 않게
            summary["failed"] += candidates
            for action, uids in grouped.items():
                log_action_run(db_path, run_at, False, user, action, len(uids), 0, len(uids), None, str(e))
            rec = {"account": user, "applied": 0, "failed": candidates, "note": str(e)}
            summary["accounts"].append(rec)
            emit and emit({"event": "account", **rec})
            continue

        acc_applied = acc_failed = 0
        for r in results:
            action, succeeded = r["action"], r["succeeded"]
            if succeeded:
                if action == "read":
                    mark_message_status(db_path, user, succeeded, is_read=True)
                elif action in ACTION_STATUS:
                    mark_message_status(db_path, user, succeeded, status=ACTION_STATUS[action])
            acc_applied += len(succeeded)
            acc_failed += r["failed"]
            log_action_run(db_path, run_at, False, user, action, r["candidates"],
                           len(succeeded), r["failed"], r["folder"], r["note"])
        summary["applied"] += acc_applied
        summary["failed"] += acc_failed
        rec = {"account": user, "applied": acc_applied, "failed": acc_failed, "note": None}
        summary["accounts"].append(rec)
        emit and emit({"event": "account", **rec})

    return summary


def main(argv=None) -> int:
    resolve_accounts_from_stdin()
    ap = argparse.ArgumentParser(description="app.db 미적용 카테고리 액션 일괄 적용")
    ap.add_argument("--count", action="store_true", help="적용하지 않고 건수만 JSON 으로")
    ap.add_argument("--json", action="store_true", help="NDJSON 이벤트 출력 (데스크톱 셸용)")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS, help=f"훑을 기간(일, 기본 {DEFAULT_DAYS})")
    args = ap.parse_args(argv)

    if not DB_PATH.exists():
        print(f"DB가 없습니다: {DB_PATH}", file=sys.stderr)
        return 1
    categories = load_categories(DB_PATH)
    if not categories:
        print(f"카테고리가 없습니다: {DB_PATH}", file=sys.stderr)
        return 1

    if args.count:
        print(json.dumps(count_pending(DB_PATH, categories, args.days), ensure_ascii=False), flush=True)
        return 0

    def emit(obj):
        if args.json:
            print(json.dumps(obj, ensure_ascii=False), flush=True)

    summary = apply_pending(DB_PATH, ACCOUNTS_PATH, args.days, emit=emit if args.json else None)
    if args.json:
        emit({"event": "done", **summary})
    else:
        print(f"적용 {summary['applied']}건 · 실패 {summary['failed']}건")
        for rec in summary["accounts"]:
            note = f" ({rec['note']})" if rec["note"] else ""
            print(f"  [{rec['account']}] 적용 {rec['applied']} · 실패 {rec['failed']}{note}")
    return 1 if summary["failed"] and not summary["applied"] else 0


if __name__ == "__main__":
    sys.exit(main())
