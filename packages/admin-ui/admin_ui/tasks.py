"""admin_ui `/tasks` 블루프린트 - 파이프라인 실행 버튼 + 개별 메일 수동 액션.

- /tasks           : 액션 대상(active) 메일 목록 + 대량 선택 UI
- /tasks/run,apply : fetch_mail -> generate_html 파이프라인(dry-run / --apply)
- /tasks/action/preview : "선택 실행" 확인 모달용 요약(JSON)
- /tasks/action    : 선택한 메일을 계정별 스레드 병렬로 IMAP 처리 + DB status 갱신
"""
import imaplib
import json
import math
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, redirect, request, url_for

from mail_core.actions import (
    decode_mailbox_name,
    find_archive_folder,
    find_trash_folder,
    mark_as_read,
    move_to_folder,
)
from mail_core.accounts import IMAP_SERVERS, load_accounts
from mail_core.imap_auth import authenticate
from mail_app.mail_log_store import log_action_run, mark_message_status, query_messages

from admin_ui._shared import ACCOUNTS_PATH, DB_PATH, build_qs, esc, page, run_state
from admin_ui._render import (
    MSG_DEFAULT_SINCE_DAYS,
    MSG_PAGE_SIZE_MAX,
    MSG_PAGE_SIZE_MIN,
    _account_options,
    _category_options,
    _filter_by_category,
    _parse_page_num,
    _parse_page_size,
    load_all_messages,
    msg_table,
    render_pagination,
    run_pipeline,
)

bp = Blueprint("tasks", __name__)

# action 이름 -> 대상 폴더 finder / messages.status 값. fetch_mail.py의 ACTION_FOLDER_FINDERS·
# ACTION_STATUS 와 같은 개념을 "선택한 메일만" 처리하는 수동 액션에 재사용. "read"는 폴더
# 이동이 아니라 여기 없음.
MSG_ACTION_FOLDER_FINDERS = {"trash": find_trash_folder, "save": find_archive_folder}
MSG_ACTION_STATUS = {"trash": "trashed", "save": "archived"}


def run_status_html(run: dict | None) -> str:
    """`run_pipeline` 결과(마지막 fetch->generate 실행)를 /tasks 상단 배너로."""
    if run is None:
        return ""
    badge = lambda ok: f'<span class="badge {"ok" if ok else "fail"}">{"성공" if ok else "실패"}</span>'
    mode = "--apply (실제 처리)" if run["apply"] else "dry-run (미리보기만)"
    return f"""
    <div class="run-status">
      <strong>마지막 실행</strong> · {run['ran_at']} · {mode}<br>
      fetch_mail.py {badge(run['fetch_ok'])}
      <pre>{esc(run['fetch_output']) or '(출력 없음)'}</pre>
      generate_html.py {badge(run['generate_ok'])}
      <pre>{esc(run['generate_output']) or '(출력 없음)'}</pre>
    </div>
    """


TASKS_SCRIPT = """<script>
(function () {
  var KEY = 'mailagent.tasks.selection';
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

  var params = new URLSearchParams(location.search);
  if (params.get('done') === '1') {
    var failed = {};
    try {
      var res = JSON.parse((document.getElementById('task-result') || {}).textContent || '{}');
      (res.failed_keys || []).forEach(function (k) { failed[k] = true; });
    } catch (e) {}
    store = failed; save(store);
    params.delete('done');
    var qs = params.toString();
    history.replaceState(null, '', location.pathname + (qs ? '?' + qs : ''));
  }

  var countEl = document.getElementById('sel-count');
  var runBtn = document.getElementById('sel-run');
  function refresh() {
    var n = Object.keys(store).length;
    if (countEl) countEl.textContent = n + '건 선택됨';
    if (runBtn) runBtn.disabled = n === 0;
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

  var modal = document.getElementById('confirm-modal');
  var bodyEl = document.getElementById('confirm-body');
  var form = document.getElementById('task-form');
  var actionBtns = document.querySelectorAll('#confirm-modal .confirm-actions button');
  var submittable = null;   // 프리뷰가 확정한 "실제로 처리 가능한" key 목록
  function keys() { return Object.keys(store); }
  function esc(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : s; return d.innerHTML; }
  function setActionsEnabled(on) { actionBtns.forEach(function (b) { b.disabled = !on; }); }

  function renderSummary(d) {
    var h = '<p class="confirm-total"><strong>' + d.total + '건</strong> 처리 대상';
    if (d.missing) h += ' <span class="confirm-warn">· ' + d.missing + '건은 제외(이미 처리/삭제됨)</span>';
    h += '</p>';
    function group(title, items, keyName) {
      if (!items || !items.length) return '';
      var s = '<div class="confirm-group"><span class="confirm-group-title">' + title + '</span><ul>';
      items.forEach(function (it) { s += '<li>' + esc(it[keyName]) + ' <b>' + it.count + '</b></li>'; });
      return s + '</ul></div>';
    }
    h += group('카테고리별', d.by_category, 'name');
    h += group('계정별', d.by_account, 'account');
    return h;
  }

  if (runBtn) runBtn.addEventListener('click', function () {
    var ks = keys();
    if (!ks.length || !modal) return;
    submittable = null;
    setActionsEnabled(false);
    bodyEl.textContent = '불러오는 중…';
    modal.showModal();
    var fd = new FormData();
    ks.forEach(function (k) { fd.append('sel', k); });
    fetch('/tasks/action/preview', { method: 'POST', body: fd })
      .then(function (r) { if (!r.ok) throw new Error('http ' + r.status); return r.json(); })
      .then(function (d) {
        submittable = d.keys || ks;
        bodyEl.innerHTML = renderSummary(d);
        setActionsEnabled(submittable.length > 0);
      })
      .catch(function () {
        bodyEl.innerHTML = '<p class="confirm-warn">요약을 불러오지 못했습니다. 선택한 '
          + ks.length + '건 전체가 대상이 됩니다.</p>';
        submittable = ks;
        setActionsEnabled(true);
      });
  });

  actionBtns.forEach(function (btn) {
    btn.addEventListener('click', function () {
      var action = btn.dataset.action;
      var ks = submittable || keys();
      if (!ks.length) return;
      var labels = { trash: '휴지통 이동', save: '보관', read: '읽음 표시' };
      if (!confirm(ks.length + '건을 ' + (labels[action] || action) + ' 처리합니다. 실제로 메일함이 바뀝니다. 계속할까요?')) return;
      form.querySelectorAll('input[name="sel"], input[name="action"]').forEach(function (el) { el.remove(); });
      ks.forEach(function (k) {
        var i = document.createElement('input');
        i.type = 'hidden'; i.name = 'sel'; i.value = k; form.appendChild(i);
      });
      var a = document.createElement('input');
      a.type = 'hidden'; a.name = 'action'; a.value = action; form.appendChild(a);
      form.submit();
    });
  });

  var cancelBtn = document.getElementById('confirm-cancel');
  if (cancelBtn) cancelBtn.addEventListener('click', function () { modal.close(); });

  refresh();
})();
</script>"""


def render_task_action_status(run: dict | None) -> str:
    if run is None:
        return ""
    rows = []
    for r in run["results"]:
        fail_note = f", {r['failed']}건 실패" if r["failed"] else ""
        extra_note = f" ({esc(r['note'])})" if r.get("note") else ""
        rows.append(f"<div>{esc(r['account'])} · {esc(r['action'])} - {r['done']}건 완료{fail_note}{extra_note}</div>")
    return f"""
    <div class="run-status">
      <strong>액션 처리 결과</strong> · {run['ran_at']}
      {"".join(rows) or '<div>처리한 항목이 없습니다.</div>'}
    </div>
    """


def render_tasks_page(
    page_items: list[dict], page_num: int, total_pages: int, total: int, page_size: int,
    account_filter: str | None, category_filter: str | None,
    all_users: list[str], account_type_by_user: dict[str, str], categories: dict,
) -> str:
    filter_state = {"account": account_filter or "", "category": category_filter or "", "page_size": page_size}
    return_qs = build_qs(**filter_state)

    account_options = _account_options(all_users, account_type_by_user, account_filter)
    category_options = _category_options(categories, category_filter)
    filter_form = f"""
    <form class="filter-form" method="get" action="/tasks">
      <label>계정<select name="account">{account_options}</select></label>
      <label>카테고리<select name="category">{category_options}</select></label>
      <label>페이지당 건수<input type="number" name="page_size" min="{MSG_PAGE_SIZE_MIN}" max="{MSG_PAGE_SIZE_MAX}" value="{page_size}"></label>
      <button class="btn" type="submit">필터 적용</button>
    </form>
    """

    table = msg_table(page_items, categories, with_account=True, with_select=True)
    prev_qs = build_qs(**filter_state, page=page_num - 1)
    next_qs = build_qs(**filter_state, page=page_num + 1)
    prev_link = f'<a href="/tasks?{prev_qs}">← 이전</a>' if page_num > 1 else '<span class="disabled">← 이전</span>'
    next_link = f'<a href="/tasks?{next_qs}">다음 →</a>' if page_num < total_pages else '<span class="disabled">다음 →</span>'
    pagination = render_pagination(prev_link, next_link, page_num, total_pages, total)

    task_result_json = json.dumps(
        {"failed_keys": (run_state["last_task_action"] or {}).get("failed_keys", [])}
    ).replace("<", "\\u003c")

    body = f"""
    <h1 class="page-title">작업 실행</h1>
    <p class="sub">새로고침은 메일함을 조회만 하고 실제로 처리하진 않습니다(dry-run). 실제 처리는
    action이 keep이 아닌 카테고리에 매칭된 메일을 진짜로 휴지통/보관 이동·읽음 표시합니다.</p>
    <div class="task-run-bar">
      <form class="slow-form" method="post" action="/tasks/run" style="margin:0">
        <button class="btn secondary" type="submit">새로고침 (dry-run)</button>
      </form>
      <form class="slow-form" method="post" action="/tasks/apply" style="margin:0"
            onsubmit="return confirm('실제로 메일함을 정리합니다(휴지통 이동/보관/읽음 표시). 계속할까요?')">
        <button class="btn danger" type="submit">실제 처리 (--apply)</button>
      </form>
    </div>
    {run_status_html(run_state["last_run"])}

    <h2 class="section-title">개별 메일 액션 처리</h2>
    <p class="sub">아래 목록에서 처리할 메일을 체크로 고르세요. 페이지를 넘겨도 선택은
    유지됩니다(이 브라우저에 저장, 7일 후 만료). "선택 실행"을 누르면 총 건수·카테고리별
    내역을 확인한 뒤 휴지통/보관/읽음을 실행합니다. 이미 처리된(휴지통/보관) 메일은
    목록에서 빠집니다.</p>
    {render_task_action_status(run_state["last_task_action"])}
    <script id="task-result" type="application/json">{task_result_json}</script>
    {filter_form}

    <div class="sel-bar">
      <span class="sel-count" id="sel-count">0건 선택됨</span>
      <button type="button" class="btn" id="sel-run" disabled>선택 실행 →</button>
      <button type="button" class="btn secondary" id="sel-clear">전체 해제</button>
    </div>
    <p id="sel-warn" class="form-error" hidden>브라우저 저장 공간이 가득 차 선택이 저장되지 않습니다. "전체 해제"로 비우세요.</p>
    {table}
    {pagination}

    <form id="task-form" method="post" action="/tasks/action">
      <input type="hidden" name="return_qs" value="{esc(return_qs)}">
    </form>

    <dialog id="confirm-modal">
      <h3 style="margin:0 0 8px">선택한 메일 처리</h3>
      <div id="confirm-body" class="confirm-body">불러오는 중…</div>
      <p class="confirm-hint">처리 방법을 고르세요 - 실제로 메일함이 바뀝니다.</p>
      <div class="confirm-actions">
        <button type="button" class="btn" data-action="trash">🗑️ 휴지통 이동</button>
        <button type="button" class="btn" data-action="save">📥 보관</button>
        <button type="button" class="btn" data-action="read">✅ 읽음 표시</button>
      </div>
      <div class="actions-row">
        <button type="button" class="btn secondary" id="confirm-cancel">닫기</button>
      </div>
    </dialog>
    {TASKS_SCRIPT}
    """
    return page("작업 실행", body, "tasks")


@bp.route("/tasks")
def tasks_page():
    account_filter = request.args.get("account", "").strip() or None
    category_filter = request.args.get("category", "").strip() or None
    page_size = _parse_page_size(request.args.get("page_size"))

    since = datetime.now() - timedelta(days=MSG_DEFAULT_SINCE_DAYS)
    all_messages, all_users, account_type_by_user, categories = load_all_messages(since, None, account_filter)

    pool = _filter_by_category(all_messages, category_filter)
    # 이미 처리된(active가 아닌) 메일은 액션 대상에서 뺀다 - 다시 처리할 게 없다.
    pool = [m for m in pool if m.get("status", "active") == "active"]
    pool.sort(key=lambda m: m.get("message_date") or "", reverse=True)

    total = len(pool)
    total_pages = max(1, math.ceil(total / page_size))
    page_num = min(_parse_page_num(request.args.get("page")), total_pages)
    start = (page_num - 1) * page_size
    page_items = pool[start : start + page_size]

    return render_tasks_page(
        page_items, page_num, total_pages, total, page_size,
        account_filter, category_filter, all_users, account_type_by_user, categories,
    )


@bp.route("/tasks/run", methods=["POST"])
def tasks_run():
    run_state["last_run"] = run_pipeline(apply=False)
    return redirect(url_for("tasks.tasks_page"))


@bp.route("/tasks/apply", methods=["POST"])
def tasks_apply():
    run_state["last_run"] = run_pipeline(apply=True)
    return redirect(url_for("tasks.tasks_page"))


def _active_by_account(sel) -> dict[str, list[str]]:
    """선택 key(`account::uid`) 중 지금도 status='active'인 것만 {계정: [uid,...]} 로.

    분류(classify) 없이 DB만 훑는다 - 실행 경로(tasks_action)는 카테고리가 필요 없다.
    선택은 며칠씩 localStorage에 남아 있을 수 있어(그새 다른 데서 처리됨) 서버에서
    다시 확인한다."""
    wanted = {s for s in sel if "::" in s}
    by_uid: dict[str, set[str]] = defaultdict(set)
    for s in wanted:
        acc, uid = s.split("::", 1)
        by_uid[acc].add(uid)
    since = datetime.now() - timedelta(days=MSG_DEFAULT_SINCE_DAYS)
    out: dict[str, list[str]] = {}
    for acc, uids in by_uid.items():
        active = {
            m["uid"] for m in query_messages(DB_PATH, since, None, account=acc, status="active")
        }
        keep = [u for u in uids if u in active]
        if keep:
            out[acc] = keep
    return out


@bp.route("/tasks/action/preview", methods=["POST"])
def tasks_action_preview():
    """"선택 실행" 확인 모달용 요약(JSON) - 지금도 처리 가능한(active) 메일의 총 건수 +
    카테고리별/계정별 내역 + 실제 제출할 key 목록. 이미 사라진 선택은 missing으로 센다."""
    wanted = {s for s in request.form.getlist("sel") if "::" in s}
    since = datetime.now() - timedelta(days=MSG_DEFAULT_SINCE_DAYS)
    all_messages, _, _, _ = load_all_messages(since, None, None)
    chosen = [
        m for m in all_messages
        if f'{m["account"]}::{m["uid"]}' in wanted and m.get("status", "active") == "active"
    ]
    valid = {f'{m["account"]}::{m["uid"]}' for m in chosen}
    by_cat = Counter((m.get("_category") or "미분류") for m in chosen)
    by_acct = Counter(m["account"] for m in chosen)
    return jsonify({
        "total": len(valid),
        "missing": len(wanted - valid),
        "keys": sorted(valid),
        "by_category": [{"name": k, "count": v} for k, v in by_cat.most_common()],
        "by_account": [{"account": k, "count": v} for k, v in by_acct.most_common()],
    })


@bp.route("/tasks/action", methods=["POST"])
def tasks_action():
    sel = request.form.getlist("sel")
    action = request.form.get("action", "")
    return_qs = request.form.get("return_qs", "")

    # 제출된 선택 중 지금도 active인 것만 처리한다(오래된 localStorage 선택 방어).
    by_account = _active_by_account(sel)
    accounts_cfg = {a["user"]: a for a in load_accounts(ACCOUNTS_PATH)}
    run_at = datetime.now().isoformat(timespec="seconds")

    def process_account(user: str, uids: list[str]) -> dict:
        """한 계정의 IMAP 액션만 수행하고 결과 dict를 반환한다 (DB 미접근 - 스레드 병렬용)."""
        account = accounts_cfg.get(user)
        if not account:
            return {"account": user, "candidates": len(uids), "succeeded": [], "failed_uids": list(uids),
                    "folder_display": None, "note": "계정 설정을 찾을 수 없음"}
        if action not in ("read", *MSG_ACTION_FOLDER_FINDERS):
            return {"account": user, "candidates": len(uids), "succeeded": [], "failed_uids": list(uids),
                    "folder_display": None, "note": f"알 수 없는 action: {action}"}

        succeeded: list[str] = []
        failed_uids: list[str] = list(uids)
        folder_display, note = None, None
        try:
            imap = imaplib.IMAP4_SSL(IMAP_SERVERS[account["type"]], 993)
            authenticate(imap, account)
            imap.select("INBOX", readonly=False)
            try:
                if action == "read":
                    succeeded, failed_uids = mark_as_read(imap, uids)
                else:
                    folder = MSG_ACTION_FOLDER_FINDERS[action](imap, account["type"])
                    if not folder:
                        succeeded, failed_uids, note = [], list(uids), "대상 폴더를 찾지 못함"
                    else:
                        succeeded, failed_uids = move_to_folder(imap, uids, folder)
                        folder_display = decode_mailbox_name(folder)
            finally:
                try:
                    imap.logout()
                except Exception:
                    pass
        except (imaplib.IMAP4.error, OSError) as e:
            succeeded, failed_uids, note = [], list(uids), str(e)

        return {"account": user, "candidates": len(uids), "succeeded": succeeded,
                "failed_uids": failed_uids, "folder_display": folder_display, "note": note}

    results = []
    failed_keys: list[str] = []
    with ThreadPoolExecutor(max_workers=max(1, len(by_account))) as executor:
        futures = [executor.submit(process_account, user, uids) for user, uids in by_account.items()]
        for future in as_completed(futures):
            r = future.result()
            user, succeeded = r["account"], r["succeeded"]
            done, failed = len(succeeded), len(r["failed_uids"])
            if succeeded and r["note"] is None:
                if action == "read":
                    mark_message_status(DB_PATH, user, succeeded, is_read=True)
                elif action in MSG_ACTION_STATUS:
                    mark_message_status(DB_PATH, user, succeeded, status=MSG_ACTION_STATUS.get(action))
            failed_keys.extend(f"{user}::{uid}" for uid in r["failed_uids"])
            manual_note = "수동 선택 처리(작업 실행 화면)" + (f" - {r['note']}" if r["note"] else "")
            log_action_run(DB_PATH, run_at, False, user, action, r["candidates"], done, failed,
                           r["folder_display"], manual_note)
            results.append({"account": user, "action": action, "done": done, "failed": failed, "note": r["note"]})

    run_state["last_task_action"] = {"ran_at": run_at, "results": results, "failed_keys": failed_keys}
    # done=1 → 클라이언트 스크립트가 성공분을 localStorage 선택에서 지운다(실패분은 남김).
    suffix = f"{return_qs}&done=1" if return_qs else "done=1"
    return redirect(f"/tasks?{suffix}")
