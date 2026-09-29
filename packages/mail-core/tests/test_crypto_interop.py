"""mail_core.crypto(Python) <-> desktop/pycrypto.js(Node) 포맷 호환 회귀 테스트.

accounts.yaml 의 password_enc 를 Python 이 쓰고 Electron 볼트 마이그레이션(Node)이
읽는다. 한쪽 포맷만 바뀌면 계정이 조용히 누락되므로(2026-09-12 사고) 양방향 왕복과
고정 벡터로 계약을 강제한다. node 가 없으면 skip.
"""
import base64
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mail_core import crypto  # noqa: E402

NODE = shutil.which("node")
PYCRYPTO_JS = Path(__file__).resolve().parents[3] / "desktop" / "pycrypto.js"

pytestmark = pytest.mark.skipif(not NODE, reason="node 미설치")

# 고정 벡터: 32바이트 0x00..0x1f 키 + 12바이트 0x00..0x0b nonce 로 만든 토큰.
# 이 값이 바뀌면 = 와이어 포맷이 바뀐 것.
_KEY = bytes(range(32))
_NONCE = bytes(range(12))
_PLAINTEXT = "hello, 비밀"


def _node(mode: str, key_path: Path, text: str) -> str:
    r = subprocess.run(
        [NODE, str(PYCRYPTO_JS), mode, str(key_path), text],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert r.returncode == 0, r.stderr
    return r.stdout


@pytest.fixture
def key_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MAIL_AGENT_OAUTH_DIR", str(tmp_path))
    return tmp_path


def test_python_encrypt_node_decrypt(key_dir):
    token = crypto.encrypt(_PLAINTEXT)
    assert _node("decrypt", key_dir / "secret.key", token) == _PLAINTEXT


def test_node_encrypt_python_decrypt(key_dir):
    crypto.encrypt("init")  # secret.key 생성
    token = _node("encrypt", key_dir / "secret.key", _PLAINTEXT)
    assert crypto.decrypt(token) == _PLAINTEXT


def test_fixed_vector_both_sides(key_dir):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    (key_dir / "secret.key").write_text(
        base64.urlsafe_b64encode(_KEY).decode("ascii"), encoding="utf-8")
    ct = AESGCM(_KEY).encrypt(_NONCE, _PLAINTEXT.encode("utf-8"), None)
    token = base64.urlsafe_b64encode(_NONCE + ct).decode("ascii")
    # 구성: nonce(12) + ciphertext + tag(16)
    assert len(base64.urlsafe_b64decode(token)) == 12 + len(_PLAINTEXT.encode()) + 16
    assert crypto.decrypt(token) == _PLAINTEXT
    assert _node("decrypt", key_dir / "secret.key", token) == _PLAINTEXT


def test_node_rejects_wrong_key(key_dir, tmp_path):
    token = crypto.encrypt("x")
    other = tmp_path / "other.key"
    other.write_text(base64.urlsafe_b64encode(_KEY).decode("ascii"), encoding="utf-8")
    r = subprocess.run([NODE, str(PYCRYPTO_JS), "decrypt", str(other), token],
                       capture_output=True, text=True, timeout=30)
    assert r.returncode != 0
