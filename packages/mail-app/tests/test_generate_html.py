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


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run()
