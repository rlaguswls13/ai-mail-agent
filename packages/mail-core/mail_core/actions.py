"""분류 결과에 따라 실제로 메일함을 조작하는 액션 (휴지통 이동 / 보관 이동 / 읽음 표시)."""
import base64
import imaplib
import re

from mail_core.mail_fetch import FETCH_BATCH_SIZE

# 계정 타입별로 흔히 쓰이는 폴더 이름 후보(계정 언어 설정에 따라 다를 수 있어
# find_trash_folder()/find_archive_folder()가 실제 폴더 목록에서 매칭을 시도한다).
TRASH_FOLDER_CANDIDATES = {
    "gmail": ["[Gmail]/Trash", "[Gmail]/휴지통"],
    "naver": ["휴지통", "Trash"],
    "outlook": ["Deleted Items", "Deleted"],
}

# Gmail은 모든 메일이 이미 [Gmail]/All Mail에 들어있어서, 여기로 이동하는 건 사실상
# INBOX 라벨만 떼는 것 — Gmail 웹 UI의 "보관" 버튼과 같은 효과다.
ARCHIVE_FOLDER_CANDIDATES = {
    "gmail": ["[Gmail]/All Mail", "[Gmail]/전체보관함"],
    "naver": ["보관함", "Archive"],
    "outlook": ["Archive"],
}


def decode_mailbox_name(name: str) -> str:
    """IMAP이 폴더명에 쓰는 modified UTF-7(RFC 3501)을 사람이 읽는 유니코드로 디코딩한다.

    후보 이름("보관함" 등)은 그냥 평범한 파이썬 문자열인데, LIST 응답으로 오는 실제
    폴더명은 비-ASCII 문자를 modified UTF-7로 인코딩해서 보낸다(예: "보관함" ->
    "&vPStANVo-") — 그래서 디코딩 없이 비교하면 한글 폴더명은 절대 후보와 매칭되지
    않는다(실제로 겪은 버그). `_find_folder()`의 비교용으로 쓰고, 로그/대시보드에
    폴더명을 사람이 읽게 표시할 때도 쓴다. IMAP 명령 자체에는 항상 원본(인코딩된)
    이름을 그대로 써야 한다.
    """
    def repl(m: re.Match) -> str:
        chunk = m.group(1)
        if chunk == "":
            return "&"
        b64 = chunk.replace(",", "/")
        padded = b64 + "=" * (-len(b64) % 4)
        try:
            return base64.b64decode(padded).decode("utf-16-be")
        except (ValueError, UnicodeDecodeError):
            return m.group(0)
    return re.sub(r"&([^-]*)-", repl, name)


def server_capabilities(imap: imaplib.IMAP4_SSL) -> set[bytes]:
    """서버 CAPABILITY(대문자 토큰 bytes) 집합을 커넥션당 한 번만 조회해서 캐시한다.

    imaplib의 `imap.capabilities`는 로그인 전 초기 greeting 기준이라, Gmail처럼
    로그인 후에야 UIDPLUS/MOVE 등을 광고하는 서버에서는 값이 낡아 있다 — 그래서
    로그인 뒤 명시적으로 CAPABILITY를 한 번 다시 물어본다.
    """
    cached = getattr(imap, "_cached_caps", None)
    if cached is None:
        try:
            typ, data = imap.capability()
            cached = (
                set(data[0].upper().split()) if typ == "OK" and data and data[0] else set()
            )
        except imaplib.IMAP4.error:
            cached = set()
        imap._cached_caps = cached
    return cached


def _list_entries(imap: imaplib.IMAP4_SSL) -> list[tuple[bytes, str]]:
    """`imap.list()`(전체 폴더 열거)를 커넥션당 한 번만 하고 파싱 결과를 캐시한다.

    trash/save 액션이 둘 다 대상이면 find_trash_folder + find_archive_folder가
    각각 LIST를 날려 Gmail 기준 왕복 0.3~0.5s를 두 번 먹던 걸 한 번으로 줄인다.
    """
    cached = getattr(imap, "_cached_list", None)
    if cached is not None:
        return cached

    entries: list[tuple[bytes, str]] = []
    status, folders = imap.list()
    if status == "OK" and folders:
        for line in folders:
            if not isinstance(line, bytes):
                continue
            decoded = line.decode(errors="ignore")
            parts = decoded.rsplit('"', 2)
            if len(parts) >= 2:
                entries.append((line, parts[-2]))
    imap._cached_list = entries
    return entries


def _find_folder(
    imap: imaplib.IMAP4_SSL, account_type: str, candidates: dict, special_use_attrs: list[str]
) -> str | None:
    entries = _list_entries(imap)
    if not entries:
        return None

    for candidate in candidates.get(account_type, []):
        for _, name in entries:
            if decode_mailbox_name(name) == candidate:
                return name

    for attr in special_use_attrs:
        attr_bytes = attr.encode()
        for line, name in entries:
            if attr_bytes in line:
                return name

    return None


def find_trash_folder(imap: imaplib.IMAP4_SSL, account_type: str) -> str | None:
    """계정의 IMAP 폴더 목록에서 휴지통 폴더를 찾는다.

    이름 규칙이 계정 언어 설정에 따라 다를 수 있어서, 먼저 흔한 이름 후보로 매칭을
    시도하고, 실패하면 IMAP 특수폴더 속성(\\Trash, RFC 6154)이 붙은 폴더를 찾는다.
    둘 다 실패하면 None.
    """
    return _find_folder(imap, account_type, TRASH_FOLDER_CANDIDATES, ["\\Trash"])


def find_archive_folder(imap: imaplib.IMAP4_SSL, account_type: str) -> str | None:
    """계정의 IMAP 폴더 목록에서 보관 폴더를 찾는다 (이름 후보 -> \\Archive/\\All 특수폴더 속성 순)."""
    return _find_folder(imap, account_type, ARCHIVE_FOLDER_CANDIDATES, ["\\Archive", "\\All"])


def _quote_mailbox(name: str) -> str:
    """IMAP 폴더명을 명령에 안전하게 넣을 수 있게 quoted-string으로 감싼다.

    RFC 3501 문법상 astring에 SP(공백) 등 특수문자가 들어가면 quoted-string(큰따옴표로
    감싸고 내부의 \\/"는 이스케이프)이어야 한다. imaplib은 이걸 자동으로 해주지 않아서,
    "Deleted Messages"처럼 공백이 들어간 폴더명을 그냥 넘기면 명령 자체가 깨진다
    (실제로 Naver 계정에서 이 폴더명 때문에 COPY가 조용히 실패하던 버그였음).
    """
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def move_to_folder(
    imap: imaplib.IMAP4_SSL,
    uids: list[str],
    target_folder: str,
    batch_size: int = FETCH_BATCH_SIZE,
) -> tuple[list[str], list[str]]:
    """uid 목록을 target_folder로 옮긴다.

    휴지통 이동과 보관 이동 둘 다 이 함수를 쓴다 (목적지 폴더만 다름).
    (성공 uid 리스트, 실패 uid 리스트)를 반환한다 — 배치 단위 명령이라 한 배치가
    성공하면 그 배치의 uid 전부가 성공, 실패하면 전부가 실패로 취급된다(부분 성공은
    구분하지 않음). 호출부가 성공한 uid만 골라 messages 테이블 상태를 갱신하는 데 쓴다.

    서버가 MOVE(RFC 6851)를 지원하면 `UID MOVE` 한 번으로 처리한다 — COPY -> \\Deleted
    -> EXPUNGE 3회 왕복이 1회로 줄고, 서버가 원자적으로 옮긴다. MOVE가 없으면 예전처럼
    COPY -> \\Deleted -> EXPUNGE로 폴백하되, UIDPLUS가 있으면 무범위 EXPUNGE 대신
    `UID EXPUNGE`(이 배치만)를 써서 다른 클라이언트에서 \\Deleted 표시해둔 무관한
    메일까지 영구 삭제하는 사고를 막는다.
    """
    moved: list[str] = []
    failed: list[str] = []
    quoted_target = _quote_mailbox(target_folder)
    uid_bytes = [u.encode() for u in uids]
    caps = server_capabilities(imap)
    supports_move = b"MOVE" in caps
    supports_uidplus = b"UIDPLUS" in caps
    for i in range(0, len(uid_bytes), batch_size):
        batch = uid_bytes[i : i + batch_size]
        batch_uids = uids[i : i + batch_size]
        uid_set = b",".join(batch)
        try:
            if supports_move:
                status, _ = imap.uid("move", uid_set, quoted_target)
                if status != "OK":
                    failed.extend(batch_uids)
                    continue
                moved.extend(batch_uids)
                continue

            status, _ = imap.uid("copy", uid_set, quoted_target)
            if status != "OK":
                failed.extend(batch_uids)
                continue
            imap.uid("store", uid_set, "+FLAGS", "(\\Deleted)")
            if supports_uidplus:
                imap.uid("expunge", uid_set)
            else:
                imap.expunge()
            moved.extend(batch_uids)
        except imaplib.IMAP4.error:
            failed.extend(batch_uids)
    return moved, failed


def mark_as_read(
    imap: imaplib.IMAP4_SSL, uids: list[str], batch_size: int = FETCH_BATCH_SIZE
) -> tuple[list[str], list[str]]:
    """uid 목록에 \\Seen 플래그를 붙인다 (읽음 표시, 폴더 이동 없음).

    (성공 uid 리스트, 실패 uid 리스트)를 반환한다.
    """
    marked: list[str] = []
    failed: list[str] = []
    uid_bytes = [u.encode() for u in uids]
    for i in range(0, len(uid_bytes), batch_size):
        batch = uid_bytes[i : i + batch_size]
        batch_uids = uids[i : i + batch_size]
        uid_set = b",".join(batch)
        try:
            status, _ = imap.uid("store", uid_set, "+FLAGS", "(\\Seen)")
            if status == "OK":
                marked.extend(batch_uids)
            else:
                failed.extend(batch_uids)
        except imaplib.IMAP4.error:
            failed.extend(batch_uids)
    return marked, failed


def select_folder(imap: imaplib.IMAP4_SSL, folder: str) -> bool:
    """folder를 읽기/쓰기로 SELECT 한다. 성공하면 True. 폴더명 인용은 여기서 처리."""
    try:
        status, _ = imap.select(_quote_mailbox(folder), readonly=False)
        return status == "OK"
    except imaplib.IMAP4.error:
        return False


def find_message_uid_by_id(
    imap: imaplib.IMAP4_SSL, folder: str, message_id: str, *, select: bool = True
) -> str | None:
    """folder 안에서 Message-ID 헤더가 일치하는 메일의 (그 폴더 기준) UID를 찾는다.

    메일이 휴지통/보관 폴더로 옮겨지면 UID가 새로 배정되므로, app.db에 저장해 둔
    (INBOX 시절의) uid로는 그 폴더에서 메일을 지목할 수 없다 — 대신 옮겨져도 변하지
    않는 Message-ID로 SEARCH 한다.

    반환:
    - str  : 찾은 UID(여러 개면 가장 최근=가장 큰 UID). 같은 메일이 여러 통이면
             어느 걸 골라도 동일 내용이므로 최신 사본을 고른다.
    - None : Message-ID가 비었거나(레거시 행) SEARCH 결과가 0건(이미 지워짐).

    SEARCH/SELECT 자체가 실패하면 예외(imaplib.IMAP4.error)를 **그대로 올린다** —
    "정말 없음"과 "조회 실패"를 호출부가 구분해야 하기 때문(조회 실패인데 없는 걸로
    처리해 DB 행을 지우면 안 됨). select=False면 folder가 이미 선택돼 있다고 보고
    SELECT를 건너뛴다(배치 조회 시 폴더당 1회만 SELECT).
    """
    if not message_id or "\r" in message_id or "\n" in message_id:
        return None
    if select:
        status, _ = imap.select(_quote_mailbox(folder), readonly=False)
        if status != "OK":
            raise imaplib.IMAP4.error(f"SELECT {folder} 실패: {status}")
    # Message-ID를 큰따옴표로 감싼다 — 값에 공백/특수문자가 있어도 SEARCH 인자 하나로
    # 넘어가도록. 내부 큰따옴표/백슬래시는 이스케이프(위에서 CR/LF는 이미 배제).
    quoted = '"' + message_id.replace("\\", "\\\\").replace('"', '\\"') + '"'
    status, data = imap.uid("search", None, "HEADER", "Message-ID", quoted)
    if status != "OK":
        raise imaplib.IMAP4.error(f"SEARCH 실패: {status}")
    if not data or not data[0]:
        return None
    uids = data[0].split()
    return uids[-1].decode() if uids else None


def permanent_delete(
    imap: imaplib.IMAP4_SSL,
    folder: str,
    uids: list[str],
    batch_size: int = FETCH_BATCH_SIZE,
) -> tuple[list[str], list[str]]:
    """folder(보통 휴지통) 안의 uid들을 \\Deleted 표시 후 EXPUNGE 해서 영구 삭제한다.

    호출부가 folder를 select 한 상태라고 가정하지 않고 여기서 다시 select 한다.
    UIDPLUS가 있으면 `UID EXPUNGE`(이 배치만)로 다른 클라이언트가 \\Deleted 표시해 둔
    무관한 메일까지 지우는 사고를 막는다. (성공 uid, 실패 uid)를 반환한다.
    """
    if not uids:
        return [], []
    deleted: list[str] = []
    failed: list[str] = []
    try:
        status, _ = imap.select(_quote_mailbox(folder), readonly=False)
        if status != "OK":
            return [], list(uids)
    except imaplib.IMAP4.error:
        return [], list(uids)

    supports_uidplus = b"UIDPLUS" in server_capabilities(imap)
    uid_bytes = [u.encode() for u in uids]
    for i in range(0, len(uid_bytes), batch_size):
        batch = uid_bytes[i : i + batch_size]
        batch_uids = uids[i : i + batch_size]
        uid_set = b",".join(batch)
        try:
            status, _ = imap.uid("store", uid_set, "+FLAGS", "(\\Deleted)")
            if status != "OK":
                failed.extend(batch_uids)
                continue
            if supports_uidplus:
                status, _ = imap.uid("expunge", uid_set)
            else:
                status, _ = imap.expunge()
            # EXPUNGE 응답을 반드시 확인한다 — read-only 메일함, 프로바이더 거부, 쿼터
            # 상태 등으로 NO/BAD가 오면 실제로는 안 지워졌는데 성공으로 세면 호출부가
            # DB 행을 지워버린다(복구 불가).
            if status != "OK":
                failed.extend(batch_uids)
                continue
            deleted.extend(batch_uids)
        except imaplib.IMAP4.error:
            failed.extend(batch_uids)
    return deleted, failed


def group_uids_by_action(classified: dict, categories: dict) -> dict[str, list[str]]:
    """분류 결과에서 action별로 uid 목록을 모은다.

    action이 "keep"인 카테고리는 제외한다 — "keep"은 정의상 아무 것도 안 하는
    액션이라 처리 대상이 아니다.
    """
    grouped: dict[str, list[str]] = {}
    for name, cfg in categories.items():
        action = cfg.get("action", "keep")
        if action == "keep":
            continue
        matches = classified["category_matches"].get(name, [])
        uids = [m["uid"] for m in matches if m.get("uid")]
        if not uids:
            continue
        grouped.setdefault(action, []).extend(uids)
    return grouped
