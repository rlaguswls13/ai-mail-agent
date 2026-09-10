"""categories.json을 대체하는 SQLite 저장소.

classify.py/actions.py/mail_fetch.py는 categories가 어디서 왔는지 전혀 모른다 - 그냥
{name: {description, action, priority, keywords: {senders, title, contents}}} 모양의
dict를 받아서 돈다. 그래서 이 파일이 반환하는 load_categories()의 결과 모양만 그대로면
파이프라인 쪽 코드는 한 줄도 안 바꿔도 된다.

sort_order 컬럼은 classify.py가 하는 stable sort(같은 priority끼리는 categories.json에
적힌 순서를 따름)를 그대로 재현하기 위한 것 - SQL은 명시적 ORDER BY 없이는 행 순서를
보장하지 않는다.
"""
import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories (
    name TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT 'keep',
    priority TEXT NOT NULL DEFAULT 'NORMAL',
    senders TEXT NOT NULL DEFAULT '[]',
    domains TEXT NOT NULL DEFAULT '[]',
    title TEXT NOT NULL DEFAULT '[]',
    contents TEXT NOT NULL DEFAULT '[]',
    sort_order INTEGER NOT NULL
);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """기존 DB에 없는 컬럼을 추가한다 (SQLite는 IF NOT EXISTS 컬럼 추가가 없음)."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(categories)")}
    if "domains" not in cols:
        conn.execute("ALTER TABLE categories ADD COLUMN domains TEXT NOT NULL DEFAULT '[]'")


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def load_categories(db_path: Path) -> dict:
    """fetch_mail.py/classify.py가 쓰는 categories dict를 그대로 만들어서 반환한다."""
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name, description, action, priority, senders, domains, title, contents "
            "FROM categories ORDER BY sort_order"
        ).fetchall()
    finally:
        conn.close()

    return {
        name: {
            "description": description,
            "action": action,
            "priority": priority,
            "keywords": {
                "senders": json.loads(senders),
                "domains": json.loads(domains),
                "title": json.loads(title),
                "contents": json.loads(contents),
            },
        }
        for name, description, action, priority, senders, domains, title, contents in rows
    }


def list_categories(db_path: Path) -> list[dict]:
    """관리 화면용 - sort_order/원본 필드를 그대로 노출."""
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT name, description, action, priority, senders, domains, title, contents, sort_order "
            "FROM categories ORDER BY sort_order"
        ).fetchall()
    finally:
        conn.close()

    return [
        {
            "name": name,
            "description": description,
            "action": action,
            "priority": priority,
            "senders": json.loads(senders),
            "domains": json.loads(domains),
            "title": json.loads(title),
            "contents": json.loads(contents),
            "sort_order": sort_order,
        }
        for name, description, action, priority, senders, domains, title, contents, sort_order in rows
    ]


def get_category(db_path: Path, name: str) -> dict | None:
    matches = [c for c in list_categories(db_path) if c["name"] == name]
    return matches[0] if matches else None


def _next_sort_order(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(sort_order), -1) FROM categories").fetchone()
    return row[0] + 1


def add_category(
    db_path: Path,
    name: str,
    description: str,
    action: str,
    priority: str,
    senders: list[str],
    title: list[str],
    contents: list[str],
    domains: list[str] | None = None,
) -> None:
    conn = connect(db_path)
    try:
        sort_order = _next_sort_order(conn)
        conn.execute(
            "INSERT INTO categories (name, description, action, priority, senders, domains, title, contents, sort_order) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                name,
                description,
                action,
                priority,
                json.dumps(senders, ensure_ascii=False),
                json.dumps(domains or [], ensure_ascii=False),
                json.dumps(title, ensure_ascii=False),
                json.dumps(contents, ensure_ascii=False),
                sort_order,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def update_category(
    db_path: Path,
    name: str,
    description: str,
    action: str,
    priority: str,
    senders: list[str],
    title: list[str],
    contents: list[str],
    domains: list[str] | None = None,
) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            "UPDATE categories SET description=?, action=?, priority=?, senders=?, domains=?, title=?, contents=? "
            "WHERE name=?",
            (
                description,
                action,
                priority,
                json.dumps(senders, ensure_ascii=False),
                json.dumps(domains or [], ensure_ascii=False),
                json.dumps(title, ensure_ascii=False),
                json.dumps(contents, ensure_ascii=False),
                name,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def delete_category(db_path: Path, name: str) -> None:
    conn = connect(db_path)
    try:
        conn.execute("DELETE FROM categories WHERE name=?", (name,))
        conn.commit()
    finally:
        conn.close()


def export_to_json(db_path: Path, json_path: Path) -> None:
    """DB 현재 상태를 categories.json 형식으로 내보낸다 - git으로 추적/비교하기 좋은
    사람이 읽을 수 있는 백업용. 파이프라인은 이 파일을 더 이상 읽지 않는다."""
    categories = load_categories(db_path)
    json_path.write_text(json.dumps(categories, ensure_ascii=False, indent=2), encoding="utf-8")
