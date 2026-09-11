"""계정 목록 로딩 - 데스크톱 앱의 암호화 볼트(환경변수 주입) 우선, accounts.yaml 폴백.

accounts.yaml 의 비밀번호는 평문이 아니라 ``password_enc`` (mail_core.crypto로 암호화)로
저장한다. load_accounts() 가 이를 복호화해 돌려주므로 호출부는 여전히 ``account["password"]``
로 평문을 본다 - 디스크에만 암호화된 값이 남는다."""
import json
import os
import sys
from pathlib import Path
from typing import TextIO

from mail_core import crypto

IMAP_SERVERS = {
    "gmail": "imap.gmail.com",
    "naver": "imap.naver.com",
    "outlook": "outlook.office365.com",
    "daum": "imap.daum.net",
}
PROVIDER_LABEL = {"gmail": "Gmail", "naver": "Naver", "outlook": "Outlook", "daum": "Daum"}

# desktop/ Electron 셸이 safeStorage 볼트를 복호화해서 JSON 배열로 넘겨준다. 값이
# 있으면 accounts.yaml 대신 이걸 쓴다 - 평문 파일 없이 파이프라인이 돈다.
# 전달: admin_ui 진입점(`_entrypoint.resolve_accounts_from_stdin`)이 자식 stdin 첫
# 줄로 받아 이 환경변수에 채운다(env=@stdin 센티널). 이후 서브프로세스는 env 상속.
ENV_ACCOUNTS_VAR = "MAIL_AGENT_ACCOUNTS"

# 계정별 IMAP 인증 방식.
AUTH_METHODS = ("password", "xoauth2")


def account_label(account: dict) -> str:
    """계정의 화면 표시용 이름 - 별칭(alias)이 있으면 그걸, 없으면 제공자 이름(Gmail/Naver/Outlook).

    같은 제공자 계정을 여러 개 등록해도(예: Gmail 앱비번용/OAuth용) 화면에서 구분할 수
    있도록 사용자가 붙이는 표시 이름. 이메일 주소 자체는 바뀌지 않는다 - 1계정=1별칭."""
    alias = str(account.get("alias") or "").strip()
    if alias:
        return alias
    t = account.get("type")
    return PROVIDER_LABEL.get(t, t or "")


def account_auth(account: dict) -> str:
    """이 계정이 쓸 IMAP 인증 방식: ``"password"`` | ``"xoauth2"``.

    명시적 ``auth`` 필드가 있으면 그 값, 없으면 타입 기본값:
    Outlook 은 서버가 비밀번호를 거부하므로 ``xoauth2``, 나머지는 ``password``.
    (Gmail 은 둘 다 가능 - 사용자가 ``auth: xoauth2`` 로 켤 수 있다. Naver/Daum 은
    IMAP OAuth 를 지원하지 않으므로 항상 ``password``.)
    """
    a = str(account.get("auth") or "").strip().lower()
    if a in AUTH_METHODS:
        return a
    return "xoauth2" if account.get("type") == "outlook" else "password"


def _valid_accounts(accounts: list[dict]) -> list[dict]:
    out = []
    for a in accounts:
        if a.get("type") not in IMAP_SERVERS or not a.get("user"):
            continue
        # xoauth2 계정은 비밀번호가 필요 없다(토큰 캐시로 인증).
        if account_auth(a) == "password" and not a.get("password"):
            continue
        out.append(a)
    return out


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


def resolve_accounts_from_stdin(stream: TextIO | None = None) -> None:
    """데스크톱 셸이 자격증명을 spawn env 대신 stdin 으로 넘긴 경우를 처리한다.

    부모(Electron)는 env 에 ``MAIL_AGENT_ACCOUNTS=@stdin`` 센티널만 두고, 실제 계정
    JSON 배열은 자식 stdin 첫 줄로 보낸다(프로세스 환경 블록에 평문 비밀번호가 남지
    않게). 여기서 실제 값으로 치환해 두면 이후 ``accounts_from_env()`` 는 평소대로
    동작한다(서브프로세스도 env 상속).

    센티널이 없으면(터미널에서 직접 실행 등) 아무것도 하지 않고 stdin 도 안 건드린다.
    빈 줄이 오면 ``""`` 로 두어 accounts.yaml 폴백이 되게 한다. 각 진입점
    (`python -m admin_ui`, `python -m mail_app.outlook_login`)이 부팅 전에 호출한다.
    """
    if os.environ.get(ENV_ACCOUNTS_VAR) != "@stdin":
        return
    src = stream if stream is not None else sys.stdin
    try:
        line = src.readline() if not getattr(src, "closed", False) else ""
    except (OSError, ValueError):
        line = ""
    os.environ[ENV_ACCOUNTS_VAR] = line.strip()
    if stream is None:
        try:
            sys.stdin.close()
        except OSError:
            pass


def env_mode() -> bool:
    """데스크톱 앱이 자격증명을 환경변수로 주입한 상태인가? (계정 파일 쓰기를 막는 데 씀)"""
    return bool(os.environ.get(ENV_ACCOUNTS_VAR))


def load_accounts(path: Path) -> list[dict]:
    """계정 목록을 반환한다.

    1순위: MAIL_AGENT_ACCOUNTS 환경변수(데스크톱 앱의 safeStorage 볼트에서 주입).
    2순위: config/accounts.yaml (개발/CLI 폴백 - 호출자가 경로를 넘겨준다).

    yaml 파싱은 PyYAML 없이 아래의 제한된 구조만 지원하는 최소 파서를 직접 구현한다.
    비밀번호는 파일에 평문으로 남지 않고 mail_core.crypto로 암호화한 password_enc 로
    저장된다(이 함수가 복호화해서 돌려준다):

        accounts:
          - type: gmail
            user: someone@gmail.com
            password_enc: "<암호화된 값 - 설정 화면에서 자동 생성>"
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
        # password 는 빈 문자열도 유효(xoauth2 계정). 그 외 키는 값이 있을 때만.
        if key and (value or key == "password"):
            current[key] = value

    if current:
        accounts.append(current)

    for a in accounts:
        enc = a.pop("password_enc", None)
        if enc:
            try:
                a["password"] = crypto.decrypt(enc)
            except crypto.CryptoError:
                a["password"] = ""  # 키 분실/손상 - 재입력 필요
        elif "password" not in a:
            a["password"] = ""

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
        pw = a.get("password", "")
        if pw:
            lines.append(f'    password_enc: "{crypto.encrypt(pw)}"')
        if account_auth(a) != ("xoauth2" if a.get("type") == "outlook" else "password"):
            lines.append(f"    auth: {account_auth(a)}")
        if str(a.get("alias") or "").strip():
            lines.append(f"    alias: {a['alias'].strip()}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def add_account(
    path: Path, type: str, user: str, password: str, auth: str = "", alias: str = ""
) -> None:
    accounts = load_accounts(path)
    entry = {"type": type, "user": user, "password": password}
    if auth:
        entry["auth"] = auth
    if alias.strip():
        entry["alias"] = alias.strip()
    accounts.append(entry)
    save_accounts(path, accounts)


def update_account(
    path: Path, original_user: str, type: str, user: str, password: str,
    auth: str = "", alias: str = "",
) -> None:
    """user(이메일)로 계정을 찾아 갱신한다 - 이메일이 사실상의 고유 식별자다."""
    accounts = load_accounts(path)
    for a in accounts:
        if a["user"] == original_user:
            a["type"], a["user"], a["password"] = type, user, password
            if auth:
                a["auth"] = auth
            else:
                a.pop("auth", None)
            if alias.strip():
                a["alias"] = alias.strip()
            else:
                a.pop("alias", None)
            break
    save_accounts(path, accounts)


def delete_account(path: Path, user: str) -> None:
    accounts = [a for a in load_accounts(path) if a["user"] != user]
    save_accounts(path, accounts)
