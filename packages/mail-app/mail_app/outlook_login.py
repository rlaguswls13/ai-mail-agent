"""Outlook 계정 IMAP OAuth2 최초 로그인 (device code flow).

accounts.yaml(또는 데스크톱 볼트)에 등록된 `type: outlook` 계정마다 브라우저 동의를
받아 refresh token 을 config/outlook_token.json 에 저장한다. 유효한 토큰이 이미 있으면
건너뛴다. `--force` 로 재로그인, `--user` 로 특정 계정만.

    python -m mail_app.outlook_login

`--json` 은 데스크톱 셸(Electron)이 stdout 을 파싱할 수 있게 NDJSON 이벤트를 낸다:
    {"event": "prompt",  "user": ..., "verification_uri": ..., "user_code": ...}
    {"event": "result",  "user": ..., "status": "ok" | "skip" | "fail", "detail": ...}
    {"event": "done",    "ok": true | false}
"""
import argparse
import json
import sys

from mail_core import oauth_outlook
from mail_core.accounts import load_accounts, resolve_accounts_from_stdin

from mail_app import app_paths

ACCOUNTS_PATH = app_paths.config_dir() / "accounts.yaml"


def _emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def main(argv=None) -> int:
    # 데스크톱 셸이 자격증명을 stdin 으로 넘긴 경우(env=@stdin 센티널)를 먼저 해석한다 -
    # `python -m admin_ui` 진입점과 동일. 없으면 accounts.yaml 폴백.
    resolve_accounts_from_stdin()

    ap = argparse.ArgumentParser(description="Outlook IMAP OAuth2 로그인")
    ap.add_argument("--user", help="이 이메일만 로그인 (기본: outlook 계정 전부)")
    ap.add_argument("--force", action="store_true", help="유효한 토큰이 있어도 다시 로그인")
    ap.add_argument("--json", action="store_true", help="NDJSON 이벤트 출력 (데스크톱 셸용)")
    args = ap.parse_args(argv)

    as_json = args.json

    def log(msg: str, *, err: bool = False) -> None:
        if not as_json:
            print(msg, file=sys.stderr if err else sys.stdout)

    outlook = [a for a in load_accounts(ACCOUNTS_PATH) if a.get("type") == "outlook"]
    if args.user:
        outlook = [a for a in outlook if a["user"] == args.user]
    if not outlook:
        log("대상 outlook 계정이 없습니다 (config/accounts.yaml 확인).", err=True)
        if as_json:
            _emit({"event": "done", "ok": False, "detail": "no-outlook-account"})
        return 1

    failed = 0
    for account in outlook:
        user = account["user"]
        if not args.force:
            try:
                oauth_outlook.access_token(user)
                log(f"[skip] {user}: 이미 유효한 토큰이 있습니다")
                if as_json:
                    _emit({"event": "result", "user": user, "status": "skip"})
                continue
            except oauth_outlook.OutlookAuthError:
                pass

        log(f"[login] {user}")

        def on_prompt(info: dict, _user=user) -> None:
            if as_json:
                _emit({"event": "prompt", "user": _user, **info})
            else:
                print(info["message"])

        try:
            oauth_outlook.device_login(user, on_prompt=on_prompt)
            log(f"[ok]   {user}: 토큰 저장 완료")
            if as_json:
                _emit({"event": "result", "user": user, "status": "ok"})
        except oauth_outlook.OutlookAuthError as e:
            log(f"[fail] {user}: {e}", err=True)
            if as_json:
                _emit({"event": "result", "user": user, "status": "fail", "detail": str(e)})
            failed += 1

    if as_json:
        _emit({"event": "done", "ok": failed == 0})
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
