"""계정 목록 로딩 - 데스크톱 앱의 암호화 볼트(환경변수 주입) 우선, accounts.yaml 폴백."""
import json
import os
from pathlib import Path

IMAP_SERVERS = {
    "gmail": "imap.gmail.com",
    "naver": "imap.naver.com",
    "outlook": "outlook.office365.com",
}

# desktop/ Electron 셸이 safeStorage 볼트를 복호화해서 이 환경변수(JSON 배열)로 넘겨준다.
# 값이 있으면 accounts.yaml 대신 이걸 쓴다 - 평문 파일 없이 파이프라인이 돈다.
ENV_ACCOUNTS_VAR = "MAIL_AGENT_ACCOUNTS"


def _valid_accounts(accounts: list[dict]) -> list[dict]:
    return [
        a
        for a in accounts
        if a.get("type") in IMAP_SERVERS and a.get("user") and a.get("password")
    ]


def accounts_from_env() -> list[dict] | None:
    """MAIL_AGENT_ACCOUNTS 환경변수가 있으면 파싱해서 반환, 없으면 None.

    형식은 accounts.yaml 과 같은 필드의 JSON 배열: [{"type","user","password"}, ...]
    """
    raw = os.environ.get(ENV_ACCOUNTS_VAR)
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return _valid_accounts([a for a in parsed if isinstance(a, dict)])


def env_mode() -> bool:
    """데스크톱 앱이 자격증명을 환경변수로 주입한 상태인가? (계정 파일 쓰기를 막는 데 씀)"""
    return bool(os.environ.get(ENV_ACCOUNTS_VAR))


def load_accounts(path: Path) -> list[dict]:
    """계정 목록을 반환한다.

    1순위: MAIL_AGENT_ACCOUNTS 환경변수(데스크톱 앱의 safeStorage 볼트에서 주입).
    2순위: config/accounts.yaml (개발/CLI 폴백 - 호출자가 경로를 넘겨준다).

    yaml 은 이 프로젝트가 표준 라이브러리만 쓰기로 했으므로(PyYAML 없이) 아래의
    제한된 구조만 지원하는 최소 파서를 직접 구현한다:

        accounts:
          - type: gmail
            user: someone@gmail.com
            password: "xxxx xxxx xxxx xxxx"
    """
    from_env = accounts_from_env()
    if from_env is not None:
        return from_env

    if not path.exists():
        return []

    accounts: list[dict] = []
    current: dict | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        stripped = line.strip()
        if not stripped or stripped == "accounts:":
            continue

        if stripped.startswith("- "):
            if current:
                accounts.append(current)
            current = {}
            stripped = stripped[2:].strip()

        if current is None or ":" not in stripped:
            continue

        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value:
            current[key] = value

    if current:
        accounts.append(current)

    return _valid_accounts(accounts)


def save_accounts(path: Path, accounts: list[dict]) -> None:
    """계정 목록을 load_accounts()가 읽을 수 있는 같은 minimal YAML 포맷으로 다시 쓴다.

    관리 화면(admin_app.py의 /settings)에서 CRUD로 저장할 때 쓴다 - 파일을 직접 손으로
    쓰던 걸 대체하는 용도라, 기존 파일에 사람이 남긴 주석은 저장할 때 사라진다(이 파일은
    이제 화면에서만 관리한다는 전제).

    데스크톱 앱 모드(MAIL_AGENT_ACCOUNTS 주입)에서는 정본이 암호화 볼트라 파일 쓰기를
    막는다 - 계정 편집은 앱의 설정창에서 한다.
    """
    if env_mode():
        raise RuntimeError(
            "데스크톱 앱 모드에서는 계정을 파일로 저장할 수 없습니다 - 앱 설정창에서 편집하세요."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["accounts:"]
    for a in accounts:
        lines.append(f"  - type: {a['type']}")
        lines.append(f"    user: {a['user']}")
        lines.append(f'    password: "{a["password"]}"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def add_account(path: Path, type: str, user: str, password: str) -> None:
    accounts = load_accounts(path)
    accounts.append({"type": type, "user": user, "password": password})
    save_accounts(path, accounts)


def update_account(path: Path, original_user: str, type: str, user: str, password: str) -> None:
    """user(이메일)로 계정을 찾아 갱신한다 - 이메일이 사실상의 고유 식별자다."""
    accounts = load_accounts(path)
    for a in accounts:
        if a["user"] == original_user:
            a["type"], a["user"], a["password"] = type, user, password
            break
    save_accounts(path, accounts)


def delete_account(path: Path, user: str) -> None:
    accounts = [a for a in load_accounts(path) if a["user"] != user]
    save_accounts(path, accounts)
