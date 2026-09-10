"""Outlook 계정 IMAP OAuth2 최초 로그인 (device code flow).

accounts.yaml(또는 데스크톱 볼트)에 등록된 `type: outlook` 계정마다 브라우저 동의를
받아 refresh token 을 config/outlook_token.json 에 저장한다. 유효한 토큰이 이미 있으면
건너뛴다. `--force` 로 재로그인, `--user` 로 특정 계정만.

    python -m mail_app.outlook_login
"""
import argparse
import sys

from mail_core import oauth_outlook
from mail_core.accounts import load_accounts

from mail_app import app_paths

ACCOUNTS_PATH = app_paths.config_dir() / "accounts.yaml"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Outlook IMAP OAuth2 로그인")
    ap.add_argument("--user", help="이 이메일만 로그인 (기본: outlook 계정 전부)")
    ap.add_argument("--force", action="store_true", help="유효한 토큰이 있어도 다시 로그인")
    args = ap.parse_args(argv)

    outlook = [a for a in load_accounts(ACCOUNTS_PATH) if a.get("type") == "outlook"]
    if args.user:
        outlook = [a for a in outlook if a["user"] == args.user]
    if not outlook:
        print("대상 outlook 계정이 없습니다 (config/accounts.yaml 확인).", file=sys.stderr)
        return 1

    failed = 0
    for account in outlook:
        user = account["user"]
        if not args.force:
            try:
                oauth_outlook.access_token(user)
                print(f"[skip] {user}: 이미 유효한 토큰이 있습니다")
                continue
            except oauth_outlook.OutlookAuthError:
                pass
        print(f"[login] {user}")
        try:
            oauth_outlook.device_login(user)
            print(f"[ok]   {user}: 토큰 저장 완료")
        except oauth_outlook.OutlookAuthError as e:
            print(f"[fail] {user}: {e}", file=sys.stderr)
            failed += 1

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
