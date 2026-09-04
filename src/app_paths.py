"""데이터 디렉터리 경로 해석.

기본은 저장소 루트의 `data/`. 데스크톱 앱(패키징 실행)은 `src/`가 읽기 전용 리소스에
있으므로 환경변수 `MAIL_AGENT_DATA_DIR`로 쓰기 가능한 위치(`%APPDATA%\...\data`)를
가리킨다. CLI/개발 실행에서는 이 환경변수를 안 쓰므로 동작이 그대로다.
"""
import os
from pathlib import Path

_REPO_DATA = Path(__file__).resolve().parent.parent / "data"
ENV_DATA_DIR = "MAIL_AGENT_DATA_DIR"


def is_bundled() -> bool:
    """데스크톱 앱이 MAIL_AGENT_DATA_DIR 로 데이터 위치를 지정한 상태인가?

    (패키징 실행: src/ 는 읽기 전용 리소스, 쓰기는 전부 이 디렉터리로.)
    """
    return bool(os.environ.get(ENV_DATA_DIR))


def data_dir() -> Path:
    override = os.environ.get(ENV_DATA_DIR)
    d = Path(override) if override else _REPO_DATA
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path:
    return data_dir() / "app.db"


def dashboard_path() -> Path:
    return data_dir() / "dashboard.html"
