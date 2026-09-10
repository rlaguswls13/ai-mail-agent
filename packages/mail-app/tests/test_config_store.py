"""config_store.py 회귀 테스트 - 카테고리 CRUD, sort_order 안정성, domains 컬럼 마이그레이션,
categories.json 내보내기.

실행: repo 루트 또는 packages/mail-app/ 에서  py -m pytest
pytest 없이도 되도록 아래 main 가드로 assert 러너를 겸한다.
"""
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mail_app import config_store as cfg  # noqa: E402


def _fresh_db() -> Path:
    return Path(tempfile.mkdtemp()) / "app.db"


def _add(db, name, **over):
    kw = dict(
        description="", action="keep", priority="NORMAL",
        senders=[], title=[], contents=[], domains=[],
    )
    kw.update(over)
    cfg.add_category(db, name=name, **kw)


def test_add_and_load_roundtrip():
    db = _fresh_db()
    _add(db, "job", description="채용", action="keep", priority="NORMAL",
         senders=["a@x.com"], domains=["greetinghr.com"], title=["채용"], contents=["지원"])
    loaded = cfg.load_categories(db)
    assert set(loaded) == {"job"}
    j = loaded["job"]
    assert j["description"] == "채용"
    assert j["action"] == "keep"
    assert j["priority"] == "NORMAL"
    assert j["keywords"] == {
        "senders": ["a@x.com"], "domains": ["greetinghr.com"],
        "title": ["채용"], "contents": ["지원"],
    }


def test_load_categories_shape_matches_classify_contract():
    """classify.py는 {name: {description, action, priority, keywords: {...}}} 모양만 안다."""
    db = _fresh_db()
    _add(db, "ad")
    v = cfg.load_categories(db)["ad"]
    assert set(v) == {"description", "action", "priority", "keywords"}
    assert set(v["keywords"]) == {"senders", "domains", "title", "contents"}


def test_sort_order_follows_insertion_and_survives_update():
    db = _fresh_db()
    for n in ["c", "a", "b"]:
        _add(db, n)
    assert [c["name"] for c in cfg.list_categories(db)] == ["c", "a", "b"]
    # 수정해도 순서는 그대로 (sort_order 는 안 건드린다)
    cfg.update_category(db, name="c", description="x", action="trash",
                        priority="HIGH", senders=[], title=[], contents=[], domains=[])
    assert [c["name"] for c in cfg.list_categories(db)] == ["c", "a", "b"]
    assert list(cfg.load_categories(db)) == ["c", "a", "b"]


def test_delete_and_next_sort_order_keeps_climbing():
    db = _fresh_db()
    _add(db, "a")
    _add(db, "b")
    cfg.delete_category(db, "a")
    _add(db, "c")
    # a(0) 삭제 후 c 는 2 를 받는다 (MAX+1) - b(1) 앞으로 끼지 않는다
    assert [c["name"] for c in cfg.list_categories(db)] == ["b", "c"]
    assert cfg.get_category(db, "a") is None


def test_get_category_returns_none_for_missing():
    db = _fresh_db()
    _add(db, "a")
    assert cfg.get_category(db, "nope") is None
    assert cfg.get_category(db, "a")["name"] == "a"


def test_update_missing_category_is_noop():
    db = _fresh_db()
    _add(db, "a")
    cfg.update_category(db, name="ghost", description="x", action="trash",
                        priority="HIGH", senders=[], title=[], contents=[], domains=[])
    assert list(cfg.load_categories(db)) == ["a"]


def test_domains_defaults_to_empty_list_when_omitted():
    db = _fresh_db()
    cfg.add_category(db, name="a", description="", action="keep", priority="NORMAL",
                     senders=[], title=[], contents=[])  # domains 인자 자체를 안 넘김
    assert cfg.load_categories(db)["a"]["keywords"]["domains"] == []
    cfg.update_category(db, name="a", description="", action="keep", priority="NORMAL",
                        senders=[], title=[], contents=[])
    assert cfg.load_categories(db)["a"]["keywords"]["domains"] == []


def test_domains_column_migration_from_old_schema():
    """domains 컬럼 없이 만들어진 기존 app.db 를 connect() 가 안전하게 ALTER 하는지."""
    db = _fresh_db()
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE categories (name TEXT PRIMARY KEY, description TEXT NOT NULL DEFAULT '',"
        " action TEXT NOT NULL DEFAULT 'keep', priority TEXT NOT NULL DEFAULT 'NORMAL',"
        " senders TEXT NOT NULL DEFAULT '[]', title TEXT NOT NULL DEFAULT '[]',"
        " contents TEXT NOT NULL DEFAULT '[]', sort_order INTEGER NOT NULL);"
    )
    conn.execute(
        "INSERT INTO categories (name, senders, title, contents, sort_order) "
        "VALUES ('legacy', '[\"a@x.com\"]', '[]', '[]', 0)"
    )
    conn.commit()
    conn.close()

    loaded = cfg.load_categories(db)
    assert loaded["legacy"]["keywords"]["domains"] == []
    assert loaded["legacy"]["keywords"]["senders"] == ["a@x.com"]
    # 마이그레이션 후에도 정상적으로 쓰기 가능
    cfg.update_category(db, name="legacy", description="", action="keep", priority="NORMAL",
                        senders=["a@x.com"], title=[], contents=[], domains=["x.com"])
    assert cfg.load_categories(db)["legacy"]["keywords"]["domains"] == ["x.com"]


def test_export_to_json_roundtrips_unicode():
    db = _fresh_db()
    _add(db, "광고", description="스팸", action="trash", title=["(광고)"])
    out = Path(tempfile.mkdtemp()) / "categories.json"
    cfg.export_to_json(db, out)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data == cfg.load_categories(db)
    assert "(광고)" in out.read_text(encoding="utf-8")  # ensure_ascii=False


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run()
