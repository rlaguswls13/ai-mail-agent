"use strict";
/*
 * 미적용 카테고리 액션(save/trash/read 대상인데 아직 INBOX 에 그대로인 메일)을
 * 세거나 적용한다. `python -m mail_app.apply_pending`.
 *
 *  count(): `--count` → {total, by_action, by_account}  (DB만, 빠름, 비대화형)
 *  apply(): `--json`  → NDJSON {event:"account"...} + {event:"done", applied, failed, accounts}
 *
 * 자격증명은 flask.js 와 동일하게 자식 stdin 첫 줄로 주입(env=@stdin 센티널).
 */
const path = require("node:path");
const { spawn } = require("node:child_process");

function buildEnv({ extraEnv = {}, accountsJson = null, repoRoot = null }) {
  const env = { ...process.env, ...extraEnv, PYTHONIOENCODING: "utf-8", PYTHONUTF8: "1" };
  if (accountsJson) env.MAIL_AGENT_ACCOUNTS = "@stdin";
  else delete env.MAIL_AGENT_ACCOUNTS;
  if (repoRoot) {
    const pkgs = ["mail-core", "mail-app", "admin-ui"].map((p) => path.join(repoRoot, "packages", p));
    env.PYTHONPATH = [...pkgs, env.PYTHONPATH].filter(Boolean).join(path.delimiter);
  }
  return env;
}

function spawnPy(opts, extraArgs, { onLine, wantStdin }) {
  const { pythonPath, repoRoot = null, accountsJson = null } = opts;
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn(pythonPath, ["-m", "mail_app.apply_pending", ...extraArgs], {
        cwd: repoRoot || undefined,
        env: buildEnv(opts),
        stdio: [wantStdin && accountsJson ? "pipe" : "ignore", "pipe", "pipe"],
        windowsHide: true,
      });
    } catch (err) {
      resolve({ error: err.message });
      return;
    }
    if (wantStdin && accountsJson) {
      child.stdin.write(accountsJson.replace(/\s*$/, "") + "\n");
      child.stdin.end();
      child.stdin.on("error", (e) => console.warn("[apply-pending] stdin:", e.message));
    }
    let buf = "";
    child.stdout.on("data", (d) => {
      buf += d.toString();
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl).trim();
        buf = buf.slice(nl + 1);
        if (line) onLine(line);
      }
    });
    child.stderr.on("data", (d) => process.stderr.write(`[apply-pending] ${d}`));
    child.on("error", (err) => resolve({ error: err.message }));
    child.on("exit", (code) => {
      if (buf.trim()) onLine(buf.trim());
      resolve({ exitCode: code });
    });
  });
}

/** 미적용 건수. {total, by_action, by_account} 또는 {error}. */
async function count(opts) {
  let parsed = null;
  const res = await spawnPy(opts, ["--count"], {
    wantStdin: false,
    onLine: (line) => {
      try {
        parsed = JSON.parse(line);
      } catch {
        console.log("[apply-pending]", line);
      }
    },
  });
  if (res.error) return { error: res.error };
  return parsed || { total: 0, by_action: {}, by_account: {} };
}

/** 실제 적용. {applied, failed, accounts} 또는 {error}. */
async function apply(opts) {
  const result = { applied: 0, failed: 0, accounts: [] };
  const res = await spawnPy(opts, ["--json"], {
    wantStdin: true,
    onLine: (line) => {
      let ev;
      try {
        ev = JSON.parse(line);
      } catch {
        console.log("[apply-pending]", line);
        return;
      }
      if (ev.event === "account") result.accounts.push(ev);
      else if (ev.event === "done") {
        result.applied = ev.applied || 0;
        result.failed = ev.failed || 0;
      }
    },
  });
  if (res.error) return { error: res.error };
  return result;
}

module.exports = { count, apply };
