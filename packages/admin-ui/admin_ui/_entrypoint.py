"""`python -m admin_ui` 부팅 전 처리 - 자격증명 stdin 주입 해석.

`admin_app` 을 import 하기 전에 실행되어야 하므로 여기에 따로 둔다(부작용 없이 import
가능하게 해서 테스트하기 쉽게).

구현 자체는 `mail_core.accounts` 로 옮겼다(센티널 계약의 소유자이고, 다른 진입점
`python -m mail_app.outlook_login` 도 같은 함수를 쓴다). 여기서는 재노출만 한다.
"""
from mail_core.accounts import resolve_accounts_from_stdin

__all__ = ["resolve_accounts_from_stdin"]
