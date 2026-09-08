"""mail_core — Gmail/Naver/Outlook IMAP 조회 + 규칙 기반 분류.

표준 라이브러리만 사용하는 재사용 가능한 레이어. SQLite 영속이나 앱 고유 경로에
의존하지 않는다 — 계정 목록/카테고리 dict/메시지 list 를 인자로 받아 동작한다.
"""
__version__ = "0.2.0"
