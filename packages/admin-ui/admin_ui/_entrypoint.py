"""`python -m admin_ui` 부팅 전 처리 - 자격증명 stdin 주입 해석.

`admin_app` 을 import 하기 전에 실행되어야 하므로 여기에 따로 둔다(부작용 없이 import
가능하게 해서 테스트하기 쉽게).
"""
import os
import sys
from typing import TextIO


def resolve_accounts_from_stdin(stream: TextIO | None = None) -> None:
    """데스크톱 셸이 자격증명을 spawn env 대신 stdin 으로 넘긴 경우를 처리한다.

    부모(Electron `desktop/flask.js`)는 env 에 ``MAIL_AGENT_ACCOUNTS=@stdin`` 센티널만
    두고, 실제 계정 JSON 배열은 자식 stdin 첫 줄로 보낸다. 프로세스 환경 블록에 평문
    비밀번호가 남지 않게 하려는 것. 여기서 실제 값으로 치환해 두면 이후
    ``mail_core.accounts.accounts_from_env()`` 는 평소대로 동작한다(서브프로세스도 env 상속).

    센티널이 없으면(개발자가 터미널에서 직접 ``-m admin_ui`` 실행 등) 아무것도 하지 않고
    stdin 도 건드리지 않는다. 빈 줄이 오면 ``""`` 로 두어 accounts.yaml 폴백이 되게 한다.
    """
    if os.environ.get("MAIL_AGENT_ACCOUNTS") != "@stdin":
        return
    src = stream if stream is not None else sys.stdin
    try:
        line = src.readline() if not getattr(src, "closed", False) else ""
    except (OSError, ValueError):
        line = ""
    os.environ["MAIL_AGENT_ACCOUNTS"] = line.strip()
    if stream is None:
        try:
            sys.stdin.close()
        except OSError:
            pass
