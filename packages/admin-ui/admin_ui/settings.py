"""admin_ui `/settings` 블루프린트 - 카테고리 action/priority 조정 + 메일 계정 CRUD.

카테고리는 이름·설명·키워드가 코드(개발자가 주기적으로 배포하는 패치)로 관리되고,
이 화면에서는 기존 카테고리의 action/priority만 바꿀 수 있다(추가/삭제/이름·키워드
수정 불가 - 개인정보가 안 들어가는 규칙이라 화면 CRUD보다 코드 배포가 정본).
인라인 편집 UI + 직접 URL 접근용 폴백 페이지(/settings/categories/<name>/edit,
/settings/accounts/{new,<user>/edit}). 계정은 config/accounts.yaml (또는 데스크톱 앱의
암호화 볼트 - env_mode()면 이 화면에선 읽기 전용), 카테고리는 data/app.db.
"""
from urllib.parse import quote

from flask import Blueprint, redirect, request, url_for

from mail_core.accounts import (
    IMAP_SERVERS,
    account_auth,
    add_account,
    delete_account,
    env_mode,
    load_accounts,
    update_account,
)
from mail_app import config_store, generate_html

from admin_ui._shared import (
    ACCOUNTS_PATH,
    ACTIONS,
    DB_PATH,
    JSON_EXPORT_PATH,
    PRIORITIES,
    PROVIDER_LABEL,
    account_label,
    esc,
    kw_to_text,
    page,
)

bp = Blueprint("settings", __name__)


def _delete_form(delete_url: str, confirm_msg: str) -> str:
    # 삭제 폼은 반드시 수정 폼 바깥의 형제 요소여야 한다 - <form> 안에 <form>을 중첩하면
    # 브라우저가 중첩을 무시하고 안쪽 폼 제출을 바깥 폼으로 흡수해버려서, 예전엔 "삭제"를
    # 눌러도 실제로는 수정(업데이트) 폼이 제출되는 버그가 있었다.
    return (
        f'<form class="cfg-delete" method="post" action="{delete_url}" '
        f"onsubmit=\"return confirm('{esc(confirm_msg)}')\">"
        f'<button class="btn danger" type="submit">삭제</button></form>'
    )


def category_fields(action_url: str, category: dict) -> str:
    """카테고리 상세 - 이름/설명/키워드는 읽기 전용(코드 배포로만 관리), action/priority만
    수정 폼으로 제출 가능. /settings 인라인 편집과 /settings/categories/<name>/edit
    폴백 페이지가 함께 쓴다."""
    category = dict(category)
    category.setdefault("domains", [])
    action_options = "".join(
        f'<option value="{a}"{" selected" if a == category["action"] else ""}>{a}</option>' for a in ACTIONS
    )
    priority_options = "".join(
        f'<option value="{p}"{" selected" if p == category["priority"] else ""}>{p}</option>' for p in PRIORITIES
    )
    readonly = f"""
    <div class="cfg-form cfg-readonly">
      <div class="row">
        <label>이름 (규칙 키)<input type="text" value="{esc(category['name'])}" disabled></label>
        <label>description<input type="text" value="{esc(category['description'])}" disabled></label>
      </div>
      <label>keywords.senders - 완전 일치하는 전체 이메일 주소
        <textarea disabled>{esc(kw_to_text(category['senders']))}</textarea></label>
      <label>keywords.domains - 발신 도메인(서브도메인 포함)
        <textarea disabled>{esc(kw_to_text(category['domains']))}</textarea></label>
      <label>keywords.title - 제목 부분 문자열
        <textarea disabled>{esc(kw_to_text(category['title']))}</textarea></label>
      <label>keywords.contents - 본문 부분 문자열
        <textarea disabled>{esc(kw_to_text(category['contents']))}</textarea></label>
      <p class="hint">이름·설명·키워드는 이 화면에서 바꿀 수 없습니다 - 개발자가 주기적으로
        배포하는 규칙 업데이트로만 바뀝니다.</p>
    </div>
    """
    form = f"""
    <form class="cfg-form" method="post" action="{action_url}">
      <div class="row">
        <label>action<select name="action">{action_options}</select>
          <span class="hint">trash=휴지통 / save=보관 / read=읽음 / keep=그대로</span>
        </label>
        <label>priority<select name="priority">{priority_options}</select>
          <span class="hint">동률이면 먼저 만든 쪽 우선</span>
        </label>
      </div>
      <div class="actions-row"><button class="btn" type="submit">저장</button></div>
    </form>
    """
    return readonly + form


def account_fields(action_url: str, submit_label: str, account: dict | None, delete_url: str | None) -> str:
    account = account or {"type": "gmail", "user": "", "password": "", "alias": ""}
    cur_auth = account_auth(account)
    type_options = "".join(
        f'<option value="{t}"{" selected" if t == account["type"] else ""}>{PROVIDER_LABEL.get(t, t)}</option>'
        for t in IMAP_SERVERS
    )
    auth_options = "".join(
        f'<option value="{v}"{" selected" if v == cur_auth else ""}>{label}</option>'
        for v, label in (("password", "앱 비밀번호"), ("xoauth2", "OAuth (브라우저 로그인)"))
    )
    form = f"""
    <form class="cfg-form" method="post" action="{action_url}">
      <div class="row">
        <label>메일 종류<select name="type">{type_options}</select></label>
        <label><span>이메일 주소 <b class="req">*</b></span><input type="email" name="user" value="{esc(account['user'])}" autocomplete="username" required></label>
      </div>
      <label>별칭 (표시 이름)
        <span class="hint">비우면 화면에 Gmail/Naver/Outlook로 표시 - 같은 제공자를 여러 개 등록했을 때 구분용</span>
        <input type="text" name="alias" value="{esc(account.get('alias', ''))}" placeholder="예: Gmail-업무용"></label>
      <label>인증 방식
        <span class="hint">Gmail 만 선택 가능 · Outlook 은 OAuth 고정, Naver/Daum 은 앱 비밀번호 고정 (저장 시 자동 보정)</span>
        <select name="auth">{auth_options}</select></label>
      <label><span>앱 비밀번호</span>
        <span class="hint">인증이 "앱 비밀번호"일 때만 필요 · OAuth 는 CLI <code>python -m mail_app.oauth_login</code> 로 로그인 · 저장 시 config/accounts.yaml에 암호화되어 저장</span>
        <input type="password" name="password" value="{esc(account.get('password', ''))}" autocomplete="off"></label>
      <div class="actions-row"><button class="btn" type="submit">{submit_label}</button></div>
    </form>
    """
    if delete_url:
        form += _delete_form(delete_url, f"{account['user']} 계정을 삭제할까요?")
    return form


def _cfg_page(submit_label: str, fields_html: str, hint: str) -> str:
    """인라인 편집을 못 쓰는 경우(직접 URL 접근)용 폴백 페이지."""
    body = (
        f'<p class="nav"><a class="back-link" href="/settings">← 설정</a></p>'
        f'<h1 class="page-title">{esc(submit_label)}</h1>'
        f'<p class="sub">{hint}</p>'
        f'<div class="card">{fields_html}</div>'
    )
    return page(submit_label, body, "settings")


def category_form(action_url: str, category: dict) -> str:
    return _cfg_page(
        f"'{category['name']}' 수정",
        category_fields(action_url, category),
        "이름·설명·키워드는 읽기 전용입니다 - 여기서는 action/priority만 바꿀 수 있습니다.",
    )


def account_form(action_url: str, submit_label: str, account: dict | None, delete_url: str | None) -> str:
    return _cfg_page(
        submit_label,
        account_fields(action_url, submit_label, account, delete_url),
        "앱 비밀번호 사용을 권장합니다. config/accounts.yaml에 암호화되어 저장됩니다 - 로컬 전용 도구.",
    )


def _err_banner(msg: str) -> str:
    return f'<div class="form-error" role="alert" tabindex="-1">{esc(msg)}</div>'


def _form_to_account(form) -> dict:
    return {
        "type": form.get("type") or "gmail",
        "user": (form.get("user") or "").strip(),
        "password": form.get("password") or "",
        "alias": (form.get("alias") or "").strip(),
    }


def _md_row(href: str, active: bool, inner: str, *, dashed: bool = False, chevron: bool = True) -> str:
    """좌측 목록 한 줄 - 클릭하면 우측 상세 패널이 이 항목으로 바뀐다(?edit_cat=/?edit_acc=).
    끝의 ▸는 예전 아코디언(cfg-item summary)의 펼침 표시를 그대로 가져온 것 - 지금은
    펼침이 아니라 "우측에서 보기/편집" 표시로 의미만 바뀌었다."""
    cls = "md-row" + (" active" if active else "") + (" md-add" if dashed else "")
    tail = '<span class="cfg-chevron">▸</span>' if chevron else ""
    return f'<a class="{cls}" href="{href}">{inner}{tail}</a>'


def _category_section(error: dict | None = None) -> str:
    categories = config_store.list_categories(DB_PATH)
    selected = request.args.get("edit_cat", "").strip()
    banner = _err_banner(error["msg"]) if error else ""

    # 좌측 목록에서 아무 것도 선택 안 돼있으면(첫 진입) 첫 카테고리를 기본 선택.
    current = next((c for c in categories if c["name"] == selected), categories[0] if categories else None)

    def _counts_tag(c: dict) -> str:
        return (
            f'<span class="cfg-tag cfg-counts" title="senders / domains / title / contents">'
            f'{len(c["senders"])} / {len(c.get("domains", []))} / {len(c["title"])} / {len(c["contents"])}</span>'
        )

    rows = []
    for c in categories:
        active = current is not None and c["name"] == current["name"]
        rows.append(_md_row(
            f'/settings?tab=0&edit_cat={quote(c["name"], safe="")}',
            active,
            f'<span class="cfg-main"><span class="cfg-name">{esc(c["name"])}</span>'
            f'<span class="cfg-desc">{esc(c["description"])}</span></span>'
            f'<span class="pill {c["action"]}">{c["action"]}</span>'
            f'<span class="cfg-tag">{c["priority"]}</span>'
            f'{_counts_tag(c)}',
        ))

    if current is None:
        detail = '<p class="empty">카테고리가 없습니다 - 개발자 배포로 추가됩니다.</p>'
    else:
        cat_name = current["name"]
        # 상세 패널 위에 왼쪽 목록 행과 같은 요약 줄(이름·설명·action·priority·건수)을
        # 반복해서 - 지금 뭘 고쳤는지 스크롤 없이 바로 보이게.
        summary_bar = (
            f'<div class="settings-detail-summary">'
            f'<span class="cfg-main"><span class="cfg-name">{esc(cat_name)}</span>'
            f'<span class="cfg-desc">{esc(current["description"])}</span></span>'
            f'<span class="pill {current["action"]}">{current["action"]}</span>'
            f'<span class="cfg-tag">{current["priority"]}</span>'
            f'{_counts_tag(current)}'
            f'</div>'
        )
        detail = summary_bar + category_fields(f"/settings/categories/{cat_name}", current)

    return (
        f'{banner}'
        f'<p class="sub">카테고리 이름·설명·키워드는 개발자가 주기적으로 배포하는 규칙'
        f' 업데이트로만 바뀝니다(개인정보 없음). 여기서는 카테고리별 action/priority만'
        f' 조정할 수 있습니다.</p>'
        f'<div class="cfg-toolbar">'
        f'<form method="post" action="/settings/categories/export" style="margin:0">'
        f'<button class="btn secondary" type="submit">categories.json으로 내보내기</button></form>'
        f'</div>'
        f'<div class="settings-split">'
        f'<div class="settings-list">{"".join(rows)}</div>'
        f'<div class="settings-detail">{detail}</div>'
        f'</div>'
    )


def _accounts_section(error: dict | None = None) -> str:
    accounts_list = load_accounts(ACCOUNTS_PATH)
    if env_mode():
        rows = "".join(
            f'<div class="md-row" style="cursor:default">'
            f'<span class="cfg-main"><span class="cfg-name">{esc(account_label(a))}</span>'
            f'<span class="cfg-desc">{esc(a["user"])}</span></span></div>'
            for a in accounts_list
        )
        return (
            '<p class="sub">계정은 데스크톱 앱의 <strong>계정 설정</strong> 창에서 관리됩니다 '
            '(자격증명은 암호화 볼트에 저장). 여기서는 편집할 수 없습니다.</p>'
            f'<div class="settings-list">{rows}</div>'
            if accounts_list else '<p class="empty">연결된 메일 계정이 없습니다.</p>'
        )

    selected = request.args.get("edit_acc", "").strip()
    add_prefill = _form_to_account(error["form"]) if error and error.get("form") else None
    banner = _err_banner(error["msg"]) if error else ""

    is_new = selected == "__new__" or (not selected and not accounts_list and not add_prefill)
    current = None if is_new else next((a for a in accounts_list if a["user"] == selected), accounts_list[0] if accounts_list else None)
    if error and add_prefill:
        current, is_new = None, True

    rows = [_md_row("/settings?tab=1&edit_acc=__new__", is_new, "+ 계정 추가", dashed=True, chevron=False)]
    for a in accounts_list:
        active = current is not None and a["user"] == current["user"]
        rows.append(_md_row(
            f'/settings?tab=1&edit_acc={quote(a["user"], safe="")}',
            active,
            f'<span class="cfg-main"><span class="cfg-name">{esc(account_label(a))}</span>'
            f'<span class="cfg-desc">{esc(a["user"])}</span></span>',
        ))

    if is_new:
        detail = f'<h2 class="section-title" style="margin-top:0">계정 추가</h2>{account_fields("/settings/accounts", "추가", add_prefill, None)}'
    else:
        u_path = quote(current["user"], safe="")
        detail = (
            f'<h2 class="section-title" style="margin-top:0">\'{esc(account_label(current))}\' 수정</h2>'
            f'{account_fields(f"/settings/accounts/{u_path}", "저장", current, f"/settings/accounts/{u_path}/delete")}'
        )

    return (
        f'{banner}'
        f'<div class="settings-split">'
        f'<div class="settings-list">{"".join(rows)}</div>'
        f'<div class="settings-detail">{detail}</div>'
        f'</div>'
    )


@bp.route("/settings")
def settings_page(cat_error: dict | None = None, acc_error: dict | None = None):
    try:
        tab_from_qs = int(request.args.get("tab", ""))
    except (TypeError, ValueError):
        tab_from_qs = None
    default_index = 1 if acc_error else (0 if cat_error else tab_from_qs)
    tabs = generate_html.render_tab_group(
        "settings",
        [
            ("카테고리", "", _category_section(cat_error)),
            ("메일 계정", "", _accounts_section(acc_error)),
        ],
        default_index if default_index is not None else 0,
    )
    body = f"""
    <h1 class="page-title">⚙️ 설정</h1>
    <p class="sub">왼쪽 목록에서 항목을 고르면 오른쪽에 수정 화면이 뜹니다. 저장하면 다음
    파이프라인 실행부터 바로 반영됩니다 (data/app.db, config/accounts.yaml).</p>
    {tabs}
    """
    return page("설정", body, "settings")


def _desktop_accounts_page():
    return page(
        "계정 편집 불가",
        "<p class='empty'>이 앱은 자격증명을 암호화 볼트에서 읽고 있습니다. "
        "계정 추가/수정/삭제는 데스크톱 앱의 <strong>계정 설정</strong> 창에서 하세요. "
        "<a href='/settings'>← 설정</a></p>",
        "settings",
    ), 403


@bp.route("/settings/categories/<name>/edit", methods=["GET"])
def edit_category_form(name: str):
    category = config_store.get_category(DB_PATH, name)
    if not category:
        return page("찾을 수 없음", "<p class='empty'>그런 카테고리가 없습니다. <a href='/settings'>설정으로</a></p>", "settings"), 404
    return category_form(f"/settings/categories/{name}", category)


@bp.route("/settings/categories/<name>", methods=["POST"])
def update_category(name: str):
    """카테고리 이름·설명·키워드는 코드 배포로만 바뀐다 - 여기선 action/priority만 반영."""
    existing = config_store.get_category(DB_PATH, name)
    if not existing:
        return settings_page(cat_error={"msg": f"'{name}' 카테고리를 찾을 수 없습니다.", "form": None}), 404
    action = request.form.get("action", "")
    priority = request.form.get("priority", "")
    if action not in ACTIONS or priority not in PRIORITIES:
        return settings_page(cat_error={"msg": "action/priority 값이 올바르지 않습니다.", "form": None}), 400
    config_store.update_category(
        DB_PATH,
        name=name,
        description=existing["description"],
        action=action,
        priority=priority,
        senders=existing["senders"],
        title=existing["title"],
        contents=existing["contents"],
        domains=existing.get("domains", []),
    )
    return redirect(url_for("settings.settings_page", tab=0, edit_cat=name))


@bp.route("/settings/categories/export", methods=["POST"])
def export_json():
    config_store.export_to_json(DB_PATH, JSON_EXPORT_PATH)
    return redirect(url_for("settings.settings_page"))


@bp.route("/settings/accounts/new", methods=["GET"])
def new_account_form():
    if env_mode():
        return _desktop_accounts_page()
    return account_form("/settings/accounts", "계정 추가", None, None)


def _resolve_auth(typ: str, form_auth: str) -> str:
    """폼 입력 + 타입에서 저장할 auth 값을 정한다.

    Outlook 은 서버가 비밀번호를 거부하므로 xoauth2 고정, Naver/Daum 은 IMAP OAuth
    미지원이라 password 고정. Gmail 만 사용자 선택을 존중한다. 반환값이 타입 기본값과
    같으면 "" (accounts.yaml 에 auth 줄을 안 씀).
    """
    if typ == "outlook":
        return ""  # 기본값이 xoauth2
    if typ in ("naver", "daum"):
        return ""  # 기본값이 password
    return "xoauth2" if form_auth == "xoauth2" else ""


@bp.route("/settings/accounts", methods=["POST"])
def create_account():
    if env_mode():
        return _desktop_accounts_page()
    typ = request.form.get("type", "")
    user = request.form.get("user", "").strip()
    password = request.form.get("password", "")
    if typ not in IMAP_SERVERS:
        return settings_page(acc_error={"msg": "메일 종류를 선택하세요.", "form": request.form}), 400
    auth = _resolve_auth(typ, request.form.get("auth", ""))
    needs_pw = account_auth({"type": typ, "auth": auth}) == "password"
    if not user or (needs_pw and not password):
        return settings_page(acc_error={"msg": "이메일 주소" + ("와 앱 비밀번호를" if needs_pw else "를") + " 입력하세요.", "form": request.form}), 400
    if any(a["user"] == user for a in load_accounts(ACCOUNTS_PATH)):
        return settings_page(acc_error={"msg": f"'{user}'은(는) 이미 등록된 계정입니다.", "form": request.form}), 400
    alias = (request.form.get("alias") or "").strip()
    add_account(ACCOUNTS_PATH, type=typ, user=user, password=password, auth=auth, alias=alias)
    return redirect(url_for("settings.settings_page", tab=1, edit_acc=user))


@bp.route("/settings/accounts/<user>/edit", methods=["GET"])
def edit_account_form(user: str):
    if env_mode():
        return _desktop_accounts_page()
    accounts_list = load_accounts(ACCOUNTS_PATH)
    account = next((a for a in accounts_list if a["user"] == user), None)
    if not account:
        return page("찾을 수 없음", "<p class='empty'>그런 계정이 없습니다. <a href='/settings'>설정으로</a></p>", "settings"), 404
    u_path = quote(user, safe="")  # URL 경로 세그먼트 - submit_label 은 _cfg_page 가 esc 함
    return account_form(f"/settings/accounts/{u_path}", f"'{user}' 수정", account,
                        f"/settings/accounts/{u_path}/delete")


@bp.route("/settings/accounts/<user>", methods=["POST"])
def update_account_route(user: str):
    if env_mode():
        return _desktop_accounts_page()
    typ = request.form.get("type", "")
    new_user = request.form.get("user", "").strip()
    password = request.form.get("password", "")
    if typ not in IMAP_SERVERS:
        return settings_page(acc_error={"msg": "메일 종류를 선택하세요.", "form": request.form}), 400
    auth = _resolve_auth(typ, request.form.get("auth", ""))
    needs_pw = account_auth({"type": typ, "auth": auth}) == "password"
    if not new_user or (needs_pw and not password):
        return settings_page(acc_error={"msg": "이메일 주소" + ("와 앱 비밀번호를" if needs_pw else "를") + " 입력하세요.", "form": request.form}), 400
    alias = (request.form.get("alias") or "").strip()
    update_account(ACCOUNTS_PATH, original_user=user, type=typ, user=new_user, password=password, auth=auth, alias=alias)
    return redirect(url_for("settings.settings_page", tab=1, edit_acc=new_user))


@bp.route("/settings/accounts/<user>/delete", methods=["POST"])
def delete_account_route(user: str):
    if env_mode():
        return _desktop_accounts_page()
    delete_account(ACCOUNTS_PATH, user)
    return redirect(url_for("settings.settings_page"))
