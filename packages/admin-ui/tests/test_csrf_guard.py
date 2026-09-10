"""cross-origin CSRF + DNS-rebinding 가드가 (세 경로가 아니라) 모든 상태 변경 POST 를
한 곳에서 막는지.

예전엔 /tasks/action·/vault/restore·/vault/purge 만 개별로 막혀 있어서, 다른 탭의
악성 페이지가 /tasks/apply(실제 메일함 정리)나 /settings 삭제를 POST 로 때릴 수 있었다.
이제 @app.before_request 가 한 곳에서 막는다.

이 파일은 conftest.py(pytest 전용) 없이 단독 실행(`python test_csrf_guard.py`)해도
실제 계정·DB 를 건드리지 않도록, admin_app import 전에 스스로 임시 data 디렉터리를 잡는다.
"""
import os
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

# --- 격리: admin_app 이 import 시점에 DB_PATH 를 고정하므로 그 전에 환경을 잡는다 -------
if not os.environ.get("MAIL_AGENT_DATA_DIR"):
    _root = Path(__file__).resolve().parents[3]
    for _pkg in ("mail-core", "mail-app", "admin-ui"):
        sys.path.insert(0, str(_root / "packages" / _pkg))
    _tmp = Path(tempfile.mkdtemp(prefix="csrf-guard-test-"))
    os.environ["MAIL_AGENT_DATA_DIR"] = str(_tmp)
    sqlite3.connect(_tmp / "app.db").close()

from admin_ui.admin_app import app  # noqa: E402

EVIL = {"Origin": "http://evil.example"}
SAME = {"Origin": "http://localhost"}

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _write_urls() -> list[str]:
    """app.url_map 에서 상태 변경(rule) URL 을 직접 뽑는다 - 하드코딩 표가 라우트 변경을
    놓치지 않도록. <name>/<user> 같은 변수 자리는 더미로 채운다."""
    urls = []
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static" or not (rule.methods & _WRITE_METHODS):
            continue
        urls.append(re.sub(r"<[^>]+>", "dummy", rule.rule))
    return urls


def test_every_write_route_rejects_cross_origin():
    c = app.test_client()
    urls = _write_urls()
    assert len(urls) >= 12, f"write 라우트가 너무 적게 잡혔다: {urls}"
    for u in urls:
        r = c.post(u, headers=EVIL)
        assert r.status_code == 403, f"{u} 가 cross-origin POST 를 막지 않음 ({r.status_code})"


def test_dns_rebinding_host_is_rejected():
    """Host/Origin 이 둘 다 attacker 도메인이면(로컬 호스트가 아님) 거부한다."""
    c = app.test_client()
    r = c.post("/vault/purge", headers={"Host": "rebind.evil.com", "Origin": "http://rebind.evil.com"})
    assert r.status_code == 403


def test_dns_rebinding_host_is_rejected_for_get_too():
    """rebind 성공 시 GET 도 same-origin 이 되어 응답(메일 메타데이터)을 읽힐 수 있으므로,
    Host 가 로컬이 아니면 GET 도 거부한다."""
    c = app.test_client()
    for path in ("/", "/vault", "/list"):
        r = c.get(path, headers={"Host": "rebind.evil.com"})
        assert r.status_code == 403, f"GET {path} (rebind Host) → {r.status_code}"


def test_same_origin_write_is_not_blocked_by_guard():
    """같은 출처 + 로컬 호스트면 가드를 통과한다. 빈 선택으로 /tasks/action 을 때리면
    IMAP·subprocess 없이 302 로 돌아온다."""
    c = app.test_client()
    r = c.post("/tasks/action", data={"sel": [], "action": "read"}, headers=SAME)
    assert r.status_code == 302


def test_missing_origin_and_referer_passes():
    """헤더 없는 curl/스크립트·Electron 스케줄러(raw http /sync) 직접 호출은 통과."""
    c = app.test_client()
    r = c.post("/tasks/action", data={"sel": []})
    assert r.status_code == 302


def test_get_requests_with_evil_origin_but_local_host_pass():
    """cross-origin GET 은 브라우저가 응답을 못 읽으므로(rebinding 이 아닌 한) 막지 않는다.
    Host 가 로컬이면 Origin 이 attacker 여도 통과."""
    c = app.test_client()
    for path in ("/", "/settings", "/tasks", "/vault", "/list"):
        r = c.get(path, headers=EVIL)
        assert r.status_code == 200, f"GET {path} → {r.status_code}"


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run()
