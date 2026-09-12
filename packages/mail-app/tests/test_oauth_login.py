"""mail_app.oauth_login CLI - --no-interactive 회귀 테스트.

계정 로딩·토큰 갱신·브라우저 로그인은 전부 monkeypatch. 실행: repo 루트에서 py -m pytest
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mail_app import oauth_login  # noqa: E402
from mail_core import oauth  # noqa: E402

ACCOUNT = {"user": "u@gmail.com", "type": "gmail", "auth": "xoauth2"}


@pytest.fixture(autouse=True)
def _one_xoauth2_account(monkeypatch):
    monkeypatch.setattr(oauth_login, "load_accounts", lambda path: [ACCOUNT])
    monkeypatch.setattr(oauth_login, "resolve_accounts_from_stdin", lambda: None)


def test_no_interactive_skips_browser_login_when_refresh_fails(monkeypatch, capsys):
    """--no-interactive: 조용한 갱신이 실패하면 oauth.login()은 절대 호출하지 않는다."""
    login_called = []
    monkeypatch.setattr(oauth, "access_token", lambda provider, user: (_ for _ in ()).throw(oauth.OAuthError("refresh dead")))
    monkeypatch.setattr(oauth, "login", lambda *a, **kw: login_called.append(True))

    rc = oauth_login.main(["--no-interactive"])

    assert rc == 1  # 계정 1개 실패
    assert login_called == []
    assert "대화형 로그인은 건너뜀" in capsys.readouterr().err


def test_without_no_interactive_falls_back_to_browser_login(monkeypatch):
    """기존 동작 보존: 플래그 없으면 조용한 갱신 실패 시 브라우저 로그인으로 넘어간다."""
    login_called = []
    monkeypatch.setattr(oauth, "access_token", lambda provider, user: (_ for _ in ()).throw(oauth.OAuthError("refresh dead")))
    monkeypatch.setattr(oauth, "login", lambda *a, **kw: login_called.append(True))

    rc = oauth_login.main([])

    assert rc == 0
    assert login_called == [True]


def test_no_interactive_still_uses_valid_cached_token(monkeypatch):
    """--no-interactive여도 유효한 토큰이 있으면 평소처럼 조용히 skip한다(원래 갱신 로직은 그대로)."""
    monkeypatch.setattr(oauth, "access_token", lambda provider, user: "cached-token")
    login_called = []
    monkeypatch.setattr(oauth, "login", lambda *a, **kw: login_called.append(True))

    rc = oauth_login.main(["--no-interactive"])

    assert rc == 0
    assert login_called == []
