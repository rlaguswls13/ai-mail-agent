"use strict";
// npm 스크립트에서 Python 을 부르는 얇은 래퍼. Windows 의 `python` 은 보통 PATH 에
// WindowsApps 스토어 스텁이라 안 통한다. 후보: $PYTHON → python
// → PATH 의 python3/python(스토어 스텁 제외).
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");

const CANDIDATES = [
  process.env.PYTHON,
  "D:\\dev-tool\\python\\python.exe",
  "python3",
  "python",
];

function usable(cmd) {
  if (!cmd) return false;
  if (cmd.includes("/") || cmd.includes("\\")) return fs.existsSync(cmd);
  const r = spawnSync(cmd, ["-c", "import sys;print(sys.version)"], { encoding: "utf-8" });
  return r.status === 0 && !/WindowsApps/i.test(r.error?.path || "");
}

const py = CANDIDATES.find(usable);
if (!py) {
  console.error("Python 을 찾지 못했습니다. PYTHON 환경변수로 지정하세요.");
  process.exit(1);
}

const args = process.argv.slice(2);
const res = spawnSync(py, args, { stdio: "inherit", cwd: process.cwd() });
process.exit(res.status ?? 1);
