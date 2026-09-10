"""메일 목록 테이블 한 행(msg_table_row) 렌더링 회귀 테스트."""
from admin_ui import admin_app


def _row(**over):
    m = {
        "account": "gcd1324@gmail.com",
        "account_type": "gmail",
        "uid": "42",
        "sender": "no-reply@example.com",
        "subject": "hello",
        "message_date": "2026-09-08T18:04",
        "web_link": None,
        "status": None,
        "is_read": None,
    }
    m.update(over)
    return admin_app.msg_table_row(m, {}, with_account=True)


def test_account_cell_is_two_lines_provider_then_full_email():
    html = _row()
    # 제공자 라벨(윗줄)과 전체 이메일(아랫줄)이 별도 span 으로 분리돼야 한다.
    assert '<span class="a-provider">Gmail</span>' in html
    assert '<span class="a-email">gcd1324@gmail.com</span>' in html
    # 예전 한 줄 형식("Gmail · 이메일")은 더 이상 없어야 한다.
    assert "Gmail · gcd1324@gmail.com" not in html


def test_account_cell_escapes_and_survives_unknown_type():
    html = _row(account="a<b>@x.com", account_type="weird")
    assert "a&lt;b&gt;@x.com" in html
    # 알 수 없는 account_type 은 그대로 라벨로 노출(PROVIDER_LABEL 미스 시 원본).
    assert '<span class="a-provider">weird</span>' in html


def test_no_account_cell_when_with_account_false():
    m = {
        "account": "x@y.com", "account_type": "gmail", "uid": "1",
        "sender": "s@x.com", "subject": "s", "message_date": "2026-09-08T00:00",
        "web_link": None, "status": None, "is_read": None,
    }
    assert "msg-account" not in admin_app.msg_table_row(m, {}, with_account=False)
