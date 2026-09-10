"""admin_ui `/settings` 블루프린트 - 카테고리 규칙 + 메일 계정 CRUD.

인라인 편집(<details>) UI + 직접 URL 접근용 폴백 페이지(/settings/categories/{new,edit}).
카테고리는 data/app.db, 계정은 config/accounts.yaml (또는 데스크톱 앱의 암호화 볼트 -
env_mode()면 이 화면에선 읽기 전용).
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
    esc,
    kw_to_text,
    page,
    text_to_kw,
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


def category_fields(action_url: str, submit_label: str, category: dict | None, delete_url: str | None) -> str:
    """카테고리 추가/수정 폼의 알맹이 (페이지 래퍼 없음). /settings의 인라인 편집과
    /settings/categories/{new,edit} 폴백 페이지가 함께 쓴다."""
    category = category or {
        "name": "", "description": "", "action": "keep", "priority": "NORMAL",
        "senders": [], "domains": [], "title": [], "contents": [],
    }
    category.setdefault("domains", [])
    name_field = (
        f'<input type="text" name="name" value="{esc(category["name"])}" required placeholder="영문 소문자 키">'
        if not delete_url
        else f'<input type="text" value="{esc(category["name"])}" disabled>'
    )
    action_options = "".join(
        f'<option value="{a}"{" selected" if a == category["action"] else ""}>{a}</option>' for a in ACTIONS
    )
    priority_options = "".join(
        f'<option value="{p}"{" selected" if p == category["priority"] else ""}>{p}</option>' for p in PRIORITIES
    )
    form = f"""
    <form class="cfg-form" method="post" action="{action_url}">
      <div class="row">
        <label><span>이름 (규칙 키) <b class="req">*</b></span>{name_field}</label>
        <label>description<input type="text" name="description" value="{esc(category['description'])}"></label>
      </div>
      <div class="row">
        <label>action<select name="action">{action_options}</select>
          <span class="hint">trash=휴지통 / save=보관 / read=읽음 / keep=그대로</span>
        </label>
        <label>priority<select name="priority">{priority_options}</select>
          <span class="hint">동률이면 먼저 만든 쪽 우선</span>
        </label>
      </div>
      <label>keywords.senders - 완전 일치하는 전체 이메일 주소, 한 줄에 하나
        <textarea name="senders" placeholder="noreply@example.com">{esc(kw_to_text(category['senders']))}</textarea></label>
      <label>keywords.domains - 발신 도메인(서브도메인 포함), 한 줄에 하나. 한 도메인을 한 카테고리가 독점할 때만
        <textarea name="domains" placeholder="lguplus.co.kr">{esc(kw_to_text(category['domains']))}</textarea></label>
      <label>keywords.title - 제목 부분 문자열, 한 줄에 하나
        <textarea name="title">{esc(kw_to_text(category['title']))}</textarea></label>
      <label>keywords.contents - 본문 부분 문자열(있으면 본문 추가 조회 발생)
        <textarea name="contents">{esc(kw_to_text(category['contents']))}</textarea></label>
      <div class="actions-row"><button class="btn" type="submit">{submit_label}</button></div>
    </form>
    """
    if delete_url:
        form += _delete_form(delete_url, f"{category['name']} 카테고리를 삭제할까요?")
    return form


def account_fields(action_url: str, submit_label: str, account: dict | None, delete_url: str | None) -> str:
    account = account or {"type": "gmail", "user": "", "password": ""}
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
      <label>인증 방식
        <span class="hint">Gmail 만 선택 가능 · Outlook 은 OAuth 고정, Naver 는 앱 비밀번호 고정 (저장 시 자동 보정)</span>
        <select name="auth">{auth_options}</select></label>
      <label><span>앱 비밀번호</span>
        <span class="hint">인증이 "앱 비밀번호"일 때만 필요 · OAuth 는 CLI <code>python -m mail_app.oauth_login</code> 로 로그인 · config/accounts.yaml에 평문 저장</span>
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


def category_form(action_url: str, submit_label: str, category: dict | None, delete_url: str | None) -> str:
    return _cfg_page(
        submit_label,
        category_fields(action_url, submit_label, category, delete_url),
        "senders는 완전 일치 이메일 주소, title/contents는 부분 문자열 키워드 (한 줄에 하나).",
    )


def account_form(action_url: str, submit_label: str, account: dict | None, delete_url: str | None) -> str:
    return _cfg_page(
        submit_label,
        account_fields(action_url, submit_label, account, delete_url),
        "앱 비밀번호 사용을 권장합니다. config/accounts.yaml에 평문으로 저장됩니다 - 로컬 전용 도구.",
    )


def _cfg_add(summary: str, fields_html: str, is_open: bool = False) -> str:
    return (
        f'<details class="cfg-item cfg-add"{" open" if is_open else ""}>'
        f'<summary><span class="cfg-add-label">{esc(summary)}</span></summary>'
        f'<div class="cfg-body">{fields_html}</div></details>'
    )


def _cfg_item(summary_cells: str, fields_html: str) -> str:
    return (
        f'<details class="cfg-item">'
        f'<summary>{summary_cells}<span class="cfg-chevron">▸</span></summary>'
        f'<div class="cfg-body">{fields_html}</div></details>'
    )


def _err_banner(msg: str) -> str:
    return f'<div class="form-error" role="alert" tabindex="-1">{esc(msg)}</div>'


def _form_to_category(form) -> dict:
    return {
        "name": (form.get("name") or "").strip(),
        "description": (form.get("description") or "").strip(),
        "action": form.get("action") or "keep",
        "priority": form.get("priority") or "NORMAL",
        "senders": text_to_kw(form.get("senders", "")),
        "domains": text_to_kw(form.get("domains", "")),
        "title": text_to_kw(form.get("title", "")),
        "contents": text_to_kw(form.get("contents", "")),
    }


def _form_to_account(form) -> dict:
    return {
        "type": form.get("type") or "gmail",
        "user": (form.get("user") or "").strip(),
        "password": form.get("password") or "",
    }


def _category_section(error: dict | None = None) -> str:
    categories = config_store.list_categories(DB_PATH)
    add_prefill = _form_to_category(error["form"]) if error and error.get("form") else None
    banner = _err_banner(error["msg"]) if error else ""
    items = _cfg_add(
        "+ 카테고리 추가",
        category_fields("/settings/categories", "추가", add_prefill, None),
        is_open=add_prefill is not None,
    )
    for c in categories:
        summary = (
            f'<span class="cfg-main"><span class="cfg-name">{esc(c["name"])}</span>'
            f'<span class="cfg-desc">{esc(c["description"])}</span></span>'
            f'<span class="pill {c["action"]}">{c["action"]}</span>'
            f'<span class="cfg-tag">{c["priority"]}</span>'
            f'<span class="cfg-tag cfg-counts" title="senders / domains / title / contents">'
            f'{len(c["senders"])} / {len(c.get("domains", []))} / {len(c["title"])} / {len(c["contents"])}</span>'
        )
        items += _cfg_item(
            summary,
            category_fields(f"/settings/categories/{c['name']}", "저장",
                            c, f"/settings/categories/{c['name']}/delete"),
        )
    return (
        f'{banner}'
        f'<div class="cfg-toolbar">'
        f'<form method="post" action="/settings/categories/export" style="margin:0">'
        f'<button class="btn secondary" type="submit">categories.json으로 내보내기</button></form>'
        f'</div>'
        f'<div class="cfg-list">{items}</div>'
    )


def _accounts_section(error: dict | None = None) -> str:
    accounts_list = load_accounts(ACCOUNTS_PATH)
    if env_mode():
        rows = "".join(
            f'<div class="cfg-item cfg-static"><div class="cfg-summary-static">'
            f'<span class="cfg-name">{esc(PROVIDER_LABEL.get(a["type"], a["type"]))}</span>'
            f'<span class="cfg-desc">{esc(a["user"])}</span></div></div>'
            for a in accounts_list
        )
        return (
            '<p class="sub">계정은 데스크톱 앱의 <strong>계정 설정</strong> 창에서 관리됩니다 '
            '(자격증명은 암호화 볼트에 저장). 여기서는 편집할 수 없습니다.</p>'
            f'<div class="cfg-list">{rows}</div>'
            if accounts_list else '<p class="empty">연결된 메일 계정이 없습니다.</p>'
        )
    add_prefill = _form_to_account(error["form"]) if error and error.get("form") else None
    banner = _err_banner(error["msg"]) if error else ""
    items = _cfg_add(
        "+ 계정 추가",
        account_fields("/settings/accounts", "추가", add_prefill, None),
        is_open=add_prefill is not None,
    )
    for a in accounts_list:
        summary = (
            f'<span class="cfg-main"><span class="cfg-name">{esc(a["user"])}</span>'
            f'<span class="cfg-desc">{esc(PROVIDER_LABEL.get(a["type"], a["type"]))}</span></span>'
        )
        # URL 경로 세그먼트라 quote (esc 로 HTML 이스케이프하면 &amp; 가 경로에 들어감).
        # quote 결과는 <, >, & 등이 없어 폼 action 속성에도 안전.
        u_path = quote(a["user"], safe="")
        items += _cfg_item(
            summary,
            account_fields(f"/settings/accounts/{u_path}", "저장",
                           a, f"/settings/accounts/{u_path}/delete"),
        )
    return f'{banner}<div class="cfg-list">{items}</div>'


@bp.route("/settings")
def settings_page(cat_error: dict | None = None, acc_error: dict | None = None):
    tabs = generate_html.render_tab_group(
        "settings",
        [
            ("카테고리", "", _category_section(cat_error)),
            ("메일 계정", "", _accounts_section(acc_error)),
        ],
        1 if acc_error else 0,
    )
    body = f"""
    <h1 class="page-title">⚙️ 설정</h1>
    <p class="sub">"수정"/"추가"를 누르면 그 자리에서 펼쳐집니다. 저장하면 다음 파이프라인
    실행부터 바로 반영됩니다 (data/app.db, config/accounts.yaml).</p>
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


@bp.route("/settings/categories/new", methods=["GET"])
def new_category_form():
    return category_form("/settings/categories", "카테고리 추가", None, None)


@bp.route("/settings/categories", methods=["POST"])
def create_category():
    name = request.form.get("name", "").strip()
    if not name:
        return settings_page(cat_error={"msg": "카테고리 이름을 입력하세요.", "form": request.form}), 400
    if config_store.get_category(DB_PATH, name):
        return settings_page(cat_error={
            "msg": f"'{name}'은(는) 이미 있는 카테고리입니다. 다른 이름을 쓰거나 기존 항목을 수정하세요.",
            "form": request.form,
        }), 400
    config_store.add_category(
        DB_PATH,
        name=name,
        description=request.form.get("description", "").strip(),
        action=request.form.get("action", "keep"),
        priority=request.form.get("priority", "NORMAL"),
        senders=text_to_kw(request.form.get("senders", "")),
        domains=text_to_kw(request.form.get("domains", "")),
        title=text_to_kw(request.form.get("title", "")),
        contents=text_to_kw(request.form.get("contents", "")),
    )
    return redirect(url_for("settings.settings_page"))


@bp.route("/settings/categories/<name>/edit", methods=["GET"])
def edit_category_form(name: str):
    category = config_store.get_category(DB_PATH, name)
    if not category:
        return page("찾을 수 없음", "<p class='empty'>그런 카테고리가 없습니다. <a href='/settings'>설정으로</a></p>", "settings"), 404
    return category_form(f"/settings/categories/{name}", f"'{name}' 수정", category, f"/settings/categories/{name}/delete")


@bp.route("/settings/categories/<name>", methods=["POST"])
def update_category(name: str):
    if not config_store.get_category(DB_PATH, name):
        return settings_page(cat_error={"msg": f"'{name}' 카테고리를 찾을 수 없습니다.", "form": None}), 404
    config_store.update_category(
        DB_PATH,
        name=name,
        description=request.form.get("description", "").strip(),
        action=request.form.get("action", "keep"),
        priority=request.form.get("priority", "NORMAL"),
        senders=text_to_kw(request.form.get("senders", "")),
        domains=text_to_kw(request.form.get("domains", "")),
        title=text_to_kw(request.form.get("title", "")),
        contents=text_to_kw(request.form.get("contents", "")),
    )
    return redirect(url_for("settings.settings_page"))


@bp.route("/settings/categories/<name>/delete", methods=["POST"])
def delete_category(name: str):
    config_store.delete_category(DB_PATH, name)
    return redirect(url_for("settings.settings_page"))


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

    Outlook 은 서버가 비밀번호를 거부하므로 xoauth2 고정, Naver 는 IMAP OAuth 미지원이라
    password 고정. Gmail 만 사용자 선택을 존중한다. 반환값이 타입 기본값과 같으면 ""
    (accounts.yaml 에 auth 줄을 안 씀).
    """
    if typ == "outlook":
        return ""  # 기본값이 xoauth2
    if typ == "naver":
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
    add_account(ACCOUNTS_PATH, type=typ, user=user, password=password, auth=auth)
    return redirect(url_for("settings.settings_page"))


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
    update_account(ACCOUNTS_PATH, original_user=user, type=typ, user=new_user, password=password, auth=auth)
    return redirect(url_for("settings.settings_page"))


@bp.route("/settings/accounts/<user>/delete", methods=["POST"])
def delete_account_route(user: str):
    if env_mode():
        return _desktop_accounts_page()
    delete_account(ACCOUNTS_PATH, user)
    return redirect(url_for("settings.settings_page"))
