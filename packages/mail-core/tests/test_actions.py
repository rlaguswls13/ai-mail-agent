"""actions.py — /vault 되돌리기·영구삭제가 쓰는 IMAP 헬퍼 회귀 테스트.

FakeIMAP으로 IMAP 왕복을 흉내 낸다(실제 서버 없이 명령 시퀀스/반환값만 검증).
실행: repo 루트 또는 packages/mail-core/ 에서  py -m pytest
"""
import imaplib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mail_core import actions  # noqa: E402


class FakeIMAP:
    """imaplib.IMAP4_SSL의 아주 얇은 대역. search로 돌려줄 uid, expunge 대상 등을 주입."""

    def __init__(self, *, search_result=b"", caps=b"CAPABILITY IMAP4rev1 UIDPLUS MOVE", raise_on=None):
        self.search_result = search_result
        self.caps = caps
        self.raise_on = raise_on or set()
        self.calls = []
        self.selected = None

    def _maybe_raise(self, tag):
        if tag in self.raise_on:
            raise imaplib.IMAP4.error(f"boom:{tag}")

    def select(self, mailbox, readonly=False):
        self._maybe_raise("select")
        self.calls.append(("select", mailbox, readonly))
        self.selected = mailbox
        return "OK", [b"1"]

    def capability(self):
        return "OK", [self.caps]

    def uid(self, command, *args):
        cmd = command.lower()
        self.calls.append(("uid", cmd, args))
        self._maybe_raise(f"uid:{cmd}")
        if cmd == "search":
            return "OK", [self.search_result]
        if cmd in ("store", "copy", "move", "expunge"):
            return "OK", [b""]
        return "OK", [b""]

    def expunge(self):
        self.calls.append(("expunge",))
        return "OK", [b""]


# --- find_message_uid_by_id --------------------------------------------------

def test_find_uid_returns_last_match():
    imap = FakeIMAP(search_result=b"11 42")
    assert actions.find_message_uid_by_id(imap, "[Gmail]/Trash", "<abc@x>") == "42"
    # 폴더를 select 하고 HEADER Message-ID 로 검색했는지
    assert ("select", '"[Gmail]/Trash"', False) in imap.calls
    search = [c for c in imap.calls if c[0] == "uid" and c[1] == "search"][0]
    assert search[2][1:3] == ("HEADER", "Message-ID")
    assert search[2][3] == '"<abc@x>"'


def test_find_uid_none_when_no_match():
    assert actions.find_message_uid_by_id(FakeIMAP(search_result=b""), "F", "<x>") is None


def test_find_uid_none_for_empty_message_id():
    imap = FakeIMAP(search_result=b"5")
    assert actions.find_message_uid_by_id(imap, "F", "") is None
    assert imap.calls == []  # 검색 자체를 안 한다


def test_find_uid_swallows_imap_errors():
    imap = FakeIMAP(raise_on={"uid:search"})
    assert actions.find_message_uid_by_id(imap, "F", "<x>") is None


def test_find_uid_quotes_message_id_with_special_chars():
    imap = FakeIMAP(search_result=b"7")
    actions.find_message_uid_by_id(imap, "F", '<a"b@x>')
    search = [c for c in imap.calls if c[1] == "search"][0]
    assert search[2][3] == '"<a\\"b@x>"'


# --- permanent_delete ------------------------------------------------------

def test_permanent_delete_marks_and_expunges():
    imap = FakeIMAP()
    ok, bad = actions.permanent_delete(imap, "[Gmail]/Trash", ["1", "2"])
    assert ok == ["1", "2"] and bad == []
    kinds = [c for c in imap.calls if c[0] == "uid"]
    assert any(k[1] == "store" and "(\\Deleted)" in k[2] for k in kinds)
    assert any(k[1] == "expunge" for k in kinds)  # UIDPLUS -> UID EXPUNGE


def test_permanent_delete_plain_expunge_without_uidplus():
    imap = FakeIMAP(caps=b"CAPABILITY IMAP4rev1")
    ok, bad = actions.permanent_delete(imap, "F", ["1"])
    assert ok == ["1"]
    assert ("expunge",) in imap.calls  # 무범위 EXPUNGE 폴백


def test_permanent_delete_empty_is_noop():
    imap = FakeIMAP()
    assert actions.permanent_delete(imap, "F", []) == ([], [])
    assert imap.calls == []


def test_permanent_delete_fails_when_select_fails():
    imap = FakeIMAP(raise_on={"select"})
    ok, bad = actions.permanent_delete(imap, "F", ["1", "2"])
    assert ok == [] and bad == ["1", "2"]


def test_permanent_delete_marks_batch_failed_on_store_error():
    imap = FakeIMAP(raise_on={"uid:store"})
    ok, bad = actions.permanent_delete(imap, "F", ["1", "2"])
    assert ok == [] and bad == ["1", "2"]


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run()
