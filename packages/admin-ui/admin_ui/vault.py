"""admin_ui `/vault` 블루프린트 - 정리함(일반함=active / 보관함=archived / 휴지통=trashed).

- /vault          : 탭별 목록 + 대량 선택(체크박스, localStorage로 페이지 넘어도 유지)
- /vault/move     : 선택한 메일을 다른 상태(일반함/보관함/휴지통)로 이동 - 목적지는 팝업에서 선택
- /vault/purge    : 선택한 메일을 서버에서 완전히 삭제(EXPUNGE) - 탭 무관하게 어디서든 가능

선택된 메일의 "현재 상태"는 탭이 아니라 DB에서 다시 조회해 확정한다 - localStorage 선택은
탭을 넘나들며 며칠씩 남아있을 수 있어(다른 탭에서 고른 것 포함), 화면에 보이던 탭을 그대로
믿으면 이미 상태가 바뀐 메일을 잘못 옮길 수 있다. 메일이 옮겨지면 UID 가 바뀌므로 message_id
로 대상 폴더에서 다시 찾는다. message_id 없는(레거시) 행은 IMAP 반영 없이 DB 만 정리한다.
"""
import imaplib
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
from datetime import datetime, timedelta

from flask import Blueprint, redirect, request

from mail_core.actions import (
    find_archive_folder,
    find_message_uid_by_id,
    find_trash_folder,
    move_to_folder,
    permanent_delete,
    select_folder,
)
from mail_core.accounts import IMAP_SERVERS, load_accounts
from mail_core.imap_auth import authenticate
from mail_app.mail_log_store import (
    delete_messages,
    log_action_run,
    mark_message_status,
    query_messages,
    rebind_uid,
)

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

# 보안: /vault/move·/vault/purge(EXPUNGE 포함) 이 블루프린트의 상태변경 POST 는
# admin_app._block_cross_origin_writes(@app.before_request)가 cross-origin/DNS-rebinding
# 으로부터 막는다. 여기엔 자체 CSRF 방어가 없다 - 반드시 그 app 에 등록해서 써야 한다.

# /vault 탭: (탭 키, 라벨, messages.status 값). 원래 있던 보관함/휴지통이 앞, 새로 생긴
# 일반함(받은편지함 그대로)이 뒤 - 기존 사용 습관(보관함이 기본 탭)을 그대로 둔다.
VAULT_TABS = [
    ("archive", "보관함", "archived"),
    ("trash", "휴지통", "trashed"),
    ("active", "일반함", "active"),
]
VAULT_TAB_BY_KEY = {t[0]: t for t in VAULT_TABS}
STATUS_BY_TAB = {k: status for k, _, status in VAULT_TABS}
TAB_BY_STATUS = {status: k for k, _, status in VAULT_TABS}
STATUS_LABEL = {status: label for _, label, status in VAULT_TABS}

LEGACY_NOTE = "식별자(Message-ID) 없음 - 웹메일에서 직접 처리하세요"


def _folder_name(status: str, account_type: str, imap: imaplib.IMAP4_SSL) -> str | None:
    """status에 해당하는 실제 IMAP 폴더명. active는 항상 INBOX(고정), 나머지는 서버에서 찾는다."""
    if status == "active":
        return "INBOX"
    finder = find_archive_folder if status == "archived" else find_trash_folder
    return finder(imap, account_type)


def render_vault_action_status(run: dict | None) -> str:
    if run is None:
        return ""
    verb = {"move": "이동", "purge": "영구 삭제"}.get(run["kind"], run["kind"])
    rows = []
    for r in run["results"]:
        bits = [f'{r["done"]}건 처리']
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


VAULT_SCRIPT = """<script>
(function () {
  var KEY = 'mailagent.vault.selection';
  var MAX_AGE = 7 * 24 * 3600 * 1000;
  function load() {
    try {
      var raw = JSON.parse(localStorage.getItem(KEY) || 'null');
      if (!raw || !raw.keys) return {};
      if (raw.ts && Date.now() - raw.ts > MAX_AGE) return {};
      return raw.keys;
    } catch (e) { return {}; }
  }
  function save(s) {
    try { localStorage.setItem(KEY, JSON.stringify({ v: 1, ts: Date.now(), keys: s })); }
    catch (e) { if (warnEl) warnEl.hidden = false; }
  }
  var warnEl = document.getElementById('sel-warn');
  var store = load();

  var countEl = document.getElementById('sel-count');
  var moveBtn = document.getElementById('sel-move');
  var purgeBtn = document.getElementById('sel-purge');
  function refresh() {
    var n = Object.keys(store).length;
    if (countEl) countEl.textContent = n + '건 선택됨';
    if (moveBtn) moveBtn.disabled = n === 0;
    if (purgeBtn) purgeBtn.disabled = n === 0;
  }

  document.querySelectorAll('.sel-box').forEach(function (cb) {
    if (store[cb.dataset.key]) cb.checked = true;
    cb.addEventListener('change', function () {
      if (cb.checked) store[cb.dataset.key] = true; else delete store[cb.dataset.key];
      save(store); refresh();
    });
  });

  var clearBtn = document.getElementById('sel-clear');
  if (clearBtn) clearBtn.addEventListener('click', function () {
    store = {}; save(store);
    document.querySelectorAll('.sel-box').forEach(function (cb) { cb.checked = false; });
    refresh();
  });

  function fillForm(form, action) {
    form.querySelectorAll('input[name="sel"]').forEach(function (el) { el.remove(); });
    Object.keys(store).forEach(function (k) {
      var i = document.createElement('input');
      i.type = 'hidden'; i.name = 'sel'; i.value = k; form.appendChild(i);
    });
    form.action = action;
  }

  var moveModal = document.getElementById('move-modal');
  var moveForm = document.getElementById('vault-move-form');
  if (moveBtn) moveBtn.addEventListener('click', function () {
    if (!Object.keys(store).length || !moveModal) return;
    moveModal.showModal();
  });
  var moveCancel = document.getElementById('move-cancel');
  if (moveCancel) moveCancel.addEventListener('click', function () { moveModal.close(); });
  var moveConfirm = document.getElementById('move-confirm');
  if (moveConfirm) moveConfirm.addEventListener('click', function () {
    var dest = document.getElementById('move-dest').value;
    var n = Object.keys(store).length;
    if (!confirm(n + '건을 옮깁니다. 실제로 메일함이 바뀝니다. 계속할까요?')) return;
    fillForm(moveForm, '/vault/move');
    var d = document.createElement('input');
    d.type = 'hidden'; d.name = 'dest'; d.value = dest; moveForm.appendChild(d);
    moveForm.submit();
  });

  var purgeForm = document.getElementById('vault-purge-form');
  if (purgeBtn) purgeBtn.addEventListener('click', function () {
    var n = Object.keys(store).length;
    if (!n) return;
    if (!confirm(n + '건을 서버에서 영구 삭제합니다. 복구할 수 없습니다. 계속할까요?')) return;
    fillForm(purgeForm, '/vault/purge');
    purgeForm.submit();
  });

  refresh();
})();
</script>"""


@bp.route("/vault")
def vault_page():
    tab = request.args.get("tab", "archive")
    if tab not in VAULT_TAB_BY_KEY:
        tab = "archive"
    status = STATUS_BY_TAB[tab]

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

    intro = {
        "active": "아직 처리되지 않은(받은편지함) 메일입니다. 골라서 보관·휴지통으로 옮기거나 영구 삭제할 수 있습니다.",
        "archive": "보관(save) 처리한 메일입니다. 골라서 일반함으로 되돌리거나 휴지통으로 옮길 수 있습니다.",
        "trash": "휴지통으로 보낸 메일입니다. 골라서 되돌리거나 서버에서 완전히 삭제(복구 불가)할 수 있습니다.",
    }[tab]

    dest_options = "".join(
        f'<option value="{status_v}">{esc(label)}</option>'
        for key, label, status_v in VAULT_TABS
        if key != tab
    )

    body = f"""
    <h1 class="page-title">🗂️ 정리함</h1>
    <p class="sub">{esc(intro)}</p>
    {_vault_tabs_html(tab)}
    {render_vault_action_status(run_state["last_vault_action"])}
    {filter_form}

    <form id="vault-move-form" method="post" action="/vault/move">
      <input type="hidden" name="return_qs" value="{esc(return_qs)}">
    </form>
    <form id="vault-purge-form" method="post" action="/vault/purge">
      <input type="hidden" name="return_qs" value="{esc(return_qs)}">
    </form>

    <div class="sel-bar">
      <span class="sel-count" id="sel-count">0건 선택됨</span>
      <button type="button" class="btn danger" id="sel-purge" disabled>영구삭제</button>
      <button type="button" class="btn" id="sel-move" disabled>선택이동</button>
      <button type="button" class="btn secondary" id="sel-clear">전체 해제</button>
    </div>
    <p id="sel-warn" class="form-error" hidden>브라우저 저장 공간이 가득 차 선택이 저장되지 않습니다. "전체 해제"로 비우세요.</p>
    {table}
    <p class="sub" style="margin-top:6px">※ 예전에 저장돼 식별자(Message-ID)가 없는 메일은
    IMAP 서버에는 반영되지 않고 이 목록에서만 정리됩니다.</p>
    {pagination}

    <dialog id="move-modal">
      <h3 style="margin:0 0 8px">선택한 메일 이동</h3>
      <p class="confirm-hint">목적지를 고르세요 - 실제로 메일함이 바뀝니다.</p>
      <label>목적지<select id="move-dest">{dest_options}</select></label>
      <div class="actions-row" style="margin-top:12px">
        <button type="button" class="btn secondary" id="move-cancel">취소</button>
        <button type="button" class="btn" id="move-confirm">이동</button>
      </div>
    </dialog>
    {VAULT_SCRIPT}
    """
    return page("정리함", body, "vault")


def _resolve_selection(sel: list[str]) -> dict[str, dict[str, list[dict]]]:
    """`account::uid` 목록을 계정별·"현재 DB 상태"별로 묶는다.

    선택은 localStorage에 며칠 남아있을 수 있고 탭을 넘나들며 고를 수도 있어서, 화면에
    보이던 탭을 그대로 믿지 않고 매번 DB에서 실제 상태를 다시 확인한다."""
    since = datetime.now() - timedelta(days=MSG_DEFAULT_SINCE_DAYS)
    requested: dict[str, set[str]] = defaultdict(set)
    for item in sel:
        if "::" in item:
            acc, uid = item.split("::", 1)
            requested[acc].add(uid)

    out: dict[str, dict[str, list[dict]]] = {}
    for account, uids in requested.items():
        rows = query_messages(DB_PATH, since, None, account=account, status=None)
        by_uid = {m["uid"]: m for m in rows}
        buckets: dict[str, list[dict]] = defaultdict(list)
        for u in uids:
            m = by_uid.get(u)
            if m:
                buckets[m["status"]].append(m)
        if buckets:
            out[account] = dict(buckets)
    return out


def _move_one_account(account: dict, status_buckets: dict[str, list[dict]], dest_status: str) -> dict:
    """한 계정의 선택 메일을 dest_status로 옮긴다. 이미 dest_status인 건 건드리지 않는다."""
    user = account["user"]
    done, failed, note = 0, 0, None
    try:
        imap = imaplib.IMAP4_SSL(IMAP_SERVERS[account["type"]], 993)
        authenticate(imap, account)
        try:
            dest_folder = _folder_name(dest_status, account["type"], imap)
            if not dest_folder:
                failed = sum(len(v) for k, v in status_buckets.items() if k != dest_status)
                return {"account": user, "done": 0, "failed": failed, "note": "이동할 폴더를 찾지 못함"}

            for src_status, msgs in status_buckets.items():
                if src_status == dest_status:
                    continue
                src_folder = _folder_name(src_status, account["type"], imap)
                if not src_folder or not select_folder(imap, src_folder):
                    failed += len(msgs)
                    note = note or f"{STATUS_LABEL.get(src_status, src_status)} 폴더를 열 수 없음"
                    continue

                legacy = [m for m in msgs if not m.get("message_id")]
                resolvable = [m for m in msgs if m.get("message_id")]
                if legacy:
                    failed += len(legacy)
                    note = note or LEGACY_NOTE

                orig_to_new: dict[str, tuple[str, str]] = {}
                claimed: set[str] = set()
                for m in resolvable:
                    try:
                        new_uid = find_message_uid_by_id(imap, src_folder, m["message_id"], select=False)
                    except imaplib.IMAP4.error:
                        new_uid = None
                    if new_uid and new_uid not in claimed:
                        orig_to_new[m["uid"]] = (new_uid, m["message_id"])
                        claimed.add(new_uid)
                    else:
                        failed += 1
                if not orig_to_new:
                    continue

                if not select_folder(imap, src_folder):
                    failed += len(orig_to_new)
                    continue
                targets = [v[0] for v in orig_to_new.values()]
                ok_new, bad_new = move_to_folder(imap, targets, dest_folder, require_move=True)
                ok_set = set(ok_new)
                if bad_new:
                    note = note or "일부 이동 실패(서버 거부 또는 MOVE 미지원)"

                if not select_folder(imap, dest_folder):
                    failed += len(orig_to_new)
                    continue
                for old_uid, (new_uid_src, mid) in orig_to_new.items():
                    if new_uid_src not in ok_set:
                        failed += 1
                        continue
                    try:
                        final_uid = find_message_uid_by_id(imap, dest_folder, mid, select=True)
                    except imaplib.IMAP4.error:
                        final_uid = None
                    if final_uid:
                        rebind_uid(DB_PATH, user, old_uid, final_uid)
                        mark_message_status(DB_PATH, user, [final_uid], status=dest_status)
                        done += 1
                    else:
                        failed += 1
                        note = note or "이동 후 새 위치 확인 실패(서버엔 반영됨 - 다음 새로고침에서 정리됨)"
        finally:
            try:
                imap.logout()
            except Exception:
                pass
    except (imaplib.IMAP4.error, OSError) as e:
        return {
            "account": user, "done": done,
            "failed": failed + sum(len(v) for v in status_buckets.values()),
            "note": str(e),
        }
    return {"account": user, "done": done, "failed": failed, "note": note}


def _purge_one_account(account: dict, status_buckets: dict[str, list[dict]]) -> dict:
    """한 계정의 선택 메일을 (그 메일이 지금 있는 폴더 기준으로) 서버에서 완전히 삭제한다."""
    user = account["user"]
    done, failed, note = 0, 0, None
    try:
        imap = imaplib.IMAP4_SSL(IMAP_SERVERS[account["type"]], 993)
        authenticate(imap, account)
        try:
            for status, msgs in status_buckets.items():
                folder = _folder_name(status, account["type"], imap)
                if not folder or not select_folder(imap, folder):
                    failed += len(msgs)
                    note = note or f"{STATUS_LABEL.get(status, status)} 폴더를 열 수 없음"
                    continue

                legacy = [m for m in msgs if not m.get("message_id")]
                resolvable = [m for m in msgs if m.get("message_id")]
                if legacy:
                    delete_messages(DB_PATH, user, [m["uid"] for m in legacy])
                    done += len(legacy)

                uid_by_target: dict[str, str] = {}
                targets = []
                for m in resolvable:
                    try:
                        new_uid = find_message_uid_by_id(imap, folder, m["message_id"], select=False)
                    except imaplib.IMAP4.error:
                        new_uid = None
                    if new_uid:
                        targets.append(new_uid)
                        uid_by_target[new_uid] = m["uid"]
                    else:
                        failed += 1
                if not targets:
                    continue
                if not select_folder(imap, folder):
                    failed += len(targets)
                    continue
                ok_new, bad_new = permanent_delete(imap, folder, targets)
                for t in ok_new:
                    delete_messages(DB_PATH, user, [uid_by_target[t]])
                    done += 1
                failed += len(bad_new)
        finally:
            try:
                imap.logout()
            except Exception:
                pass
    except (imaplib.IMAP4.error, OSError) as e:
        return {
            "account": user, "done": done,
            "failed": failed + sum(len(v) for v in status_buckets.values()),
            "note": str(e),
        }
    return {"account": user, "done": done, "failed": failed, "note": note}


@bp.route("/vault/move", methods=["POST"])
def vault_move():
    dest_status = request.form.get("dest", "")
    return_qs = request.form.get("return_qs", "")
    if dest_status not in STATUS_LABEL:
        return redirect(f"/vault?{return_qs}" if return_qs else "/vault")

    sel = request.form.getlist("sel")
    by_account = _resolve_selection(sel)
    accounts_cfg = {a["user"]: a for a in load_accounts(ACCOUNTS_PATH)}
    run_at = datetime.now().isoformat(timespec="seconds")

    results = []
    with ThreadPoolExecutor(max_workers=max(1, len(by_account))) as executor:
        futures = {
            executor.submit(_move_one_account, accounts_cfg[u], buckets, dest_status): u
            for u, buckets in by_account.items() if u in accounts_cfg
        }
        for future in as_completed(futures):
            r = future.result()
            log_action_run(
                DB_PATH, run_at, False, r["account"], "move", r["done"] + r["failed"],
                r["done"], r["failed"], STATUS_LABEL.get(dest_status),
                f"정리함 이동 → {STATUS_LABEL.get(dest_status)}" + (f" - {r['note']}" if r["note"] else ""),
            )
            results.append(r)

    if not results:
        results = [{"account": "-", "done": 0, "failed": 0, "note": "처리 대상이 없습니다."}]
    run_state["last_vault_action"] = {"ran_at": run_at, "kind": "move", "results": results}
    return redirect(f"/vault?{return_qs}" if return_qs else "/vault")


@bp.route("/vault/purge", methods=["POST"])
def vault_purge():
    return_qs = request.form.get("return_qs", "")
    sel = request.form.getlist("sel")
    by_account = _resolve_selection(sel)
    accounts_cfg = {a["user"]: a for a in load_accounts(ACCOUNTS_PATH)}
    run_at = datetime.now().isoformat(timespec="seconds")

    results = []
    with ThreadPoolExecutor(max_workers=max(1, len(by_account))) as executor:
        futures = {
            executor.submit(_purge_one_account, accounts_cfg[u], buckets): u
            for u, buckets in by_account.items() if u in accounts_cfg
        }
        for future in as_completed(futures):
            r = future.result()
            log_action_run(
                DB_PATH, run_at, False, r["account"], "purge", r["done"] + r["failed"],
                r["done"], r["failed"], None,
                "정리함 영구삭제" + (f" - {r['note']}" if r["note"] else ""),
            )
            results.append(r)

    if not results:
        results = [{"account": "-", "done": 0, "failed": 0, "note": "처리 대상이 없습니다."}]
    run_state["last_vault_action"] = {"ran_at": run_at, "kind": "purge", "results": results}
    return redirect(f"/vault?{return_qs}" if return_qs else "/vault")
