"""로컬 대칭키(AES-256-GCM) 암복호화 - OAuth 토큰 캐시 / 계정 비밀번호를 저장소에
평문으로 두지 않기 위한 공용 유틸.

키: ``<dir>/secret.key`` (base64, 32바이트). 없으면 최초 호출 시 무작위 생성.
dir 우선순위는 oauth.py 의 토큰 캐시와 동일: ``MAIL_AGENT_OAUTH_DIR`` →
``MAIL_AGENT_DATA_DIR`` → 저장소 ``config/``. 이 키가 곧 신뢰 경계이므로
``config/secret.key`` 는 git에 커밋하지 않는다(.gitignore).

이 키를 잃으면 그 키로 암호화된 값(토큰 캐시, accounts.yaml의 password_enc)은
복구할 수 없다 - 토큰은 재로그인, 비밀번호는 재입력으로 복구.
"""
import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_REPO_ROOT = Path(__file__).resolve().parents[3]
_ENV_OAUTH_DIR = "MAIL_AGENT_OAUTH_DIR"
_ENV_DATA_DIR = "MAIL_AGENT_DATA_DIR"


class CryptoError(RuntimeError):
    """복호화 실패(키 불일치, 손상된 값 등) - 호출부가 평문 폴백/재발급으로 처리."""


def _key_dir() -> Path:
    override = os.environ.get(_ENV_OAUTH_DIR) or os.environ.get(_ENV_DATA_DIR)
    base = Path(override) if override else _REPO_ROOT / "config"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _key_path() -> Path:
    return _key_dir() / "secret.key"


def _load_or_create_key() -> bytes:
    p = _key_path()
    if p.exists():
        try:
            key = base64.urlsafe_b64decode(p.read_text(encoding="utf-8").strip())
            if len(key) == 32:
                return key
        except (ValueError, OSError):
            pass
    key = AESGCM.generate_key(bit_length=256)
    p.write_text(base64.urlsafe_b64encode(key).decode("ascii"), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass
    return key


def encrypt(plaintext: str) -> str:
    """평문 문자열 -> base64(nonce(12B) + ciphertext+tag)."""
    key = _load_or_create_key()
    nonce = os.urandom(12)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.urlsafe_b64encode(nonce + ct).decode("ascii")


def decrypt(token: str) -> str:
    """encrypt() 의 역연산. 키 불일치/손상이면 CryptoError."""
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        nonce, ct = raw[:12], raw[12:]
        key = _load_or_create_key()
        return AESGCM(key).decrypt(nonce, ct, None).decode("utf-8")
    except Exception as e:  # noqa: BLE001 - cryptography.InvalidTag 등 전부 CryptoError로 통일
        raise CryptoError(f"복호화 실패: {e}") from e
