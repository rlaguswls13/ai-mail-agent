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
    pool.sort(key=lambda m: m.get("message_date") or "", reverse=True)

    total = len(pool)
    total_pages = max(1, math.ceil(total / ACCOUNT_LIST_PAGE_SIZE))
    page_num = min(max(1, page_num), total_pages)
    start = (page_num - 1) * ACCOUNT_LIST_PAGE_SIZE
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
        user, account_type, since, until, categories, selected_cat, page_num, base_qs_params, anchor
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


@bp.route("/")
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
