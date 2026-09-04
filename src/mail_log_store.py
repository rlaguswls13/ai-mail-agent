"""data/app.db(SQLite)의 messages/action_runs 테이블 — 원본 메일 영구 저장소 + 액션 실행 로그.

같은 파일의 categories 테이블(config_store.py가 다룸)과 이름이 겹치지 않아서 하나의
SQLite 파일을 공유해서 쓴다. report_*.json/latest.json을 완전히 대체한다. fetch_mail.py는
매 실행마다 조회한 원본 메일(제목/발신인/날짜/uid)을 여기에 upsert하고, generate_html.py는
원하는 기간을 이 DB에서 쿼리해서 **현재** categories 규칙으로 분류해 대시보드를 만든다 —
그래서 카테고리 규칙을 나중에 바꿔도 IMAP을 다시 조회하지 않고 과거 메일을 재분류할
수 있다.

action_runs는 실제로 메일함에 접속해서 시도한 액션(휴지통 이동/보관 등)의 결과 로그다.
이건 "그 순간 실제 메일함 상태"에 대한 기록이라 임의 기간에 대해 소급 계산할 수 없다 —
그래서 messages와 달리 매 실행 결과를 계속 쌓아두고, 대시보드는 그중 가장 최근 실행
분만 보여준다.
"""
import sqlite3
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    account TEXT NOT NULL,
    uid TEXT NOT NULL,
    account_type TEXT NOT NULL,
    sender TEXT NOT NULL,
    subject TEXT NOT NULL,
    message_date TEXT,
    web_link TEXT,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (account, uid)
);
CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(message_date);
CREATE INDEX IF NOT EXISTS idx_messages_account_date ON messages(account, message_date);

CREATE TABLE IF NOT EXISTS action_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT NOT NULL,
    dry_run INTEGER NOT NULL,
    account TEXT NOT NULL,
    action TEXT NOT NULL,
    candidates INTEGER NOT NULL,
    done INTEGER NOT NULL,
    failed INTEGER NOT NULL,
    folder TEXT,
    note TEXT
);
CREATE INDEX IF NOT EXISTS idx_action_runs_run_at ON action_runs(run_at);
"""

# messages 테이블에 나중에 추가된 컬럼들 — 기존 app.db(이미 실 메일 수천 건이 쌓여있음)에
# ALTER TABLE로 안전하게 얹는다. sqlite는 "ADD COLUMN IF NOT EXISTS"가 없어서
# PRAGMA table_info로 직접 존재 여부를 확인한다. status/is_read는 액션 처리 결과를
# 반영하는 용도(3b)이고, status='active'가 기본값이라 기존 행은 전부 "아직 처리 안 됨"
# 상태로 자연스럽게 채워진다.
EXTRA_COLUMNS = [
    ("status", "TEXT NOT NULL DEFAULT 'active'"),
    ("is_read", "INTEGER NOT NULL DEFAULT 0"),
]


def _ensure_columns(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    for name, ddl in EXTRA_COLUMNS:
        if name not in existing:
            conn.execute(f"ALTER TABLE messages ADD COLUMN {name} {ddl}")


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    _ensure_columns(conn)
    conn.commit()
    return conn


def upsert_messages(db_path: Path, messages: list[dict]) -> None:
    """messages: mail_fetch.fetch_account_headers()가 반환하는 dict 리스트
    (account/account_type/uid/sender/subject/message_date/web_link 포함).

    같은 (account, uid)는 실제 메일 내용이 안 변하는 값들이라 INSERT OR IGNORE로
    처리한다 — 겹치는 기간을 다시 조회해도 중복/갱신 걱정 없이 그냥 스킵된다.
    """
    if not messages:
        return
    fetched_at = datetime.now().isoformat(timespec="seconds")
    conn = connect(db_path)
    try:
        conn.executemany(
            "INSERT OR IGNORE INTO messages "
            "(account, uid, account_type, sender, subject, message_date, web_link, fetched_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    m["account"],
                    m["uid"],
                    m["account_type"],
                    m["sender"],
                    m["subject"],
                    m.get("message_date"),
                    m.get("web_link"),
                    fetched_at,
                )
                for m in messages
                if m.get("uid")
            ],
        )
        conn.commit()
    finally:
        conn.close()


def query_messages(
    db_path: Path,
    since: datetime,
    until: datetime | None = None,
    account: str | None = None,
) -> list[dict]:
    """[since, until) 기간(message_date 기준, 반개구간)의 메일을 classify()가 바로
    쓸 수 있는 dict 모양으로 반환한다. until=None이면 지금까지 전부."""
    conn = connect(db_path)
    try:
        query = (
            "SELECT account, uid, sender, subject, web_link, message_date, status, is_read "
            "FROM messages WHERE message_date >= ?"
        )
        params: list = [since.isoformat()]
        if until is not None:
            query += " AND message_date < ?"
            params.append(until.isoformat())
        if account is not None:
            query += " AND account = ?"
            params.append(account)
        query += " ORDER BY message_date"
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()

    return [
        {
            "account": acc,
            "uid": uid,
            "sender": sender,
            "subject": subject,
            "web_link": web_link,
            "message_date": message_date,
            "status": status,
            "is_read": bool(is_read),
        }
        for acc, uid, sender, subject, web_link, message_date, status, is_read in rows
    ]


def mark_message_status(
    db_path: Path,
    account: str,
    uids: list[str],
    status: str | None = None,
    is_read: bool | None = None,
) -> None:
    """실제 IMAP 액션이 성공한 uid만 골라 상태를 갱신한다 (3b: 액션 후 동기화).

    status(trash/save 액션 -> 'trashed'/'archived')와 is_read(read 액션 -> True) 중
    필요한 것만 넘기면 된다. 성공한 uid만 넘겨야 한다 — 실패한 메일은 여전히 INBOX에
    남아있으므로 상태를 바꾸면 안 된다.
    """
    if not uids:
        return
    sets = []
    params: list = []
    if status is not None:
        sets.append("status = ?")
        params.append(status)
    if is_read is not None:
        sets.append("is_read = ?")
        params.append(int(is_read))
    if not sets:
        return
    conn = connect(db_path)
    try:
        placeholders = ",".join("?" * len(uids))
        conn.execute(
            f"UPDATE messages SET {', '.join(sets)} WHERE account = ? AND uid IN ({placeholders})",
            [*params, account, *uids],
        )
        conn.commit()
    finally:
        conn.close()


def reconcile_missing(
    db_path: Path,
    account: str,
    since: datetime,
    until: datetime | None,
    live_uids: list[str],
) -> int:
    """이번 IMAP 조회로 확인된 [since, until) 구간의 실제 살아있는 uid 목록과 대조해서,
    아직 'active' 상태인데 더 이상 INBOX에 없는(=사용자가 웹메일/다른 클라이언트에서
    직접 지운) 메일 행을 SQLite에서 삭제한다 (3c). status가 이미 trashed/archived인
    행은 우리 액션으로 정상적으로 없어진 것이라 제외 — 배지 표시를 위해 남겨둔다.

    반드시 이번 조회에서 나온 live_uids와 비교해야 한다 — live_uids가 비어있다는 건
    "이 구간에 진짜 메일이 하나도 없다"는 뜻일 수도 있으므로(정상적으로 전부 삭제
    대상), 호출부가 빈 리스트를 걸러내면 안 된다.
    """
    conn = connect(db_path)
    try:
        query = (
            "SELECT uid FROM messages WHERE account = ? AND status = 'active' "
            "AND message_date >= ?"
        )
        params: list = [account, since.isoformat()]
        if until is not None:
            query += " AND message_date < ?"
            params.append(until.isoformat())
        stored_uids = {row[0] for row in conn.execute(query, params).fetchall()}

        missing = stored_uids - set(live_uids)
        if not missing:
            return 0

        placeholders = ",".join("?" * len(missing))
        conn.execute(
            f"DELETE FROM messages WHERE account = ? AND uid IN ({placeholders})",
            [account, *missing],
        )
        conn.commit()
        return len(missing)
    finally:
        conn.close()


def last_message_date(db_path: Path, account: str) -> datetime | None:
    """해당 계정의 messages 테이블에 저장된 가장 최근 message_date를 반환한다.

    fetch_mail.py가 --since 없이 실행될 때 이 값을 시작점으로 써서, 고정된
    "어제부터"가 아니라 "그 계정이 마지막으로 갱신된 시점부터" 이어서 조회하게 한다
    (며칠 건너뛰고 실행해도 그 사이 메일을 놓치지 않는다). 저장된 메일이 아직 없는
    신규 계정이면 None을 반환 — 호출부가 폴백 기본값(어제)을 쓴다.
    """
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT MAX(message_date) FROM messages WHERE account = ?", (account,)
        ).fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        return None
    try:
        parsed = datetime.fromisoformat(row[0])
    except ValueError:
        return None
    # message_date는 원본 메일의 Date 헤더에서 온 값이라 오프셋이 있을 수 있는데(예:
    # "+00:00"), 나머지 파이프라인(now(), --since 파싱 등)은 전부 offset-naive
    # datetime을 쓴다 — 둘을 섞어 비교하면 TypeError가 난다. IMAP SINCE도 어차피
    # 날짜(일) 단위만 보고 중복은 upsert_messages()의 INSERT OR IGNORE가 막아주므로,
    # 오프셋을 버려도(재변환하지 않고 그냥 떼도) 안전하다.
    return parsed.replace(tzinfo=None)


def distinct_accounts(db_path: Path, since: datetime, until: datetime | None = None) -> list[str]:
    conn = connect(db_path)
    try:
        query = "SELECT DISTINCT account FROM messages WHERE message_date >= ?"
        params: list = [since.isoformat()]
        if until is not None:
            query += " AND message_date < ?"
            params.append(until.isoformat())
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def account_type_for(db_path: Path, account: str) -> str | None:
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT account_type FROM messages WHERE account = ? LIMIT 1", (account,)
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def log_action_run(
    db_path: Path,
    run_at: str,
    dry_run: bool,
    account: str,
    action: str,
    candidates: int,
    done: int,
    failed: int,
    folder: str | None,
    note: str | None,
) -> None:
    conn = connect(db_path)
    try:
        conn.execute(
            "INSERT INTO action_runs "
            "(run_at, dry_run, account, action, candidates, done, failed, folder, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_at, int(dry_run), account, action, candidates, done, failed, folder, note),
        )
        conn.commit()
    finally:
        conn.close()


def latest_action_summary(db_path: Path) -> dict:
    """가장 최근 실행 배치(run_at 최댓값)의 action_runs만 골라서 report의
    actions 구조({dry_run, accounts: {user: {action: {...}}}})와 같은 모양으로 반환한다.
    """
    conn = connect(db_path)
    try:
        latest = conn.execute("SELECT MAX(run_at) FROM action_runs").fetchone()[0]
        if latest is None:
            return {"run_at": None, "dry_run": True, "accounts": {}}
        rows = conn.execute(
            "SELECT dry_run, account, action, candidates, done, failed, folder, note "
            "FROM action_runs WHERE run_at = ?",
            (latest,),
        ).fetchall()
    finally:
        conn.close()

    accounts: dict = {}
    dry_run = True
    for row_dry_run, account, action, candidates, done, failed, folder, note in rows:
        dry_run = bool(row_dry_run)
        entry = {"candidates": candidates, "done": done, "failed": failed, "folder": folder}
        if note:
            entry["note"] = note
        accounts.setdefault(account, {})[action] = entry

    return {"run_at": latest, "dry_run": dry_run, "accounts": accounts}
