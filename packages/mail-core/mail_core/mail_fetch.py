"""IMAP 계정별 메일 헤더 조회 - UID 기반, 날짜 청크/배치 처리."""
import email
import email.header
import email.utils
import imaplib
import re
from datetime import datetime, timedelta
from urllib.parse import quote

from mail_core.accounts import IMAP_SERVERS
from mail_core.imap_auth import authenticate

# 계정별 병렬 조회 + 백필용 날짜 청크 분할 설정.
# 청크 경계는 SINCE(이상)/BEFORE(미만) 반개구간이라 겹치는 날짜가 없다 -
# 서로 다른 청크가 같은 메일을 두 번 세지 않는다는 뜻.
CHUNK_DAYS = 7
MAX_WORKERS = 6
MAX_CONNECTIONS_PER_ACCOUNT = 3

# 메일 ID를 이만큼씩 묶어서 FETCH 한 번에 요청한다(건당 왕복 대신 배치당 왕복).
FETCH_BATCH_SIZE = 200

UID_RE = re.compile(rb"UID (\d+)")
GM_THRID_RE = re.compile(rb"X-GM-THRID (\d+)")


def decode_mime(raw: str) -> str:
    if not raw:
        return ""
    parts = email.header.decode_header(raw)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            try:
                out.append(text.decode(enc or "utf-8", errors="ignore"))
            except LookupError:
                out.append(text.decode("utf-8", errors="ignore"))
        else:
            out.append(text)
    return "".join(out)


def extract_sender(raw_from) -> str:
    name, addr = email.utils.parseaddr(str(raw_from or ""))
    return addr.lower() if addr else (name or "").lower()


def parse_message_date(raw_date) -> str | None:
    """메일의 Date 헤더를 ISO 8601 문자열로 바꾼다. app.db에 저장해서 나중에
    IMAP 재조회 없이 기간별로 쿼리할 수 있게 하려는 용도 - 실패하면 None
    (그 메일은 fetched_at 기준으로만 시점을 알 수 있게 된다)."""
    if not raw_date:
        return None
    try:
        return email.utils.parsedate_to_datetime(str(raw_date)).isoformat()
    except (TypeError, ValueError):
        return None


def build_web_link(
    account_type: str, uid: str | None, thread_id: str | None, account_user: str | None = None
) -> str | None:
    """계정 타입에 맞는, 특정 메일로 바로 열리는 웹메일 링크를 만든다.

    - Gmail: IMAP 서버가 비표준 확장 속성 X-GM-THRID(Gmail 내부 스레드 ID)를 FETCH로
      직접 내려준다 - 이 값을 16진수로 바꿔 `#all/<hex>` 형태로 링크를 만들면 검색을
      거치지 않고 바로 해당 메일(스레드)을 연다("받은편지함에서 클릭한" 것과 동일한
      형태). **`/mail/u/0/`처럼 계정 슬롯 번호를 0으로 고정하면 안 된다** - 브라우저에
      Google 계정이 여러 개 로그인돼 있으면 "u/0"은 그중 아무 계정("가장 먼저 로그인한
      계정")이나 가리키고, 그 계정에 해당 스레드가 없으면 Gmail이 에러 없이 그냥 전체
      메일함 목록으로 조용히 빠진다(실제로 재현: 계정 2개 로그인된 브라우저에서 두
      번째 계정의 메일 링크를 열었더니 첫 번째 계정의 "전체보관함"이 열림) - 이게
      "링크를 눌러도 메일이 안 열린다"는 버그의 원인이었다. 대신 `authuser=<email>`
      쿼리 파라미터를 쓰면 Gmail이 그 이메일이 실제로 로그인된 슬롯(u/1, u/2, ...)을
      알아서 찾아 연결해준다(실제 브라우저로 검증: `/mail/?authuser=<email>#all/<hex>`
      -> `/mail/u/1/#all/<hex>`로 정상 리다이렉트되고 정확한 메일이 열림).
    - Naver: 사용자가 웹메일에서 메일을 직접 열어 확인한 URL(`/v2/read/0/<id>`)의 숫자
      부분이 IMAP UID와 정확히 일치함을 확인했다 - 별도 확장 조회 없이 이미 갖고 있는
      UID 그대로 `https://mail.naver.com/v2/read/0/<uid>` 링크를 만들면 된다("0"은
      INBOX를 가리키는 고정 폴더 인덱스로 보인다 - 이 파이프라인은 INBOX만 조회한다).
    - Outlook: 특정 메일을 여는 URL이 웹메일 세션 내부 ID를 필요로 해서 IMAP 정보만으로는
      아직 만들 수 없다 (계정 미연동 상태라 확인 보류).
    """
    if account_type == "gmail":
        if not thread_id or not account_user:
            return None
        try:
            hex_id = format(int(thread_id), "x")
        except ValueError:
            return None
        return f"https://mail.google.com/mail/?authuser={quote(account_user)}#all/{hex_id}"
    if account_type == "naver":
        if not uid:
            return None
        return f"https://mail.naver.com/v2/read/0/{uid}"
    return None


def build_date_chunks(
    since_date: datetime, chunk_days: int = CHUNK_DAYS
) -> list[tuple[datetime, datetime | None]]:
    """[since_date, now) 구간을 chunk_days 단위 반개구간 리스트로 쪼갠다.

    마지막 조각은 until=None(=SINCE만 사용, 지금까지 전부 포함)으로 남겨서
    조회 시점까지의 메일을 놓치지 않는다. 매일 실행처럼 범위가 chunk_days보다
    짧으면 조각이 하나뿐이라 기존 단일 쿼리와 동일하게 동작한다.
    """
    now = datetime.now()
    chunks: list[tuple[datetime, datetime | None]] = []
    cursor = since_date
    while cursor < now:
        nxt = cursor + timedelta(days=chunk_days)
        if nxt >= now:
            chunks.append((cursor, None))
            break
        chunks.append((cursor, nxt))
        cursor = nxt
    return chunks or [(since_date, None)]


def fetch_account_headers(
    account: dict, since_date: datetime, until_date: datetime | None = None
) -> list[dict]:
    server = IMAP_SERVERS[account["type"]]
    messages = []
    imap = imaplib.IMAP4_SSL(server, 993)
    try:
        authenticate(imap, account)
        imap.select("INBOX", readonly=True)
        since_str = since_date.strftime("%d-%b-%Y")
        if until_date is not None:
            until_str = until_date.strftime("%d-%b-%Y")
            criteria = f'(SINCE "{since_str}" BEFORE "{until_str}")'
        else:
            criteria = f'(SINCE "{since_str}")'
        status, data = imap.uid("search", None, criteria)
        if status != "OK":
            return messages
        uids = data[0].split()
        # UID 기반으로 조회한다 - 시퀀스 번호는 메일함에서 뭔가 지워지거나 옮겨지면
        # 재배치되지만, UID는 이후 휴지통 이동 등 액션을 걸 때도 안정적으로 같은
        # 메일을 가리킨다.
        # X-GM-THRID는 Gmail IMAP 서버만 지원하는 비표준 확장이라, 다른 프로바이더에
        # 요청하면 명령 자체가 거부될 수 있어 Gmail 계정일 때만 같이 요청한다.
        fetch_fields = "(UID BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE MESSAGE-ID)])"
        if account["type"] == "gmail":
            fetch_fields = "(UID X-GM-THRID BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE MESSAGE-ID)])"
        for i in range(0, len(uids), FETCH_BATCH_SIZE):
            batch = uids[i : i + FETCH_BATCH_SIZE]
            status, msg_data = imap.uid("fetch", b",".join(batch), fetch_fields)
            if status != "OK" or not msg_data:
                continue
            # 여러 메시지를 한 번에 요청하면 msg_data에 (헤더 튜플, b')' 닫힘 마커)가
            # 메시지 수만큼 번갈아 들어온다 - 튜플만 걸러서 처리한다.
            for item in msg_data:
                if not isinstance(item, tuple):
                    continue
                header_line, raw_header = item
                uid_match = UID_RE.search(header_line)
                uid = uid_match.group(1).decode() if uid_match else None
                thrid_match = GM_THRID_RE.search(header_line)
                thread_id = thrid_match.group(1).decode() if thrid_match else None
                msg = email.message_from_bytes(raw_header)
                subject = decode_mime(msg.get("Subject", ""))
                sender = extract_sender(msg.get("From", ""))
                # 접힌/변조된 헤더에서 온 CR/LF는 제거한다 - 나중에 이 값을 IMAP SEARCH
                # 명령에 끼워 넣으므로(actions.find_message_uid_by_id), 개행이 남아 있으면
                # 명령 인젝션이 된다.
                message_id = (msg.get("Message-ID") or "").replace("\r", "").replace("\n", "").strip() or None
                messages.append(
                    {
                        "subject": subject,
                        "sender": sender,
                        "uid": uid,
                        "message_id": message_id,
                        "web_link": build_web_link(account["type"], uid, thread_id, account["user"]),
                        "message_date": parse_message_date(msg.get("Date")),
                        "account": account["user"],
                        "account_type": account["type"],
                    }
                )
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return messages


def _extract_text_body(msg) -> str:
    """MIME 메시지에서 text/plain 본문을 뽑아낸다. 못 찾으면 빈 문자열(HTML 전용
    메일 등) - contents 키워드 매칭이 그냥 안 걸리는 것으로 자연스럽게 처리된다."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() != "text/plain":
                continue
            if part.get("Content-Disposition", "").startswith("attachment"):
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                return payload.decode(charset, errors="ignore")
            except LookupError:
                return payload.decode("utf-8", errors="ignore")
        return ""

    payload = msg.get_payload(decode=True)
    if not payload:
        return ""
    charset = msg.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="ignore")
    except LookupError:
        return payload.decode("utf-8", errors="ignore")


def fetch_body_texts(account: dict, uids: list[str]) -> dict[str, str]:
    """주어진 UID들의 본문 텍스트를 UID -> 본문 딕셔너리로 가져온다.

    categories.json의 contents 키워드 매칭은 헤더만으로 안 되므로, senders/title로
    분류가 안 된 메일에 대해서만(fetch_mail.py가 골라서) 호출되는 2차 조회다 -
    항상 본문까지 가져오면 배치 fetch로 얻은 속도 이점이 사라지기 때문에, 정말
    필요한 메일에 대해서만 이 함수를 쓴다.
    """
    if not uids:
        return {}
    server = IMAP_SERVERS[account["type"]]
    texts: dict[str, str] = {}
    imap = imaplib.IMAP4_SSL(server, 993)
    try:
        authenticate(imap, account)
        imap.select("INBOX", readonly=True)
        uid_bytes = [u.encode() for u in uids]
        for i in range(0, len(uid_bytes), FETCH_BATCH_SIZE):
            batch = uid_bytes[i : i + FETCH_BATCH_SIZE]
            status, msg_data = imap.uid("fetch", b",".join(batch), "(UID BODY.PEEK[])")
            if status != "OK" or not msg_data:
                continue
            for item in msg_data:
                if not isinstance(item, tuple):
                    continue
                header_line, raw_message = item
                uid_match = UID_RE.search(header_line)
                if not uid_match:
                    continue
                msg = email.message_from_bytes(raw_message)
                texts[uid_match.group(1).decode()] = _extract_text_body(msg)
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return texts
