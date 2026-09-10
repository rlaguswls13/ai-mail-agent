"""data/app.db 의 message_labels 테이블 - 사람이 직접 매긴 "정답" 카테고리 저장소.

규칙(classify.py)이 예측한 카테고리와 별개로, 사용자가 admin-ui `/label` 화면에서
"이 메일의 진짜 카테고리는 X" 라고 지정한 값을 모은다. eval 하네스가 규칙 정확도를
측정할 때 쓰는 ground-truth 데이터다.

categories / messages / action_runs 테이블과 같은 app.db 파일을 공유한다(이름이 안
겹침). 스키마 관리 방식은 config_store.py / mail_log_store.py 와 동일하다 - 멱등
CREATE TABLE IF NOT EXISTS + PRAGMA table_info 기반 _migrate + connect() 헬퍼.

export_jsonl() 이 쓰는 data/labels/manual.jsonl 은 eval 하네스와의 공유 계약 포맷이다
(한 줄에 JSON 하나): {"key", "sender", "subject", "label", "source", "labeled_at"}.
"""
import json
import sqlite3
from datetime import datetime
from pathlib import Path

NONE_LABEL = "__none__"  # "이 메일은 어떤 카테고리에도 해당 안 됨" 을 나타내는 명시적 라벨.

SCHEMA = """
CREATE TABLE IF NOT EXISTS message_labels (
    key TEXT PRIMARY KEY,
    account TEXT,
    uid TEXT,
    sender TEXT,
    subject TEXT,
    label TEXT NOT NULL,
    labeled_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual'
);
"""

# 나중에 추가된 컬럼 - 기존 message_labels 테이블에 PRAGMA table_info 로 존재 확인 후
# ALTER TABLE 로 얹는다 (sqlite 는 "ADD COLUMN IF NOT EXISTS" 가 없음).
EXTRA_COLUMNS: list[tuple[str, str]] = [
    ("source", "TEXT NOT NULL DEFAULT 'manual'"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(message_labels)")}
    for name, ddl in EXTRA_COLUMNS:
        if name not in cols:
            conn.execute(f"ALTER TABLE message_labels ADD COLUMN {name} {ddl}")


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def make_key(account: str, uid: str) -> str:
    """저장 키 포맷 - `acct:<account>:<uid>`."""
    return f"acct:{account}:{uid}"


def set_label(
    db_path: Path,
    account: str,
    uid: str,
    sender: str,
    subject: str,
    label: str,
    source: str = "manual",
) -> None:
    """한 메일의 정답 라벨을 upsert 한다. label 은 카테고리 이름 또는 `__none__`."""
    key = make_key(account, uid)
    labeled_at = datetime.now().isoformat(timespec="seconds")
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO message_labels (key, account, uid, sender, subject, label, labeled_at, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "account=excluded.account, uid=excluded.uid, sender=excluded.sender, "
            "subject=excluded.subject, label=excluded.label, labeled_at=excluded.labeled_at, "
            "source=excluded.source",
            (key, account, uid, sender, subject, label, labeled_at, source),
        )
        conn.commit()
    finally:
        conn.close()


def get_labels(db_path: Path) -> list[dict]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT key, account, uid, sender, subject, label, labeled_at, source "
            "FROM message_labels ORDER BY labeled_at DESC, key"
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "key": key,
            "account": account,
            "uid": uid,
            "sender": sender,
            "subject": subject,
            "label": label,
            "labeled_at": labeled_at,
            "source": source,
        }
        for key, account, uid, sender, subject, label, labeled_at, source in rows
    ]


def get_label_map(db_path: Path) -> dict[str, str]:
    """{key: label} - `/label` 화면이 <select> 를 preselect 할 때 쓴다."""
    conn = connect(db_path)
    try:
        rows = conn.execute("SELECT key, label FROM message_labels").fetchall()
    finally:
        conn.close()
    return {key: label for key, label in rows}


def delete_label(db_path: Path, key: str) -> None:
    conn = connect(db_path)
    try:
        conn.execute("DELETE FROM message_labels WHERE key = ?", (key,))
        conn.commit()
    finally:
        conn.close()


def count_labels(db_path: Path) -> int:
    conn = connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM message_labels").fetchone()[0]
    finally:
        conn.close()


def export_jsonl(db_path: Path, out_path: Path) -> int:
    """message_labels 를 eval 하네스 공유 계약 포맷(JSON Lines)으로 내보낸다.

    한 줄에 JSON 하나: {"key", "sender", "subject", "label", "source", "labeled_at"}.
    반환값은 기록한 줄 수. out_path 의 상위 디렉터리(data/labels/)는 만든다.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows = get_labels(db_path)
    with out_path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(
                json.dumps(
                    {
                        "key": r["key"],
                        "sender": r["sender"],
                        "subject": r["subject"],
                        "label": r["label"],
                        "source": r["source"],
                        "labeled_at": r["labeled_at"],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
    return len(rows)
