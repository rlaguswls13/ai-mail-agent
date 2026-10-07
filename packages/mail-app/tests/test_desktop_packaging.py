"""Electron 패키징 회귀 가드 - desktop/package.json 의 build.files 가 메인 프로세스가
require 하는 로컬 모듈을 전부 담고 있는지 검사한다.

2026-09-29: oauthLogin.js / applyPending.js / pycrypto.js 가 build.files 에 없어서
패키지된 앱이 시작하자마자 require 오류로 죽을 뻔했다(개발 실행에서는 안 드러남).
"""
import json
import re
from pathlib import Path

DESKTOP = Path(__file__).resolve().parents[3] / "desktop"
REQUIRE_RE = re.compile(r"""require\(\s*["'](\.{1,2}/[^"']+)["']\s*\)""")


def _build_files() -> list[str]:
    return json.loads((DESKTOP / "package.json").read_text(encoding="utf-8"))["build"]["files"]


def _main_entry() -> str:
    return json.loads((DESKTOP / "package.json").read_text(encoding="utf-8"))["main"]


def _reachable_from_main() -> set[str]:
    """main 진입점에서 로컬 require 로 닿는 .js 파일(desktop 기준 상대경로)."""
    seen: set[str] = set()
    stack = [_main_entry()]
    while stack:
        rel = stack.pop()
        if rel in seen:
            continue
        seen.add(rel)
        src = (DESKTOP / rel).read_text(encoding="utf-8")
        for mod in REQUIRE_RE.findall(src):
            target = (Path(rel).parent / mod).as_posix()
            if not target.endswith(".js"):
                target += ".js"
            stack.append(target)
    return seen


def test_every_required_module_is_packaged():
    files = _build_files()
    missing = [f for f in sorted(_reachable_from_main()) if f not in files]
    assert not missing, f"build.files 에 없음(패키지 앱이 시작 시 죽음): {missing}"


def test_every_packaged_js_exists():
    gone = [f for f in _build_files()
            if "*" not in f and f.endswith(".js") and not (DESKTOP / f).is_file()]
    assert not gone, f"build.files 에 있으나 파일 없음: {gone}"
