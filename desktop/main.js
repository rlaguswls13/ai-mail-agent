"use strict";
/*
 * ai-mail-agent 데스크톱 셸 — Electron main 프로세스.
 *
 * Phase 1: src/admin_app.py(Flask) 자식 spawn → 헬스 폴링 → 창에서
 *   http://127.0.0.1:<port> 로드. 트레이 상주(닫기 = 트레이로 숨김, 트레이 "종료" = 진짜 종료).
 *   종료 시 Flask 자식 kill → 포트 스테일 프로세스 gotcha 해결.
 * Phase 2: scheduler.js — 앱 내부 스케줄러(매일 06:00 dry-run /sync) + 트레이
 *   "지금 동기화"·"자동 실행" 토글·다음/마지막 실행 표시 + 놓친 실행 자가복구.
 * Phase 3: vault.js — safeStorage(DPAPI) 자격증명 볼트. 시작 시 accounts.yaml 자동
 *   마이그레이션 → 복호화 → MAIL_AGENT_ACCOUNTS 로 Flask 자식에 주입. 트레이
 *   "계정 설정…" → renderer/settings.html 에서 CRUD → Flask 자식 재시작으로 즉시 반영.
 * Phase 4: electron-builder 포터블 exe 패키징 + 로그인 자동 실행 토글.
 * Phase 5: 완전 독립 실행. 번들된 Python(python-embed + Flask vendor, resources/python)과
 *   파이프라인(resources/pipeline/src)을 쓰고, 데이터는 userData/data (MAIL_AGENT_DATA_DIR).
 *   cfg.repoPath 지정 시 "checkout" 모드(파워유저용).
 * 그 외: 데이터 폴더 EFS 암호화(applyEfsEncryption) · GitHub Releases 업데이트 확인
 *   (updater.js, 외부 패키지 없음) · 페이지네이션 UI 정리.
 */
const path = require("node:path");
const { app, BrowserWindow, Tray, Menu, shell, dialog, nativeImage, ipcMain } = require("electron");
const config = require("./config");
const flask = require("./flask");
const scheduler = require("./scheduler");
const vault = require("./vault");
const updater = require("./updater");

const fs = require("node:fs");
const DEV_ROOT = path.resolve(__dirname, "..");
const TRAY_ICON = path.join(__dirname, "assets", "tray.png");
const APP_ICON = path.join(__dirname, "assets", "icon.png");
// 패키징 실행: extraResources 로 번들된 Python 과 파이프라인.
const BUNDLED_PY = path.join(process.resourcesPath || "", "python", "python.exe");
const BUNDLED_PIPELINE = path.join(process.resourcesPath || "", "pipeline");

/** <dir>/src/admin_app.py 가 실제로 있는지. */
function looksLikeRepo(dir) {
  return !!dir && fs.existsSync(path.join(dir, "src", "admin_app.py"));
}

// 실행 모드에 따라 결정되는 값 (resolveRuntime 에서 채움).
let repoRoot = DEV_ROOT;     // 파이프라인 스크립트가 있는 곳 (<repoRoot>/src/*.py)
let pythonExe = null;        // 쓸 python
let dataDir = null;          // app.db / dashboard.html 이 살 디렉터리 (null = repo/data 기본)
let runtimeMode = "dev";     // "dev" | "bundled" | "checkout"

function accountsYaml() {
  return path.join(repoRoot, "src", "config", "accounts.yaml");
}

/** Flask/스케줄러 자식에 넘길 공통 env. */
function pipelineEnv() {
  const env = { ...accountsEnv() };
  if (dataDir) env.MAIL_AGENT_DATA_DIR = dataDir;
  return env;
}

/** @type {BrowserWindow | null} */
let mainWindow = null;
/** @type {BrowserWindow | null} */
let settingsWindow = null;
/** @type {Tray | null} */
let tray = null;
let cfg = config.DEFAULTS;
let flaskInfo = { reused: false };
app.isQuitting = false;

function baseUrl() {
  return `http://127.0.0.1:${cfg.flaskPort}`;
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1320,
    height: 920,
    title: "ai-mail-agent",
    icon: APP_ICON,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  mainWindow.loadURL(baseUrl());
  mainWindow.once("ready-to-show", () => mainWindow && mainWindow.show());

  // 외부 링크(메일 딥링크 등)는 기본 브라우저로.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("http://127.0.0.1") || url.startsWith("http://localhost")) {
      return { action: "allow" };
    }
    shell.openExternal(url);
    return { action: "deny" };
  });
  mainWindow.webContents.on("will-navigate", (e, url) => {
    if (!url.startsWith(baseUrl())) {
      e.preventDefault();
      shell.openExternal(url);
    }
  });

  // 창 닫기 = 트레이로 숨김 (트레이 "종료"를 눌러야 앱이 끝난다).
  mainWindow.on("close", (e) => {
    if (!app.isQuitting) {
      e.preventDefault();
      mainWindow.hide();
    }
  });
  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

function showWindow() {
  if (!mainWindow) {
    createWindow();
    return;
  }
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}

// --- 자동 실행 ---

/** OS 로그인 시 자동 실행 등록/해제. 개발 실행(npm start)에서는 무의미하니 스킵. */
function applyAutoLaunch(enabled) {
  if (!app.isPackaged) return;
  try {
    app.setLoginItemSettings({ openAtLogin: !!enabled, args: [] });
  } catch (err) {
    console.error("[main] setLoginItemSettings 실패:", err.message);
  }
}

function toggleAutoLaunch(enabled) {
  cfg = config.update({ autoLaunch: !!enabled });
  applyAutoLaunch(cfg.autoLaunch);
  console.log("[main] 자동 실행:", enabled ? "켬" : "끔");
  refreshTray();
}

// --- 자격증명 볼트 ---

/** 볼트 계정 → Flask 자식에 줄 환경변수. 볼트가 비었거나 불가면 {} (accounts.yaml 폴백). */
function accountsEnv() {
  const accounts = vault.isAvailable() ? vault.read() : null;
  if (accounts && accounts.length) {
    return { MAIL_AGENT_ACCOUNTS: JSON.stringify(accounts) };
  }
  return {};
}

/** 계정 편집 후 파이프라인이 새 자격증명을 쓰도록 Flask 자식을 재시작한다. */
async function restartFlaskWithAccounts() {
  try {
    flaskInfo = await flask.restart(pipelineEnv());
    if (mainWindow) mainWindow.webContents.reload();
    refreshTray();
  } catch (err) {
    console.error("[main] Flask 재시작 실패:", err.message);
    dialog.showErrorBox("Flask 재시작 실패", `계정 변경은 저장됐지만 파이프라인 재시작에 실패했습니다.\n앱을 다시 시작하세요.\n\n${err.message}`);
  }
}

function createSettingsWindow() {
  settingsWindow = new BrowserWindow({
    width: 640,
    height: 640,
    title: "계정 설정 — ai-mail-agent",
    icon: APP_ICON,
    parent: mainWindow || undefined,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  settingsWindow.loadFile(path.join(__dirname, "renderer", "settings.html"));
  settingsWindow.on("closed", () => {
    settingsWindow = null;
  });
}

function showSettings() {
  if (!settingsWindow) {
    createSettingsWindow();
    return;
  }
  settingsWindow.show();
  settingsWindow.focus();
}

let accountIpcRegistered = false;

/** renderer/settings.html 이 쓰는 계정 CRUD IPC. 편집 성공 시 Flask 자식 재시작. */
function registerAccountIpc() {
  if (accountIpcRegistered) return;
  accountIpcRegistered = true;

  ipcMain.handle("accounts:status", () => ({
    encryptionAvailable: vault.isAvailable(),
    vaultPath: vault.vaultPath(),
    count: vault.isAvailable() ? vault.list().length : 0,
  }));

  // 비밀번호는 렌더러로 돌려보내지 않는다 — 목록 표시에 필요 없다.
  ipcMain.handle("accounts:list", () =>
    (vault.isAvailable() ? vault.list() : []).map((a) => ({ type: a.type, user: a.user })),
  );

  const mutate = async (fn) => {
    try {
      fn();
      await restartFlaskWithAccounts();
      return { ok: true };
    } catch (err) {
      return { error: err.message || String(err) };
    }
  };

  ipcMain.handle("accounts:add", (_e, account) => mutate(() => vault.add(account)));
  ipcMain.handle("accounts:update", (_e, { originalUser, account }) =>
    mutate(() => vault.update(originalUser, account)),
  );
  ipcMain.handle("accounts:remove", (_e, user) => mutate(() => vault.remove(user)));

  ipcMain.on("settings:close", () => settingsWindow && settingsWindow.close());
}

function buildTrayMenu() {
  return Menu.buildFromTemplate([
    { label: "열기", click: showWindow },
    {
      label: "대시보드 새로고침",
      click: () => mainWindow && mainWindow.webContents.reload(),
    },
    { type: "separator" },
    {
      label: scheduler.isRunning() ? "동기화 중…" : "지금 동기화",
      enabled: !scheduler.isRunning(),
      click: () => scheduler.runNow("수동"),
    },
    {
      label: "자동 실행 (매일 " +
        `${String(cfg.schedule.hour).padStart(2, "0")}:${String(cfg.schedule.minute).padStart(2, "0")})`,
      type: "checkbox",
      checked: !!cfg.schedule.enabled,
      click: (item) => scheduler.setEnabled(item.checked),
    },
    { label: scheduler.nextRunLabel(), enabled: false },
    { label: scheduler.lastRunLabel(), enabled: false },
    { type: "separator" },
    { label: "계정 설정…", click: showSettings },
    {
      label: vault.isAvailable()
        ? `계정: ${vault.list().length}개 (암호화 볼트)`
        : "계정: accounts.yaml 폴백",
      enabled: false,
    },
    ...(runtimeMode === "bundled"
      ? [{ label: cfg.efsApplied ? "메일 로그: EFS 암호화됨" : "메일 로그: 평문(EFS 미적용)", enabled: false }]
      : []),
    {
      label: "로그인 시 자동 실행",
      type: "checkbox",
      checked: !!cfg.autoLaunch,
      enabled: app.isPackaged,
      click: (item) => toggleAutoLaunch(item.checked),
    },
    { label: `업데이트 확인… (v${app.getVersion()})`, click: () => updater.checkAndNotify({ silent: false }) },
    { type: "separator" },
    {
      label:
        (runtimeMode === "bundled" ? "번들 실행" : runtimeMode === "checkout" ? "체크아웃 실행" : "개발 실행") +
        ` · Flask ${flaskInfo.reused ? "재사용" : "실행 중"} (:${cfg.flaskPort})`,
      enabled: false,
    },
    { type: "separator" },
    {
      label: "종료",
      click: () => {
        app.isQuitting = true;
        app.quit();
      },
    },
  ]);
}

function refreshTray() {
  if (tray) tray.setContextMenu(buildTrayMenu());
}

function createTray() {
  const img = nativeImage.createFromPath(TRAY_ICON);
  tray = new Tray(img.isEmpty() ? nativeImage.createEmpty() : img);
  tray.setToolTip("ai-mail-agent");
  tray.setContextMenu(buildTrayMenu());
  tray.on("click", showWindow);
  tray.on("double-click", showWindow);
}

/**
 * 실행 모드를 정한다:
 *  - dev       : `npm start` — desktop/.. 의 소스, cfg.pythonPath, repo/data
 *  - checkout  : 패키징 exe + cfg.repoPath 지정 — 그 체크아웃의 소스/data 로 실행(파워유저)
 *  - bundled   : 패키징 exe 기본 — 번들된 Python·파이프라인, 데이터는 userData/data
 */
async function resolveRuntime() {
  if (!app.isPackaged) {
    runtimeMode = "dev";
    repoRoot = DEV_ROOT;
    pythonExe = cfg.pythonPath;
    dataDir = null; // repo/data 기본값
    return true;
  }

  if (looksLikeRepo(cfg.repoPath)) {
    runtimeMode = "checkout";
    repoRoot = cfg.repoPath;
    pythonExe = fs.existsSync(cfg.pythonPath) ? cfg.pythonPath : BUNDLED_PY;
    dataDir = path.join(cfg.repoPath, "data");
    return true;
  }

  // 기본: 완전 독립 실행. 번들된 것만 쓴다.
  if (looksLikeRepo(BUNDLED_PIPELINE) && fs.existsSync(BUNDLED_PY)) {
    runtimeMode = "bundled";
    repoRoot = BUNDLED_PIPELINE;
    pythonExe = BUNDLED_PY;
    dataDir = path.join(app.getPath("userData"), "data");
    fs.mkdirSync(dataDir, { recursive: true });
    await maybeImportDatabase();
    await applyEfsEncryption(dataDir); // app.db/dashboard.html 을 저장 시 암호화 (Windows EFS)
    return true;
  }

  dialog.showErrorBox(
    "번들이 손상되었습니다",
    "앱에 포함된 Python/파이프라인을 찾지 못했습니다. 앱을 다시 설치하세요.\n" +
      `python: ${BUNDLED_PY}\npipeline: ${BUNDLED_PIPELINE}`,
  );
  return false;
}

/**
 * 데이터 폴더에 Windows EFS(파일 시스템 암호화)를 건다 — app.db/dashboard.html 이 이
 * Windows 계정으로만 복호화되도록. safeStorage(자격증명)와 같은 신뢰 모델.
 *  - 한 번만 실행(config.efsApplied 플래그).
 *  - `cipher /e <dir>`: 폴더에 암호화 속성 → 이후 그 안에 만들어지는 파일(app.db,
 *    dashboard.html, SQLite 저널)이 자동으로 암호화된다. 자식 프로세스가 만든 파일도 상속.
 *  - Windows Home 에디션 등 EFS 불가 환경이면 조용히 스킵(cipher 가 non-zero 로 끝남).
 *  - 비차단: 부팅을 막지 않는다.
 */
function applyEfsEncryption(dir) {
  if (process.platform !== "win32") return Promise.resolve();
  if (process.env.MAIL_AGENT_SMOKE && !process.env.MAIL_AGENT_TEST_EFS) return Promise.resolve();
  if (cfg.efsApplied) return Promise.resolve();

  return new Promise((resolve) => {
    const { spawn } = require("node:child_process");
    // /e = 암호화, /a = 폴더뿐 아니라 파일도, /s: = 하위까지. (import 로 이미 들어온 DB 도 걸리게)
    const p = spawn("cipher", ["/e", "/a", "/s:" + dir], { windowsHide: true });
    let out = "";
    p.stdout.on("data", (d) => (out += d));
    p.stderr.on("data", (d) => (out += d));
    p.on("error", (err) => {
      console.error("[efs] cipher 실행 실패:", err.message);
      resolve();
    });
    p.on("exit", (code) => {
      if (code === 0) {
        console.log("[efs] 데이터 폴더 암호화 적용:", dir);
        cfg = config.update({ efsApplied: true });
        refreshTray();
      } else {
        console.warn(`[efs] cipher 종료 코드 ${code} — EFS 미지원 환경으로 보임(스킵). ${out.trim().slice(-200)}`);
      }
      resolve();
    });
  });
}

/** 독립 실행 첫 부팅: userData/data/app.db 가 없으면 기존 DB 가져오기를 제안. */
async function maybeImportDatabase() {
  const target = path.join(dataDir, "app.db");
  if (fs.existsSync(target)) return;
  if (process.env.MAIL_AGENT_SMOKE) return; // 스모크: 다이얼로그 스킵(빈 상태로 시작)

  const choice = dialog.showMessageBoxSync({
    type: "question",
    buttons: ["기존 app.db 가져오기…", "빈 상태로 시작"],
    defaultId: 0,
    cancelId: 1,
    title: "데이터 가져오기",
    message: "기존 ai-mail-agent 데이터(app.db)를 가져올까요?",
    detail:
      "예전에 CLI/개발 버전을 썼다면 그때의 data/app.db 를 선택하세요.\n" +
      "처음이라면 '빈 상태로 시작'을 누르세요 — 앱에서 계정·카테고리를 추가하면 됩니다.",
  });
  if (choice !== 0) return;

  const picked = dialog.showOpenDialogSync({
    title: "app.db 선택",
    properties: ["openFile"],
    filters: [{ name: "SQLite DB", extensions: ["db"] }],
  });
  if (picked && picked[0]) {
    try {
      fs.copyFileSync(picked[0], target);
      console.log(`[main] app.db 가져옴: ${picked[0]} → ${target}`);
    } catch (err) {
      dialog.showErrorBox("가져오기 실패", err.message);
    }
  }
}

async function boot() {
  cfg = config.load();
  console.log("[main] config:", config.configPath());

  if (!(await resolveRuntime())) {
    app.isQuitting = true;
    app.quit();
    return;
  }
  console.log(`[main] mode=${runtimeMode} python=${pythonExe}`);
  console.log(`[main] repoRoot=${repoRoot} dataDir=${dataDir || "(repo/data)"}`);

  // 로그인 시 자동 실행 설정을 config 와 동기화.
  applyAutoLaunch(cfg.autoLaunch);

  // 자격증명 볼트: 최초 실행이면 accounts.yaml → 암호화 볼트로 자동 이관.
  if (vault.isAvailable()) {
    const mig = vault.migrateFromYaml(accountsYaml());
    if (mig.migrated > 0) {
      console.log(`[vault] accounts.yaml → 볼트 마이그레이션: ${mig.migrated}개`);
    } else {
      console.log(`[vault] 마이그레이션 건너뜀 (${mig.reason}) · 계정 ${vault.list().length}개`);
    }
  } else {
    console.log("[vault] safeStorage 불가 — accounts.yaml 폴백 사용");
  }
  registerAccountIpc();

  const extraEnv = pipelineEnv();
  console.log("[main] 자격증명 소스:", extraEnv.MAIL_AGENT_ACCOUNTS ? "암호화 볼트(env 주입)" : "accounts.yaml");

  try {
    flaskInfo = await flask.start({ pythonPath: pythonExe, port: cfg.flaskPort, repoRoot, extraEnv });
    console.log("[main] flask ready:", flaskInfo);
  } catch (err) {
    dialog.showErrorBox(
      "Flask 시작 실패",
      `admin_app.py 를 띄우지 못했습니다.\n\npython: ${pythonExe}\n포트: ${cfg.flaskPort}\n\n${err.message}`,
    );
    app.isQuitting = true;
    app.quit();
    return;
  }

  // 스케줄러를 트레이보다 먼저 초기화한다 — buildTrayMenu()가 scheduler 라벨을 읽으므로.
  scheduler.start({
    port: cfg.flaskPort,
    getConfig: () => cfg,
    saveConfig: (partial) => {
      cfg = config.update(partial);
      refreshTray();
    },
    onChange: refreshTray,
  });

  createWindow();
  createTray();

  // 부팅 후 조용히 1회 업데이트 확인 (패키징 실행만, 스모크 제외).
  if (app.isPackaged && !process.env.MAIL_AGENT_SMOKE) {
    setTimeout(() => {
      updater.checkAndNotify({ silent: true }).catch((e) => console.warn("[updater]", e.message));
    }, 30000);
  }

  // 스모크 테스트: MAIL_AGENT_SMOKE=<초> 이면 그 시간 뒤 정상 종료 경로로 빠져나간다
  // (Flask 자식 정리까지 실제로 타는지 확인용). 평상시엔 설정 안 함.
  // 통합 테스트 훅: 계정 add → Flask 재시작 → remove → Flask 재시작 왕복 확인.
  if (process.env.MAIL_AGENT_TEST_CRUD) {
    setTimeout(async () => {
      const TESTUSER = "__smoketest__@gmail.com";
      const before = vault.list().length;
      vault.add({ type: "gmail", user: TESTUSER, password: "test" });
      await restartFlaskWithAccounts();
      const up1 = await flask.ping(cfg.flaskPort);
      const mid = vault.list().length;
      vault.remove(TESTUSER);
      await restartFlaskWithAccounts();
      const up2 = await flask.ping(cfg.flaskPort);
      const after = vault.list().length;
      console.log(`[test-crud] before=${before} mid=${mid} after=${after} up1=${up1} up2=${up2}`);
    }, 2000);
  }

  const smoke = Number(process.env.MAIL_AGENT_SMOKE);
  if (smoke > 0) {
    if (process.env.MAIL_AGENT_SMOKE_SETTINGS) showSettings();
    setTimeout(async () => {
      const shotTarget =
        process.env.MAIL_AGENT_SMOKE_SETTINGS && settingsWindow
          ? settingsWindow
          : mainWindow;
      if (process.env.MAIL_AGENT_SMOKE_SHOT && shotTarget) {
        try {
          const img = await shotTarget.webContents.capturePage();
          require("node:fs").writeFileSync(process.env.MAIL_AGENT_SMOKE_SHOT, img.toPNG());
          console.log("[main] smoke screenshot ->", process.env.MAIL_AGENT_SMOKE_SHOT);
        } catch (e) {
          console.log("[main] smoke screenshot failed:", e.message);
        }
      }
      console.log("[main] smoke timeout — quitting");
      app.isQuitting = true;
      app.quit();
    }, smoke * 1000);
  }
}

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", showWindow);

  app.whenReady().then(boot).catch((err) => {
    console.error("[main] boot 실패:", err);
    app.isQuitting = true;
    flask.stop();
    scheduler.stop();
    app.quit();
  });

  // 트레이 상주 앱이라 모든 창을 닫아도 앱은 살아있다.
  app.on("window-all-closed", () => {});

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });

  app.on("before-quit", () => {
    app.isQuitting = true;
    scheduler.stop();
    flask.stop();
  });
}
