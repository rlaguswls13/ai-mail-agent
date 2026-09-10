"""admin_ui `/vault` 블루프린트 - 정리함(보관함=archived / 휴지통=trashed).

- /vault          : 탭별 목록 + 대량 선택(체크박스 → vault-form 제출)
- /vault/restore  : 보관 폴더에서 message_id 로 찾아 INBOX 로 MOVE + 행 rebind
- /vault/purge    : 휴지통 폴더에서 message_id 로 찾아 EXPUNGE + 행 삭제
메일이 옮겨지면 UID 가 바뀌므로 message_id 로 대상 폴더에서 다시 찾는다.
message_id 없는(레거시) 행은 IMAP 반영 없이 DB 만 정리.
"""
import imaplib
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from datetime import datetime, timedelta

from flask import Blueprint, redirect, request

from mail_core.actions import (
    decode_mailbox_name,
    find_archive_folder,
    find_message_uid_by_id,
    find_trash_folder,
    move_to_folder,
    permanent_delete,
    select_folder,
)
from mail_core.accounts import IMAP_SERVERS, load_accounts
from mail_app.mail_log_store import delete_messages, log_action_run, query_messages, rebind_uid

from admin_ui._shared import ACCOUNTS_PATH, DB_PATH, build_qs, esc, page, run_state
from admin_ui._render import (
    MSG_DEFAULT_SINCE_DAYS,
    MSG_PAGE_SIZE_MAX,
    MSG_PAGE_SIZE_MIN,
    _account_options,
    _parse_page_num,
    _parse_page_size,
    load_all_messages,
    msg_table,
    render_pagination,
)

bp = Blueprint("vault", __name__)

# 보안: /vault/purge(영구삭제 EXPUNGE) 포함 이 블루프린트의 상태변경 POST 는
# admin_app._block_cross_origin_writes(@app.before_request)가 cross-origin/DNS-rebinding
# 으로부터 막는다. 여기엔 자체 CSRF 방어가 없다 - 반드시 그 app 에 등록해서 써야 한다.

# /vault 탭: (탭 키, 라벨, messages.status 값, 대상 폴더 finder, 되돌리기/삭제 동사).
VAULT_TABS = [
    ("archive", "보관함", "archived", find_archive_folder, "restore"),
    ("trash", "휴지통", "trashed", find_trash_folder, "purge"),
]
VAULT_TAB_BY_KEY = {t[0]: t for t in VAULT_TABS}


def render_vault_action_status(run: dict | None) -> str:
    if run is None:
        return ""
    verb = {"restore": "되돌리기", "purge": "영구 삭제"}.get(run["kind"], run["kind"])
    done_word = "되돌림" if run["kind"] == "restore" else "삭제됨"
    rows = []
    for r in run["results"]:
        bits = [f'서버 반영 {r["done"]}건 {done_word}']
        if r.get("db_only"):
            bits.append(f'{r["db_only"]}건은 목록에서만 정리(예전 메일, IMAP 미반영)')
        if r.get("failed"):
            bits.append(f'{r["failed"]}건 실패(서버에서 못 찾음 - 목록 유지)')
        if r.get("note"):
            bits.append(esc(r["note"]))
        rows.append(f'<div>{esc(r["account"])} - {" · ".join(bits)}</div>')
    return f"""
    <div class="run-status">
      <strong>{verb} 결과</strong> · {run['ran_at']}
      {"".join(rows) or "<div>처리한 항목이 없습니다.</div>"}
    </div>
    """


def _vault_tabs_html(active_tab: str) -> str:
    parts = []
    for key, label, *_ in VAULT_TABS:
        cls = ' class="active"' if key == active_tab else ""
        parts.append(f'<a href="/vault?tab={key}"{cls}>{label}</a>')
    return f'<div class="range-tabs">{"".join(parts)}</div>'


@bp.route("/vault")
def vault_page():
    tab = request.args.get("tab", "archive")
    if tab not in VAULT_TAB_BY_KEY:
        tab = "archive"
    _, _, status, _, verb = VAULT_TAB_BY_KEY[tab]

    account_filter = request.args.get("account", "").strip() or None
    page_size = _parse_page_size(request.args.get("page_size"))

    since = datetime.now() - timedelta(days=MSG_DEFAULT_SINCE_DAYS)
    all_messages, all_users, account_type_by_user, categories = load_all_messages(
        since, None, account_filter, status=status
    )
    pool = sorted(all_messages, key=lambda m: m.get("message_date") or "", reverse=True)

    total = len(pool)
    total_pages = max(1, math.ceil(total / page_size))
    page_num = min(_parse_page_num(request.args.get("page")), total_pages)
    start = (page_num - 1) * page_size
    page_items = pool[start : start + page_size]

    filter_state = {"tab": tab, "account": account_filter or "", "page_size": page_size}
    return_qs = build_qs(**filter_state)

    account_options = _account_options(all_users, account_type_by_user, account_filter)
    filter_form = f"""
    <form class="filter-form" method="get" action="/vault">
      <input type="hidden" name="tab" value="{esc(tab)}">
      <label>계정<select name="account">{account_options}</select></label>
      <label>페이지당 건수<input type="number" name="page_size" min="{MSG_PAGE_SIZE_MIN}" max="{MSG_PAGE_SIZE_MAX}" value="{page_size}"></label>
      <button class="btn" type="submit">필터 적용</button>
    </form>
    """

    table = msg_table(page_items, categories, with_account=True, with_select=True)
    prev_qs = build_qs(**filter_state, page=page_num - 1)
    next_qs = build_qs(**filter_state, page=page_num + 1)
    prev_link = f'<a href="/vault?{prev_qs}">← 이전</a>' if page_num > 1 else '<span class="disabled">← 이전</span>'
    next_link = f'<a href="/vault?{next_qs}">다음 →</a>' if page_num < total_pages else '<span class="disabled">다음 →</span>'
    pagination = render_pagination(prev_link, next_link, page_num, total_pages, total)

    if verb == "restore":
        action_url, intro = "/vault/restore", "보관(save) 처리한 메일입니다. 골라서 원래 받은편지함으로 되돌릴 수 있습니다."
        action_btn = '<button class="btn" type="submit" form="vault-form">선택한 메일 되돌리기</button>'
    else:
        action_url, intro = "/vault/purge", "휴지통으로 보낸 메일입니다. 골라서 서버에서 완전히 삭제(복구 불가)할 수 있습니다."
        action_btn = (
            '<button class="btn danger" type="submit" form="vault-form" '
            "onclick=\"return confirm('선택한 메일을 영구 삭제합니다. 복구할 수 없습니다. 계속할까요?')\">"
            "선택한 메일 영구 삭제</button>"
        )

    body = f"""
    <h1 class="page-title">🗂️ 정리함</h1>
    <p class="sub">{esc(intro)}</p>
    {_vault_tabs_html(tab)}
    {render_vault_action_status(run_state["last_vault_action"])}
    {filter_form}
    <form id="vault-form" method="post" action="{action_url}">
      <input type="hidden" name="return_qs" value="{esc(return_qs)}">
    </form>
    <div class="sel-bar">
      <span class="sel-count">이 페이지에서 체크한 메일에 적용 · 전체 {total}건</span>
      {action_btn}
    </div>
    {table}
    <p class="sub" style="margin-top:6px">※ 예전에 저장돼 식별자(Message-ID)가 없는 메일은
    IMAP 서버에는 반영되지 않고 이 목록에서만 정리됩니다.</p>
    {pagination}
    """
    return page("정리함", body, "vault")


def _vault_process(kind: str):
    """/vault/restore · /vault/purge 공통 처리.

    kind="restore": 보관 폴더에서 message_id로 찾아 INBOX로 MOVE → 그 행의 uid를 새
                    INBOX UID로 바꿔치고 status='active' (rebind_uid). 새 UID를 못 찾으면
                    행 삭제(다음 fetch가 재삽입할 수도).
    kind="purge":   휴지통 폴더에서 message_id로 찾아 EXPUNGE → messages 행 삭제.
    둘 다 계정별로 스레드 병렬(IMAP만), DB 갱신은 메인 스레드에서.

    안전장치:
    - 제출된 uid를 그 탭의 status(archived/trashed)인 실제 행하고만 교집합 - 오래된
      화면이나 위조 POST로 active(받은편지함) 메일을 지우는 걸 막는다.
    - message_id가 있는데 서버 폴더에서 못 찾으면(=조회 실패거나 이미 지워짐) '실패'로
      친다. DB 행은 건드리지 않는다.
    - message_id 없는 레거시 행: 되돌리기는 자동으로 못 한다(stale UID를 active 풀에 다시
      넣으면 이후 액션이 조용히 무효가 됨) → '실패'로 안내. 영구삭제는 행만 지운다.
    """
    # tab은 kind에서 파생 - VAULT_TABS의 불변식(archive↔restore, trash↔purge)을 강제.
    tab = "archive" if kind == "restore" else "trash"
    _, _, status_want, finder, _ = VAULT_TAB_BY_KEY[tab]
    sel = request.form.getlist("sel")
    return_qs = request.form.get("return_qs", "")

    submitted: dict[str, set[str]] = defaultdict(set)
    for item in sel:
        if "::" in item:
            acc, uid = item.split("::", 1)
            submitted[acc].add(uid)

    accounts_cfg = {a["user"]: a for a in load_accounts(ACCOUNTS_PATH)}
    since = datetime.now() - timedelta(days=MSG_DEFAULT_SINCE_DAYS)

    # 메인 스레드에서: 제출 uid ∩ (그 탭 status인 실제 행) + message_id 매핑.
    by_account: dict[str, list[str]] = {}
    mids_by_account: dict[str, dict[str, str | None]] = {}
    for user, uids in submitted.items():
        valid_rows = {
            m["uid"]: m for m in query_messages(DB_PATH, since, None, account=user, status=status_want)
        }
        keep = [u for u in uids if u in valid_rows]
        if keep:
            by_account[user] = keep
            mids_by_account[user] = {u: valid_rows[u].get("message_id") for u in keep}

    run_at = datetime.now().isoformat(timespec="seconds")
    LEGACY_NOTE = "식별자(Message-ID) 없음 - 웹메일에서 직접 처리하세요"

    def process_account(user: str, uids: list[str]) -> dict:
        account = accounts_cfg.get(user)
        mids = mids_by_account.get(user, {})
        legacy = [u for u in uids if not mids.get(u)]
        resolvable = [u for u in uids if mids.get(u)]
        # 레거시 행: 되돌리기는 자동 불가(실패), 영구삭제는 행만 지운다(purge_db).
        failed: list[str] = list(legacy) if kind == "restore" else []
        purge_db: list[str] = [] if kind == "restore" else list(legacy)
        rebind: dict[str, str | None] = {}   # restore 성공분: old_uid -> new INBOX uid(or None)
        imap_purged: list[str] = []           # purge 성공분: old_uid
        folder_display = None
        note = LEGACY_NOTE if legacy else None

        if account and resolvable:
            try:
                imap = imaplib.IMAP4_SSL(IMAP_SERVERS[account["type"]], 993)
                imap.login(account["user"], account["password"])
                try:
                    folder = finder(imap, account["type"])
                    if not folder:
                        note = "대상 폴더를 찾지 못함"
                        failed.extend(resolvable)
                    elif not select_folder(imap, folder):
                        note = f"{decode_mailbox_name(folder)} 폴더를 열 수 없음"
                        failed.extend(resolvable)
                    else:
                        folder_display = decode_mailbox_name(folder)
                        orig_to_new: dict[str, str] = {}
                        claimed: set[str] = set()
                        for u in resolvable:
                            try:
                                new_uid = find_message_uid_by_id(imap, folder, mids[u], select=False)
                            except imaplib.IMAP4.error:
                                new_uid = None  # 조회 실패 → 실패로 (DB는 안 건드림)
                            if new_uid and new_uid not in claimed:
                                orig_to_new[u] = new_uid
                                claimed.add(new_uid)
                            else:
                                failed.append(u)
                        if orig_to_new:
                            targets = list(orig_to_new.values())
                            if kind == "restore":
                                if not select_folder(imap, folder):
                                    failed.extend(orig_to_new)
                                    note = note or "폴더 재선택 실패"
                                else:
                                    ok_new, bad_new = move_to_folder(
                                        imap, targets, "INBOX", require_move=True
                                    )
                                    ok_set = set(ok_new)
                                    # 되돌린 메일의 새 INBOX UID를 조회해 행을 rebind.
                                    for orig, new in orig_to_new.items():
                                        if new not in ok_set:
                                            failed.append(orig)
                                            continue
                                        try:
                                            rebind[orig] = find_message_uid_by_id(
                                                imap, "INBOX", mids[orig], select=True
                                            )
                                        except imaplib.IMAP4.error:
                                            rebind[orig] = None
                                    if bad_new:
                                        note = note or "일부 되돌리기 실패(서버 거부 또는 MOVE 미지원)"
                            else:
                                ok_new, bad_new = permanent_delete(imap, folder, targets)
                                ok_set, bad_set = set(ok_new), set(bad_new)
                                for orig, new in orig_to_new.items():
                                    if new in ok_set:
                                        imap_purged.append(orig)
                                    elif new in bad_set:
                                        failed.append(orig)
                finally:
                    try:
                        imap.logout()
                    except Exception:
                        pass
            except (imaplib.IMAP4.error, OSError) as e:
                note = str(e)
                # 연결/로그인 자체가 실패 → 이 계정은 이번에 아무것도 안 건드린다.
                failed = [u for u in uids if u not in rebind and u not in imap_purged]
                purge_db = []
        elif not account:
            note = "계정 설정을 찾을 수 없음"
            failed = list(uids)
            purge_db = []

        done = len(rebind) if kind == "restore" else len(imap_purged)
        return {
            "account": user, "candidates": len(uids), "rebind": rebind,
            "purge_db": purge_db, "imap_purged": imap_purged, "failed": failed,
            "done": done, "legacy_db": len(purge_db), "folder_display": folder_display, "note": note,
        }

    results = []
    with ThreadPoolExecutor(max_workers=max(1, len(by_account))) as executor:
        futures = [executor.submit(process_account, u, uids) for u, uids in by_account.items()]
        for future in as_completed(futures):
            r = future.result()
            user = r["account"]
            if kind == "restore":
                for old_uid, new_uid in r["rebind"].items():
                    if new_uid:
                        rebind_uid(DB_PATH, user, old_uid, new_uid)
                    else:
                        delete_messages(DB_PATH, user, [old_uid])  # 새 UID 못 찾음 → 행 제거
            else:
                gone = list(r["imap_purged"]) + list(r["purge_db"])
                if gone:
                    delete_messages(DB_PATH, user, gone)
            action_name = "restore" if kind == "restore" else "purge"
            log_note = ("정리함 " + ("되돌리기" if kind == "restore" else "영구삭제")
                        + (f" - {r['note']}" if r["note"] else ""))
            log_action_run(DB_PATH, run_at, False, user, action_name, r["candidates"],
                           r["done"] + r["legacy_db"], len(r["failed"]), r["folder_display"], log_note)
            results.append({
                "account": user, "done": r["done"], "db_only": r["legacy_db"],
                "failed": len(r["failed"]), "note": r["note"],
            })

    if not results:
        results = [{"account": "-", "done": 0, "db_only": 0, "failed": 0,
                    "note": "처리 대상이 없습니다(이미 처리됐거나 상태가 바뀜)"}]
    run_state["last_vault_action"] = {"ran_at": run_at, "kind": kind, "results": results}
    return redirect(f"/vault?{return_qs}" if return_qs else "/vault")


@bp.route("/vault/restore", methods=["POST"])
def vault_restore():
    return _vault_process("restore")


@bp.route("/vault/purge", methods=["POST"])
def vault_purge():
    return _vault_process("purge")
