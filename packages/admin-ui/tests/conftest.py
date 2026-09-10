"""admin-ui 테스트용 격리 환경 - 임시 data 디렉터리로 app.db를 가리키게 한 뒤
admin_ui.admin_app 을 import 한다(모듈 로드 시 DB_PATH 가 고정되므로 import 전에 설정)."""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
for pkg in ("mail-core", "mail-app", "admin-ui"):
    sys.path.insert(0, str(_ROOT / "packages" / pkg))

_TMP = Path(tempfile.mkdtemp(prefix="admin-ui-test-"))
os.environ["MAIL_AGENT_DATA_DIR"] = str(_TMP)
os.environ.pop("MAIL_AGENT_ACCOUNTS", None)

# 최소 스키마로 app.db 생성 (connect() 가 status/is_read/message_id 를 얹는다).
_conn = sqlite3.connect(_TMP / "app.db")
_conn.executescript(
    "CREATE TABLE messages (account TEXT NOT NULL, uid TEXT NOT NULL, account_type TEXT NOT NULL,"
    " sender TEXT NOT NULL, subject TEXT NOT NULL, message_date TEXT, web_link TEXT,"
    " fetched_at TEXT NOT NULL, PRIMARY KEY (account, uid));"
)
_conn.close()


# --- 라우트 레벨 테스트 하네스 --------------------------------------------------
# admin_app(2078줄)을 블루프린트로 쪼갤 때 "동작을 안 바꿨다"를 값싸게 검증하려고
# 2026-09-10 에 만든 픽스처(분리는 완료됨, 커밋 4d90943..cab2ff9). 대표 데이터를 심고
# 모든 GET 라우트 200 + 필터/페이지네이션/카테고리 CRUD 를 보는 공용 픽스처.
# (import time 격리는 위쪽 모듈 레벨 코드가 이미 처리.)
import datetime as _datetime  # noqa: E402

import pytest  # noqa: E402

from mail_app import config_store as _config_store  # noqa: E402
from mail_app import mail_log_store as _mail_log_store  # noqa: E402

_NOW = _datetime.datetime.now()


def _iso(days_ago: int) -> str:
    return (_NOW - _datetime.timedelta(days=days_ago)).isoformat(timespec="seconds")


# 계정 3개 × 카테고리 매칭/미분류 × 기간(오늘/이번주/이번달/오래됨) × status 를 섞은 샘플.
_SAMPLE_MESSAGES = [
    dict(account="a@gmail.com", uid="1", account_type="gmail", sender="news@shop.com",
         subject="오늘 세일 안내", message_date=_iso(0), web_link="https://mail.google.com/x", message_id="<1@s>"),
    dict(account="a@gmail.com", uid="2", account_type="gmail", sender="hr@corp.com",
         subject="면접 일정 & 안내 <b>", message_date=_iso(2), web_link=None, message_id="<2@s>"),
    dict(account="a@gmail.com", uid="3", account_type="gmail", sender="random@nowhere.io",
         subject="분류 안 되는 메일", message_date=_iso(9), web_link=None, message_id="<3@s>"),
    dict(account="b@naver.com", uid="10", account_type="naver", sender="news@shop.com",
         subject="주간 뉴스레터", message_date=_iso(4), web_link=None, message_id="<10@s>"),
    dict(account="b@naver.com", uid="11", account_type="naver", sender="alert@bank.com",
         subject="보안 경고", message_date=_iso(20), web_link=None, message_id="<11@s>"),
    dict(account="c@gmail.com", uid="20", account_type="gmail", sender="hr@corp.com",
         subject="채용 확정", message_date=_iso(1), web_link=None, message_id="<20@s>"),
]

_SAMPLE_CATEGORIES = [
    dict(name="광고", description="쇼핑/뉴스레터", action="save", priority="LOW",
         senders=[], title=["세일", "뉴스레터"], contents=[], domains=["shop.com"]),
    dict(name="채용", description="면접/지원", action="keep", priority="HIGH",
         senders=["hr@corp.com"], title=["면접", "채용"], contents=[], domains=[]),
]


def _reset_db():
    conn = _mail_log_store.connect(_DB_PATH)
    conn.execute("DELETE FROM messages")
    conn.execute("DELETE FROM action_runs")
    try:
        conn.execute("DELETE FROM categories")
    except sqlite3.OperationalError:
        pass
    conn.commit()
    conn.close()


# admin_app 이 import 시점에 고정한 DB_PATH 를 그대로 쓴다.
from admin_ui.admin_app import DB_PATH as _DB_PATH  # noqa: E402


@pytest.fixture
def sample_data():
    """대표 메일/카테고리/액션런을 심은 뒤 테스트마다 초기화한다."""
    _reset_db()
    _mail_log_store.upsert_messages(_DB_PATH, _SAMPLE_MESSAGES)
    _mail_log_store.mark_message_status(_DB_PATH, "b@naver.com", ["11"], status="trashed")
    _mail_log_store.mark_message_status(_DB_PATH, "a@gmail.com", ["3"], status="archived")
    for c in _SAMPLE_CATEGORIES:
        _config_store.add_category(_DB_PATH, **c)
    _mail_log_store.log_action_run(
        _DB_PATH, run_at=_iso(0), dry_run=True, account="a@gmail.com",
        action="save", candidates=3, done=3, failed=0, folder="Archive", note=None,
    )
    yield
    _reset_db()


@pytest.fixture
def client():
    from admin_ui.admin_app import app

    app.config["TESTING"] = True
    return app.test_client()


@pytest.fixture
def local_headers():
    """같은 출처 + 로컬 호스트 (cross-origin/rebinding 가드 통과용)."""
    return {"Origin": "http://localhost", "Host": "localhost"}
