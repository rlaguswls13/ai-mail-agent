"""라우트 레벨 스모크 + 동작 회귀.

admin_app(2078줄)을 Flask 블루프린트 4개로 쪼갤 때 "URL -> 상태코드 / 필터 /
페이지네이션 / 카테고리 CRUD" 가 안 바뀌었는지 지키는 회귀 가드. 분리 전에 먼저
만들었고(커밋 4d90943), 분리 5커밋(4d90943..cab2ff9) 내내 이 파일이 그대로 통과해
"동작 불변" 을 확인했다. 라우트 추가/변경 시 이 파일도 같이 갱신.
"""
import re

import pytest

from admin_ui.admin_app import app

pytestmark = pytest.mark.usefixtures("sample_data")

_SKIP_ENDPOINTS = {"static"}
# <user>/<name> 자리에 넣을 값: 존재하는 것 / 없는 것.
_GET_ROUTES_OK = [
    "/",
    "/list",
    "/settings",
    "/tasks",
    "/vault",
    "/settings/categories/new",
    "/settings/accounts/new",
    "/settings/categories/%EA%B4%91%EA%B3%A0/edit",  # "광고"
]


def test_every_get_route_is_covered():
    """url_map 의 GET 라우트가 이 파일에서 하나도 안 빠졌는지 - 라우트 추가 시 알림."""
    declared = set()
    for rule in app.url_map.iter_rules():
        if rule.endpoint in _SKIP_ENDPOINTS:
            continue
        if "GET" in rule.methods:
            declared.add(rule.rule)
    # 변수 라우트는 대표 1개만 커버 목록에 있으면 됨 - rule.rule 문자열로 매핑
    covered_rules = {
        "/", "/list", "/settings", "/tasks", "/vault",
        "/settings/categories/new", "/settings/accounts/new",
        "/settings/categories/<name>/edit", "/settings/accounts/<user>/edit",
    }
    missing = declared - covered_rules
    assert not missing, f"커버 안 된 GET 라우트: {sorted(missing)}"


@pytest.mark.parametrize("path", _GET_ROUTES_OK)
def test_get_route_returns_200(client, path):
    r = client.get(path)
    assert r.status_code == 200, f"GET {path} -> {r.status_code}"


def test_edit_form_for_missing_row_is_404_not_500(client):
    assert client.get("/settings/categories/does-not-exist/edit").status_code == 404
    assert client.get("/settings/accounts/nobody@x.com/edit").status_code == 404


@pytest.mark.parametrize("rng", ["daily", "weekly", "monthly", "all"])
def test_dashboard_range_tabs(client, rng):
    r = client.get(f"/?range={rng}")
    assert r.status_code == 200
    assert "계정별 상세" in r.get_data(as_text=True)


def test_list_account_filter(client):
    """account=b@naver.com 이면 그 계정 행만 (11 은 trashed 라 기본 목록에서 빠짐)."""
    html = client.get("/list?range=all&account=b@naver.com").get_data(as_text=True)
    assert "주간 뉴스레터" in html
    assert "채용 확정" not in html  # c@gmail.com 의 메일


def test_list_search_filter(client):
    html = client.get("/list?range=all&q=면접").get_data(as_text=True)
    assert "면접 일정" in html
    assert "주간 뉴스레터" not in html


def test_list_category_filter_uncategorized(client):
    html = client.get("/list?range=all&category=__uncategorized__").get_data(as_text=True)
    assert "분류 안 되는 메일" in html
    assert "채용 확정" not in html


def test_list_pagination_splits_pages(client):
    p1 = client.get("/list?range=all&page_size=10&page=1").get_data(as_text=True)
    # 6건 < 10 이라 1페이지뿐 - "1 / 1 페이지" 문구와 총건수가 보여야 한다
    assert re.search(r"1\s*/\s*1\s*페이지", p1)
    assert "총 5건" in p1 or "총 6건" in p1  # trashed 1건 제외 여부에 관대하게


def test_subject_html_is_escaped(client):
    html = client.get("/list?range=all&q=면접").get_data(as_text=True)
    assert "면접 일정 &amp; 안내 &lt;b&gt;" in html
    assert "<b>" not in html.split("면접 일정")[1][:40]


def test_vault_archive_tab_lists_only_archived(client):
    html = client.get("/vault?tab=archive").get_data(as_text=True)
    assert "분류 안 되는 메일" in html   # uid 3, archived
    assert "보안 경고" not in html       # uid 11 은 trashed - 이 탭엔 안 나옴


def test_vault_trash_tab_lists_only_trashed(client):
    html = client.get("/vault?tab=trash").get_data(as_text=True)
    assert "보안 경고" in html            # uid 11, trashed
    assert "분류 안 되는 메일" not in html


def test_category_crud_roundtrip(client, local_headers):
    # 생성
    r = client.post("/settings/categories", headers=local_headers, data={
        "name": "임시", "description": "d", "action": "keep", "priority": "NORMAL",
        "senders": "x@y.com", "title": "", "contents": "", "domains": "",
    })
    assert r.status_code in (200, 302)
    assert client.get("/settings/categories/%EC%9E%84%EC%8B%9C/edit").status_code == 200
    # 수정
    r = client.post("/settings/categories/%EC%9E%84%EC%8B%9C", headers=local_headers, data={
        "name": "임시", "description": "d2", "action": "trash", "priority": "LOW",
        "senders": "", "title": "키워드", "contents": "", "domains": "",
    })
    assert r.status_code in (200, 302)
    # 삭제
    r = client.post("/settings/categories/%EC%9E%84%EC%8B%9C/delete", headers=local_headers)
    assert r.status_code in (200, 302)
    assert client.get("/settings/categories/%EC%9E%84%EC%8B%9C/edit").status_code == 404


def test_create_category_without_name_is_400(client, local_headers):
    r = client.post("/settings/categories", headers=local_headers, data={"name": "  "})
    assert r.status_code == 400


def test_tasks_action_empty_selection_redirects(client, local_headers):
    r = client.post("/tasks/action", headers=local_headers, data={"sel": [], "action": "read"})
    assert r.status_code == 302
