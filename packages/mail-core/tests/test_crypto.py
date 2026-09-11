"""mail_core.crypto 회귀 테스트. 실행: repo 루트에서 py -m pytest"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mail_core import crypto  # noqa: E402


@pytest.fixture
def key_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MAIL_AGENT_OAUTH_DIR", str(tmp_path))
    return tmp_path


def test_roundtrip(key_dir):
    ct = crypto.encrypt("hello, 비밀")
    assert ct != "hello, 비밀"
    assert crypto.decrypt(ct) == "hello, 비밀"


def test_key_is_generated_once_and_reused(key_dir):
    crypto.encrypt("a")
    key_file = key_dir / "secret.key"
    assert key_file.exists()
    first_key = key_file.read_text(encoding="utf-8")
    crypto.encrypt("b")
    assert key_file.read_text(encoding="utf-8") == first_key


def test_decrypt_with_wrong_key_raises(key_dir, monkeypatch, tmp_path):
    ct = crypto.encrypt("secret")
    other_dir = tmp_path / "other"
    monkeypatch.setenv("MAIL_AGENT_OAUTH_DIR", str(other_dir))
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt(ct)


def test_decrypt_garbage_raises(key_dir):
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt("not-a-valid-token")
