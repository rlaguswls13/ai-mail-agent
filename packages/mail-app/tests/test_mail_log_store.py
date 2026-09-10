"""mail_log_store.py 회귀 테스트 - message_id 컬럼, status 필터, 되돌리기/영구삭제 헬퍼.

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


def test_bulk_ops_chunk_past_sqlite_var_limit():
    """400개(_SQL_VARS_CHUNK) 넘는 uid 리스트도 청크로 나눠 안전하게 처리."""
    db = _fresh_db()
    many = [_msg(str(i)) for i in range(950)]
    store.upsert_messages(db, many)
    store.mark_message_status(db, "a@x.com", [str(i) for i in range(950)], status="archived")
    assert len(store.query_messages(db, D1, D2, status="archived")) == 950
    assert store.delete_messages(db, "a@x.com", [str(i) for i in range(950)]) == 950


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


def test_reconcile_missing_deletes_only_active_and_gone():
    db = _fresh_db()
    store.upsert_messages(db, [_msg("1"), _msg("2"), _msg("3"), _msg("4")])
    store.mark_message_status(db, "a@x.com", ["3"], status="archived")
    store.mark_message_status(db, "a@x.com", ["4"], status="trashed")
    # 서버에는 1 만 살아있다. 2 는 사용자가 웹메일에서 직접 삭제. 3/4 는 우리 액션으로 이동.
    removed = store.reconcile_missing(db, "a@x.com", D1, D2, live_uids=["1"])
    assert removed == 1
    assert {m["uid"] for m in store.query_messages(db, D1, D2)} == {"1", "3", "4"}


def test_reconcile_missing_empty_live_uids_clears_active_in_range():
    """live_uids 가 비었다는 건 '이 구간에 진짜 메일이 없다' - active 는 전부 정리된다."""
    db = _fresh_db()
    store.upsert_messages(db, [_msg("1"), _msg("2")])
    removed = store.reconcile_missing(db, "a@x.com", D1, D2, live_uids=[])
    assert removed == 2
    assert store.query_messages(db, D1, D2) == []


def test_reconcile_missing_respects_date_bounds():
    db = _fresh_db()
    store.upsert_messages(db, [
        _msg("old", date="2025-06-01T00:00:00"),
        _msg("in", date="2026-01-15T00:00:00"),
    ])
    # 구간 밖(old)은 손대지 않는다
    removed = store.reconcile_missing(db, "a@x.com", D1, D2, live_uids=[])
    assert removed == 1
    assert {m["uid"] for m in store.query_messages(db, datetime(2020, 1, 1), None)} == {"old"}


def test_rebind_uid_moves_row_to_new_uid_and_reactivates():
    db = _fresh_db()
    store.upsert_messages(db, [_msg("10", mid="<a@x>")])
    store.mark_message_status(db, "a@x.com", ["10"], status="archived")
    store.rebind_uid(db, "a@x.com", "10", "900")
    rows = {m["uid"]: m for m in store.query_messages(db, D1, D2)}
    assert "10" not in rows
    assert rows["900"]["status"] == "active"
    assert rows["900"]["message_id"] == "<a@x>"


def test_rebind_uid_same_uid_just_reactivates():
    db = _fresh_db()
    store.upsert_messages(db, [_msg("5")])
    store.mark_message_status(db, "a@x.com", ["5"], status="archived")
    store.rebind_uid(db, "a@x.com", "5", "5")
    assert store.query_messages(db, D1, D2)[0]["status"] == "active"


def test_rebind_uid_replaces_when_new_uid_row_already_exists():
    """드묾: rebind 전에 fetch 가 새 INBOX uid 행을 이미 넣은 경우 REPLACE."""
    db = _fresh_db()
    store.upsert_messages(db, [_msg("10", mid="<a@x>"), _msg("900", mid="<a@x>")])
    store.mark_message_status(db, "a@x.com", ["10"], status="archived")
    store.rebind_uid(db, "a@x.com", "10", "900")
    rows = {m["uid"]: m for m in store.query_messages(db, D1, D2)}
    assert set(rows) == {"900"}
    assert rows["900"]["status"] == "active"


def test_last_message_date_strips_timezone_offset():
    db = _fresh_db()
    store.upsert_messages(db, [
        _msg("1", date="2026-01-15T09:00:00+00:00"),
        _msg("2", date="2026-01-16T10:00:00+09:00"),
    ])
    last = store.last_message_date(db, "a@x.com")
    assert last is not None and last.tzinfo is None
    # now() 등 naive datetime 과 섞어 비교해도 TypeError 안 남
    assert last < datetime.now()


def test_last_message_date_none_for_new_account():
    db = _fresh_db()
    store.upsert_messages(db, [_msg("1")])
    assert store.last_message_date(db, "other@x.com") is None


def test_latest_action_summary_returns_only_newest_batch():
    db = _fresh_db()
    store.log_action_run(db, "2026-01-01T00:00:00", True, "a@x.com", "trash", 5, 0, 0, None, None)
    store.log_action_run(db, "2026-01-02T00:00:00", False, "a@x.com", "trash", 3, 3, 0, "[Gmail]/Trash", None)
    store.log_action_run(db, "2026-01-02T00:00:00", False, "b@x.com", "save", 2, 1, 1, "Archive", "일부 실패")
    summ = store.latest_action_summary(db)
    assert summ["run_at"] == "2026-01-02T00:00:00"
    assert summ["dry_run"] is False
    assert summ["accounts"]["a@x.com"]["trash"] == {
        "candidates": 3, "done": 3, "failed": 0, "folder": "[Gmail]/Trash"
    }
    assert summ["accounts"]["b@x.com"]["save"]["note"] == "일부 실패"


def test_latest_action_summary_empty_db():
    db = _fresh_db()
    assert store.latest_action_summary(db) == {"run_at": None, "dry_run": True, "accounts": {}}


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run()
