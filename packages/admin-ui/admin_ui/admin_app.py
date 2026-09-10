"""메일 대시보드(기간별 탭) + 카테고리/계정 설정 + 작업 실행(액션 태스크) 로컬 웹 UI.

로컬(127.0.0.1)에서만 실행하는 걸 전제로 인증을 넣지 않았다 - 외부에 노출하지 말 것.
`python -m admin_ui`로 실행하면 http://127.0.0.1:5000 에서 다음 세 화면을 쓸 수 있다:

- `/` - 메인 대시보드. 일일/주간/월별/연도별/전체 탭(기간만 다르고 같은 화면 -
  generate_html.py의 build_report()/render_report_*() 조각 함수들을 재사용해 그때그때
  app.db를 쿼리해서 렌더링한다. IMAP 재조회 없음). 일일/주간/월간은 ◀이전/다음▶
  화살표로 과거 기간을 오갈 수 있다(기본값: 오늘/이번 주/이번 달, `?date=YYYY-MM-DD`로
  기준일 지정 - 미래로는 못 감). 연도별은 이동 기능 없이 항상 "올해"만 보여준다(요청
  범위 밖). "전체" 탭은 고정 기간이 아니라 app.db에 있는 진짜 전체 기간 통계를 보여준다.
  화면 오른쪽 위 "동기화" 버튼은 fetch_mail.py를 --since 없이(계정별 마지막 저장
  시점부터, dry-run) 실행하고 돌아온다 - 실제 메일함 변경(--apply)은 여전히 /tasks
  전용. 카테고리 통계 타일/탭과 계정 태그/계정 카드 내부 카테고리는 전부 건수
  상위 N개만 기본 노출하고 나머지는 "···"로 접는다(JS 없이 CSS 체크박스 토글).
  계정 태그(포털·이메일·총건수)를 클릭하면 아래 "계정별 상세"의 그 계정 카드로
  포커스가 이동하며 펼쳐진다 - 계정 카드를 클릭해도 같은 방식으로 펼쳐지고, 그 안에서
  카테고리 필터 칩 + 실제 페이지네이션 목록을 볼 수 있다(한 번에 한 계정만 열림,
  `?acct=`/`acct_cat=`/`acct_page=` 쿼리로 서버가 상태를 관리 - JS 없음).
- `/list` - 일일/주간/월간/전체 각 기간의 메일을 계정/카테고리 드롭다운 필터 +
  페이지네이션으로 보여주는 별도 화면(`/`의 리포트 화면 안 "이 기간 목록 보기" 링크로
  진입, "전체"는 최근 2년 고정 기간). "이 조건으로 작업 실행" 버튼으로 /tasks와
  연결되는 대량 필터링용 화면이라 그대로 남겨뒀다("/"의 계정 카드 인라인 목록과는
  별개 - 계정 카드는 한 계정만, 이 화면은 여러 계정을 한 번에 필터링한다).
- `/settings` - 카테고리 관리 + 메일 계정 관리(⚙️ 아이콘으로 진입).
- `/tasks` - fetch_mail.py를 버튼으로 실행(새로고침/실제 처리)하고, 페이지네이션 목록에서
  메일을 체크박스로 선택(페이지를 넘겨도 localStorage로 유지)해 "선택 실행" → 요약 확인
  모달 → 휴지통/보관/읽음 처리한다.
- `/vault` - 정리함. 처리된 메일을 `보관함`(archived)/`휴지통`(trashed) 탭으로 나눠 보고,
  보관함은 원래 받은편지함으로 "되돌리기", 휴지통은 서버에서 "영구 삭제"한다. 메일이
  옮겨지면 UID가 바뀌므로 messages.message_id로 대상 폴더에서 다시 찾는다.

여기서 저장/실행한 내용은 바로 다음 fetch_mail.py 실행에 반영된다(같은 data/app.db를
config_store.load_categories()로 읽으므로 - app.db는 categories 테이블과 원본 메일 로그
messages/action_runs 테이블을 함께 담고 있는 단일 SQLite 파일).
"""
import imaplib
import json
import math
from collections import Counter, defaultdict
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from flask import Flask, jsonify, redirect, request, url_for

from mail_core.actions import (
    decode_mailbox_name,
    find_archive_folder,
    find_message_uid_by_id,
    find_trash_folder,
    mark_as_read,
    move_to_folder,
    permanent_delete,
    select_folder,
)
from mail_core.classify import classify

from mail_core.accounts import IMAP_SERVERS, load_accounts

from mail_app import config_store, generate_html
from mail_app.mail_log_store import (
    delete_messages,
    log_action_run,
    mark_message_status,
    query_messages,
    rebind_uid,
)

# 공유 커널(상수 + page()/esc() + build_qs). 블루프린트들도 여기서 읽는다.
from admin_ui._shared import (
    ACCOUNTS_PATH,
    DB_PATH,
    PROVIDER_LABEL,
    build_qs,
    esc,
    page,
    run_state,
)
# 여러 화면 공용 메일-목록 조회/렌더 + 파이프라인 실행 레이어.
from admin_ui._render import (
    MSG_ACTION_FOLDER_FINDERS,
    MSG_ACTION_STATUS,
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
    run_status_html,
)
from admin_ui.settings import bp as _settings_bp

EPOCH_START = datetime(2000, 1, 1)  # "/"의 전체 탭 통계 - app.db에 있는 진짜 전체 기간.
ACCOUNT_LIST_PAGE_SIZE = 20  # 계정 카드를 펼쳤을 때 그 안 페이지네이션 목록의 페이지당 건수.

RANGE_TABS = [
    ("daily", "일일"),
    ("weekly", "주간"),
    ("monthly", "월별"),
    ("yearly", "연도별"),
    ("all", "전체"),
]

app = Flask(__name__)
app.register_blueprint(_settings_bp)  # /settings, 카테고리·계정 CRUD (admin_ui/settings.py)

# /vault 탭: (탭 키, 라벨, messages.status 값, 대상 폴더 finder, 되돌리기/삭제 동사).
VAULT_TABS = [
    ("archive", "보관함", "archived", find_archive_folder, "restore"),
    ("trash", "휴지통", "trashed", find_trash_folder, "purge"),
]
VAULT_TAB_BY_KEY = {t[0]: t for t in VAULT_TABS}


# 대시보드 "계정별 상세" 캐러셀 - ‹/› 버튼으로 트랙을 좌우 스크롤하고, 양 끝에 닿으면
# 해당 버튼을 숨긴다. 펼쳐진 계정 카드(data-open-acct)가 있으면 로드 시 거기로 스크롤한다.
# 접기/펼치기 자체는 여전히 <a href="/?acct=…"> 링크(서버 왕복)라 JS가 필요 없다.
CAROUSEL_SCRIPT = """<script>
(function () {
  var box = document.querySelector('.account-carousel');
  if (!box) return;
  var track = box.querySelector('.account-track');
  var prev = box.querySelector('.carousel-nav.prev');
  var next = box.querySelector('.carousel-nav.next');
  if (!track || !prev || !next) return;

  function step() {
    var card = track.querySelector('.account-detail');
    var gap = parseFloat(getComputedStyle(track).columnGap || getComputedStyle(track).gap) || 12;
    return card ? card.getBoundingClientRect().width + gap : track.clientWidth * 0.8;
  }
  function sync() {
    var max = track.scrollWidth - track.clientWidth - 1;
    var overflowing = max > 0;
    prev.disabled = !overflowing || track.scrollLeft <= 0;
    next.disabled = !overflowing || track.scrollLeft >= max;
  }
  prev.addEventListener('click', function () { track.scrollBy({ left: -step(), behavior: 'smooth' }); });
  next.addEventListener('click', function () { track.scrollBy({ left: step(), behavior: 'smooth' }); });
  track.addEventListener('scroll', sync, { passive: true });
  window.addEventListener('resize', sync);

  var openId = box.dataset.openAcct;
  if (openId) {
    var openCard = document.getElementById(openId);
    if (openCard) {
      var left = openCard.offsetLeft - track.offsetLeft;
      track.scrollTo({ left: left, behavior: 'auto' });
    }
  }
  sync();
})();
</script>"""


# /tasks 목록의 인라인 대량 선택 - 체크 상태를 localStorage에 저장해서 페이지를 넘기거나
# 필터를 바꿔도 선택이 유지된다(7일 후 자동 만료). "선택 실행" → /tasks/action/preview(JSON)
# 요약을 <dialog>에 채우고 → 액션 버튼 → confirm() → 숨은 #task-form 제출(/tasks/action).
# 처리 후 서버는 ?done=1 + <script id="task-result">(실패 key)로 되돌려보내고, 그때
# 성공분만 선택에서 지운다(실패분은 재시도할 수 있게 남긴다).
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


def render_sync_button(base_qs_params: dict) -> str:
    """"동기화" 버튼 - fetch_mail.py를 --since 없이(=계정별 app.db 마지막 저장 시점부터
    이어서, dry-run) 실행하고 지금 보던 화면(range/date)으로 돌아온다. 실제 메일함을
    바꾸는 --apply는 여기서 쓰지 않는다(그건 여전히 /tasks의 "실제 처리" 버튼 전용)."""
    hidden_fields = "".join(
        f'<input type="hidden" name="{esc(k)}" value="{esc(str(v))}">' for k, v in base_qs_params.items()
    )
    return f"""
    <form class="slow-form" method="post" action="/sync" style="margin:0">
      {hidden_fields}
      <button class="btn secondary" type="submit">동기화</button>
    </form>
    """


@app.route("/sync", methods=["POST"])
def sync_now():
    run_state["last_run"] = run_pipeline(apply=False)
    range_key = request.form.get("range", "daily")
    qs_params = {"range": range_key}
    if range_key in {"daily", "weekly", "monthly"}:
        qs_params["date"] = request.form.get("date", "")
    return redirect(f"/?{build_qs(**qs_params)}")


# ---------------------------------------------------------------------------
# 메인 대시보드 (/) - 일일/주간/월별/연도별/전체 탭
# ---------------------------------------------------------------------------

def parse_anchor_date(raw: str | None) -> datetime | None:
    """?date=YYYY-MM-DD 쿼리 파라미터를 파싱한다. 없거나 형식이 잘못되면 None
    (호출부가 오늘 날짜로 폴백한다)."""
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return None


def normalize_anchor(range_key: str, raw: datetime) -> datetime:
    """range_key에 맞게 기준일을 정규화한다 - daily는 그날 0시, weekly는 그 주의
    월요일 0시, monthly는 그 달 1일 0시. 정규화해두면 shift_anchor()로 ±1칸씩 이동해도
    (월말 day-overflow 등) 항상 같은 종류의 기준일을 유지하고, "다음" 버튼을 막을
    시점(오늘 기준 기본 구간과 비교)도 정확히 계산할 수 있다."""
    day0 = datetime(raw.year, raw.month, raw.day)
    if range_key == "weekly":
        return day0 - timedelta(days=day0.weekday())
    if range_key == "monthly":
        return datetime(day0.year, day0.month, 1)
    return day0


def shift_anchor(range_key: str, anchor: datetime, steps: int) -> datetime:
    """정규화된 anchor를 이전/다음 한 칸(daily=1일, weekly=7일, monthly=1개월) 이동한다."""
    if range_key == "weekly":
        return anchor + timedelta(days=7 * steps)
    if range_key == "monthly":
        month_index = anchor.month - 1 + steps
        year = anchor.year + month_index // 12
        month = month_index % 12 + 1
        return datetime(year, month, 1)
    return anchor + timedelta(days=steps)


def range_bounds(range_key: str, anchor: datetime) -> tuple[datetime, datetime | None, str]:
    """range_key 탭의 [since, until) 구간과 화면에 보여줄 라벨을 만든다.

    daily/weekly/monthly는 anchor(normalize_anchor()로 정규화된 기준일)를 기준으로
    구간을 계산해서 화살표 네비게이션(render_period_nav)으로 과거 기간을 오갈 수 있게
    한다. yearly는 사용자가 이번 변경 범위에서 제외하기로 확정해서 기존과 동일하게
    항상 "올해"만 보여준다(anchor 무시, 이동 기능 없음)."""
    if range_key == "weekly":
        until = anchor + timedelta(days=7)
        label = f"{anchor.strftime('%Y-%m-%d')} ~ {(until - timedelta(days=1)).strftime('%Y-%m-%d')}"
        return anchor, until, label
    if range_key == "monthly":
        if anchor.month == 12:
            until = datetime(anchor.year + 1, 1, 1)
        else:
            until = datetime(anchor.year, anchor.month + 1, 1)
        return anchor, until, anchor.strftime("%Y-%m")
    if range_key == "yearly":
        now = datetime.now()
        return datetime(now.year, 1, 1), None, "올해"
    until = anchor + timedelta(days=1)
    return anchor, until, anchor.strftime("%Y-%m-%d")


def render_period_nav(range_key: str, anchor: datetime) -> str:
    """일일/주간/월간 탭에서 이전/다음 기간으로 이동하는 화살표 네비게이션.
    "다음"은 오늘 기준 기본 구간(현재)보다 미래로는 못 가게 막는다 - 그 이후엔 어차피
    저장된 메일이 없다."""
    now = datetime.now()
    today0 = datetime(now.year, now.month, now.day)
    current_default = normalize_anchor(range_key, today0)
    prev_anchor = shift_anchor(range_key, anchor, -1)
    _, _, label = range_bounds(range_key, anchor)

    prev_qs = build_qs(range=range_key, date=prev_anchor.strftime("%Y-%m-%d"))
    prev_link = f'<a href="/?{prev_qs}">← 이전</a>'
    if anchor >= current_default:
        next_link = '<span class="disabled">다음 →</span>'
    else:
        next_anchor = shift_anchor(range_key, anchor, 1)
        next_qs = build_qs(range=range_key, date=next_anchor.strftime("%Y-%m-%d"))
        next_link = f'<a href="/?{next_qs}">다음 →</a>'
    return f'<div class="period-nav">{prev_link}<span class="period-label">{esc(label)}</span>{next_link}</div>'


def render_range_tabs(active_range: str) -> str:
    links = []
    for key, label in RANGE_TABS:
        cls = ' class="active"' if key == active_range else ""
        links.append(f'<a href="/?range={key}"{cls}>{label}</a>')
    return f'<div class="range-tabs">{"".join(links)}</div>'


def render_message_list(
    since: datetime,
    until: datetime | None,
    list_route: str,
    base_params: dict,
    note: str,
) -> str:
    """계정/카테고리 필터 + 페이지네이션으로 [since, until) 기간의 메일 목록을 보여준다.

    "전체" 탭(`/`)과 일일/주간/월간 목록 페이지(`/list`)가 이 함수 하나를 공유한다 -
    since/until만 다르고 필터·페이지네이션 로직은 완전히 동일하기 때문. list_route/
    base_params는 필터폼 action과 페이지 링크에 실어야 하는 고정 쿼리(예: range=all,
    또는 range=daily&date=2026-08-24)를 결정한다."""
    page_num = _parse_page_num(request.args.get("page"))
    page_size = _parse_page_size(request.args.get("page_size"))

    account_filter = request.args.get("account", "").strip() or None
    category_filter = request.args.get("category", "").strip() or None
    query = request.args.get("q", "").strip()

    all_messages, all_users, account_type_by_user, categories = load_all_messages(since, until, account_filter)

    pool = _filter_by_category(all_messages, category_filter)
    if query:
        q = query.lower()
        pool = [m for m in pool if q in (m.get("subject") or "").lower() or q in (m.get("sender") or "").lower()]
    pool.sort(key=lambda m: m.get("message_date") or "", reverse=True)

    total = len(pool)
    total_pages = max(1, math.ceil(total / page_size))
    page_num = min(page_num, total_pages)
    start = (page_num - 1) * page_size
    page_items = pool[start : start + page_size]

    filter_state = {
        "account": account_filter or "",
        "category": category_filter or "",
        "q": query,
        "page_size": page_size,
    }

    account_options = _account_options(all_users, account_type_by_user, account_filter)
    category_options = _category_options(categories, category_filter)
    hidden_fields = "".join(
        f'<input type="hidden" name="{esc(k)}" value="{esc(str(v))}">' for k, v in base_params.items()
    )
    filter_form = f"""
    <form class="filter-form" method="get" action="{list_route}">
      {hidden_fields}
      <label>검색<input type="search" name="q" value="{esc(query)}" placeholder="발신인 · 제목 키워드"></label>
      <label>계정<select name="account">{account_options}</select></label>
      <label>카테고리<select name="category">{category_options}</select></label>
      <label>페이지당 건수<input type="number" name="page_size" min="{MSG_PAGE_SIZE_MIN}" max="{MSG_PAGE_SIZE_MAX}" value="{page_size}"></label>
      <button class="btn" type="submit">필터 적용</button>
      <a class="btn secondary" href="/tasks?{build_qs(**filter_state)}">이 조건으로 작업 실행 →</a>
    </form>
    """

    table = msg_table(page_items, categories, with_account=True)

    prev_qs = build_qs(**filter_state, **base_params, page=page_num - 1)
    next_qs = build_qs(**filter_state, **base_params, page=page_num + 1)
    prev_link = f'<a href="{list_route}?{prev_qs}">← 이전</a>' if page_num > 1 else '<span class="disabled">← 이전</span>'
    next_link = f'<a href="{list_route}?{next_qs}">다음 →</a>' if page_num < total_pages else '<span class="disabled">다음 →</span>'
    pagination = render_pagination(prev_link, next_link, page_num, total_pages, total)

    return f"""
    <p class="sub">{esc(note)}</p>
    {filter_form}
    {table}
    {pagination}
    """


def render_account_inline_list(
    user: str,
    account_type: str,
    since: datetime,
    until: datetime | None,
    categories: dict,
    selected_cat: str,
    page: int,
    base_qs_params: dict,
    anchor: str,
) -> str:
    """펼쳐진 계정 카드 안쪽 - 카테고리 필터 칩("전체" + 건수 상위 몇 개 + "···") +
    실제 페이지네이션 목록. sample_cap=None으로 이 계정만 다시 분류한다 - build_report()의
    전체 리포트는 미분류 샘플을 20건으로 캡해두는데(대시보드 요약용), 여기서는 미분류를
    선택해도 페이지네이션이 20건에서 끊기면 안 되기 때문이다."""
    msgs = query_messages(DB_PATH, since, until, account=user)
    for m in msgs:
        m["account_type"] = account_type
    classified = classify(msgs, categories, sample_cap=None)

    category_of: dict[str, str] = {}
    for name, matches in classified["category_matches"].items():
        for m in matches:
            category_of[m["uid"]] = name
    for m in msgs:
        m["_category"] = category_of.get(m["uid"])

    pool = _filter_by_category(msgs, selected_cat)
    pool.sort(key=lambda m: m.get("message_date") or "", reverse=True)

    total = len(pool)
    total_pages = max(1, math.ceil(total / ACCOUNT_LIST_PAGE_SIZE))
    page = min(max(1, page), total_pages)
    start = (page - 1) * ACCOUNT_LIST_PAGE_SIZE
    page_items = pool[start : start + ACCOUNT_LIST_PAGE_SIZE]

    def filter_link(label_text: str, value: str, count: int, active: bool) -> str:
        qs = build_qs(**base_qs_params, acct=user, acct_cat=value, acct_page=1)
        cls = "acct-filter-chip active" if active else "acct-filter-chip"
        return f'<a class="{cls}" href="/?{qs}#{anchor}">{esc(label_text)} <span class="chip-value">{count}</span></a>'

    cat_counts = [(name, classified["categories"][name]["count"]) for name in classified["categories"]]
    if classified["uncategorized_count"]:
        cat_counts.append(("미분류", classified["uncategorized_count"]))
    cat_counts.sort(key=lambda t: (-t[1], t[0]))

    filter_items = [
        filter_link(
            name,
            "__uncategorized__" if name == "미분류" else name,
            count,
            selected_cat == ("__uncategorized__" if name == "미분류" else name),
        )
        for name, count in cat_counts
        if count
    ]
    capped_filters = generate_html.render_capped(
        filter_items, generate_html.ACCOUNT_CHIP_CAP, f"{anchor}-filter-more",
        "acct-filter-chip-extra", "acct-filter-chip acct-filter-more",
    )
    filter_row = (
        f'<div class="acct-filter-row">'
        f'{filter_link("전체", "", len(msgs), not selected_cat)}'
        f'{capped_filters}'
        f'</div>'
    )

    table = msg_table(page_items, categories, with_account=False)

    def page_qs(p: int) -> str:
        return build_qs(**base_qs_params, acct=user, acct_cat=selected_cat or "", acct_page=p)

    prev_link = f'<a href="/?{page_qs(page - 1)}#{anchor}">← 이전</a>' if page > 1 else '<span class="disabled">← 이전</span>'
    next_link = f'<a href="/?{page_qs(page + 1)}#{anchor}">다음 →</a>' if page < total_pages else '<span class="disabled">다음 →</span>'
    pagination = render_pagination(prev_link, next_link, page, total_pages, total)

    return f'{filter_row}{table}{pagination}'


def render_live_account_card(
    user: str,
    account_data: dict,
    account_type: str,
    since: datetime,
    until: datetime | None,
    categories: dict,
    is_open: bool,
    base_qs_params: dict,
    selected_cat: str,
    page: int,
) -> str:
    """계정 카드 하나. 접혀 있으면 상위 4개 카테고리 미니칩 요약만(정적 Artifact와 같은
    모양), 펼쳐져 있으면(?acct= 쿼리파라미터가 이 계정과 일치) 그 아래 실제 페이지네이션
    목록까지 보여준다. 한 번에 하나의 계정만 열 수 있다 - 다른 계정 카드를 클릭하면
    URL의 acct 값이 바뀌면서 이전 계정은 자동으로 닫힌다(서버 렌더링만으로 동작, JS 없음)."""
    anchor = generate_html.account_anchor_id(user)
    label = PROVIDER_LABEL.get(account_type, account_type or "")
    total = account_data["total"]

    cat_counts = [(name, account_data["categories"][name]["count"]) for name in account_data["categories"]]
    uncategorized_count = account_data.get("uncategorized_count", 0)
    if uncategorized_count:
        cat_counts.append(("미분류", uncategorized_count))
    cat_counts.sort(key=lambda t: (-t[1], t[0]))

    mini_items = []
    for name, count in cat_counts:
        color_class = (
            "muted" if name == "미분류"
            else generate_html.ACTION_COLOR_CLASS.get(account_data["categories"][name]["action"], "muted")
        )
        mini_items.append(generate_html.render_mini_stat(name, count, color_class))
    mini_stats = generate_html.render_mini_stat("총", total) + generate_html.render_capped(
        mini_items, generate_html.ACCOUNT_CHIP_CAP, f"{anchor}-mini-more", "mini-stat-extra", "mini-stat msw3 mini-stat-more"
    )

    if not is_open:
        open_qs = build_qs(**base_qs_params, acct=user)
        return f"""
        <div class="account-detail" id="{anchor}">
          <a class="account-summary-link" href="/?{open_qs}#{anchor}">
            <span class="account-name">{esc(label)} · {esc(user)}</span>
            <span class="account-mini-stats">{mini_stats}</span>
            <span class="chevron">▸</span>
          </a>
        </div>
        """

    close_qs = build_qs(**base_qs_params)
    body = render_account_inline_list(
        user, account_type, since, until, categories, selected_cat, page, base_qs_params, anchor
    )
    return f"""
    <div class="account-detail open" id="{anchor}">
      <a class="account-summary-link" href="/?{close_qs}#{anchor}">
        <span class="account-name">{esc(label)} · {esc(user)}</span>
        <span class="account-mini-stats">{mini_stats}</span>
        <span class="chevron">▸</span>
      </a>
      <div class="account-detail-body">{body}</div>
    </div>
    """


def render_dashboard_report(range_key: str, since: datetime, until: datetime | None, label: str, base_qs_params: dict) -> str:
    """리포트 화면(헤더+동기화 버튼/통계/액션/카테고리별 상세)은 generate_html.py의
    조각 함수들을 그대로 재사용하고, "계정별 상세"만 실시간 페이지네이션이 가능한
    이 파일만의 버전(render_live_account_card)으로 바꿔치기한다."""
    try:
        report = generate_html.build_report(since, until)
    except SystemExit:
        return (
            f'<p class="empty">{esc(label)} 기간에 조회된 메일이 없습니다. '
            f'"작업 실행"에서 새로고침을 먼저 실행해보세요.</p>'
        )

    header = generate_html.render_report_header(report, render_sync_button(base_qs_params))
    stats = generate_html.render_report_stats(report)
    actions = generate_html.render_report_actions(report)
    cats = generate_html.render_report_categories(report)

    accounts_cfg = {a["user"]: a["type"] for a in load_accounts(ACCOUNTS_PATH)}
    per_account = report.get("per_account", {})
    accounts = generate_html.sorted_accounts(report)
    open_acct = request.args.get("acct", "").strip() or None
    selected_cat = request.args.get("acct_cat", "").strip()
    acct_page = _parse_page_num(request.args.get("acct_page"))
    categories = config_store.load_categories(DB_PATH)

    account_cards = "".join(
        render_live_account_card(
            p, per_account[p], accounts_cfg.get(p) or per_account[p].get("type"),
            since, until, categories,
            p == open_acct, base_qs_params, selected_cat, acct_page,
        )
        for p in accounts
    )
    # 계정 카드를 세로로 쌓지 않고 좌우 캐러셀(가로 스크롤 + ‹/› 버튼)로 보여준다.
    # 접고/펼치기(?acct=)는 그대로 서버가 관리 - 펼쳐진 카드로는 로드 시 JS가 스크롤한다.
    open_attr = f' data-open-acct="{esc(generate_html.account_anchor_id(open_acct))}"' if open_acct else ""
    accounts_html = (
        '<h2 class="section-title">계정별 상세</h2>'
        f'<div class="account-carousel"{open_attr}>'
        '<div class="carousel-bar">'
        '<button type="button" class="carousel-nav prev" aria-label="이전 계정" disabled>‹</button>'
        '<button type="button" class="carousel-nav next" aria-label="다음 계정" disabled>›</button>'
        '</div>'
        f'<div class="account-track">{account_cards}</div>'
        '</div>'
    )

    return header + stats + actions + cats + accounts_html + CAROUSEL_SCRIPT


@app.route("/")
def dashboard_page():
    range_key = request.args.get("range", "daily")
    if range_key not in {k for k, _ in RANGE_TABS}:
        range_key = "daily"

    period_nav_html = ""
    list_qs: str | None = None

    if range_key in {"daily", "weekly", "monthly"}:
        now = datetime.now()
        today0 = datetime(now.year, now.month, now.day)
        raw_anchor = parse_anchor_date(request.args.get("date")) or today0
        anchor = normalize_anchor(range_key, raw_anchor)
        since, until, label = range_bounds(range_key, anchor)
        base_qs_params = {"range": range_key, "date": anchor.strftime("%Y-%m-%d")}
        period_nav_html = render_period_nav(range_key, anchor)
        list_qs = build_qs(**base_qs_params)
    elif range_key == "yearly":
        # 화살표 이동 없이 기존과 동일하게 "올해"만 보여준다(요청 범위 밖) - 목록 링크도 없음.
        since, until, label = range_bounds("yearly", datetime.now())
        base_qs_params = {"range": "yearly"}
    else:  # all - 고정 기간이 아니라 app.db에 있는 진짜 전체 기간 기준 통계.
        since, until, label = EPOCH_START, None, "전체(누적)"
        base_qs_params = {"range": "all"}
        list_qs = "range=all"

    content = render_dashboard_report(range_key, since, until, label, base_qs_params)
    list_link_html = (
        f'<a class="period-list-link" href="/list?{list_qs}">→ 이 기간 목록 보기 (페이지네이션)</a>'
        if list_qs else ""
    )
    body = f'{render_range_tabs(range_key)}{period_nav_html}{content}{list_link_html}'
    return page("메일 대시보드", body, "dashboard")


@app.route("/list")
def message_list_page():
    """일일/주간/월간/전체 각 기간의 메일을 계정/카테고리 필터 + 페이지네이션으로
    보여주는 별도 화면. 리포트 화면(카테고리별 요약)과 달리 개별 메일 행을 그대로
    나열한다 - "전체" 탭(`/`)이 원래 하던 걸 render_message_list()로 일반화해서
    daily/weekly/monthly 기간에도 재활용한다."""
    range_key = request.args.get("range", "daily")
    if range_key not in {"daily", "weekly", "monthly", "all"}:
        range_key = "daily"

    if range_key == "all":
        since = datetime.now() - timedelta(days=MSG_DEFAULT_SINCE_DAYS)
        until = None
        base_params = {"range": "all"}
        note = f"최근 {MSG_DEFAULT_SINCE_DAYS}일(약 2년)간 저장된 전체 메일입니다."
        period_nav = ""
        back_qs = "range=all"
    else:
        now = datetime.now()
        today0 = datetime(now.year, now.month, now.day)
        raw_anchor = parse_anchor_date(request.args.get("date")) or today0
        anchor = normalize_anchor(range_key, raw_anchor)
        since, until, label = range_bounds(range_key, anchor)
        base_params = {"range": range_key, "date": anchor.strftime("%Y-%m-%d")}
        note = f"{label} 기간의 메일 목록입니다."
        period_nav = render_period_nav(range_key, anchor)
        back_qs = build_qs(**base_params)

    content = render_message_list(since, until, "/list", base_params, note)
    body = (
        f'<p class="nav"><a class="back-link" href="/?{back_qs}">← 리포트로 돌아가기</a></p>'
        f'{period_nav}{content}'
    )
    return page("메일 목록", body, "dashboard")


# ---------------------------------------------------------------------------
# 작업 실행 (/tasks) - 파이프라인 실행 버튼 + 목록 인라인 대량 선택(localStorage로
# 페이지 간 유지) + 실행 전 요약 확인 모달
# ---------------------------------------------------------------------------

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


@app.route("/tasks")
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


@app.route("/tasks/run", methods=["POST"])
def tasks_run():
    run_state["last_run"] = run_pipeline(apply=False)
    return redirect(url_for("tasks_page"))


@app.route("/tasks/apply", methods=["POST"])
def tasks_apply():
    run_state["last_run"] = run_pipeline(apply=True)
    return redirect(url_for("tasks_page"))


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


@app.route("/tasks/action/preview", methods=["POST"])
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


@app.route("/tasks/action", methods=["POST"])
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
            imap.login(account["user"], account["password"])
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


# ---------------------------------------------------------------------------
# 정리함 (/vault) - 보관함(archived) / 휴지통(trashed) 전용 화면
#   보관함: "되돌리기"(원래 메일함=INBOX로 이동)  ·  휴지통: "영구 삭제"(EXPUNGE)
# 메일이 옮겨지면 UID가 바뀌므로, 대상 폴더에서 messages.message_id 로 다시 찾아
# 처리한다. message_id 가 없는(레거시) 행은 IMAP 반영 없이 DB만 정리한다.
# ---------------------------------------------------------------------------

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


@app.route("/vault")
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


# 이 앱이 바인드되는 호스트(데스크톱 앱이 포트를 바꿔도 호스트명은 이 셋 중 하나).
# request.host 는 Host 헤더라 공격자가 조종할 수 있어서(DNS rebinding), 로컬 호스트가
# 아니면 상태 변경을 아예 거부한다. Origin/Host 가 둘 다 attacker 도메인이면 예전엔
# 통과했다.
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"}


def _host_name(host: str) -> str:
    """"127.0.0.1:5000" → "127.0.0.1", "[::1]:5000" → "::1"."""
    h = host.rsplit(":", 1)[0] if host.count(":") == 1 or host.startswith("[") else host
    return h.strip("[]")


def _is_cross_origin_post() -> bool:
    """상태 변경 POST가 로컬 앱 자신이 아닌 다른 출처에서 왔는지 판정한다.

    이 앱은 127.0.0.1 전용이고 세션 쿠키가 없어 SameSite 보호가 없다 - 브라우저의
    다른 탭이 cross-origin 폼 POST로 /vault/purge(영구삭제) 등을 때리는 CSRF를 막는다.
    포트는 request.host(실제 바인드) 기준으로 비교해 데스크톱 앱이 포트를 바꿔도 동작한다.

    Origin/Referer 헤더가 아예 없으면(curl/스크립트, 그리고 **Electron 스케줄러가
    raw http 로 때리는 /sync**. desktop/scheduler.js 참고) 통과시킨다. 이 폴백을
    없애면 백그라운드 동기화가 깨지므로 주의.
    """
    host = request.host  # 예: "127.0.0.1:5000"
    origin = request.headers.get("Origin")
    if origin is not None:
        return urlparse(origin).netloc != host
    referer = request.headers.get("Referer")
    if referer:
        return urlparse(referer).netloc != host
    return False


@app.before_request
def _block_cross_origin_writes():
    """모든 상태 변경 POST(설정 CRUD·/sync·/tasks/apply·/vault/* 포함)를 한 곳에서
    cross-origin CSRF + DNS rebinding 으로부터 막는다. 예전엔 /tasks/action·
    /vault/restore·/vault/purge 세 곳에만 개별로 걸려 있어서, 다른 탭의 악성 페이지가
    /tasks/apply(실제 메일함 정리)나 /settings/accounts/<user>/delete 를 POST 로
    때릴 수 있었다.

    Host 검증은 GET 을 포함한 **모든** 메서드에 건다: 공격자가 자기 도메인을
    127.0.0.1 로 rebind 하면 브라우저 입장에선 same-origin 이 되어 GET 응답
    (메일 발신인·제목 등 메타데이터)까지 스크립트로 읽어갈 수 있기 때문이다.
    이 앱은 127.0.0.1 에만 바인드하므로 정상 요청의 Host 는 늘 로컬이다."""
    if _host_name(request.host) not in _LOCAL_HOSTS:
        return "로컬 호스트가 아닌 요청 거부", 403
    if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
        return None
    if _is_cross_origin_post():
        return "cross-origin 요청 거부", 403
    return None


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


@app.route("/vault/restore", methods=["POST"])
def vault_restore():
    return _vault_process("restore")


@app.route("/vault/purge", methods=["POST"])
def vault_purge():
    return _vault_process("purge")


def main() -> None:
    """`python -m admin_ui` / `mail-admin` 진입점."""
    print(f"메일 대시보드: http://127.0.0.1:5000  (DB: {DB_PATH})")
    app.run(host="127.0.0.1", port=5000, debug=False)


if __name__ == "__main__":
    main()
