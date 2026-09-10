"""`python -m admin_ui` 진입점의 stdin 자격증명 주입 해석.

데스크톱 셸이 계정 JSON 을 env 대신 stdin 으로 넘긴다(env 블록에 평문 비밀번호 방지).
env 에는 `MAIL_AGENT_ACCOUNTS=@stdin` 센티널만 오고, 실제 값은 여기서 stdin 첫 줄을
읽어 치환한다. 그래야 이후 mail_core.accounts.accounts_from_env() 가 평소대로 동작.

conftest.py 없이 단독 실행(`python test_entrypoint_stdin.py`)해도 되게 한다.
"""
import io
import os
import sys
from pathlib import Path

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from admin_ui._entrypoint import resolve_accounts_from_stdin  # noqa: E402

ACCTS = '[{"type":"gmail","user":"a@gmail.com","password":"pw"}]'


def _run(env_value, stdin_text):
    """env 를 임시로 세팅하고 resolve 를 돌린 뒤 MAIL_AGENT_ACCOUNTS 최종값을 돌려준다."""
    saved = os.environ.get("MAIL_AGENT_ACCOUNTS")
    if env_value is None:
        os.environ.pop("MAIL_AGENT_ACCOUNTS", None)
    else:
        os.environ["MAIL_AGENT_ACCOUNTS"] = env_value
    try:
        resolve_accounts_from_stdin(io.StringIO(stdin_text))
        return os.environ.get("MAIL_AGENT_ACCOUNTS")
    finally:
        if saved is None:
            os.environ.pop("MAIL_AGENT_ACCOUNTS", None)
        else:
            os.environ["MAIL_AGENT_ACCOUNTS"] = saved


def test_sentinel_replaced_with_stdin_first_line():
    assert _run("@stdin", ACCTS + "\n") == ACCTS


def test_sentinel_reads_only_first_line():
    assert _run("@stdin", ACCTS + "\nsecond-line\n") == ACCTS


def test_empty_stdin_falls_back_to_blank():
    # 빈 줄 → "" → accounts.yaml 폴백 경로
    assert _run("@stdin", "") == ""


def test_no_sentinel_leaves_env_untouched():
    # 이미 실제 JSON 이 env 에 있으면(옛 경로/직접 실행) 그대로 둔다
    assert _run(ACCTS, "ignored\n") == ACCTS


def test_unset_env_stays_unset():
    assert _run(None, "ignored\n") is None


def test_no_sentinel_does_not_consume_stdin():
    stream = io.StringIO("keep-me\n")
    os.environ.pop("MAIL_AGENT_ACCOUNTS", None)
    resolve_accounts_from_stdin(stream)
    assert stream.readline() == "keep-me\n"  # 안 읽혔어야 함


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")
