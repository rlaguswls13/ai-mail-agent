"""여러 화면이 공유하는 메일-목록 조회/렌더 + 파이프라인 실행 레이어.

`/list`, `/tasks`, `/vault`(그리고 대시보드 전체 탭)가 똑같이 쓰는:
 - 페이지 파라미터 파싱, 계정/카테고리 필터 <select>, 카테고리 필터링
 - 메일 테이블(msg_table) + 페이지네이션
 - load_all_messages: 계정 필터 + classify 를 붙인 메시지 로딩
 - run_pipeline: fetch_mail -> generate_html 자식 프로세스 실행 (/sync·/tasks/run·/tasks/apply)

블루프린트 분리 중이라 이 공용 로직을 한 곳에 모은다. `_shared` 커널만 의존하고
admin_app / 블루프린트는 import 하지 않는다(순환 방지).
"""
import subprocess
import sys
from datetime import datetime

from mail_core.actions import find_archive_folder, find_trash_folder
from mail_core.classify import classify
from mail_core.accounts import load_accounts
from mail_app import config_store, generate_html
from mail_app.mail_log_store import account_type_for, distinct_accounts, query_messages

from admin_ui._shared import (
    ACCOUNTS_PATH,
    DB_PATH,
    PROVIDER_LABEL,
    RUN_TIMEOUT_SECONDS,
    esc,
)

MSG_PAGE_SIZE_DEFAULT = 50
MSG_PAGE_SIZE_MIN = 10
MSG_PAGE_SIZE_MAX = 100
MSG_DEFAULT_SINCE_DAYS = 730  # /list의 "전체"·작업 실행 화면이 쓰는 고정 기간(최근 2년).

# action 이름 -> 대상 폴더를 찾는 함수. fetch_mail.py의 ACTION_FOLDER_FINDERS와 동일한 개념을
# "선택한 메일만" 처리하는 수동 액션에도 재사용한다. "read"는 폴더 이동이 아니라서 여기 없음.
MSG_ACTION_FOLDER_FINDERS = {"trash": find_trash_folder, "save": find_archive_folder}
# action 이름 -> messages.status에 기록할 값 (fetch_mail.py의 ACTION_STATUS와 동일한 개념).
MSG_ACTION_STATUS = {"trash": "trashed", "save": "archived"}
MSG_ACTION_PILL_CLASS = {"trash": "trash", "save": "save", "read": "read"}


def run_pipeline(apply: bool) -> dict:
    """`python -m mail_app.fetch_mail`(dry-run 또는 --apply) -> `-m mail_app.generate_html`
    순서로 동기 실행한다.

    자식 프로세스로 띄우는 이유는 이 두 CLI가 자기 완결적인 진입점으로 설계돼 있어서 -
    여기서 함수를 직접 import해서 부르는 것보다 실제 명령줄 실행과 동일한 경로를 타는 게
    더 안전하고, 스케줄러(run_daily.bat)가 실행하는 것과도 같은 코드 경로가 된다.
    generate_html 실행은 data/dashboard.html(Artifact 게시용 정적 파일)을 최신 상태로
    유지하기 위한 것 - 화면 자체는 build_report()를 직접 호출해서 그리므로 이 결과를
    기다릴 필요는 없지만, 부수효과로 계속 최신화해둔다.

    같은 인터프리터(sys.executable)로 실행하고 env(PYTHONPATH 등)를 그대로 물려주므로,
    이 프로세스에서 mail_app 을 import 할 수 있으면 자식도 할 수 있다. 데이터 경로는
    app_paths(MAIL_AGENT_DATA_DIR / 패키지 위치 기준)로 해석되어 cwd 와 무관하다.
    """
    python_exe = sys.executable
    fetch_args = [python_exe, "-m", "mail_app.fetch_mail"]
    if apply:
        fetch_args.append("--apply")

    result = {"ran_at": datetime.now().isoformat(timespec="seconds"), "apply": apply}
    try:
        fetch_proc = subprocess.run(
            fetch_args, capture_output=True, text=True, timeout=RUN_TIMEOUT_SECONDS
        )
        result["fetch_ok"] = fetch_proc.returncode == 0
        result["fetch_output"] = (fetch_proc.stdout + fetch_proc.stderr).strip()
    except subprocess.TimeoutExpired:
        result["fetch_ok"] = False
        result["fetch_output"] = f"{RUN_TIMEOUT_SECONDS}초 넘게 걸려서 중단했습니다."
        result["generate_ok"] = False
        result["generate_output"] = "fetch가 실패해서 건너뜀."
        return result

    try:
        gen_proc = subprocess.run(
            [python_exe, "-m", "mail_app.generate_html"],
            capture_output=True, text=True, timeout=RUN_TIMEOUT_SECONDS,
        )
        result["generate_ok"] = gen_proc.returncode == 0
        result["generate_output"] = (gen_proc.stdout + gen_proc.stderr).strip()
    except subprocess.TimeoutExpired:
        result["generate_ok"] = False
        result["generate_output"] = f"{RUN_TIMEOUT_SECONDS}초 넘게 걸려서 중단했습니다."
    return result


def run_status_html(run: dict | None) -> str:
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


def _parse_page_size(raw: str | None) -> int:
    """?page_size= 를 [MIN, MAX] 로 클램프한다. /list·/tasks·/vault 가 공유."""
    try:
        n = int(raw) if raw is not None else MSG_PAGE_SIZE_DEFAULT
    except (TypeError, ValueError):
        n = MSG_PAGE_SIZE_DEFAULT
    return max(MSG_PAGE_SIZE_MIN, min(MSG_PAGE_SIZE_MAX, n))


def _parse_page_num(raw: str | None) -> int:
    """?page= 를 1 이상 정수로. 형식이 틀리면 1."""
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1


def _account_options(users: list[str], account_type_by_user: dict[str, str], selected: str | None) -> str:
    """계정 필터 <select> 옵션. /list·/tasks·/vault 필터폼이 공유(모양 동일)."""
    return '<option value="">전체 계정</option>' + "".join(
        f'<option value="{esc(u)}"{" selected" if u == selected else ""}>'
        f'{esc(PROVIDER_LABEL.get(account_type_by_user.get(u, ""), ""))} · {esc(u)}</option>'
        for u in users
    )


def _category_options(categories: dict, selected: str | None) -> str:
    """카테고리 필터 <select> 옵션(맨 위에 "미분류" 특수값 __uncategorized__ 포함)."""
    return (
        '<option value="">전체 카테고리</option>'
        '<option value="__uncategorized__"'
        + (" selected" if selected == "__uncategorized__" else "")
        + ">미분류</option>"
        + "".join(
            f'<option value="{esc(name)}"{" selected" if name == selected else ""}>{esc(name)}</option>'
            for name in categories
        )
    )


def _filter_by_category(messages: list[dict], category_filter: str | None) -> list[dict]:
    """_category 가 붙은 메시지 리스트를 카테고리 필터로 거른다.
    "__uncategorized__" 는 미분류만, 빈 값은 전체."""
    if category_filter == "__uncategorized__":
        return [m for m in messages if m["_category"] is None]
    if category_filter:
        return [m for m in messages if m["_category"] == category_filter]
    return list(messages)


def status_badges_html(m: dict) -> str:
    return generate_html.render_status_badge(m)


def msg_date_cell(iso: str) -> str:
    """message_date(ISO)를 날짜/시각 2줄로. 값이 없으면 빈 셀."""
    raw = (iso or "")[:16].replace("T", " ")
    if not raw:
        return '<td class="msg-date"></td>'
    date_part, _, time_part = raw.partition(" ")
    return (
        f'<td class="msg-date"><span class="d-date">{esc(date_part)}</span>'
        f'<span class="d-time">{esc(time_part)}</span></td>'
    )


def msg_table_row(m: dict, categories: dict, *, with_account: bool, with_select: bool = False) -> str:
    """메일 목록 테이블 한 행. 제목은 링크가 있으면 굵게(클릭 가능 표시), 상태는 별도 칸.

    with_select=True면 맨 앞에 선택 체크박스 칸을 붙인다(/tasks·/vault 대량 선택).
    체크박스 값(data-key)은 `account::uid` - 폼 제출/localStorage 키로 함께 쓴다.
    """
    cat_name = m.get("_category")
    cat_action = categories.get(cat_name, {}).get("action", "keep") if cat_name else "keep"
    pill_class = MSG_ACTION_PILL_CLASS.get(cat_action, "")
    subject = esc((m["subject"] or "")[:120])  # 자른 뒤 escape - 반대로 하면 &amp; 가 &am 으로 잘림
    web_link = m.get("web_link")
    if web_link:
        subject = f'<a href="{esc(web_link)}" target="_blank" rel="noopener">{subject}</a>'
    select_cell = ""
    if with_select:
        key = f'{m["account"]}::{m["uid"]}'
        # name/value/form="vault-form" 은 /vault의 폼 제출용(선택→되돌리기/영구삭제).
        # /tasks 에는 vault-form 이 없어 무해하게 무시되고, 그쪽은 JS가 data-key +
        # localStorage 로 페이지 간 선택을 관리한다.
        select_cell = (
            f'<td class="msg-select">'
            f'<input type="checkbox" class="sel-box" data-key="{esc(key)}" '
            f'name="sel" value="{esc(key)}" form="vault-form" aria-label="이 메일 선택"></td>'
        )
    account_cell = ""
    if with_account:
        provider = PROVIDER_LABEL.get(m.get("account_type"), m.get("account_type") or "")
        # 제공자 라벨(윗줄) + 전체 이메일(아랫줄, 작게) 2줄 - 한 줄이면 좁은 c-account
        # 칸에서 이메일이 말줄임으로 잘려 계정 구분이 안 됐다(같은 제공자 다계정).
        account_cell = (
            f'<td class="msg-account"><span class="a-provider">{esc(provider)}</span>'
            f'<span class="a-email">{esc(m["account"])}</span></td>'
        )
    status_html = status_badges_html(m) or '<span class="st-none">-</span>'
    cat_label = esc(cat_name or "미분류")
    return (
        f"<tr>"
        f"{select_cell}"
        f"{msg_date_cell(m.get('message_date'))}"
        f"{account_cell}"
        f'<td class="msg-cat"><span class="pill {pill_class}">{cat_label}</span></td>'
        f'<td class="msg-subj"><span class="subj">{subject}</span></td>'
        f'<td class="msg-status">{status_html}</td>'
        f'<td class="msg-sender">{esc(m["sender"])}</td>'
        f"</tr>"
    )


def msg_table(
    page_items: list[dict], categories: dict, *, with_account: bool, with_select: bool = False
) -> str:
    """<table class="msg-table"> 전체. 열 너비는 colgroup으로 고정(table-layout: fixed)해서
    데스크톱에선 제목/발신인/계정이 말줄임(…)으로 잘린다. 좁은 화면(테이블 min-width 미만)
    에선 .table-scroll 래퍼 안에서 테이블만 가로 스크롤 - 페이지 본문은 안 넘친다.

    with_select=True면 맨 앞에 체크박스 열을 추가한다(/tasks·/vault 대량 선택)."""
    if not page_items:
        return '<p class="empty">해당 조건의 메일이 없습니다. 검색어나 계정·카테고리 필터를 바꿔보세요.</p>'
    sel_col = '<col class="c-select">' if with_select else ""
    sel_head = '<th aria-label="선택"></th>' if with_select else ""
    if with_account:
        cols = (
            f'{sel_col}<col class="c-date"><col class="c-account"><col class="c-cat">'
            '<col class="c-subj"><col class="c-status"><col class="c-sender">'
        )
        head = f"{sel_head}<th>날짜</th><th>계정</th><th>카테고리</th><th>제목</th><th>상태</th><th>발신인</th>"
        table_cls = "msg-table msg-table--wide"
    else:
        cols = f'{sel_col}<col class="c-date"><col class="c-cat"><col class="c-subj"><col class="c-status"><col class="c-sender">'
        head = f"{sel_head}<th>날짜</th><th>카테고리</th><th>제목</th><th>상태</th><th>발신인</th>"
        table_cls = "msg-table"
    if with_select:
        table_cls += " msg-table--select"
    rows = "".join(
        msg_table_row(m, categories, with_account=with_account, with_select=with_select)
        for m in page_items
    )
    return (
        f'<div class="table-scroll"><table class="{table_cls}"><colgroup>{cols}</colgroup>'
        f"<thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table></div>"
    )


def load_all_messages(since, until, account_filter: str | None, status: str | None = None):
    """계정 필터를 적용해 메시지를 모으고 카테고리 분류를 붙여서 반환한다.
    (/의 전체 탭과 /tasks, /vault가 공유하는 로직.)

    status를 넘기면 그 status인 행만 조회한다 - /vault(보관함=archived / 휴지통=trashed).
    """
    accounts_cfg = load_accounts(ACCOUNTS_PATH)
    account_type_by_user = {a["user"]: a["type"] for a in accounts_cfg}
    all_users = sorted(account_type_by_user) or sorted(distinct_accounts(DB_PATH, since, until))
    target_users = [account_filter] if account_filter else all_users

    all_messages: list[dict] = []
    for user in target_users:
        msgs = query_messages(DB_PATH, since, until, account=user, status=status)
        atype = account_type_by_user.get(user) or account_type_for(DB_PATH, user)
        for m in msgs:
            m["account_type"] = atype
        all_messages.extend(msgs)

    categories = config_store.load_categories(DB_PATH)
    classified = classify(all_messages, categories, sample_cap=None)
    category_of: dict[tuple[str, str], str] = {}
    for name, matches in classified["category_matches"].items():
        for m in matches:
            category_of[(m["account"], m["uid"])] = name
    for m in all_messages:
        m["_category"] = category_of.get((m["account"], m["uid"]))

    return all_messages, all_users, account_type_by_user, categories


def render_pagination(prev_link: str, next_link: str, page: int, total_pages: int, total: int) -> str:
    """목록 하단 페이지네이션 한 줄. prev_link/next_link 는 이미 완성된 <a>/<span> 문자열.

    목록 위·아래에 두 번 찍던 걸 아래 한 번으로 줄였다(사용자 요청) - 위쪽 건 필터 폼
    바로 아래라 오히려 시선을 흐렸다."""
    return (
        f'<div class="pagination">{prev_link}'
        f'<span class="page-label">{page} / {total_pages} 페이지 · 총 {total}건</span>'
        f'{next_link}</div>'
    )
