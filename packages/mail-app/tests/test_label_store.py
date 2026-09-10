"""label_store.py 회귀 테스트 - upsert/dedupe, get_label_map, delete, JSONL 내보내기 라운드트립.

실행: repo 루트 또는 packages/mail-app/ 에서  py -m pytest
pytest 없이도 되도록 아래 main 가드로 assert 러너를 겸한다.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mail_app import label_store as ls  # noqa: E402


def _fresh_db() -> Path:
    return Path(tempfile.mkdtemp()) / "app.db"


def test_set_label_and_get_labels_roundtrip():
    db = _fresh_db()
    ls.set_label(db, "a@gmail.com", "1", "news@shop.com", "세일", "광고")
    rows = ls.get_labels(db)
    assert len(rows) == 1
    r = rows[0]
    assert r["key"] == "acct:a@gmail.com:1"
    assert r["account"] == "a@gmail.com"
    assert r["uid"] == "1"
    assert r["label"] == "광고"
    assert r["source"] == "manual"
    assert r["labeled_at"]


def test_set_label_upserts_same_key():
    db = _fresh_db()
    ls.set_label(db, "a@gmail.com", "1", "s", "subj", "광고")
    ls.set_label(db, "a@gmail.com", "1", "s2", "subj2", "채용", source="import")
    assert ls.count_labels(db) == 1
    r = ls.get_labels(db)[0]
    assert r["label"] == "채용"
    assert r["sender"] == "s2"
    assert r["source"] == "import"


def test_none_label_is_stored_verbatim():
    db = _fresh_db()
    ls.set_label(db, "b@naver.com", "10", "x@y.com", "무엇", ls.NONE_LABEL)
    assert ls.get_label_map(db) == {"acct:b@naver.com:10": "__none__"}


def test_get_label_map_and_delete():
    db = _fresh_db()
    ls.set_label(db, "a@gmail.com", "1", "s", "subj", "광고")
    ls.set_label(db, "a@gmail.com", "2", "s", "subj", "채용")
    assert ls.get_label_map(db) == {"acct:a@gmail.com:1": "광고", "acct:a@gmail.com:2": "채용"}
    ls.delete_label(db, "acct:a@gmail.com:1")
    assert ls.get_label_map(db) == {"acct:a@gmail.com:2": "채용"}
    assert ls.count_labels(db) == 1


def test_export_jsonl_roundtrip_and_contract():
    db = _fresh_db()
    ls.set_label(db, "a@gmail.com", "1", "news@shop.com", "세일 & 안내", "광고")
    ls.set_label(db, "c@gmail.com", "20", "hr@corp.com", "채용 확정", ls.NONE_LABEL)
    out = Path(tempfile.mkdtemp()) / "labels" / "manual.jsonl"
    n = ls.export_jsonl(db, out)
    assert n == 2
    assert out.parent.is_dir()
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert set(rec) == {"key", "sender", "subject", "label", "source", "labeled_at"}
    by_key = {json.loads(l)["key"]: json.loads(l) for l in lines}
    assert by_key["acct:a@gmail.com:1"]["label"] == "광고"
    assert by_key["acct:c@gmail.com:20"]["label"] == "__none__"
    assert "세일 & 안내" in out.read_text(encoding="utf-8")  # ensure_ascii=False


def test_migrate_adds_source_column_to_legacy_table():
    import sqlite3

    db = _fresh_db()
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE message_labels (key TEXT PRIMARY KEY, account TEXT, uid TEXT,"
        " sender TEXT, subject TEXT, label TEXT NOT NULL, labeled_at TEXT NOT NULL);"
    )
    conn.execute(
        "INSERT INTO message_labels (key, account, uid, sender, subject, label, labeled_at) "
        "VALUES ('acct:a@x.com:1', 'a@x.com', '1', 's', 'subj', '광고', '2026-01-01T00:00:00')"
    )
    conn.commit()
    conn.close()

    assert ls.get_labels(db)[0]["source"] == "manual"
    ls.set_label(db, "a@x.com", "1", "s", "subj", "채용")
    assert ls.get_labels(db)[0]["label"] == "채용"


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run()
