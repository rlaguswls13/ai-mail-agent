"""IMAP XOAUTH2 용 OAuth2 토큰 관리 - 표준 라이브러리만 사용.

프로바이더별로 두 가지 인증 플로우를 지원한다:

  - **device**  (Outlook/consumers): CLI 가 코드를 안내하고 사용자가
    microsoft.com/link 에서 입력·동의. 로컬 서버 불필요.
  - **loopback** (Gmail): 임시 ``127.0.0.1:<빈 포트>`` HTTP 서버를 띄우고 기본
    브라우저로 동의 화면을 연 뒤 리다이렉트로 ``?code=`` 를 받는다(+PKCE, +state).
    Gmail 은 device flow 를 데스크톱 클라이언트에 허용하지 않아 이 방식만 가능.

client_id 는 Mozilla Thunderbird 의 공개 클라이언트를 재사용한다(오픈소스라 값이
공개돼 있고, CLI 메일 도구들이 자체 앱 등록 없이 관행적으로 함께 쓴다). Google 의
"client_secret" 은 installed-app 용이라 RFC 8252 상 기밀이 아니다.

토큰 캐시: ``<dir>/<provider>_token.json`` (``{user: {refresh_token, access_token,
expires_at}}``). dir 우선순위: ``MAIL_AGENT_OAUTH_DIR`` → ``MAIL_AGENT_DATA_DIR``
→ 저장소 ``config/``.
"""
import base64
import hashlib
import http.server
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import NamedTuple

# access token 을 만료 이 초 전에 미리 갱신한다(요청 도중 만료 방지).
_REFRESH_SKEW = 120
_LOOPBACK_TIMEOUT = 600

# .../packages/mail-core/mail_core/oauth.py -> parents[3] == 저장소 루트
_REPO_ROOT = Path(__file__).resolve().parents[3]
ENV_OAUTH_DIR = "MAIL_AGENT_OAUTH_DIR"


class Provider(NamedTuple):
    flow: str                      # "device" | "loopback"
    client_id: str
    client_secret: str | None
    token_url: str
    scope: str
    device_code_url: str | None = None
    auth_url: str | None = None     # loopback: authorization endpoint
    verification_uri: str | None = None  # device: 사용자에게 보여줄 URL


PROVIDERS: dict[str, Provider] = {
    "outlook": Provider(
        flow="device",
        client_id="9e5f94bc-e8a4-4e73-b8be-63364c29d753",  # Mozilla Thunderbird
        client_secret=None,
        device_code_url="https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode",
        token_url="https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
        scope="https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
        verification_uri="https://www.microsoft.com/link",
    ),
    "gmail": Provider(
        flow="loopback",
        client_id="406964657835-aq8lmia8j95dhl1a2bvharmfk3t1hgqj.apps.googleusercontent.com",
        client_secret="kSmqreRr0qwBWJgbf5Y-PjSU",  # Thunderbird installed-app secret (RFC 8252: 비기밀)
        auth_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scope="https://mail.google.com/",
    ),
}

# XOAUTH2 를 쓸 수 있는(= OAuth 로그인이 필요한) 계정 타입.
OAUTH_TYPES = tuple(PROVIDERS)


class OAuthError(RuntimeError):
    """토큰이 없거나 갱신/발급에 실패함 - 재로그인이 필요하다."""


# ── 토큰 캐시 ────────────────────────────────────────────────────────────────

def _token_path(provider: str) -> Path:
    override = os.environ.get(ENV_OAUTH_DIR) or os.environ.get("MAIL_AGENT_DATA_DIR")
    base = Path(override) if override else _REPO_ROOT / "config"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{provider}_token.json"


def _load_store(provider: str) -> dict:
    p = _token_path(provider)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


def _save_store(provider: str, store: dict) -> None:
    p = _token_path(provider)
    p.write_text(json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def _store_token(provider: str, user: str, tok: dict) -> None:
    store = _load_store(provider)
    entry = store.get(user, {})
    # Google 은 refresh_token 을 회전하지 않고 보통 재발급도 안 하므로 기존 값을 유지.
    if tok.get("refresh_token"):
        entry["refresh_token"] = tok["refresh_token"]
    entry["access_token"] = tok.get("access_token", "")
    entry["expires_at"] = int(time.time()) + int(tok.get("expires_in", 3600))
    store[user] = entry
    _save_store(provider, store)


# ── HTTP ────────────────────────────────────────────────────────────────────

def _post(url: str, data: dict) -> dict:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/x-www-form-urlencoded"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))  # OAuth 오류는 본문에 JSON
        except (ValueError, OSError):
            raise OAuthError(f"{url}: HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise OAuthError(f"{url}: {e.reason}") from e


# ── access token (자동 갱신) ────────────────────────────────────────────────

def access_token(provider: str, user: str) -> str:
    """유효한 access token 을 반환한다. 캐시가 신선하면 그대로, 아니면 refresh.

    캐시가 없거나 refresh 가 실패하면 OAuthError - 호출부(imap_auth)가 이를
    imaplib.IMAP4.error 로 바꿔 계정 단위 실패로 처리한다.
    """
    p = _provider(provider)
    entry = _load_store(provider).get(user)
    if not entry or not entry.get("refresh_token"):
        raise OAuthError(
            f"{user}: {provider} 인증 토큰이 없습니다. "
            f"먼저 `python -m mail_app.oauth_login` 을 실행해 로그인하세요."
        )
    if entry.get("access_token") and time.time() < entry.get("expires_at", 0) - _REFRESH_SKEW:
        return entry["access_token"]

    data = {
        "client_id": p.client_id,
        "grant_type": "refresh_token",
        "refresh_token": entry["refresh_token"],
        "scope": p.scope,
    }
    if p.client_secret:
        data["client_secret"] = p.client_secret
    tok = _post(p.token_url, data)
    if tok.get("error") or not tok.get("access_token"):
        raise OAuthError(
            f"{user}: 토큰 갱신 실패 ({tok.get('error', 'unknown')}). "
            f"`python -m mail_app.oauth_login --force` 로 재로그인이 필요할 수 있습니다."
        )
    _store_token(provider, user, tok)
    return tok["access_token"]


def token_status(provider: str, user: str) -> str:
    """토큰 상태: ``"ok"`` | ``"missing"`` | ``"revoked"`` (네트워크 최소).

    신선한 access token 이 있으면 네트워크 없이 ``"ok"``. 만료됐으면 refresh 시도.
    """
    entry = _load_store(provider).get(user)
    if not entry or not entry.get("refresh_token"):
        return "missing"
    if entry.get("access_token") and time.time() < entry.get("expires_at", 0) - _REFRESH_SKEW:
        return "ok"
    try:
        access_token(provider, user)
        return "ok"
    except OAuthError:
        return "revoked"


def xoauth2_string(user: str, token: str) -> bytes:
    """IMAP AUTHENTICATE XOAUTH2 에 넘길 SASL 문자열(base64 전)."""
    return f"user={user}\x01auth=Bearer {token}\x01\x01".encode()


# ── 최초 로그인 ─────────────────────────────────────────────────────────────

def login(provider: str, user: str, *, on_prompt=None, print_fn=print, open_browser: bool = True) -> None:
    """프로바이더의 플로우(device | loopback)로 최초 refresh token 을 받는다."""
    p = _provider(provider)
    if p.flow == "device":
        _device_login(provider, user, on_prompt=on_prompt, print_fn=print_fn)
    else:
        _loopback_login(provider, user, on_prompt=on_prompt, print_fn=print_fn, open_browser=open_browser)


def _provider(provider: str) -> Provider:
    try:
        return PROVIDERS[provider]
    except KeyError:
        raise OAuthError(f"OAuth 미지원 프로바이더: {provider}") from None


def _device_login(provider: str, user: str, *, on_prompt=None, print_fn=print) -> None:
    p = _provider(provider)
    init = _post(p.device_code_url, {"client_id": p.client_id, "scope": p.scope})
    if "user_code" not in init:
        raise OAuthError(f"device code 요청 실패: {init.get('error_description') or init}")

    message = init.get("message") or (
        f"{p.verification_uri} 에서 코드 입력: {init['user_code']}"
    )
    _fire_prompt(on_prompt, print_fn, {
        "verification_uri": init.get("verification_uri") or p.verification_uri,
        "user_code": init["user_code"],
        "message": message,
    })

    interval = int(init.get("interval", 5))
    deadline = time.time() + int(init.get("expires_in", 900))
    while time.time() < deadline:
        time.sleep(interval)
        tok = _post(p.token_url, {
            "client_id": p.client_id,
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
            raise OAuthError(f"인증 실패: {err}: {tok.get('error_description', '')}")
        if not tok.get("refresh_token"):
            raise OAuthError(f"refresh token 이 응답에 없습니다: {tok}")
        _store_token(provider, user, tok)
        return
    raise OAuthError("device code 가 만료됐습니다 - 다시 시도하세요")


def _loopback_login(provider: str, user: str, *, on_prompt=None, print_fn=print, open_browser: bool = True) -> None:
    p = _provider(provider)
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(40)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    captured: dict[str, str] = {}

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" in params or "error" in params:
                captured.update({k: v[0] for k, v in params.items()})
                ok = "code" in params
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                body = (
                    "<!doctype html><meta charset=utf-8><body style=\"font-family:sans-serif;"
                    "text-align:center;padding-top:3rem\"><p>"
                    + ("인증이 완료되었습니다. 이 창을 닫아도 됩니다."
                       if ok else f"인증 실패: {params.get('error', [''])[0]}")
                    + "</p></body>"
                )
                self.wfile.write(body.encode("utf-8"))
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *_a):  # 서버 로그 소음 제거
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    redirect_uri = f"http://127.0.0.1:{server.server_port}/"
    auth_url = p.auth_url + "?" + urllib.parse.urlencode({
        "client_id": p.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": p.scope,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
        "login_hint": user,
    })

    _fire_prompt(on_prompt, print_fn, {
        "verification_uri": auth_url,
        "user_code": "",
        "message": f"브라우저에서 {user} 로 로그인하고 메일 접근에 동의하세요.",
    })
    if open_browser:
        try:
            webbrowser.open(auth_url)
        except Exception:  # noqa: BLE001 - 브라우저 없음 등은 무시(수동으로 열면 됨)
            pass

    server.timeout = 1
    deadline = time.time() + _LOOPBACK_TIMEOUT
    try:
        while not captured and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()

    if not captured:
        raise OAuthError("브라우저 인증 시간이 초과됐습니다 - 다시 시도하세요")
    if captured.get("state") != state:
        raise OAuthError("state 불일치 - 요청이 변조됐을 수 있습니다")
    if captured.get("error"):
        raise OAuthError(f"인증이 거부됐습니다: {captured['error']}")

    tok = _post(p.token_url, {
        "client_id": p.client_id,
        "client_secret": p.client_secret or "",
        "code": captured["code"],
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    })
    if tok.get("error") or not tok.get("access_token"):
        raise OAuthError(f"토큰 교환 실패: {tok.get('error_description') or tok.get('error')}")
    if not tok.get("refresh_token"):
        raise OAuthError(
            "refresh token 이 응답에 없습니다. 계정 보안 설정에서 이 앱의 접근 권한을 "
            "지운 뒤 다시 로그인하세요."
        )
    _store_token(provider, user, tok)


def _fire_prompt(on_prompt, print_fn, info: dict) -> None:
    if on_prompt is not None:
        on_prompt(info)
    else:
        print_fn(info["message"])
