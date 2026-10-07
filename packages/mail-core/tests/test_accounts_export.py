"""`python -m mail_core.accounts export` - 데스크톱 볼트 마이그레이션이 부르는 단일 구현.

Node(vault.js)는 yaml 파싱/password_enc 복호화를 직접 하지 않고 이 CLI 의 JSON 만 받는다.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mail_core import accounts, crypto  # noqa: E402

PKG_ROOT = Path(__file__).resolve().parents[1]


def _run(yaml_path: Path, extra_env=None):
    env = {**os.environ, "PYTHONPATH": str(PKG_ROOT), "PYTHONIOENCODING": "utf-8"}
    env.pop("MAIL_AGENT_ACCOUNTS", None)
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-m", "mail_core.accounts", "export", str(yaml_path)],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=30,
    )


@pytest.fixture
def key_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MAIL_AGENT_OAUTH_DIR", str(tmp_path))
    return tmp_path


def test_export_decrypts_password_enc_with_yaml_dir_key(key_dir):
    yaml_path = key_dir / "accounts.yaml"
    accounts.save_accounts(yaml_path, [
        {"type": "gmail", "user": "a@gmail.com", "password": "앱비번 1234"},
        {"type": "outlook", "user": "b@outlook.kr", "password": "", "auth": "xoauth2"},
    ])
    assert "앱비번" not in yaml_path.read_text(encoding="utf-8")  # 디스크엔 암호문만
    # 호출 환경에 다른 키 디렉터리가 걸려 있어도 yaml 옆 secret.key 를 쓴다.
    r = _run(yaml_path, {"MAIL_AGENT_OAUTH_DIR": str(key_dir / "elsewhere")})
    assert r.returncode == 0, r.stderr
    out = {a["user"]: a for a in json.loads(r.stdout)}
    assert out["a@gmail.com"]["password"] == "앱비번 1234"
    assert out["b@outlook.kr"]["password"] == ""
    assert "password_enc" not in out["a@gmail.com"]


def test_export_ignores_env_injected_accounts(key_dir):
    yaml_path = key_dir / "accounts.yaml"
    accounts.save_accounts(yaml_path, [{"type": "naver", "user": "n@naver.com", "password": "pw"}])
    injected = json.dumps([{"type": "gmail", "user": "x@gmail.com", "password": "x"}])
    r = _run(yaml_path, {"MAIL_AGENT_ACCOUNTS": injected})
    assert [a["user"] for a in json.loads(r.stdout)] == ["n@naver.com"]


def test_export_usage_error():
    r = subprocess.run([sys.executable, "-m", "mail_core.accounts"],
                       capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(PKG_ROOT)})
    assert r.returncode == 2
