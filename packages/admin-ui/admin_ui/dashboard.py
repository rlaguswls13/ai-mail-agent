"""admin_ui 대시보드 블루프린트 - 메인 리포트(`/`), 메일 목록(`/list`), 동기화(`/sync`).

- /        : 일일/주간/월별/연도별/전체 탭. generate_html 조각 재사용 + "계정별 상세"만
             실시간 페이지네이션(render_live_account_card).
- /list    : 개별 메일 행을 필터 + 페이지네이션으로 나열(render_message_list).
- /sync    : fetch_mail -> generate_html dry-run 파이프라인 후 보던 화면으로 복귀.
기간 이동(화살표)은 normalize_anchor/shift_anchor/range_bounds 로 계산.
"""
import math
from datetime import datetime, timedelta

from flask import Blueprint, redirect, request

from mail_core.classify import classify
from mail_core.accounts import load_accounts
from mail_app import config_store, generate_html
from mail_app.mail_log_store import query_messages

from admin_ui._shared import (
    ACCOUNTS_PATH,
    DB_PATH,
    PROVIDER_LABEL,
    build_qs,
    esc,
    page,
    run_state,
)
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

bp = Blueprint("dashboard", __name__)

EPOCH_START = datetime(2000, 1, 1)  # "/"의 전체 탭 통계 - app.db에 있는 진짜 전체 기간.
ACCOUNT_LIST_PAGE_SIZE = 20  # 계정 카드를 펼쳤을 때 그 안 페이지네이션 목록의 페이지당 건수.

RANGE_TABS = [
    ("daily", "일일"),
    ("weekly", "주간"),
    ("monthly", "월별"),
    ("yearly", "연도별"),
    ("all", "전체"),
]


# 대시보드 "상세 조회" 캐러셀 - 한 번에 카드 1개만 꽉 차게 보여주고 ‹/› 버튼으로 넘긴다
# (1칸=카드 1개). 양 끝에서는 해당 버튼을 비활성화하고, 가운데 "n / 전체" 위치 표시를
# 갱신한다. 펼쳐진 카드(data-open-acct)가 있으면 로드 시 거기로 스크롤한다. 접기/펼치기
# 자체는 여전히 <a href="/?acct=…"> 링크(서버 왕복).
CAROUSEL_SCRIPT = """<script>
(function () {
  var box = document.querySelector('.account-carousel');
  if (!box) return;
  var track = box.querySelector('.account-track');
  var bar = box.querySelector('.carousel-bar');
  var prev = box.querySelector('.carousel-nav.prev');
  var next = box.querySelector('.carousel-nav.next');
  var count = box.querySelector('.carousel-count');
  if (!track || !prev || !next) return;

  var cards = track.querySelectorAll('.account-detail');
  if (cards.length <= 1 && bar) bar.style.display = 'none';

  function step() {
    var card = cards[0];
    var gap = parseFloat(getComputedStyle(track).columnGap || getComputedStyle(track).gap) || 12;
    return card ? card.getBoundingClientRect().width + gap : track.clientWidth;
  }
  function index() {
    return Math.round(track.scrollLeft / step());
  }
  function sync() {
    var max = track.scrollWidth - track.clientWidth - 1;
    prev.disabled = track.scrollLeft <= 0;
    next.disabled = track.scrollLeft >= max;
    var i = Math.min(index(), cards.length - 1);
    if (count) count.textContent = (i + 1) + ' / ' + cards.length;
    // 트랙 높이를 현재 슬라이드에 맞춘다 - 안 그러면 가장 큰 카드 높이만큼
    // 빈 공간이 남는다(캐러셀은 flex라 트랙 높이 = 최대 자식 높이).
    if (cards[i]) track.style.height = cards[i].offsetHeight + 'px';
  }
  // 트랙(.account-track)의 scrollLeft만 직접 계산해서 옮긴다 - scrollIntoView는
  // 조상 스크롤 컨테이너(문서 전체 포함)를 다 동원해서 세로로도 페이지를 흔들어
  // "움직임이 이상하다"는 원인이었다. 카드 폭이 항상 트랙 폭 100%라 가로로만
  // 옮기면 그 카드가 정확히 뷰포트를 꽉 채운다.
  function goTo(i) {
    i = Math.max(0, Math.min(i, cards.length - 1));
    var card = cards[i];
    if (!card) return;
    track.scrollTo({ left: card.offsetLeft - track.offsetLeft, behavior: 'smooth' });
    // 포커스(outline)로 "지금 보는 카드"를 표시하되, preventScroll로 focus() 자신이
    // 또 스크롤을 만들어 방금 맞춘 화면을 흔들지 않게 한다.
    window.setTimeout(function () { card.focus({ preventScroll: true }); }, 260);
  }
  prev.addEventListener('click', function () { goTo(index() - 1); });
  next.addEventListener('click', function () { goTo(index() + 1); });
  track.addEventListener('scroll', sync, { passive: true });
  window.addEventListener('resize', sync);

  // 첫 렌더에서는 트랙 높이 전환(transition)을 꺼서 "로딩되며 쑥 늘어나는" 애니메이션이
  // 안 보이게 하고, 페인트가 끝난 뒤에야 되살려서 이후 이동에만 부드러운 전환이 걸리게 한다.
  track.style.transition = 'none';
  var openId = box.dataset.openAcct;
  if (openId) {
    var openCard = document.getElementById(openId);
    if (openCard) track.scrollLeft = openCard.offsetLeft - track.offsetLeft;
  }
  sync();
  requestAnimationFrame(function () {
    requestAnimationFrame(function () { track.style.transition = ''; });
  });
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


@bp.route("/sync", methods=["POST"])
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
    *,
    compact: bool = False,
) -> str:
    """계정/카테고리 필터 + 페이지네이션으로 [since, until) 기간의 메일 목록을 보여준다.

    "전체" 탭(`/`)과 일일/주간/월간 목록 페이지(`/list`)가 이 함수 하나를 공유한다 -
    since/until만 다르고 필터·페이지네이션 로직은 완전히 동일하기 때문. list_route/
    base_params는 필터폼 action과 페이지 링크에 실어야 하는 고정 쿼리(예: range=all,
    또는 range=daily&date=2026-08-24)를 결정한다.

    compact=True면 필터폼을 아예 안 그린다 - 대시보드 캐러셀 안(통합 카드)에선 검색/
    카테고리/페이지당 건수/작업 실행이 캐러셀 위 공용 검색바(render_carousel_search)로
    이미 다 옮겨져 있어서 여기 또 그릴 게 없다(계정 필터는 캐러셀에서 그 계정 카드로
    바로 이동하는 걸로 대체돼 없앴다)."""
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

    if compact:
        filter_form = ""
    else:
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
    page_num: int,
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
    query = request.args.get("q", "").strip()
    if query:
        ql = query.lower()
        pool = [m for m in pool if ql in (m.get("subject") or "").lower() or ql in (m.get("sender") or "").lower()]
    pool.sort(key=lambda m: m.get("message_date") or "", reverse=True)

    total = len(pool)
    total_pages = max(1, math.ceil(total / ACCOUNT_LIST_PAGE_SIZE))
    page_num = min(max(1, page_num), total_pages)
    start = (page_num - 1) * ACCOUNT_LIST_PAGE_SIZE
    page_items = pool[start : start + ACCOUNT_LIST_PAGE_SIZE]

    def filter_link(label_text: str, value: str, count: int, active: bool) -> str:
        qs = build_qs(**base_qs_params, acct=user, acct_cat=value, acct_page=1, q=query)
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
        return build_qs(**base_qs_params, acct=user, acct_cat=selected_cat or "", acct_page=p, q=query)

    prev_link = f'<a href="/?{page_qs(page_num - 1)}#{anchor}">← 이전</a>' if page_num > 1 else '<span class="disabled">← 이전</span>'
    next_link = f'<a href="/?{page_qs(page_num + 1)}#{anchor}">다음 →</a>' if page_num < total_pages else '<span class="disabled">다음 →</span>'
    pagination = render_pagination(prev_link, next_link, page_num, total_pages, total)

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
    page_num: int,
    per_action: dict | None = None,
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

    action_line = render_action_summary_line(None, per_action or {})
    action_note = f'<div class="acct-action-note">{action_line}</div>' if action_line else ""

    if not is_open:
        open_qs = build_qs(**base_qs_params, acct=user)
        return f"""
        <div class="account-detail" id="{anchor}" tabindex="-1">
          <a class="account-summary-link" href="/?{open_qs}#{anchor}">
            <span class="account-name">{esc(label)} · {esc(user)}</span>
            <span class="account-mini-stats">{mini_stats}</span>
            <span class="chevron">▸</span>
          </a>
          {action_note}
        </div>
        """

    close_qs = build_qs(**base_qs_params)
    body = render_account_inline_list(
        user, account_type, since, until, categories, selected_cat, page_num, base_qs_params, anchor
    )
    return f"""
    <div class="account-detail open" id="{anchor}" tabindex="-1">
      <a class="account-summary-link" href="/?{close_qs}#{anchor}">
        <span class="account-name">{esc(label)} · {esc(user)}</span>
        <span class="account-mini-stats">{mini_stats}</span>
        <span class="chevron">▸</span>
      </a>
      {action_note}
      <div class="account-detail-body">{body}</div>
    </div>
    """


def _action_summary_parts(per_action: dict) -> list[str]:
    """계정별 액션 요약을 "보관 6 · 휴지통 이동 4" 형태 조각으로."""
    parts = []
    for action, data in per_action.items():
        n = data.get("candidates", 0)
        if n:
            parts.append(f'{generate_html.ACTION_LABEL.get(action, action)} {n}')
    return parts


def compute_action_preview(per_account: dict) -> dict[str, dict]:
    """지금 화면에 보이는 기간([since, until))에 맞춰 계정별 자동 처리 예정 건수를
    다시 계산한다.

    mail_log_store.latest_action_summary()(=report["actions"])는 "가장 최근 동기화
    실행" 스냅샷 하나뿐이라 일일/주간/월간 어느 탭을 봐도 항상 같은 숫자만 보여줘서
    화면 위 기간별 통계(총 메일·카테고리 건수)와 안 맞았다 - 그 대신 이미 기간으로
    걸러 분류해둔 per_account[user]["category_matches"]에서, action이 keep이 아니고
    아직 처리 안 된(활성 상태/안 읽음) 메일만 세어 항상 화면 기간과 일치시킨다."""
    out: dict[str, dict] = {}
    for user, classified in per_account.items():
        categories = classified.get("categories", {})
        per_action: dict[str, dict] = {}
        for name, matches in classified.get("category_matches", {}).items():
            action = categories.get(name, {}).get("action", "keep")
            if action == "keep":
                continue
            if action == "read":
                pending = [m for m in matches if not m.get("is_read")]
            else:
                pending = [m for m in matches if (m.get("status") or "active") == "active"]
            if pending:
                per_action.setdefault(action, {"candidates": 0})
                per_action[action]["candidates"] += len(pending)
        if per_action:
            out[user] = per_action
    return out


# 이 기간에 자동 분류 규칙에 따라 다음 동기화 때 실행될 액션을 가리키는 용어.
# "액션"·"처리 예상 항목" 등으로 제각각 부르던 걸 통일 - 통합 카드와 계정 카드가
# 완전히 같은 문구·형식(한 줄: "{계정 ·} {용어}: 보관 N · 휴지통 이동 N")을 쓴다.
ACTION_PREVIEW_TERM = "자동 처리 예정"


def render_action_summary_line(user: str | None, per_action: dict) -> str:
    """계정 하나의 액션 요약 한 줄. user를 주면 앞에 계정명을 붙인다(통합 카드가
    여러 계정을 한 목록으로 나열할 때 씀) - 그 외엔 render_live_account_card가
    자기 계정 몫만 이 형식 그대로 보여준다(통합 카드와 완전히 같은 한 줄 서식)."""
    parts = _action_summary_parts(per_action)
    if not parts:
        return ""
    prefix = f'{esc(user)} · ' if user else ''
    return f'<div class="action-line">{prefix}{esc(ACTION_PREVIEW_TERM)}: {esc(" · ".join(parts))}</div>'


UNIFIED_ACCT_ID = "__all__"  # ?acct= 에 쓰는 예약값 - 실제 계정 이메일과 절대 안 겹친다.


def render_unified_card(
    report: dict, since: datetime, until: datetime | None, base_qs_params: dict, is_open: bool
) -> str:
    """캐러셀 첫 슬라이드(기본값 위치) - 전 계정을 하나로 합친 카드. 계정 카드와 똑같이
    접혀있다가(미니 통계 요약만) 펼쳐야(?acct=__all__) 자동 처리 예정 + 통합 목록을
    보여준다. 기존 "카테고리별 상세"·"액션" 섹션은 펼친 상태 안으로 흡수했다."""
    overall = report["overall"]
    cat_items = generate_html.sorted_category_items(overall)
    mini_items = []
    for name, count in cat_items:
        if not count:
            continue
        color = (
            "muted" if name == "미분류"
            else generate_html.ACTION_COLOR_CLASS.get(overall["categories"][name]["action"], "muted")
        )
        mini_items.append(generate_html.render_mini_stat(name, count, color))
    mini_stats = generate_html.render_mini_stat("총", overall["total"]) + generate_html.render_capped(
        mini_items, generate_html.ACCOUNT_CHIP_CAP, "all-mini-more", "mini-stat-extra", "mini-stat msw3 mini-stat-more"
    )

    if not is_open:
        open_qs = build_qs(**base_qs_params, acct=UNIFIED_ACCT_ID)
        return f"""
        <div class="account-detail" id="acct-all" tabindex="-1">
          <a class="account-summary-link" href="/?{open_qs}#acct-all">
            <span class="account-name">전체 계정 통합</span>
            <span class="account-mini-stats">{mini_stats}</span>
            <span class="chevron">▸</span>
          </a>
        </div>
        """

    action_accounts = compute_action_preview(report.get("per_account", {}))
    action_lines = "".join(
        render_action_summary_line(user, per_action)
        for user, per_action in action_accounts.items()
    )
    if not action_lines:
        action_lines = '<p class="empty">지금은 처리할 메일이 없습니다.</p>'
    preview = f'<div class="unified-actions"><h3 class="unified-sub">{esc(ACTION_PREVIEW_TERM)}</h3>{action_lines}</div>'

    listing = render_message_list(since, until, "/", base_qs_params, compact=True)
    close_qs = build_qs(**base_qs_params)
    return f"""
    <div class="account-detail open unified-card" id="acct-all" tabindex="-1">
      <a class="account-summary-link" href="/?{close_qs}#acct-all">
        <span class="account-name">전체 계정 통합</span>
        <span class="account-mini-stats">{mini_stats}</span>
        <span class="chevron">▸</span>
      </a>
      <div class="account-detail-body">
        {preview}
        {listing}
      </div>
    </div>
    """


def render_carousel_search(base_qs_params: dict) -> str:
    """검색·카테고리·페이지당 건수는 통합 카드 목록에 적용되는 조건이라 카드 안이
    아니라 캐러셀 위에 한 번 둔다. 지금 URL의 다른 조건(범위/날짜/열린 계정 등)은
    hidden으로 그대로 실어서, 필터를 바꿔도 나머지 상태가 안 날아가게 한다."""
    query = request.args.get("q", "").strip()
    category_filter = request.args.get("category", "").strip() or None
    page_size = _parse_page_size(request.args.get("page_size"))
    explicit = {"q", "page", "category", "page_size"}
    preserved = {k: v for k, v in request.args.items() if k not in explicit}
    hidden_fields = "".join(
        f'<input type="hidden" name="{esc(k)}" value="{esc(v)}">' for k, v in preserved.items()
    )
    categories = config_store.load_categories(DB_PATH)
    category_options = _category_options(categories, category_filter)
    filter_state = {
        **base_qs_params,
        "category": category_filter or "",
        "q": query,
        "page_size": page_size,
    }
    return f"""
    <form class="carousel-search" method="get" action="/">
      {hidden_fields}
      <label>검색<input type="search" name="q" value="{esc(query)}" placeholder="발신인 · 제목 키워드"></label>
      <label>카테고리<select name="category">{category_options}</select></label>
      <label>페이지당 건수<input type="number" name="page_size" min="{MSG_PAGE_SIZE_MIN}" max="{MSG_PAGE_SIZE_MAX}" value="{page_size}"></label>
      <button class="btn" type="submit">필터 적용</button>
      <a class="btn secondary" href="/tasks?{build_qs(**filter_state)}">이 조건으로 작업 실행 →</a>
    </form>
    """


def render_dashboard_report(range_key: str, since: datetime, until: datetime | None, label: str, base_qs_params: dict) -> str:
    """리포트 화면(헤더+동기화 버튼+통계) + "상세 조회" 캐러셀. 캐러셀 첫 슬라이드는
    전 계정 통합 카드, 나머지가 계정별 카드 - 모두 계정 카드와 똑같이 기본은 접혀있고
    (?acct=)로 펼쳐야 내용이 보인다. 예전의 독립 "액션"·"카테고리별 상세" 섹션은
    통합 카드를 펼쳤을 때 안으로 흡수했다."""
    try:
        report = generate_html.build_report(since, until)
    except SystemExit:
        return (
            f'<p class="empty">{esc(label)} 기간에 조회된 메일이 없습니다. '
            f'"작업 실행"에서 새로고침을 먼저 실행해보세요.</p>'
        )

    header = generate_html.render_report_header(report, render_sync_button(base_qs_params))
    stats = generate_html.render_report_stats(report)

    accounts_cfg = {a["user"]: a["type"] for a in load_accounts(ACCOUNTS_PATH)}
    per_account = report.get("per_account", {})
    accounts = generate_html.sorted_accounts(report)
    open_acct = request.args.get("acct", "").strip() or None
    selected_cat = request.args.get("acct_cat", "").strip()
    acct_page = _parse_page_num(request.args.get("acct_page"))
    categories = config_store.load_categories(DB_PATH)
    action_accounts = compute_action_preview(per_account)

    unified_card = render_unified_card(report, since, until, base_qs_params, open_acct == UNIFIED_ACCT_ID)
    account_cards = "".join(
        render_live_account_card(
            p, per_account[p], accounts_cfg.get(p) or per_account[p].get("type"),
            since, until, categories,
            p == open_acct, base_qs_params, selected_cat, acct_page,
            action_accounts.get(p, {}),
        )
        for p in accounts
    )
    # 계정 카드를 세로로 쌓지 않고 좌우 캐러셀(가로 스크롤 + ‹/› 버튼)로 보여준다.
    # 접고/펼치기(?acct=)는 그대로 서버가 관리 - 펼쳐진 카드로는 로드 시 JS가 스크롤한다.
    open_attr = f' data-open-acct="{esc(generate_html.account_anchor_id(open_acct))}"' if open_acct else ""
    accounts_html = (
        '<h2 class="section-title">상세 조회</h2>'
        f'{render_carousel_search(base_qs_params)}'
        f'<div class="account-carousel"{open_attr}>'
        '<div class="carousel-bar">'
        '<button type="button" class="carousel-nav prev" aria-label="이전" disabled>‹</button>'
        '<span class="carousel-count" aria-live="polite"></span>'
        '<button type="button" class="carousel-nav next" aria-label="다음" disabled>›</button>'
        '</div>'
        f'<div class="account-track">{unified_card}{account_cards}</div>'
        '</div>'
    )

    return header + stats + accounts_html + CAROUSEL_SCRIPT


@bp.route("/")
def dashboard_page():
    range_key = request.args.get("range", "daily")
    if range_key not in {k for k, _ in RANGE_TABS}:
        range_key = "daily"

    period_nav_html = ""

    if range_key in {"daily", "weekly", "monthly"}:
        now = datetime.now()
        today0 = datetime(now.year, now.month, now.day)
        raw_anchor = parse_anchor_date(request.args.get("date")) or today0
        anchor = normalize_anchor(range_key, raw_anchor)
        since, until, label = range_bounds(range_key, anchor)
        base_qs_params = {"range": range_key, "date": anchor.strftime("%Y-%m-%d")}
        period_nav_html = render_period_nav(range_key, anchor)
    elif range_key == "yearly":
        # 화살표 이동 없이 기존과 동일하게 "올해"만 보여준다(요청 범위 밖).
        since, until, label = range_bounds("yearly", datetime.now())
        base_qs_params = {"range": "yearly"}
    else:  # all - 고정 기간이 아니라 app.db에 있는 진짜 전체 기간 기준 통계.
        since, until, label = EPOCH_START, None, "전체(누적)"
        base_qs_params = {"range": "all"}

    content = render_dashboard_report(range_key, since, until, label, base_qs_params)
    # "이 기간 목록 보기" 링크는 삭제 - 통합 카드 안 목록이 계정/카테고리/검색 필터 +
    # 페이지네이션까지 이미 제공해서 /list 로 또 나가는 게 기능 중복이었다.
    body = f'{render_range_tabs(range_key)}{period_nav_html}{content}'
    return page("메일 대시보드", body, "dashboard")


@bp.route("/list")
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
        period_nav = ""
        back_qs = "range=all"
    else:
        now = datetime.now()
        today0 = datetime(now.year, now.month, now.day)
        raw_anchor = parse_anchor_date(request.args.get("date")) or today0
        anchor = normalize_anchor(range_key, raw_anchor)
        since, until, label = range_bounds(range_key, anchor)
        base_params = {"range": range_key, "date": anchor.strftime("%Y-%m-%d")}
        period_nav = render_period_nav(range_key, anchor)
        back_qs = build_qs(**base_params)

    content = render_message_list(since, until, "/list", base_params)
    body = (
        f'<p class="nav"><a class="back-link" href="/?{back_qs}">← 리포트로 돌아가기</a></p>'
        f'{period_nav}{content}'
    )
    return page("메일 목록", body, "dashboard")
