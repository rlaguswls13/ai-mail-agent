"use strict";
/*
 * src/admin_app.py (Flask 127.0.0.1:<port>) 자식 프로세스 수명주기.
 *  - 이미 그 포트가 살아있으면(스테일 프로세스 등) 새로 띄우지 않고 재사용
 *  - 헬스 폴링으로 준비될 때까지 대기
 *  - 앱 종료 시 stop()으로 확실히 정리 → "포트 5000 스테일 프로세스" gotcha 해결
 */
const path = require("node:path");
const http = require("node:http");
const { spawn } = require("node:child_process");

/** @type {import('node:child_process').ChildProcess | null} */
let child = null;
/** 마지막 start() 옵션 — restart() 가 재사용한다. */
let lastOpts = null;

function ping(port) {
  return new Promise((resolve) => {
    const req = http.get(
      { host: "127.0.0.1", port, path: "/", timeout: 1200 },
      (res) => {
        res.resume();
        resolve(res.statusCode > 0);
      },
    );
    req.on("error", () => resolve(false));
    req.on("timeout", () => {
      req.destroy();
      resolve(false);
    });
  });
}

async function waitForFlask(port, timeoutMs = 20000) {
  const start = Date.now();
  // eslint-disable-next-line no-constant-condition
  while (true) {
    if (await ping(port)) return;
    if (Date.now() - start > timeoutMs) throw new Error(`Flask 시작 타임아웃 (${port})`);
    await new Promise((r) => setTimeout(r, 400));
  }
}

/**
 * @param {{pythonPath: string, port: number, repoRoot: string, extraEnv?: Record<string,string>}} opts
 * @returns {Promise<{reused: boolean, pid?: number}>}
 */
async function start({ pythonPath, port, repoRoot, extraEnv = {} }) {
  lastOpts = { pythonPath, port, repoRoot, extraEnv };
  if (await ping(port)) return { reused: true };

  const script = path.join(repoRoot, "src", "admin_app.py");
  child = spawn(pythonPath, [script], {
    cwd: repoRoot,
    env: { ...process.env, ...extraEnv, PYTHONIOENCODING: "utf-8", PYTHONUTF8: "1" },
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });
  child.stdout.on("data", (d) => process.stdout.write(`[flask] ${d}`));
  child.stderr.on("data", (d) => process.stderr.write(`[flask] ${d}`));
  child.on("exit", (code, sig) => {
    console.log(`[flask] exited code=${code} sig=${sig}`);
    child = null;
  });

  await waitForFlask(port);
  return { reused: false, pid: child ? child.pid : undefined };
}

function stop() {
  if (child && !child.killed) {
    child.kill(); // Windows: TerminateProcess. admin_app는 단일 프로세스 dev server라 이걸로 정리됨.
    child = null;
  }
}

/**
 * 자식 Flask 를 죽이고 새 환경(주로 갱신된 MAIL_AGENT_ACCOUNTS)으로 다시 띄운다.
 * 계정을 편집한 뒤 파이프라인이 새 자격증명을 쓰게 하려면 필요하다.
 * @param {Record<string,string>} extraEnv
 */
async function restart(extraEnv) {
  if (!lastOpts) throw new Error("flask.start() 가 먼저 호출돼야 합니다.");
  stop();
  // 자식이 포트를 완전히 놓을 때까지 잠깐 대기.
  for (let i = 0; i < 20 && (await ping(lastOpts.port)); i++) {
    await new Promise((r) => setTimeout(r, 200));
  }
  return start({ ...lastOpts, extraEnv: extraEnv || {} });
}

module.exports = { start, stop, restart, ping };
