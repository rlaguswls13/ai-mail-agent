"""mail_log_store.py 회귀 테스트 — message_id 컬럼, status 필터, 되돌리기/영구삭제 헬퍼.

실행: repo 루트 또는 packages/mail-app/ 에서  py -m pytest
pytest 없이도 되도록 아래 main 가드로 assert 러너를 겸한다.
"""
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mail_app import mail_log_store as store  # noqa: E402

D1 = datetime(2026, 1, 10)
D2 = datetime(2026, 1, 20)


def _fresh_db() -> Path:
    tmp = Path(tempfile.mkdtemp()) / "app.db"
    return tmp


def _msg(uid, *, mid=None, date="2026-01-15T09:00:00", account="a@x.com"):
    return {
        "account": account,
        "uid": uid,
        "account_type": "gmail",
        "sender": "s@x.com",
        "subject": f"subject {uid}",
        "message_date": date,
        "web_link": None,
        "message_id": mid,
    }


def test_upsert_and_query_roundtrip_includes_message_id():
    db = _fresh_db()
    store.upsert_messages(db, [_msg("1", mid="<a@x>"), _msg("2", mid=None)])
    rows = {m["uid"]: m for m in store.query_messages(db, D1, D2)}
    assert rows["1"]["message_id"] == "<a@x>"
    assert rows["2"]["message_id"] is None
    assert rows["1"]["status"] == "active"
    assert rows["1"]["is_read"] is False


def test_query_messages_status_filter():
    db = _fresh_db()
    store.upsert_messages(db, [_msg("1"), _msg("2"), _msg("3")])
    store.mark_message_status(db, "a@x.com", ["2"], status="archived")
    store.mark_message_status(db, "a@x.com", ["3"], status="trashed")

    assert {m["uid"] for m in store.query_messages(db, D1, D2, status="active")} == {"1"}
    assert {m["uid"] for m in store.query_messages(db, D1, D2, status="archived")} == {"2"}
    assert {m["uid"] for m in store.query_messages(db, D1, D2, status="trashed")} == {"3"}
    assert {m["uid"] for m in store.query_messages(db, D1, D2, status=None)} == {"1", "2", "3"}


def test_mark_message_status_is_read():
    db = _fresh_db()
    store.upsert_messages(db, [_msg("1")])
    store.mark_message_status(db, "a@x.com", ["1"], is_read=True)
    assert store.query_messages(db, D1, D2)[0]["is_read"] is True


def test_message_ids_for():
    db = _fresh_db()
    store.upsert_messages(db, [_msg("1", mid="<a@x>"), _msg("2", mid=None), _msg("3", mid="<c@x>")])
    got = store.message_ids_for(db, "a@x.com", ["1", "2", "3", "missing"])
    assert got == {"1": "<a@x>", "2": None, "3": "<c@x>"}
    assert store.message_ids_for(db, "a@x.com", []) == {}


def test_delete_messages():
    db = _fresh_db()
    store.upsert_messages(db, [_msg("1"), _msg("2"), _msg("3")])
    removed = store.delete_messages(db, "a@x.com", ["1", "3"])
    assert removed == 2
    assert {m["uid"] for m in store.query_messages(db, D1, D2)} == {"2"}
    assert store.delete_messages(db, "a@x.com", []) == 0
    # 다른 계정 uid는 안 지운다
    store.upsert_messages(db, [_msg("9", account="b@x.com")])
    assert store.delete_messages(db, "a@x.com", ["9"]) == 0


def test_message_id_column_migration_from_old_schema():
    """message_id 없이 만들어진 기존 app.db를 connect()가 안전하게 ALTER 하는지."""
    db = _fresh_db()
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE messages (account TEXT NOT NULL, uid TEXT NOT NULL, "
        "account_type TEXT NOT NULL, sender TEXT NOT NULL, subject TEXT NOT NULL, "
        "message_date TEXT, web_link TEXT, fetched_at TEXT NOT NULL, "
        "PRIMARY KEY (account, uid));"
    )
    conn.execute(
        "INSERT INTO messages VALUES ('a@x.com','1','gmail','s','subj','2026-01-15T09:00:00',NULL,'2026-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()

    # connect()가 status/is_read/message_id 컬럼을 얹어야 한다
    rows = store.query_messages(db, D1, D2)
    assert len(rows) == 1
    assert rows[0]["message_id"] is None
    assert rows[0]["status"] == "active"


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run()
