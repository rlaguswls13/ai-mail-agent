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
