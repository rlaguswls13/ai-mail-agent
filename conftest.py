"""저장소 루트 conftest - 3개 패키지를 sys.path 에 얹어서, editable 설치(dev-install.bat)
없이도 `python -m pytest` 가 루트에서 바로 돈다.

각 테스트 파일도 자기 패키지 경로를 스스로 insert 하지만(단독 실행 대비), mail-app 의
일부 모듈(generate_html 등)은 mail-core 를 import 하므로 셋 다 필요하다.
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).parent
for _pkg in ("mail-core", "mail-app", "admin-ui"):
    _p = str(_ROOT / "packages" / _pkg)
    if _p not in sys.path:
        sys.path.insert(0, _p)
