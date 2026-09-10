"""actions.py - /vault 되돌리기·영구삭제가 쓰는 IMAP 헬퍼 회귀 테스트.

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

    def __init__(self, *, search_result=b"", caps=b"CAPABILITY IMAP4rev1 UIDPLUS MOVE",
                 raise_on=None, expunge_status="OK", list_result=None, store_status="OK"):
        self.search_result = search_result
        self.caps = caps
        self.raise_on = raise_on or set()
        self.expunge_status = expunge_status
        self.store_status = store_status
        self.list_result = list_result
        self.calls = []
        self.selected = None

    def list(self, *args):
        self.calls.append(("list", args))
        self._maybe_raise("list")
        if self.list_result is None:
            return "OK", [
                b'(\\HasNoChildren \\Trash) "/" "[Gmail]/Trash"',
                b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"',
            ]
        return "OK", self.list_result

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
        if cmd == "expunge":
            return self.expunge_status, [b""]
        if cmd == "store":
            return self.store_status, [b""]
        if cmd in ("copy", "move"):
            return "OK", [b""]
        return "OK", [b""]

    def expunge(self):
        self.calls.append(("expunge",))
        return self.expunge_status, [b""]


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


def test_find_uid_none_for_message_id_with_newline():
    imap = FakeIMAP(search_result=b"5")
    assert actions.find_message_uid_by_id(imap, "F", "<a@x>\r\nEXPUNGE") is None
    assert imap.calls == []  # CR/LF 있으면 명령을 아예 안 보낸다(인젝션 방지)


def test_find_uid_raises_on_imap_error_not_none():
    """조회 실패와 '정말 없음'을 구분해야 한다 - 실패는 예외로 올린다."""
    import imaplib as _imaplib
    imap = FakeIMAP(raise_on={"uid:search"})
    try:
        actions.find_message_uid_by_id(imap, "F", "<x>")
        assert False, "should have raised"
    except _imaplib.IMAP4.error:
        pass


def test_find_uid_select_false_skips_select():
    imap = FakeIMAP(search_result=b"9")
    actions.find_message_uid_by_id(imap, "F", "<x>", select=False)
    assert not any(c[0] == "select" for c in imap.calls)


def test_find_uid_quotes_message_id_with_special_chars():
    imap = FakeIMAP(search_result=b"7")
    actions.find_message_uid_by_id(imap, "F", '<a"b@x>')
    search = [c for c in imap.calls if c[1] == "search"][0]
    assert search[2][3] == '"<a\\"b@x>"'


def test_select_folder():
    ok = FakeIMAP()
    assert actions.select_folder(ok, "[Gmail]/Trash") is True
    assert ("select", '"[Gmail]/Trash"', False) in ok.calls
    bad = FakeIMAP(raise_on={"select"})
    assert actions.select_folder(bad, "F") is False


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


def test_move_to_folder_require_move_bails_without_move_cap():
    """MOVE 없는 서버에서 require_move=True면 COPY+EXPUNGE 폴백을 안 타고 전부 실패."""
    imap = FakeIMAP(caps=b"CAPABILITY IMAP4rev1 UIDPLUS")
    ok, bad = actions.move_to_folder(imap, ["1", "2"], "INBOX", require_move=True)
    assert ok == [] and bad == ["1", "2"]
    assert not any(c[0] == "uid" and c[1] in ("copy", "store", "expunge") for c in imap.calls)


def test_move_to_folder_uses_move_when_available():
    imap = FakeIMAP()  # caps에 MOVE 포함
    ok, bad = actions.move_to_folder(imap, ["1"], "INBOX", require_move=True)
    assert ok == ["1"] and bad == []
    assert any(c[0] == "uid" and c[1] == "move" for c in imap.calls)


def test_permanent_delete_fails_when_expunge_returns_no():
    """EXPUNGE가 NO/BAD면 성공으로 세면 안 된다(호출부가 DB 행을 지워버림)."""
    imap = FakeIMAP(expunge_status="NO")
    ok, bad = actions.permanent_delete(imap, "F", ["1", "2"])
    assert ok == [] and bad == ["1", "2"]


# --- decode_mailbox_name (modified UTF-7, RFC 3501) -------------------------

def test_decode_mailbox_name_ascii_passthrough():
    assert actions.decode_mailbox_name("[Gmail]/Trash") == "[Gmail]/Trash"


def test_decode_mailbox_name_korean_modified_utf7():
    # "보관함" 은 modified UTF-7 로 "&vPStANVo-" (docstring 의 실제 겪은 버그 케이스)
    assert actions.decode_mailbox_name("&vPStANVo-") == "보관함"


def test_decode_mailbox_name_literal_ampersand():
    # "&-" 는 리터럴 "&" 로 디코딩된다
    assert actions.decode_mailbox_name("A&-B") == "A&B"


# --- find_trash_folder / find_archive_folder --------------------------------

def test_find_trash_folder_by_name_candidate():
    imap = FakeIMAP(list_result=[
        b'(\\HasNoChildren) "/" "INBOX"',
        b'(\\HasNoChildren) "/" "[Gmail]/Trash"',
    ])
    assert actions.find_trash_folder(imap, "gmail") == "[Gmail]/Trash"


def test_find_archive_folder_by_special_use_attr_when_name_differs():
    # 이름 후보엔 없지만 \\All 특수폴더 속성이 붙어 있으면 그걸 고른다
    imap = FakeIMAP(list_result=[
        b'(\\HasNoChildren) "/" "INBOX"',
        b'(\\HasNoChildren \\All) "/" "Todos"',
    ])
    assert actions.find_archive_folder(imap, "gmail") == "Todos"


def test_find_trash_folder_matches_encoded_korean_name():
    # LIST 응답의 폴더명이 modified UTF-7 로 와도 "휴지통" 후보와 매칭돼야 한다
    imap = FakeIMAP(list_result=[b'(\\HasNoChildren) "/" "&1zTJwNG1-"'])  # "휴지통"
    assert actions.find_trash_folder(imap, "naver") == "&1zTJwNG1-"


def test_find_folder_none_when_absent():
    imap = FakeIMAP(list_result=[b'(\\HasNoChildren) "/" "INBOX"'])
    assert actions.find_trash_folder(imap, "gmail") is None


# --- mark_as_read ----------------------------------------------------------

def test_mark_as_read_sets_seen_flag():
    imap = FakeIMAP()
    ok, bad = actions.mark_as_read(imap, ["1", "2"])
    assert ok == ["1", "2"] and bad == []
    store = [c for c in imap.calls if c[0] == "uid" and c[1] == "store"][0]
    assert store[2][1:] == ("+FLAGS", "(\\Seen)")


def test_mark_as_read_batch_failed_on_store_no():
    ok, bad = actions.mark_as_read(FakeIMAP(store_status="NO"), ["1", "2"])
    assert ok == [] and bad == ["1", "2"]


def test_mark_as_read_batch_failed_on_imap_error():
    ok, bad = actions.mark_as_read(FakeIMAP(raise_on={"uid:store"}), ["1"])
    assert ok == [] and bad == ["1"]


# --- group_uids_by_action ------------------------------------------------------

def test_group_uids_by_action_skips_keep_and_empty():
    categories = {
        "ad": {"action": "trash"},
        "sec": {"action": "save"},
        "keepcat": {"action": "keep"},
        "emptycat": {"action": "read"},
    }
    classified = {"category_matches": {
        "ad": [{"uid": "1"}, {"uid": "2"}],
        "sec": [{"uid": "3"}],
        "keepcat": [{"uid": "9"}],
        "emptycat": [],
    }}
    grouped = actions.group_uids_by_action(classified, categories)
    assert grouped == {"trash": ["1", "2"], "save": ["3"]}


def test_group_uids_by_action_merges_multiple_categories_same_action():
    categories = {"a": {"action": "trash"}, "b": {"action": "trash"}}
    classified = {"category_matches": {"a": [{"uid": "1"}], "b": [{"uid": "2"}]}}
    assert actions.group_uids_by_action(classified, categories) == {"trash": ["1", "2"]}


def _run():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run()
