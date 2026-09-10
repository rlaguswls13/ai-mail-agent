"""IMAP 계정 인증 한 곳 - 비밀번호(LOGIN) vs OAuth2(XOAUTH2).

호출부는 예전처럼 `imaplib.IMAP4_SSL(...)` 로 커넥션을 만든 뒤(테스트가 이 지점을
monkeypatch 하므로 그대로 둔다) `authenticate(imap, account)` 한 줄만 호출한다.

인증 방식은 `mail_core.accounts.account_auth(account)` 가 정한다: 명시적 `auth`
필드 우선, 없으면 타입 기본값(outlook=xoauth2, 그 외=password). Outlook 은 서버가
비밀번호를 거부하고 Gmail 은 선택 가능하다.

OAuth 토큰 문제(미로그인/갱신 실패)는 imaplib.IMAP4.error 로 바꿔서 던진다 -
조회/액션 경로가 이미 그 예외를 계정 단위 실패로 잡고 있기 때문.
"""
import imaplib

from mail_core import oauth
from mail_core.accounts import account_auth


def authenticate(imap: imaplib.IMAP4, account: dict) -> None:
    if account_auth(account) == "xoauth2":
        provider = account.get("type", "")
        try:
            token = oauth.access_token(provider, account["user"])
        except oauth.OAuthError as e:
            raise imaplib.IMAP4.error(str(e)) from e
        sasl = oauth.xoauth2_string(account["user"], token)
        imap.authenticate("XOAUTH2", lambda _challenge: sasl)
    else:
        imap.login(account["user"], account["password"])
