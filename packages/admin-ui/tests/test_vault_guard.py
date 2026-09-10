"""/vault 되돌리기·영구삭제의 안전장치 + 정상 경로 회귀 테스트.

- 제출 uid는 그 탭의 status(archived/trashed) 실제 행하고만 교집합 (위조/오래된 POST 방어)
- cross-origin POST 거부
- 되돌리기: 성공 시 행을 새 INBOX UID로 rebind (stale UID 방지)
- 영구삭제: EXPUNGE 성공분만 DB 행 제거
- 레거시(message_id 없음): 되돌리기는 실패로 안내, 영구삭제는 행만 제거
"""
import datetime as _dt
import sqlite3

from mail_app import mail_log_store as store
from admin_ui import admin_app
from admin_ui.admin_app import app, DB_PATH

_RECENT = (_dt.datetime.now() - _dt.timedelta(days=5)).isoformat(timespec="seconds")


def _seed():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM messages")
    conn.commit()
    conn.close()
    rows = [
        dict(account="a@x.com", uid="1", account_type="gmail", sender="s", subject="active",
             message_date=_RECENT, web_link=None, message_id="<active@x>"),
        dict(account="a@x.com", uid="2", account_type="gmail", sender="s", subject="archived legacy",
             message_date=_RECENT, web_link=None, message_id=None),
        dict(account="a@x.com", uid="3", account_type="gmail", sender="s", subject="trashed",
             message_date=_RECENT, web_link=None, message_id="<trash@x>"),
        dict(account="a@x.com", uid="4", account_type="gmail", sender="s", subject="archived real",
             message_date=_RECENT, web_link=None, message_id="<arch@x>"),
    ]
    store.upsert_messages(DB_PATH, rows)
    store.mark_message_status(DB_PATH, "a@x.com", ["2", "4"], status="archived")
    store.mark_message_status(DB_PATH, "a@x.com", ["3"], status="trashed")


def _status(uid):
    conn = sqlite3.connect(DB_PATH)
    r = conn.execute("SELECT status FROM messages WHERE uid=?", (uid,)).fetchone()
    conn.close()
    return r[0] if r else None


def _uids():
    conn = sqlite3.connect(DB_PATH)
    us = {u for (u,) in conn.execute("SELECT uid FROM messages")}
    conn.close()
    return us


class _FakeIMAP:
    """폴더별 SEARCH 결과를 주입할 수 있는 얇은 IMAP 대역."""
    def __init__(self, *a, **k):
        self.selected = None
        # folder -> uid to return from SEARCH HEADER Message-ID
        self.search_by_folder = {"[Gmail]/All Mail": b"77", "INBOX": b"900", "[Gmail]/Trash": b"55"}

    def login(self, *a, **k):
        return "OK", [b""]

    def list(self, *a, **k):
        return "OK", [
            b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Trash"',
            b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"',
        ]

    def select(self, mailbox, readonly=False):
        self.selected = mailbox.strip('"')
        return "OK", [b"1"]

    def capability(self):
        return "OK", [b"CAPABILITY IMAP4rev1 UIDPLUS MOVE"]

    def uid(self, cmd, *args):
        c = cmd.lower()
        if c == "search":
            return "OK", [self.search_by_folder.get(self.selected, b"")]
        return "OK", [b""]

    def expunge(self):
        return "OK", [b""]

    def logout(self):
        return "OK", [b""]


def _client():
    return app.test_client()


# --- 안전장치 -------------------------------------------------------------

def test_purge_ignores_active_uid(monkeypatch):
    _seed()
    monkeypatch.setattr(admin_app, "load_accounts", lambda *a, **k: [])
    r = _client().post("/vault/purge", data={"sel": ["a@x.com::1", "a@x.com::3"]},
                       headers={"Origin": "http://localhost"})
    assert r.status_code == 302
    assert _status("1") == "active"    # active 행 그대로
    assert _status("3") == "trashed"   # 계정 없음 → 실패, 행 유지


def test_cross_origin_post_rejected():
    _seed()
    r = _client().post("/vault/purge", data={"sel": ["a@x.com::3"]},
                       headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert _status("3") == "trashed"


def test_restore_legacy_row_is_not_auto_restored(monkeypatch):
    """message_id 없는 archived 행은 stale UID를 active 풀에 다시 넣지 않는다 — archived 유지."""
    _seed()
    monkeypatch.setattr(admin_app, "load_accounts",
                        lambda *a, **k: [{"user": "a@x.com", "type": "gmail", "password": "x"}])
    r = _client().post("/vault/restore", data={"sel": ["a@x.com::2"]},
                       headers={"Origin": "http://localhost"})
    assert r.status_code == 302
    assert _status("2") == "archived"


# --- 정상 경로 (FakeIMAP) ----------------------------------------------------

def _patch_imap(monkeypatch):
    monkeypatch.setattr(admin_app, "load_accounts",
                        lambda *a, **k: [{"user": "a@x.com", "type": "gmail", "password": "x"}])
    monkeypatch.setattr(admin_app.imaplib, "IMAP4_SSL", _FakeIMAP)


def test_restore_rebinds_row_to_new_inbox_uid(monkeypatch):
    _seed()
    _patch_imap(monkeypatch)
    r = _client().post("/vault/restore", data={"sel": ["a@x.com::4"]},
                       headers={"Origin": "http://localhost"})
    assert r.status_code == 302
    # 원래 uid 4 → 새 INBOX uid 900 으로 바뀌고 active
    assert "4" not in _uids()
    assert _status("900") == "active"


def test_purge_deletes_row_after_expunge(monkeypatch):
    _seed()
    _patch_imap(monkeypatch)
    r = _client().post("/vault/purge", data={"sel": ["a@x.com::3"]},
                       headers={"Origin": "http://localhost"})
    assert r.status_code == 302
    assert "3" not in _uids()   # EXPUNGE 성공 → DB 행 제거


def test_purge_legacy_row_deletes_row_only(monkeypatch):
    _seed()
    _patch_imap(monkeypatch)
    # uid 2 는 archived 지만, trash 탭 교집합에서 빠지므로 아무 일도 안 일어난다
    r = _client().post("/vault/purge", data={"sel": ["a@x.com::2"]},
                       headers={"Origin": "http://localhost"})
    assert r.status_code == 302
    assert "2" in _uids() and _status("2") == "archived"


def _run():
    ns = {k: v for k, v in sorted(globals().items()) if k.startswith("test_")}

    class _MP:
        def __init__(self): self._u = []
        def setattr(self, obj, name, val):
            self._u.append((obj, name, getattr(obj, name)))
            setattr(obj, name, val)
        def undo(self):
            for o, n, v in reversed(self._u):
                setattr(o, n, v)

    passed = 0
    for name, fn in ns.items():
        mp = _MP()
        try:
            fn(mp) if fn.__code__.co_argcount else fn()
            passed += 1
            print(f"ok  {name}")
        finally:
            mp.undo()
    print(f"\n{passed} passed")


if __name__ == "__main__":
    _run()
