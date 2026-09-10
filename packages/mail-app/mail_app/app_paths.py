"""데이터 / 설정 디렉터리 경로 해석.

기본은 저장소 루트의 `data/` 와 `config/`. 데스크톱 앱(패키징 실행)은 패키지가 읽기
전용 리소스에 있으므로 환경변수 `MAIL_AGENT_DATA_DIR` 로 쓰기 가능한 위치
(`%APPDATA%\\...\\data`)를 가리킨다 - 이때 설정 파일도 그 아래에 둔다.
CLI/개발(`pip install -e`) 실행에서는 이 환경변수를 안 쓰므로 저장소 경로를 쓴다.

주의: non-editable `pip install` (site-packages 로 복사) 후에는 아래 `_REPO_ROOT`
계산이 저장소를 못 가리킨다 - 그 경우 반드시 `MAIL_AGENT_DATA_DIR` 를 설정할 것.
"""
import os
from pathlib import Path

# .../packages/mail-app/mail_app/app_paths.py -> parents[3] == 저장소 루트
_REPO_ROOT = Path(__file__).resolve().parents[3]
_REPO_DATA = _REPO_ROOT / "data"
_REPO_CONFIG = _REPO_ROOT / "config"
ENV_DATA_DIR = "MAIL_AGENT_DATA_DIR"


def is_bundled() -> bool:
    """데스크톱 앱이 MAIL_AGENT_DATA_DIR 로 데이터 위치를 지정한 상태인가?

    (패키징 실행: 패키지는 읽기 전용 리소스, 쓰기는 전부 이 디렉터리로.)
    """
    return bool(os.environ.get(ENV_DATA_DIR))


def data_dir() -> Path:
    override = os.environ.get(ENV_DATA_DIR)
    d = Path(override) if override else _REPO_DATA
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_dir() -> Path:
    """accounts.yaml / categories.json 백업이 사는 곳.

    번들 실행은 쓰기 가능한 data_dir() 아래, CLI/개발은 저장소의 config/.
    (데스크톱 앱은 자격증명을 암호화 볼트 + MAIL_AGENT_ACCOUNTS 로 주입하므로
    보통 이 경로의 accounts.yaml 을 읽지 않는다.)
    """
    d = data_dir() if is_bundled() else _REPO_CONFIG
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path:
    return data_dir() / "app.db"


def dashboard_path() -> Path:
    return data_dir() / "dashboard.html"
