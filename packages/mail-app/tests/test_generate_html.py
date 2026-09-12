"""generate_html.py 렌더 조각 회귀 테스트 (순수 함수만 - DB/IMAP 없음)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mail_app import generate_html as g  # noqa: E402


def test_render_matches_truncates_before_escaping():
    """제목을 escape 한 뒤 자르면 경계의 &amp; 가 &am 으로 잘려 깨진 엔티티가 된다.
    자른 뒤 escape 해야 한다."""
    # 자르기 한계(70) 안에 '&' 와 '<b>' 태그가 둘 다 들어가도록 payload 를 짧게 잡는다.
    subject = "A" * 55 + " & <b>x</b>"
    html = g.render_matches([{"subject": subject, "sender": "s@x.com", "web_link": None}])
    # 경계의 '&' 는 온전한 &amp; 엔티티여야 한다 (예전엔 escape 후 자르느라 '&am' 로 깨졌다)
    assert "&amp;" in html
    assert "&am<" not in html and "&am " not in html
    # 자르기 안에 살아남은 태그는 정상적으로 escape 된다
    assert "&lt;b&gt;x&lt;/b&gt;" in html
    assert "<b>" not in html


def test_render_matches_handles_missing_subject():
    html = g.render_matches([{"subject": None, "sender": "s@x.com", "web_link": None}])
    assert "s@x.com" in html
    assert "None" not in html  # subject or "" 가 "None" 문자열을 렌더하면 안 된다


def test_render_status_badge():
    assert g.render_status_badge({"status": "trashed"}).count("status-badge") == 1
    assert g.render_status_badge({"status": "archived", "is_read": True}).count("status-badge") == 2
    assert g.render_status_badge({"status": "active"}) == ""
    assert g.render_status_badge({}) == ""


def test_render_account_chip_prefers_alias_over_provider_label():
    """admin_ui의 라이브 계정 카드(dashboard.py)는 별칭을 우선 표시한다 - 정적 Artifact
    리포트(generate_html.py)도 같은 규칙을 따라야 한다(2026-09-12 발견/수정, 전엔 항상
    제공자명만 보였다)."""
    with_alias = g.render_account_chip("u@gmail.com", {"type": "gmail", "alias": "메인", "total": 3})
    assert "메인" in with_alias
    assert "Gmail" not in with_alias

    without_alias = g.render_account_chip("u@gmail.com", {"type": "gmail", "alias": "", "total": 3})
    assert "Gmail" in without_alias


def test_render_account_detail_prefers_alias_over_provider_label():
    data = {
        "type": "naver", "alias": "부계정", "total": 2,
        "categories": {"notice": {"count": 2, "action": "keep"}},
        "category_matches": {"notice": []},
        "uncategorized_count": 0, "uncategorized_sample": [],
    }
    html = g.render_account_detail("u@naver.com", data, "acct-u")
    assert "부계정" in html
    assert "Naver" not in html


def test_build_includes_charset_meta():
    """data/dashboard.html을 Artifact 게시 없이 직접 열어도(file://, http.server 등)
    UTF-8로 렌더되도록 - 예전엔 charset 선언이 아예 없어서 브라우저가 인코딩을 잘못
    추측해 한글이 깨졌다(2026-09-12 발견/수정)."""
    report = {
        "report_date": "2026-09-12", "generated_at": "2026-09-12T00:00:00",
        "overall": {"categories": {}, "total": 0, "category_matches": {}, "uncategorized_count": 0, "uncategorized_sample": []},
        "accounts": [], "per_account": {}, "actions": {},
    }
    html = g.build(report)
    assert html.startswith('<meta charset="utf-8">')


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run()
