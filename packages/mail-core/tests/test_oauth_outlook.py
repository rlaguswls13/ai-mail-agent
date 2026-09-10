"""oauth_outlook / imap_auth - 토큰 캐시·갱신·XOAUTH2 분기 회귀 테스트.

네트워크(_post)는 전부 monkeypatch 한다. 실행: repo 루트에서  py -m pytest
"""
import imaplib
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mail_core import imap_auth, oauth_outlook  # noqa: E402


@pytest.fixture
def token_file(tmp_path, monkeypatch):
    p = tmp_path / "outlook_token.json"
    monkeypatch.setenv(oauth_outlook.ENV_TOKENS_VAR, str(p))
    return p


def test_access_token_missing_raises(token_file):
    with pytest.raises(oauth_outlook.OutlookAuthError):
        oauth_outlook.access_token("u@outlook.kr")


def test_access_token_uses_fresh_cache(token_file):
    token_file.write_text(json.dumps({"u@outlook.kr": {
        "refresh_token": "r", "access_token": "cached", "expires_at": time.time() + 999,
    }}), encoding="utf-8")
    assert oauth_outlook.access_token("u@outlook.kr") == "cached"


def test_access_token_refreshes_when_expired(token_file, monkeypatch):
    token_file.write_text(json.dumps({"u@outlook.kr": {
        "refresh_token": "old-r", "access_token": "stale", "expires_at": time.time() - 1,
    }}), encoding="utf-8")
    calls = {}

    def fake_post(url, data):
        calls["grant"] = data["grant_type"]
        return {"access_token": "new-a", "refresh_token": "new-r", "expires_in": 3600}

    monkeypatch.setattr(oauth_outlook, "_post", fake_post)
    assert oauth_outlook.access_token("u@outlook.kr") == "new-a"
    assert calls["grant"] == "refresh_token"
    saved = json.loads(token_file.read_text(encoding="utf-8"))["u@outlook.kr"]
    assert saved["refresh_token"] == "new-r"  # 회전된 refresh token 저장됨


def test_access_token_refresh_failure_raises(token_file, monkeypatch):
    token_file.write_text(json.dumps({"u@outlook.kr": {
        "refresh_token": "old-r", "access_token": "stale", "expires_at": time.time() - 1,
    }}), encoding="utf-8")
    monkeypatch.setattr(oauth_outlook, "_post", lambda u, d: {"error": "invalid_grant"})
    with pytest.raises(oauth_outlook.OutlookAuthError):
        oauth_outlook.access_token("u@outlook.kr")


def test_device_login_polls_until_authorized(token_file, monkeypatch):
    monkeypatch.setattr(oauth_outlook.time, "sleep", lambda _: None)
    responses = [
        {"user_code": "ABC", "device_code": "dev", "interval": 0, "expires_in": 900,
         "message": "enter ABC"},
        {"error": "authorization_pending"},
        {"access_token": "a", "refresh_token": "r", "expires_in": 3600},
    ]
    seq = iter(responses)
    monkeypatch.setattr(oauth_outlook, "_post", lambda u, d: next(seq))
    oauth_outlook.device_login("u@outlook.kr", print_fn=lambda *_: None)
    assert json.loads(token_file.read_text(encoding="utf-8"))["u@outlook.kr"]["refresh_token"] == "r"


def test_device_login_on_prompt_gets_structured_dict(token_file, monkeypatch):
    monkeypatch.setattr(oauth_outlook.time, "sleep", lambda _: None)
    seq = iter([
        {"user_code": "WXYZ", "device_code": "dev", "interval": 0, "expires_in": 900,
         "verification_uri": "https://ms/link", "message": "enter WXYZ"},
        {"access_token": "a", "refresh_token": "r", "expires_in": 3600},
    ])
    monkeypatch.setattr(oauth_outlook, "_post", lambda u, d: next(seq))
    seen = {}
    oauth_outlook.device_login("u@outlook.kr", on_prompt=seen.update)
    assert seen == {"verification_uri": "https://ms/link", "user_code": "WXYZ", "message": "enter WXYZ"}


def test_token_status_missing(token_file):
    assert oauth_outlook.token_status("u@outlook.kr") == "missing"


def test_token_status_ok_without_network(token_file, monkeypatch):
    token_file.write_text(json.dumps({"u@outlook.kr": {
        "refresh_token": "r", "access_token": "a", "expires_at": time.time() + 999,
    }}), encoding="utf-8")
    monkeypatch.setattr(oauth_outlook, "_post", lambda *a: pytest.fail("네트워크를 쓰면 안 됨"))
    assert oauth_outlook.token_status("u@outlook.kr") == "ok"


def test_token_status_revoked_when_refresh_fails(token_file, monkeypatch):
    token_file.write_text(json.dumps({"u@outlook.kr": {
        "refresh_token": "r", "access_token": "a", "expires_at": time.time() - 1,
    }}), encoding="utf-8")
    monkeypatch.setattr(oauth_outlook, "_post", lambda u, d: {"error": "invalid_grant"})
    assert oauth_outlook.token_status("u@outlook.kr") == "revoked"


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


def test_authenticate_outlook_xoauth2_branch(monkeypatch):
    monkeypatch.setattr(oauth_outlook, "access_token", lambda user: "Tok")
    imap = _FakeIMAP()
    imap_auth.authenticate(imap, {"type": "outlook", "user": "o@outlook.kr", "password": ""})
    assert imap.authed[0] == "XOAUTH2"
    assert imap.authed[1] == "user=o@outlook.kr\x01auth=Bearer Tok\x01\x01"


def test_authenticate_outlook_missing_token_becomes_imap_error(monkeypatch):
    def boom(user):
        raise oauth_outlook.OutlookAuthError("no token")

    monkeypatch.setattr(oauth_outlook, "access_token", boom)
    with pytest.raises(imaplib.IMAP4.error):
        imap_auth.authenticate(_FakeIMAP(), {"type": "outlook", "user": "o@x.kr", "password": ""})
