"""
data/app.db(SQLite)에 쌓인 원본 메일을 원하는 기간만큼 쿼리해서 **현재** 카테고리
규칙으로 다시 분류하고, 아티팩트로 게시할 수 있는 HTML 대시보드 조각(fragment)을
만든다. 이 파일 자체는 <html>/<head>/<body> 태그가 없는 상태로 저장되며, Claude의
Artifact 게시 도구가 감싸서 배포한다.

report_*.json/latest.json을 읽던 예전 방식을 완전히 대체한다 — 카테고리 규칙이
바뀌어도 IMAP을 다시 조회할 필요 없이, app.db에 쌓인 과거 메일을 그대로 재분류해서
리포트를 뽑을 수 있다.
"""
import argparse
import html
import re
from datetime import datetime, timedelta

from mail_core.classify import classify

from mail_app import app_paths
from mail_app.config_store import load_categories
from mail_app.mail_log_store import account_type_for, distinct_accounts, latest_action_summary, query_messages
from mail_app.web_style import STYLE_CSS

DATA_DIR = app_paths.data_dir()  # 기본: 저장소의 data/. 데스크톱 앱은 MAIL_AGENT_DATA_DIR.
DB_PATH = app_paths.db_path()
OUT_PATH = app_paths.dashboard_path()

PROVIDER_LABEL = {"gmail": "Gmail", "naver": "Naver", "outlook": "Outlook"}

# 통계 타일/카테고리별 상세 탭에 기본으로 보여줄 카테고리 개수 상한(넘으면 "···"로 접힘).
# "총 메일" 타일은 이 상한과 별개로 항상 먼저 보인다 — 그래서 통계 타일은 최대
# TOP_CATEGORY_CAP + 1(총 메일)개가 보인다.
TOP_CATEGORY_CAP = 9
# 계정 카드 안쪽(미니칩/탭)에 기본으로 보여줄 카테고리 개수 상한 — "총" 칩은 별개로 항상 보임.
ACCOUNT_CHIP_CAP = 4
# 상단 "연결된 계정" 태그 줄에 기본으로 보여줄 계정 개수 상한(그 이상은 "···"로 접힘).
ACCOUNT_TAG_CAP = 8

# 카테고리의 action에 따라 색을 다르게 준다 — "이 메일에 무슨 일이 생길지"를 색으로
# 바로 알 수 있게. trash=경고색, save=강조색, read=포인트색, keep(그대로 둠)=중립.
ACTION_COLOR_CLASS = {"trash": "trash", "save": "accent", "read": "trend", "keep": "muted"}
ACTION_LABEL = {"trash": "휴지통 이동", "save": "보관", "read": "읽음 표시"}


def esc(s: str) -> str:
    return html.escape(s or "")


# messages.status -> (배지 클래스, 배지 라벨). status가 'active'거나 없으면 배지를 안 보여준다.
STATUS_BADGE = {"trashed": ("trashed", "🗑️ 휴지통 이동됨"), "archived": ("archived", "📥 보관됨")}


def render_status_badge(m: dict) -> str:
    """3b: 액션이 이미 처리된 메일임을 알려주는 배지. is_read는 trash/archive와 동시에
    표시될 수 있어(예: 읽고 나서 보관) 별도로 붙인다."""
    badges = []
    css_class, label = STATUS_BADGE.get(m.get("status"), (None, None))
    if css_class:
        badges.append(f'<span class="status-badge {css_class}">{label}</span>')
    if m.get("is_read"):
        badges.append('<span class="status-badge read">✅ 읽음</span>')
    return "".join(badges)


def render_matches(matches: list[dict], limit: int = 8, total_count: int | None = None) -> str:
    if not matches:
        return '<p class="empty">해당 없음</p>'
    if total_count is None:
        total_count = len(matches)
    rows = []
    for m in matches[:limit]:
        subject_html = esc(m["subject"])[:70]
        web_link = m.get("web_link")
        if web_link:
            subject_html = f'<a href="{esc(web_link)}" target="_blank" rel="noopener">{subject_html}</a>'
        rows.append(
            f'<li><span class="subj">{subject_html}</span>'
            f'<span class="sender">{esc(m["sender"])}{render_status_badge(m)}</span></li>'
        )
    more = total_count - limit
    more_html = f'<li class="more">외 {more}건</li>' if more > 0 else ""
    return f'<ul class="mail-list">{"".join(rows)}{more_html}</ul>'


def render_uncategorized_block(count: int, total: int, matches: list[dict]) -> str:
    """어떤 카테고리에도 안 걸린 메일 블록. uncategorized_sample은 최대 20건까지만
    리포트에 저장돼 있어서(용량 때문에), "외 N건" 표시는 실제 count 기준으로 계산한다."""
    pct = (count / total * 100) if total else 0
    return (
        f'<div class="cat-row muted">'
        f'<div class="cat-head">'
        f'<span class="cat-name">미분류'
        f'<span class="cat-action-pill muted">unmatched</span>'
        f'</span>'
        f'<span class="cat-count">{count}건 · {pct:.1f}%</span></div>'
        f'<div class="cat-bar"><div class="cat-bar-fill" style="width:{min(pct,100):.1f}%"></div></div>'
        f'{render_matches(matches, total_count=count)}'
        f'</div>'
    )


def account_anchor_id(user: str) -> str:
    """계정 이메일을 HTML id/URL fragment로 쓸 수 있는 안전한 문자열로 바꾼다
    (계정 태그 클릭 -> 해당 계정 카드로 포커스 이동에 쓰는 앵커)."""
    return "acct-" + re.sub(r"[^a-zA-Z0-9]+", "-", user).strip("-").lower()


def render_capped(
    items: list[str],
    cap: int,
    toggle_id: str,
    extra_class: str,
    more_class: str,
    more_label: str = "···",
) -> str:
    """items(각각 class="..." 속성을 가진 HTML 조각)를 cap개까지는 그대로 보여주고,
    넘는 건 체크박스+레이블로 만든 "···" 토글을 눌러야 펼쳐지게 한다 — JS 없이 CSS
    :checked ~ 형제 선택자만으로 동작(기존 CSS 전용 라디오 탭과 같은 패턴). 넘치는
    항목엔 extra_class를 덧붙여 기본 숨김 처리하고, 체크박스가 checked면 그 형제들이
    다시 보이도록 하는 CSS 규칙은 web_style.py에 있다."""
    if len(items) <= cap:
        return "".join(items)
    visible = items[:cap]
    hidden = [item.replace('class="', f'class="{extra_class} ', 1) for item in items[cap:]]
    return (
        "".join(visible)
        + f'<input type="checkbox" id="{toggle_id}" class="more-toggle">'
        + "".join(hidden)
        + f'<label for="{toggle_id}" class="{more_class}">{more_label}</label>'
    )


def render_tab_group(
    prefix: str,
    tabs: list[tuple[str, str, str]],
    default_index: int = 0,
    nav_cap: int | None = None,
) -> str:
    """CSS 전용(라디오+레이블) 탭 — JS 없이 동작한다. tabs는 (레이블 HTML, 색 클래스, 패널
    HTML) 튜플 리스트. 탭마다 고유 id가 필요해서, 보여줄 패널을 라디오 :checked 상태에 맞춰
    보여주는 CSS 규칙을 탭 개수만큼 그때그때 생성해서 함께 반환한다. default_index는 처음에
    선택돼 있을 탭 — 범위를 벗어나면 0번째로 보정한다. 호출부가 tabs를 건수 내림차순으로
    정렬해서 넘기면(관례) 0번째가 항상 건수가 가장 많은 탭이 된다. nav_cap을 주면 탭 개수가 그걸 넘을 때 nav_cap개까지만 레이블을 보여주고 나머지는
    "···" 토글(render_capped())로 접는다 — 패널 자체는 전부 렌더링되므로, 호출부가
    tabs를 건수 내림차순으로 미리 정렬해두면(관례) 기본 선택 탭은 항상 접히기 전
    영역 안에 있게 된다."""
    if not tabs:
        return '<p class="empty">해당 없음</p>'
    if not (0 <= default_index < len(tabs)):
        default_index = 0

    inputs = []
    nav_items = []
    panels = []
    rules = []
    for i, (label_html, color_class, panel_html) in enumerate(tabs):
        tab_id = f"{prefix}-{i}"
        checked = " checked" if i == default_index else ""
        inputs.append(f'<input type="radio" name="{prefix}" id="{tab_id}" class="tab-input"{checked}>')
        nav_items.append(f'<label for="{tab_id}" class="tab-label {color_class}">{label_html}</label>')
        panels.append(f'<div class="tab-panel" id="{tab_id}-panel">{panel_html}</div>')
        rules.append(
            f'#{tab_id}:checked ~ .tab-panels #{tab_id}-panel {{ display: block; }}'
            f'#{tab_id}:checked ~ .tab-nav label[for="{tab_id}"] {{ background: var(--surface); '
            f'color: var(--text); box-shadow: inset 0 0 0 1px var(--border); }}'
            f'#{tab_id}:focus-visible ~ .tab-nav label[for="{tab_id}"] {{ outline: 2px solid var(--accent); outline-offset: 1px; }}'
        )

    if nav_cap is not None and len(nav_items) > nav_cap:
        nav_html = render_capped(
            nav_items, nav_cap, f"{prefix}-nav-more", "tab-label-extra", "tab-label tab-label-more"
        )
    else:
        nav_html = "".join(nav_items)

    return (
        f'<div class="tabs">'
        f'{"".join(inputs)}'
        f'<div class="tab-nav">{nav_html}</div>'
        f'<div class="tab-panels">{"".join(panels)}</div>'
        f'<style>{"".join(rules)}</style>'
        f'</div>'
    )


def render_account_chip(user: str, data: dict) -> str:
    """포털-이메일-총이메일수 태그. 클릭하면 아래 "계정별 상세"의 해당 계정 카드로
    포커스가 이동한다(같은 id를 향하는 순수 #fragment 링크 — JS 없음, `:target` CSS로
    카드가 자동으로 펼쳐 보이는 것까지 web_style.py에서 처리)."""
    label = PROVIDER_LABEL.get(data.get("type"), data.get("type", ""))
    anchor = account_anchor_id(user)
    return (
        f'<a class="chip" href="#{anchor}">'
        f'<span class="chip-label">{esc(label)} · {esc(user)}</span>'
        f'<span class="chip-value">{data["total"]}</span>'
        f'</a>'
    )


def render_category_block(name: str, info: dict, total: int, matches: list[dict]) -> str:
    count = info["count"]
    action = info.get("action", "keep")
    color_class = ACTION_COLOR_CLASS.get(action, "muted")
    pct = (count / total * 100) if total else 0
    return (
        f'<div class="cat-row {color_class}">'
        f'<div class="cat-head">'
        f'<span class="cat-name">{esc(name)}'
        f'<span class="cat-action-pill {color_class}">{esc(action)}</span>'
        f'</span>'
        f'<span class="cat-count">{count}건 · {pct:.1f}%</span></div>'
        f'<div class="cat-bar"><div class="cat-bar-fill" style="width:{min(pct,100):.1f}%"></div></div>'
        f'{render_matches(matches)}'
        f'</div>'
    )


def render_account_detail(user: str, data: dict, tab_prefix: str) -> str:
    """계정 하나의 상세 카드. 카테고리는 건수 내림차순 · 이름 오름차순으로 정렬해서
    상위 ACCOUNT_CHIP_CAP개까지만 미니칩/탭 레이블로 보여주고 나머지는 "···"로 접는다
    (정적 Artifact 등 실시간 서버가 없는 화면에서도 쓰이므로, 각 카테고리 패널 자체는
    여전히 전부 렌더링돼 있다 — 접힌 레이블을 펼치기만 하면 바로 보인다. admin_app.py의
    라이브 화면은 이 카드 대신 실제 페이지네이션 목록을 쓴다)."""
    label = PROVIDER_LABEL.get(data.get("type"), data.get("type", ""))
    total = data["total"]
    uncategorized_count = data.get("uncategorized_count", len(data.get("uncategorized_sample", [])))

    cat_items = [(name, data["categories"][name]["count"]) for name in data["categories"]]
    if uncategorized_count:
        cat_items.append(("미분류", uncategorized_count))
    cat_items.sort(key=lambda t: (-t[1], t[0]))

    tabs = []
    mini_stat_items = []
    for name, count in cat_items:
        if name == "미분류":
            tabs.append(
                (
                    f'미분류 <span class="tab-count">{count}</span>',
                    "muted",
                    render_uncategorized_block(count, total, data.get("uncategorized_sample", [])),
                )
            )
            mini_stat_items.append(f'<span class="mini-stat muted">미분류 {count}</span>')
            continue
        color_class = ACTION_COLOR_CLASS.get(data["categories"][name]["action"], "muted")
        tabs.append(
            (
                f'{esc(name)} <span class="tab-count">{count}</span>',
                color_class,
                render_category_block(name, data["categories"][name], total, data["category_matches"].get(name, [])),
            )
        )
        mini_stat_items.append(f'<span class="mini-stat {color_class}">{esc(name)} {count}</span>')

    mini_stats = f'<span class="mini-stat">총 {total}</span>' + render_capped(
        mini_stat_items, ACCOUNT_CHIP_CAP, f"{tab_prefix}-mini-more", "mini-stat-extra", "mini-stat mini-stat-more"
    )

    anchor = account_anchor_id(user)
    return (
        f'<details class="account-detail" id="{anchor}">'
        f'<summary>'
        f'<span class="account-name">{esc(label)} · {esc(user)}</span>'
        f'<span class="account-mini-stats">{mini_stats}</span>'
        f'<span class="chevron">▸</span>'
        f'</summary>'
        f'<div class="account-detail-body">'
        f'{render_tab_group(tab_prefix, tabs, 0, ACCOUNT_CHIP_CAP)}'
        f'</div>'
        f'</details>'
    )


def render_action_row(user: str, action: str, data: dict, dry_run: bool) -> str:
    candidates = data.get("candidates", 0)
    done = data.get("done", 0)
    failed = data.get("failed", 0)
    folder = data.get("folder")
    note = data.get("note")
    label = ACTION_LABEL.get(action, action)

    if dry_run:
        status_class = "pending"
        status_text = f"{candidates}건 {label} 예정 (dry-run)"
    elif note:
        status_class = "warn"
        status_text = (
            f"{note} ({candidates}건 유지)" if not done else f"{note} ({done}건 완료, {failed}건 실패)"
        )
    elif done:
        status_class = "done"
        fail_note = f", {failed}건 실패" if failed else ""
        target = f" '{folder}'(으)로" if folder else ""
        status_text = f"{done}건{target} {label} 완료{fail_note}"
    else:
        status_class = "warn"
        status_text = f"{failed}건 실패"

    return (
        f'<div class="action-row">'
        f'<span class="action-account">{esc(user)} · {esc(label)}</span>'
        f'<span class="action-badge {status_class}">{esc(status_text)}</span>'
        f'</div>'
    )


def sorted_accounts(report: dict) -> list[str]:
    """계정 정렬: 포털(제공자) 오름차순 -> 이메일 오름차순. 이메일 자체가 이미 고유
    키라서 "총 이메일수 내림차순"까지 갈 일은 실제로 없지만(동점이 생길 수 없음),
    요청 스펙 표기를 그대로 따라 정렬 키에 포함해둔다. admin_app.py의 라이브 계정
    카드도 이 함수로 같은 순서를 쓴다(계정 태그 줄과 계정 카드 줄이 항상 같은 순서)."""
    per_account = report.get("per_account", {})
    accounts = [p for p in report.get("accounts", []) if p in per_account]
    return sorted(
        accounts,
        key=lambda p: (
            PROVIDER_LABEL.get(per_account[p].get("type"), per_account[p].get("type") or ""),
            p,
            -per_account[p]["total"],
        ),
    )


def sorted_category_items(overall: dict) -> list[tuple[str, int]]:
    """카테고리 관련 위젯(통계 타일/카테고리별 상세 탭)이 공유하는 정렬 — 건수
    내림차순 · 이름 오름차순, 미분류도 같은 풀에 섞여서 정렬된다."""
    uncategorized_count = overall.get("uncategorized_count", len(overall.get("uncategorized_sample", [])))
    items = [(name, overall["categories"][name]["count"]) for name in overall["categories"]]
    if uncategorized_count:
        items.append(("미분류", uncategorized_count))
    items.sort(key=lambda t: (-t[1], t[0]))
    return items


def render_report_header(report: dict, extra_actions_html: str = "") -> str:
    """제목 + (선택) 추가 액션 버튼 + 생성 시각/계정 수. extra_actions_html은
    admin_app.py가 "동기화" 버튼을 끼워 넣을 때 쓴다(정적 Artifact엔 없음)."""
    date = report["report_date"]
    generated_at = report["generated_at"]
    accounts_count = len(sorted_accounts(report))
    return f"""<div class="header">
  <h1>메일 리포트 · {esc(date)}</h1>
  <div class="header-actions">
    {extra_actions_html}
    <div class="meta">생성 {esc(generated_at)} · 계정 {accounts_count}개</div>
  </div>
</div>"""


def render_report_stats(report: dict) -> str:
    """통계 타일 행("총 메일" + 카테고리 상위 TOP_CATEGORY_CAP개) + 계정 태그 행."""
    overall = report["overall"]
    cat_items = sorted_category_items(overall)

    stat_tile_items = []
    for name, count in cat_items:
        if name == "미분류":
            stat_tile_items.append(
                f'<div class="stat-tile muted"><div class="label">미분류</div><div class="value">{count}</div></div>'
            )
            continue
        color_class = ACTION_COLOR_CLASS.get(overall["categories"][name]["action"], "muted")
        stat_tile_items.append(
            f'<div class="stat-tile {color_class}"><div class="label">{esc(name)}</div>'
            f'<div class="value">{count}</div></div>'
        )
    stat_tiles = render_capped(
        stat_tile_items, TOP_CATEGORY_CAP, "stat-more", "stat-tile-extra", "stat-tile stat-tile-more"
    )

    per_account = report.get("per_account", {})
    accounts = sorted_accounts(report)
    account_chip_items = [render_account_chip(p, per_account[p]) for p in accounts]
    account_chips = render_capped(
        account_chip_items, ACCOUNT_TAG_CAP, "acct-tag-more", "chip-extra", "chip chip-more"
    )

    return f"""<div class="stat-row">
  <div class="stat-tile">
    <div class="label">총 메일</div>
    <div class="value">{overall['total']}</div>
  </div>
  {stat_tiles}
</div>

<div class="chip-row">
  {account_chips}
</div>"""


def render_report_actions(report: dict) -> str:
    actions = report.get("actions", {})
    dry_run = actions.get("dry_run", True)
    action_accounts = actions.get("accounts", {})
    action_rows = "".join(
        render_action_row(user, action, data, dry_run)
        for user, per_action in action_accounts.items()
        for action, data in per_action.items()
    )
    if not action_rows:
        action_rows = '<p class="empty">지금은 처리할 메일이 없습니다.</p>'
    action_title = "액션 — 카테고리 자동 처리" + (" (dry-run 미리보기)" if dry_run else "")
    return f"""<div class="section-title">{esc(action_title)}</div>
<div class="action-list">
  {action_rows}
</div>"""


def render_report_categories(report: dict) -> str:
    """"카테고리별 상세" 섹션 — 카테고리 건수 내림차순으로 상위 TOP_CATEGORY_CAP개
    탭 레이블만 기본 노출하고 나머지는 "···"로 접는다(패널 자체는 전부 렌더링됨)."""
    overall = report["overall"]
    cat_items = sorted_category_items(overall)

    category_tabs = []
    for name, count in cat_items:
        if name == "미분류":
            category_tabs.append(
                (
                    f'미분류 <span class="tab-count">{count}</span>',
                    "muted",
                    render_uncategorized_block(count, overall["total"], overall.get("uncategorized_sample", [])),
                )
            )
            continue
        color_class = ACTION_COLOR_CLASS.get(overall["categories"][name]["action"], "muted")
        category_tabs.append(
            (
                f'{esc(name)} <span class="tab-count">{count}</span>',
                color_class,
                render_category_block(
                    name, overall["categories"][name], overall["total"], overall["category_matches"].get(name, [])
                ),
            )
        )
    return (
        f'<div class="section-title">카테고리별 상세</div>'
        f'{render_tab_group("cat-tabs", category_tabs, 0, TOP_CATEGORY_CAP)}'
    )


def render_report_account_details(report: dict) -> str:
    """"계정별 상세" 섹션의 정적(비-라이브) 버전 — 계정마다 상위 4개 카테고리
    미니칩/탭 + 작은 샘플 목록만 보여준다(page_size 제한, DB 실시간 페이지네이션
    없음). Artifact/CLI 산출물과 admin_app.py 둘 다 기본값으로 이걸 쓴다."""
    per_account = report.get("per_account", {})
    accounts = sorted_accounts(report)
    account_details = "".join(
        render_account_detail(p, per_account[p], f"acct-{i}-tabs") for i, p in enumerate(accounts)
    )
    return (
        f'<div class="section-title">계정별 상세</div>'
        f'<div class="account-list">{account_details}</div>'
    )


def render_report(report: dict) -> str:
    """리포트 본문(헤더/통계/액션 현황/카테고리·계정별 상세)만 반환한다 — <title>/<style>
    없음. admin_app.py가 기간 탭 화면 안에 인라인으로 끼워 넣을 때 쓴다(그 페이지가 이미
    web_style.STYLE_CSS를 한 번 포함하고 있으므로 <style>을 중복으로 넣지 않기 위함).
    각 섹션을 개별 함수(render_report_header/stats/actions/categories/account_details)로
    쪼개둔 걸 그대로 이어붙인 것 — admin_app.py는 "계정별 상세"만 실시간 페이지네이션
    버전으로 바꿔치기해서 쓰고 나머지는 이 함수와 똑같이 재사용한다."""
    return (
        render_report_header(report)
        + render_report_stats(report)
        + render_report_actions(report)
        + render_report_categories(report)
        + render_report_account_details(report)
    )


def build(report: dict) -> str:
    """Claude Artifact로 독립 게시하는 정적 fragment용 — <title>/<style>을 포함한
    자기 완결적인 조각을 반환한다(Claude의 Artifact 게시 도구가 <head>/<body>로 감싼다).
    admin_app.py처럼 이미 web_style.STYLE_CSS를 포함한 페이지 안에 인라인으로 끼워
    넣을 때는 render_report()를 직접 쓴다(<style> 중복 방지)."""
    date = report["report_date"]
    return f"<title>메일 리포트 {esc(date)}</title>\n<style>{STYLE_CSS}</style>\n{render_report(report)}"


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="YYYY-MM-DD. 이 날짜 0시부터의 메일로 리포트를 만든다 (기본값: 어제, fetch_mail.py 기본 조회 범위와 동일).",
    )
    parser.add_argument(
        "--until",
        type=str,
        default=None,
        help="YYYY-MM-DD. 이 날짜 0시 이전(미만)까지만 포함한다 (기본값: 지금까지 전부).",
    )
    return parser.parse_args()


def parse_date_arg(value: str, flag: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise SystemExit(f"{flag} 형식이 올바르지 않습니다 (YYYY-MM-DD): {value}")


def build_report(since: datetime, until: datetime | None) -> dict:
    if not DB_PATH.exists():
        raise SystemExit(f"DB가 없습니다: {DB_PATH} (fetch_mail.py 먼저 실행)")

    categories = load_categories(DB_PATH)
    if not categories:
        raise SystemExit(f"카테고리가 하나도 없습니다: {DB_PATH}")

    accounts = sorted(distinct_accounts(DB_PATH, since, until))
    if not accounts:
        raise SystemExit("해당 기간에 저장된 메일이 없습니다.")

    per_account: dict[str, dict] = {}
    all_messages: list[dict] = []
    for user in accounts:
        msgs = query_messages(DB_PATH, since, until, account=user)
        all_messages.extend(msgs)
        classified = classify(msgs, categories)
        classified["type"] = account_type_for(DB_PATH, user)
        per_account[user] = classified

    overall = classify(all_messages, categories)

    if until is not None:
        report_date = f"{since:%Y-%m-%d} ~ {(until - timedelta(days=1)):%Y-%m-%d}"
    else:
        report_date = f"{since:%Y-%m-%d} ~ 지금"

    return {
        "overall": overall,
        "report_date": report_date,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "accounts": accounts,
        "per_account": per_account,
        "actions": latest_action_summary(DB_PATH),
    }


def main():
    args = parse_args()
    since = parse_date_arg(args.since, "--since") if args.since else datetime.now() - timedelta(days=1)
    until = parse_date_arg(args.until, "--until") if args.until else None

    report = build_report(since, until)
    html_out = build(report)
    OUT_PATH.write_text(html_out, encoding="utf-8")
    print(f"대시보드 생성 완료: {OUT_PATH}")


if __name__ == "__main__":
    main()
