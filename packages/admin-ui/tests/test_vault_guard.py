"""/vault 되돌리기·영구삭제의 안전장치 회귀 테스트.

핵심: 제출된 uid가 그 탭의 status(archived/trashed)인 실제 행하고만 교집합되어야 한다
— 오래된 화면이나 위조 POST로 active(받은편지함) 메일을 지우면 안 된다.
"""
import sqlite3

from mail_app import mail_log_store as store
from admin_ui import admin_app
from admin_ui.admin_app import app, DB_PATH


def _seed():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM messages")
    conn.commit()
    conn.close()
    rows = [
        dict(account="a@x.com", uid="1", account_type="gmail", sender="s", subject="active one",
             message_date="2026-02-01T00:00:00", web_link=None, message_id="<active@x>"),
        dict(account="a@x.com", uid="2", account_type="gmail", sender="s", subject="archived legacy",
             message_date="2026-02-01T00:00:00", web_link=None, message_id=None),
        dict(account="a@x.com", uid="3", account_type="gmail", sender="s", subject="trashed one",
             message_date="2026-02-01T00:00:00", web_link=None, message_id="<trash@x>"),
    ]
    store.upsert_messages(DB_PATH, rows)
    store.mark_message_status(DB_PATH, "a@x.com", ["2"], status="archived")
    store.mark_message_status(DB_PATH, "a@x.com", ["3"], status="trashed")


def _status(uid):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT status FROM messages WHERE uid=?", (uid,)).fetchone()
    conn.close()
    return row[0] if row else None


def test_purge_ignores_active_uid(monkeypatch):
    """/vault?tab=trash 에서 active uid를 제출해도 그 행은 삭제/변경되지 않는다."""
    _seed()
    monkeypatch.setattr(admin_app, "load_accounts", lambda *a, **k: [])  # 계정 없음 → IMAP 안 함
    c = app.test_client()
    r = c.post("/vault/purge", data={"tab": "trash", "sel": ["a@x.com::1", "a@x.com::3"]},
               headers={"Origin": "http://127.0.0.1:5000"})
    assert r.status_code == 302
    assert _status("1") == "active"      # active 행은 그대로
    # uid 3 은 trashed 이고 계정 설정이 없어 '실패'로 처리 → 행 유지(데이터 손실 없음)
    assert _status("3") == "trashed"


def test_restore_legacy_row_without_message_id_flips_to_active(monkeypatch):
    """archived 인데 message_id 없는 레거시 행은 계정만 있으면 IMAP 없이 active 로."""
    _seed()
    fake_acc = {"user": "a@x.com", "type": "gmail", "password": "x"}
    monkeypatch.setattr(admin_app, "load_accounts", lambda *a, **k: [fake_acc])
    c = app.test_client()
    r = c.post("/vault/restore", data={"tab": "archive", "sel": ["a@x.com::2", "a@x.com::1"]},
               headers={"Origin": "http://127.0.0.1:5000"})
    assert r.status_code == 302
    assert _status("2") == "active"   # 레거시 archived → 되돌림(DB만)
    assert _status("1") == "active"   # active 였던 건 애초에 교집합에서 빠짐 → 그대로


def test_cross_origin_post_rejected():
    _seed()
    c = app.test_client()
    r = c.post("/vault/purge", data={"tab": "trash", "sel": ["a@x.com::3"]},
               headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert _status("3") == "trashed"


def _run():
    class _MP:
        def __init__(self): self._undo = []
        def setattr(self, obj, name, val):
            self._undo.append((obj, name, getattr(obj, name)))
            setattr(obj, name, val)
        def undo(self):
            for obj, name, val in reversed(self._undo):
                setattr(obj, name, val)
    for fn in [test_purge_ignores_active_uid, test_restore_legacy_row_without_message_id_flips_to_active]:
        mp = _MP()
        try:
            fn(mp)
        finally:
            mp.undo()
        print(f"ok  {fn.__name__}")
    test_cross_origin_post_rejected()
    print("ok  test_cross_origin_post_rejected")
    print("\n3 passed")


if __name__ == "__main__":
    _run()
