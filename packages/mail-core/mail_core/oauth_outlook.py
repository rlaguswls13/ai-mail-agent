"""Outlook.com(개인 MS 계정) IMAP XOAUTH2 토큰 관리 - 표준 라이브러리만 사용.

MS가 2024년 9월 Outlook.com 개인 계정의 Basic Auth(앱 비밀번호 포함)를 없애서,
IMAP 서버가 이제 OAuth2 access token(`AUTH=XOAUTH2`)만 받는다. 이 모듈은:

  - device code flow 로 최초 1회 사용자 동의를 받고 (`python -m mail_app.outlook_login`)
  - refresh token 을 로컬 JSON 파일에 캐시하고
  - access token 이 만료되면 조용히 refresh 한다.

client_id 는 Mozilla Thunderbird 의 공개 데스크톱 client_id 를 재사용한다 - 개인 MS
계정 IMAP scope 는 Azure 에 등록된 앱을 요구하는데, Thunderbird 것이 오픈소스로
공개돼 있어 자체 앱 등록 없이 CLI 메일 도구들이 관행적으로 함께 쓴다. client_id 는
비밀이 아니라 공개 식별자다(로그인 화면에 "Mozilla Thunderbird" 로 표시됨).
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"  # Mozilla Thunderbird (public)
_BASE = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
DEVICECODE_URL = f"{_BASE}/devicecode"
TOKEN_URL = f"{_BASE}/token"
SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"

# 데스크톱 앱은 config/ 가 읽기 전용 리소스라, 토큰 캐시 경로를 이 환경변수(파일 경로)
# 또는 MAIL_AGENT_DATA_DIR(쓰기 가능 디렉터리) 로 지정한다. CLI/개발은 저장소 config/.
ENV_TOKENS_VAR = "MAIL_AGENT_OUTLOOK_TOKENS"
# .../packages/mail-core/mail_core/oauth_outlook.py -> parents[3] == 저장소 루트
_REPO_ROOT = Path(__file__).resolve().parents[3]

# access token 을 만료 이 초 전에 미리 갱신한다(요청 중 만료 방지).
_REFRESH_SKEW = 120


class OutlookAuthError(RuntimeError):
    """토큰이 없거나 갱신에 실패함 - 재로그인이 필요하다."""


def _token_path() -> Path:
    override = os.environ.get(ENV_TOKENS_VAR)
    if override:
        p = Path(override)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    data_dir = os.environ.get("MAIL_AGENT_DATA_DIR")
    base = Path(data_dir) if data_dir else _REPO_ROOT / "config"
    base.mkdir(parents=True, exist_ok=True)
    return base / "outlook_token.json"


def _load_store() -> dict:
    p = _token_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def _save_store(store: dict) -> None:
    p = _token_path()
    p.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def _post(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # OAuth 오류는 4xx 본문에 JSON({"error": ...})으로 온다 - 그대로 반환.
        try:
            return json.loads(e.read().decode("utf-8"))
        except (ValueError, OSError):
            raise OutlookAuthError(f"{url}: HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise OutlookAuthError(f"{url}: {e.reason}") from e


def _store_token(user: str, tok: dict) -> None:
    store = _load_store()
    entry = store.get(user, {})
    if tok.get("refresh_token"):
        entry["refresh_token"] = tok["refresh_token"]
    entry["access_token"] = tok.get("access_token", "")
    entry["expires_at"] = int(time.time()) + int(tok.get("expires_in", 3600))
    store[user] = entry
    _save_store(store)


def device_login(user: str, *, print_fn=print) -> None:
    """device code flow: 코드를 안내하고 사용자가 브라우저에서 승인할 때까지 폴링한다."""
    init = _post(DEVICECODE_URL, {"client_id": CLIENT_ID, "scope": SCOPE})
    if "user_code" not in init:
        raise OutlookAuthError(f"device code 요청 실패: {init.get('error_description') or init}")

    print_fn(init.get("message") or (
        f"https://microsoft.com/devicelogin 에서 코드 입력: {init['user_code']}"
    ))

    interval = int(init.get("interval", 5))
    deadline = time.time() + int(init.get("expires_in", 900))
    while time.time() < deadline:
        time.sleep(interval)
        tok = _post(TOKEN_URL, {
            "client_id": CLIENT_ID,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": init["device_code"],
        })
        err = tok.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        if err:
            raise OutlookAuthError(f"인증 실패: {err}: {tok.get('error_description', '')}")
        if not tok.get("refresh_token"):
            raise OutlookAuthError(f"refresh token 이 응답에 없습니다: {tok}")
        _store_token(user, tok)
        return
    raise OutlookAuthError("device code 가 만료됐습니다 - 다시 시도하세요")


def access_token(user: str) -> str:
    """유효한 access token 을 반환한다. 캐시가 신선하면 그대로, 아니면 refresh.

    토큰 캐시가 없거나 refresh 가 실패하면 OutlookAuthError - 호출부(imap_auth)가
    이를 imaplib.IMAP4.error 로 바꿔서 계정 단위 실패로 처리한다.
    """
    entry = _load_store().get(user)
    if not entry or not entry.get("refresh_token"):
        raise OutlookAuthError(
            f"{user}: Outlook 인증 토큰이 없습니다. "
            f"먼저 `python -m mail_app.outlook_login` 을 실행해 로그인하세요."
        )
    if entry.get("access_token") and time.time() < entry.get("expires_at", 0) - _REFRESH_SKEW:
        return entry["access_token"]

    tok = _post(TOKEN_URL, {
        "client_id": CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": entry["refresh_token"],
        "scope": SCOPE,
    })
    if tok.get("error") or not tok.get("access_token"):
        raise OutlookAuthError(
            f"{user}: 토큰 갱신 실패 ({tok.get('error', 'unknown')}). "
            f"`python -m mail_app.outlook_login --force` 로 재로그인이 필요할 수 있습니다."
        )
    _store_token(user, tok)
    return tok["access_token"]


def xoauth2_string(user: str, token: str) -> bytes:
    """IMAP AUTHENTICATE XOAUTH2 에 넘길 SASL 문자열(base64 전)."""
    return f"user={user}\x01auth=Bearer {token}\x01\x01".encode()
