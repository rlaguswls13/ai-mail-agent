"use strict";
/*
 * 메일 IMAP OAuth2 최초 로그인 자식 프로세스 구동 (Outlook: device code / Gmail: loopback).
 *
 * `python -m mail_app.oauth_login --json` 을 띄우고 stdout 의 NDJSON 이벤트를
 * 파싱한다. flask.js 와 같은 방식으로 자격증명을 자식 stdin 첫 줄로 넘긴다
 * (env=@stdin 센티널) - 프로세스 환경 블록에 평문 비밀번호가 남지 않게.
 *
 * 이벤트: {"event":"prompt", user, provider, verification_uri, user_code, message}
 *         {"event":"result", user, provider, status:"ok"|"skip"|"fail", detail?}
 *         {"event":"status", user, provider, status:"ok"|"missing"|"revoked"}   // check()
 *         {"event":"done", ok}
 */
const path = require("node:path");
const { spawn } = require("node:child_process");

/**
 * @param {{
 *   pythonPath: string, repoRoot?: string|null,
 *   extraEnv?: Record<string,string>, accountsJson?: string|null,
 *   force?: boolean, user?: string|null, check?: boolean,
 *   onPrompt?: (ev: object) => void,
 * }} opts
 * @returns {Promise<{ok: boolean, results: object[], statuses: object[], error?: string, exitCode?: number}>}
 */
function run(opts) {
  const {
    pythonPath, repoRoot = null, extraEnv = {}, accountsJson = null,
    force = false, user = null, check = false, onPrompt,
  } = opts;

  return new Promise((resolve) => {
    const env = { ...process.env, ...extraEnv, PYTHONIOENCODING: "utf-8", PYTHONUTF8: "1" };
    if (accountsJson) env.MAIL_AGENT_ACCOUNTS = "@stdin";
    else delete env.MAIL_AGENT_ACCOUNTS;
    if (repoRoot) {
      const pkgs = ["mail-core", "mail-app", "admin-ui"].map((p) => path.join(repoRoot, "packages", p));
      env.PYTHONPATH = [...pkgs, env.PYTHONPATH].filter(Boolean).join(path.delimiter);
    }

    const args = ["-m", "mail_app.oauth_login", "--json"];
    if (check) args.push("--check");
    if (force) args.push("--force");
    if (user) args.push("--user", user);

    let child;
    try {
      child = spawn(pythonPath, args, {
        cwd: repoRoot || undefined,
        env,
        stdio: [accountsJson ? "pipe" : "ignore", "pipe", "pipe"],
        windowsHide: true,
      });
    } catch (err) {
      resolve({ ok: false, results: [], error: err.message });
      return;
    }

    if (accountsJson) {
      child.stdin.write(accountsJson.replace(/\s*$/, "") + "\n");
      child.stdin.end();
      child.stdin.on("error", (e) => console.warn("[oauth-login] stdin:", e.message));
    }

    const result = { ok: false, results: [], statuses: [] };
    let buf = "";
    child.stdout.on("data", (d) => {
      buf += d.toString();
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (!line) continue;
        let ev;
        try {
          ev = JSON.parse(line);
        } catch {
          console.log("[oauth-login]", line);
          continue;
        }
        if (ev.event === "prompt") onPrompt && onPrompt(ev);
        else if (ev.event === "result") result.results.push(ev);
        else if (ev.event === "status") result.statuses.push(ev);
        else if (ev.event === "done") result.ok = !!ev.ok;
      }
    });
    child.stderr.on("data", (d) => process.stderr.write(`[oauth-login] ${d}`));
    child.on("error", (err) => resolve({ ok: false, results: [], error: err.message }));
    child.on("exit", (code) => resolve({ ...result, exitCode: code }));
  });
}

/**
 * device flow 를 시작하지 않고 계정별 토큰 상태만 조회한다(빠름, 비대화형).
 * @returns {Promise<{ok: boolean, statuses: {user:string,status:string}[], error?: string}>}
 */
function check(opts) {
  return run({ ...opts, check: true });
}

module.exports = { run, check };
