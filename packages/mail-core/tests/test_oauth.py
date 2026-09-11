"""mail_core.oauth / imap_auth / accounts.account_auth 회귀 테스트.

네트워크(_post)·브라우저(webbrowser)는 전부 monkeypatch. 실행: repo 루트에서 py -m pytest
"""
import imaplib
import json
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mail_core import crypto, imap_auth, oauth  # noqa: E402
from mail_core.accounts import account_auth  # noqa: E402


def _read_store(path):
    """토큰 캐시 파일을 복호화해서 dict 로 반환 (테스트 전용 헬퍼)."""
    return json.loads(crypto.decrypt(path.read_text(encoding="utf-8")))


@pytest.fixture
def oauth_dir(tmp_path, monkeypatch):
    monkeypatch.setenv(oauth.ENV_OAUTH_DIR, str(tmp_path))
    return tmp_path


# ── account_auth ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("account, expected", [
    ({"type": "gmail", "password": "x"}, "password"),
    ({"type": "naver", "password": "x"}, "password"),
    ({"type": "outlook", "password": ""}, "xoauth2"),        # 타입 기본값
    ({"type": "gmail", "auth": "xoauth2"}, "xoauth2"),        # 명시적
    ({"type": "outlook", "auth": "password"}, "password"),    # 명시적 override
    ({"type": "gmail", "auth": "  XOAUTH2 "}, "xoauth2"),     # 정규화
    ({"type": "gmail", "auth": "garbage"}, "password"),       # 잘못된 값 → 기본값
])
def test_account_auth(account, expected):
    assert account_auth(account) == expected


# ── 토큰 캐시 / 갱신 / 상태 ────────────────────────────────────────────────

def test_access_token_missing_raises(oauth_dir):
    with pytest.raises(oauth.OAuthError):
        oauth.access_token("gmail", "u@gmail.com")


def test_access_token_uses_fresh_cache(oauth_dir):
    (oauth_dir / "outlook_token.json").write_text(json.dumps({"u@o.kr": {
        "refresh_token": "r", "access_token": "cached", "expires_at": time.time() + 999,
    }}), encoding="utf-8")
    assert oauth.access_token("outlook", "u@o.kr") == "cached"


def test_access_token_refreshes_and_sends_client_secret_for_gmail(oauth_dir, monkeypatch):
    (oauth_dir / "gmail_token.json").write_text(json.dumps({"u@gmail.com": {
        "refresh_token": "old-r", "access_token": "stale", "expires_at": time.time() - 1,
    }}), encoding="utf-8")
    seen = {}

    def fake_post(url, data):
        seen.update(data)
        return {"access_token": "new-a", "expires_in": 3600}  # Google: refresh_token 미회전

    monkeypatch.setattr(oauth, "_post", fake_post)
    assert oauth.access_token("gmail", "u@gmail.com") == "new-a"
    assert seen["grant_type"] == "refresh_token"
    assert seen["client_secret"] == oauth.PROVIDERS["gmail"].client_secret
    saved = _read_store(oauth_dir / "gmail_token.json")["u@gmail.com"]
    assert saved["refresh_token"] == "old-r"  # 기존 값 유지


def test_outlook_refresh_omits_client_secret(oauth_dir, monkeypatch):
    (oauth_dir / "outlook_token.json").write_text(json.dumps({"u@o.kr": {
        "refresh_token": "r", "access_token": "s", "expires_at": time.time() - 1,
    }}), encoding="utf-8")
    seen = {}
    monkeypatch.setattr(oauth, "_post", lambda u, d: seen.update(d) or {"access_token": "a", "expires_in": 3600})
    oauth.access_token("outlook", "u@o.kr")
    assert "client_secret" not in seen


def test_token_status(oauth_dir, monkeypatch):
    assert oauth.token_status("gmail", "u@gmail.com") == "missing"
    (oauth_dir / "gmail_token.json").write_text(json.dumps({"u@gmail.com": {
        "refresh_token": "r", "access_token": "a", "expires_at": time.time() + 999,
    }}), encoding="utf-8")
    monkeypatch.setattr(oauth, "_post", lambda *a: pytest.fail("네트워크 쓰면 안 됨"))
    assert oauth.token_status("gmail", "u@gmail.com") == "ok"


def test_token_status_revoked(oauth_dir, monkeypatch):
    (oauth_dir / "gmail_token.json").write_text(json.dumps({"u@gmail.com": {
        "refresh_token": "r", "access_token": "a", "expires_at": time.time() - 1,
    }}), encoding="utf-8")
    monkeypatch.setattr(oauth, "_post", lambda u, d: {"error": "invalid_grant"})
    assert oauth.token_status("gmail", "u@gmail.com") == "revoked"


# ── device flow (Outlook) ─────────────────────────────────────────────────

def test_device_login_polls_then_stores(oauth_dir, monkeypatch):
    monkeypatch.setattr(oauth.time, "sleep", lambda _: None)
    seq = iter([
        {"user_code": "AB", "device_code": "dev", "interval": 0, "expires_in": 900,
         "verification_uri": "https://ms/link", "message": "enter AB"},
        {"error": "authorization_pending"},
        {"access_token": "a", "refresh_token": "r", "expires_in": 3600},
    ])
    monkeypatch.setattr(oauth, "_post", lambda u, d: next(seq))
    seen = {}
    oauth.login("outlook", "u@o.kr", on_prompt=seen.update)
    assert seen["user_code"] == "AB" and seen["verification_uri"] == "https://ms/link"
    assert _read_store(oauth_dir / "outlook_token.json")["u@o.kr"]["refresh_token"] == "r"


# ── loopback flow (Gmail) ─────────────────────────────────────────────────

def test_loopback_login_full_roundtrip(oauth_dir, monkeypatch):
    """webbrowser.open 이 받은 auth_url 에서 redirect_uri/state 를 뽑아 실제로 콜백을 친다."""
    def fake_open(url):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        redirect_uri = q["redirect_uri"][0]
        state = q["state"][0]
        assert q["code_challenge_method"] == ["S256"]

        def hit():
            time.sleep(0.05)
            urllib.request.urlopen(f"{redirect_uri}?code=the-code&state={state}", timeout=5).read()

        threading.Thread(target=hit, daemon=True).start()

    monkeypatch.setattr(oauth.webbrowser, "open", fake_open)

    def fake_post(url, data):
        assert data["grant_type"] == "authorization_code"
        assert data["code"] == "the-code" and data["code_verifier"]
        return {"access_token": "a", "refresh_token": "r", "expires_in": 3600}

    monkeypatch.setattr(oauth, "_post", fake_post)
    oauth.login("gmail", "u@gmail.com", print_fn=lambda *_: None)
    assert _read_store(oauth_dir / "gmail_token.json")["u@gmail.com"]["refresh_token"] == "r"


def test_loopback_login_rejects_bad_state(oauth_dir, monkeypatch):
    def fake_open(url):
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        redirect_uri = q["redirect_uri"][0]

        def hit():
            time.sleep(0.05)
            urllib.request.urlopen(f"{redirect_uri}?code=x&state=WRONG", timeout=5).read()

        threading.Thread(target=hit, daemon=True).start()

    monkeypatch.setattr(oauth.webbrowser, "open", fake_open)
    monkeypatch.setattr(oauth, "_post", lambda *a: pytest.fail("state 불일치면 교환 안 함"))
    with pytest.raises(oauth.OAuthError, match="state"):
        oauth.login("gmail", "u@gmail.com", print_fn=lambda *_: None)


# ── imap_auth 분기 ────────────────────────────────────────────────────────

class _FakeIMAP:
    def __init__(self):
        self.logged_in = None
        self.authed = None

    def login(self, user, password):
        self.logged_in = (user, password)

    def authenticate(self, mech, authobject):
        self.authed = (mech, authobject(b"").decode())


def test_authenticate_password_branch():
    imap = _FakeIMAP()
    imap_auth.authenticate(imap, {"type": "gmail", "user": "g@x.com", "password": "pw"})
    assert imap.logged_in == ("g@x.com", "pw")


def test_authenticate_gmail_xoauth2_when_opted_in(monkeypatch):
    monkeypatch.setattr(oauth, "access_token", lambda provider, user: f"T-{provider}")
    imap = _FakeIMAP()
    imap_auth.authenticate(imap, {"type": "gmail", "user": "g@x.com", "password": "", "auth": "xoauth2"})
    assert imap.authed == ("XOAUTH2", "user=g@x.com\x01auth=Bearer T-gmail\x01\x01")


def test_authenticate_outlook_missing_token_becomes_imap_error(monkeypatch):
    def boom(provider, user):
        raise oauth.OAuthError("no token")

    monkeypatch.setattr(oauth, "access_token", boom)
    with pytest.raises(imaplib.IMAP4.error):
        imap_auth.authenticate(_FakeIMAP(), {"type": "outlook", "user": "o@x.kr", "password": ""})
