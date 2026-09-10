"""IMAP 계정 인증 한 곳 - Gmail/Naver 는 비밀번호(LOGIN), Outlook 은 XOAUTH2.

호출부는 예전처럼 `imaplib.IMAP4_SSL(...)` 로 커넥션을 만든 뒤(테스트가 이 지점을
monkeypatch 하므로 그대로 둔다) `authenticate(imap, account)` 한 줄만 호출한다.

Outlook 토큰 문제(미로그인/갱신 실패)는 imaplib.IMAP4.error 로 바꿔서 던진다 -
조회/액션 경로가 이미 그 예외를 계정 단위 실패로 잡고 있기 때문.
"""
import imaplib

from mail_core import oauth_outlook


def authenticate(imap: imaplib.IMAP4, account: dict) -> None:
    if account.get("type") == "outlook":
        try:
            token = oauth_outlook.access_token(account["user"])
        except oauth_outlook.OutlookAuthError as e:
            raise imaplib.IMAP4.error(str(e)) from e
        sasl = oauth_outlook.xoauth2_string(account["user"], token)
        imap.authenticate("XOAUTH2", lambda _challenge: sasl)
    else:
        imap.login(account["user"], account["password"])
