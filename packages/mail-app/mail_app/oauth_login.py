"""메일 계정 IMAP OAuth2 최초 로그인 (Outlook: device code / Gmail: loopback).

accounts.yaml(또는 데스크톱 볼트)에 `auth: xoauth2` 로 표시된 계정마다 브라우저
동의를 받아 refresh token 을 `<config>/<provider>_token.json` 에 저장한다. 유효한
토큰이 이미 있으면 건너뛴다. `--force` 로 재로그인, `--user` 로 특정 계정만.

    python -m mail_app.oauth_login

`--json` 은 데스크톱 셸(Electron)이 stdout 을 파싱할 수 있게 NDJSON 이벤트를 낸다:
    {"event": "prompt",  "user", "provider", "verification_uri", "user_code", "message"}
    {"event": "result",  "user", "provider", "status": "ok"|"skip"|"fail", "detail"?}
    {"event": "status",  "user", "provider", "status": "ok"|"missing"|"revoked"}   # --check
    {"event": "done",    "ok": true|false}

`--check` 는 브라우저 플로우를 절대 시작하지 않고 계정별 토큰 상태만 보고한다.

`--no-interactive` 는 `oauth.access_token()`의 조용한 refresh_token 갱신(실제 만료
120초 전부터, `mail_core.oauth._REFRESH_SKEW`)까지는 그대로 시도하되, 그게 실패하면
(refresh_token 자체가 죽었거나 없음 - 흔치 않음) 브라우저를 띄우는 `oauth.login()`
으로 넘어가지 않고 그 계정만 실패 처리한다. 사람이 없는 자동 스케줄러 실행용.
"""
import argparse
import json
import sys

from mail_core import oauth
from mail_core.accounts import account_auth, load_accounts, resolve_accounts_from_stdin

from mail_app import app_paths

ACCOUNTS_PATH = app_paths.config_dir() / "accounts.yaml"


def _emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def main(argv=None) -> int:
    # 데스크톱 셸이 자격증명을 stdin 으로 넘긴 경우(env=@stdin 센티널)를 먼저 해석한다.
    resolve_accounts_from_stdin()

    ap = argparse.ArgumentParser(description="메일 IMAP OAuth2 로그인")
    ap.add_argument("--user", help="이 이메일만 (기본: xoauth2 계정 전부)")
    ap.add_argument("--force", action="store_true", help="유효한 토큰이 있어도 다시 로그인")
    ap.add_argument("--json", action="store_true", help="NDJSON 이벤트 출력 (데스크톱 셸용)")
    ap.add_argument("--check", action="store_true", help="토큰 상태만 보고하고 로그인은 안 함")
    ap.add_argument("--no-browser", action="store_true", help="브라우저 자동 오픈 안 함(URL만 출력)")
    ap.add_argument(
        "--no-interactive", action="store_true",
        help="조용한 토큰 갱신만 시도하고, 그게 실패해도 브라우저 로그인으로 넘어가지 않음(스케줄러용)",
    )
    args = ap.parse_args(argv)

    as_json = args.json

    def log(msg: str, *, err: bool = False) -> None:
        if not as_json:
            print(msg, file=sys.stderr if err else sys.stdout)

    targets = [
        a for a in load_accounts(ACCOUNTS_PATH)
        if account_auth(a) == "xoauth2" and a.get("type") in oauth.OAUTH_TYPES
    ]
    if args.user:
        targets = [a for a in targets if a["user"] == args.user]
    if not targets:
        log("OAuth 로그인이 필요한 계정이 없습니다 (auth: xoauth2).", err=True)
        if as_json:
            _emit({"event": "done", "ok": False, "detail": "no-oauth-account"})
        return 1

    if args.check:
        all_ok = True
        for a in targets:
            st = oauth.token_status(a["type"], a["user"])
            all_ok = all_ok and st == "ok"
            log(f"{a['user']} ({a['type']}): {st}")
            if as_json:
                _emit({"event": "status", "user": a["user"], "provider": a["type"], "status": st})
        if as_json:
            _emit({"event": "done", "ok": all_ok})
        return 0

    failed = 0
    for a in targets:
        user, provider = a["user"], a["type"]
        if not args.force:
            try:
                oauth.access_token(provider, user)
                log(f"[skip] {user}: 이미 유효한 토큰이 있습니다")
                if as_json:
                    _emit({"event": "result", "user": user, "provider": provider, "status": "skip"})
                continue
            except oauth.OAuthError as e:
                if args.no_interactive:
                    log(f"[fail] {user}: 조용한 토큰 갱신 실패, 대화형 로그인은 건너뜀 ({e})", err=True)
                    if as_json:
                        _emit({
                            "event": "result", "user": user, "provider": provider,
                            "status": "fail", "detail": f"no-interactive: {e}",
                        })
                    failed += 1
                    continue

        log(f"[login] {user} ({provider})")

        def on_prompt(info: dict, _u=user, _p=provider) -> None:
            if as_json:
                _emit({"event": "prompt", "user": _u, "provider": _p, **info})
            else:
                print(info["message"])
                print(info["verification_uri"])

        try:
            oauth.login(provider, user, on_prompt=on_prompt, open_browser=not args.no_browser)
            log(f"[ok]   {user}: 토큰 저장 완료")
            if as_json:
                _emit({"event": "result", "user": user, "provider": provider, "status": "ok"})
        except oauth.OAuthError as e:
            log(f"[fail] {user}: {e}", err=True)
            if as_json:
                _emit({"event": "result", "user": user, "provider": provider,
                       "status": "fail", "detail": str(e)})
            failed += 1

    if as_json:
        _emit({"event": "done", "ok": failed == 0})
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
