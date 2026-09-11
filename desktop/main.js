"use strict";
/*
 * ai-mail-agent 데스크톱 셸 - Electron main 프로세스.
 *
 * Phase 1: admin_ui 패키지(Flask, `python -m admin_ui`) 자식 spawn → 헬스 폴링 → 창에서
 *   http://127.0.0.1:<port> 로드. 트레이 상주(닫기 = 트레이로 숨김, 트레이 "종료" = 진짜 종료).
 *   종료 시 Flask 자식 kill → 포트 스테일 프로세스 gotcha 해결.
 * Phase 2: scheduler.js - 앱 내부 스케줄러(매일 06:00 dry-run /sync) + 트레이
 *   "지금 동기화"·"자동 실행" 토글·다음/마지막 실행 표시 + 놓친 실행 자가복구.
 * Phase 3: vault.js - 앱 관리 대칭키(AES-256-GCM) 자격증명 볼트. 시작 시 accounts.yaml 자동
 *   마이그레이션 → 복호화 → Flask 자식 stdin 으로 주입(env 아님). 트레이
 *   "계정 설정…" → renderer/settings.html 에서 CRUD → Flask 자식 재시작으로 즉시 반영.
 * Phase 4: electron-builder 포터블 exe 패키징 + 로그인 자동 실행 토글.
 * Phase 5: 완전 독립 실행. 번들된 Python(python-embed + Flask vendor + 3패키지
 *   mail_core/mail_app/admin_ui vendor, resources/python/Lib)을 쓰고, 데이터는
 *   userData/data (MAIL_AGENT_DATA_DIR). cfg.repoPath 지정 시 "checkout" 모드(파워유저용).
 * 그 외: 데이터 폴더 EFS 암호화(applyEfsEncryption) · GitHub Releases 업데이트 확인
 *   (updater.js, 외부 패키지 없음) · 페이지네이션 UI 정리.
 */
const path = require("node:path");
const { app, BrowserWindow, Tray, Menu, shell, dialog, nativeImage, ipcMain, Notification } = require("electron");
const config = require("./config");
const flask = require("./flask");
const scheduler = require("./scheduler");
const vault = require("./vault");
const updater = require("./updater");
const oauthLogin = require("./oauthLogin");
const applyPending = require("./applyPending");

const fs = require("node:fs");
const DEV_ROOT = path.resolve(__dirname, "..");
const TRAY_ICON = path.join(__dirname, "assets", "tray.png");
const APP_ICON = path.join(__dirname, "assets", "icon.png");
// 패키징 실행: extraResources 로 번들된 Python. 3개 파이프라인 패키지(mail_core /
// mail_app / admin_ui)는 prepare_python.py 가 이 Python 의 Lib/ 에 vendor 해 넣는다.
const BUNDLED_PY = path.join(process.resourcesPath || "", "python", "python.exe");

/** <dir>/packages/admin-ui/admin_ui/admin_app.py 가 실제로 있는지 (개발 체크아웃 판별). */
function looksLikeRepo(dir) {
  return !!dir && fs.existsSync(path.join(dir, "packages", "admin-ui", "admin_ui", "admin_app.py"));
}

// ── 테스트/스모크 훅 게이트 ──────────────────────────────────────────────────
// MAIL_AGENT_SMOKE*(타임아웃 종료·스크린샷)·MAIL_AGENT_TEST_CRUD(볼트 CRUD 왕복)는
// 개발/CI 전용이다. 개발 실행(!app.isPackaged)에서는 항상 활성이고, 배포된 포터블
// exe 에서는 릴리스 스모크 검증 시에만 MAIL_AGENT_TEST_HOOKS=1 로 명시적으로 켠다.
// 그 외 배포 실행에서는 관련 env 가 설정돼 있어도 전부 무시된다.
const TEST_HOOKS = !app.isPackaged || process.env.MAIL_AGENT_TEST_HOOKS === "1";
const SMOKE_SECONDS = TEST_HOOKS ? Number(process.env.MAIL_AGENT_SMOKE) || 0 : 0;
const SMOKE = SMOKE_SECONDS > 0;

// 실행 모드에 따라 결정되는 값 (resolveRuntime 에서 채움).
let repoRoot = DEV_ROOT;     // 개발 체크아웃 루트 (<repoRoot>/packages/*). bundled 는 null.
let pythonExe = null;        // 쓸 python
let dataDir = null;          // app.db / dashboard.html 이 살 디렉터리 (null = repo/data 기본)
let runtimeMode = "dev";     // "dev" | "bundled" | "checkout"

/** 부팅 시 1회 accounts.yaml -> 암호화 볼트 마이그레이션에만 쓰인다. 없으면 그냥 스킵된다. */
function accountsYaml() {
  const base = repoRoot || dataDir || app.getPath("userData");
  return path.join(base, "config", "accounts.yaml");
}

/** Flask 자식에 넘길 비-비밀 env. 자격증명은 여기 넣지 않는다(stdin 으로 간다). */
function pipelineEnv() {
  const env = {};
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

/**
 * 볼트 계정 → Flask 자식 stdin 으로 넘길 JSON 문자열. 볼트가 비었거나 불가면 null
 * (accounts.yaml 폴백). env 대신 stdin 을 쓰는 이유: 프로세스 환경 블록에 평문
 * 비밀번호가 남지 않게 하려는 것 - 같은 사용자의 다른 프로세스가 env 를 읽거나
 * 크래시 덤프/자식 프로세스로 새어나가는 경로를 줄인다.
 */
function accountsPayload() {
  const accounts = vault.isAvailable() ? vault.read() : null;
  if (accounts && accounts.length) return JSON.stringify(accounts);
  return null;
}

/** 계정 편집 후 파이프라인이 새 자격증명을 쓰도록 Flask 자식을 재시작한다. */
async function restartFlaskWithAccounts() {
  try {
    flaskInfo = await flask.restart(pipelineEnv(), accountsPayload());
    if (mainWindow) mainWindow.webContents.reload();
    refreshTray();
  } catch (err) {
    console.error("[main] Flask 재시작 실패:", err.message);
    dialog.showErrorBox("Flask 재시작 실패", `계정 변경은 저장됐지만 파이프라인 재시작에 실패했습니다.\n앱을 다시 시작하세요.\n\n${err.message}`);
  }
}

// --- 메일 OAuth2 로그인 (Outlook: device code / Gmail: loopback) ---

let oauthLoginRunning = false;

/** 이 계정이 IMAP 에 OAuth2 토큰을 쓰는가? (accounts.account_auth 와 동일 규칙) */
function accountUsesOAuth(a) {
  const explicit = String(a.auth || "").trim().toLowerCase();
  if (explicit === "xoauth2") return true;
  if (explicit === "password") return false;
  return a.type === "outlook";
}

/** 볼트(또는 accounts.yaml 폴백)에서 OAuth 로그인이 필요한 계정 이메일 목록. */
function oauthAccountUsers() {
  const accounts = vault.isAvailable() ? vault.list() : [];
  return accounts.filter(accountUsesOAuth).map((a) => a.user);
}

/**
 * 트레이 "메일 로그인 (OAuth)…". Outlook 은 device code(코드 안내 + 브라우저 열기 +
 * 코드 클립보드 복사), Gmail 은 loopback(브라우저 자동 오픈)으로 진행하고, 자식이
 * 마치면 결과를 알린 뒤 파이프라인을 재시작한다. 토큰은 `<데이터>/<provider>_token.json`.
 */
async function runOAuthLogin({ force = false } = {}) {
  if (oauthLoginRunning) return;
  oauthLoginRunning = true;
  refreshTray();
  try {
    const res = await oauthLogin.run({
      pythonPath: pythonExe,
      repoRoot,
      extraEnv: pipelineEnv(),
      accountsJson: accountsPayload(),
      force,
      onPrompt: (ev) => {
        if (ev.user_code) {
          // device code flow (Outlook)
          try {
            require("electron").clipboard.writeText(ev.user_code);
          } catch {
            /* 클립보드 실패는 무시 */
          }
          const choice = dialog.showMessageBoxSync({
            type: "info",
            noLink: true,
            title: "메일 로그인",
            message: `${ev.user} — 브라우저에서 로그인`,
            detail:
              `1. "브라우저 열기"를 누릅니다 (주소: ${ev.verification_uri}).\n` +
              `2. 코드 입력: ${ev.user_code}  (클립보드에 복사됨 — Ctrl+V)\n` +
              `3. ${ev.user} 로 로그인하고 메일 접근에 동의합니다\n` +
              `   (앱 이름은 "Mozilla Thunderbird"로 표시됩니다).\n\n` +
              `동의를 마치면 이 창을 닫아도 됩니다 — 백그라운드에서 자동으로 완료됩니다.`,
            buttons: ["브라우저 열기", "닫기"],
            defaultId: 0,
          });
          if (choice === 0) shell.openExternal(ev.verification_uri);
        } else {
          // loopback flow (Gmail) - 자식이 이미 기본 브라우저를 열었다.
          const choice = dialog.showMessageBoxSync({
            type: "info",
            noLink: true,
            title: "메일 로그인",
            message: `${ev.user} — 브라우저에서 로그인`,
            detail:
              `기본 브라우저에 로그인 창이 열렸습니다. ${ev.user} 로 로그인하고\n` +
              `메일 접근에 동의하세요 (앱 이름 "Mozilla Thunderbird").\n\n` +
              `창이 안 열렸으면 "브라우저 열기"를 누르세요.`,
            buttons: ["브라우저 열기", "닫기"],
            defaultId: 1,
          });
          if (choice === 0) shell.openExternal(ev.verification_uri);
        }
      },
    });

    if (res.error) {
      dialog.showErrorBox("메일 로그인 실패", res.error);
      return;
    }
    const ok = res.results.filter((r) => r.status === "ok").map((r) => r.user);
    const skipped = res.results.filter((r) => r.status === "skip").map((r) => r.user);
    const failed = res.results.filter((r) => r.status === "fail");

    if (res.ok && !failed.length) {
      cfg = config.update({ oauthNagAt: null }); // 재로그인 알림 throttle 리셋
      dialog.showMessageBoxSync({
        type: "info",
        noLink: true,
        title: "메일 로그인",
        message: "완료되었습니다.",
        detail:
          (ok.length ? `로그인됨: ${ok.join(", ")}\n` : "") +
          (skipped.length ? `이미 유효한 토큰: ${skipped.join(", ")}\n` : "") +
          `다음 동기화부터 반영됩니다.`,
      });
      await restartFlaskWithAccounts();
    } else {
      dialog.showErrorBox(
        "메일 로그인 실패",
        failed.map((r) => `${r.user}: ${r.detail || "알 수 없는 오류"}`).join("\n") ||
          "인증이 완료되지 않았습니다. 다시 시도하세요.",
      );
    }
  } finally {
    oauthLoginRunning = false;
    refreshTray();
  }
}

const OAUTH_NAG_THROTTLE_MS = 20 * 3600 * 1000; // ~하루 1회

/**
 * OAuth 계정 토큰 상태를 확인해, 없거나 폐기됐으면 "재로그인 필요" OS 알림을 띄운다
 * (클릭 시 로그인 플로우). 하루 1회로 throttle. 앱 시작 후 + 매 스케줄 sync 뒤 호출.
 * 브라우저 플로우는 시작하지 않는다(`--check`). 조용히 실패해도 무방(비차단).
 */
async function checkOAuthTokens() {
  if (oauthLoginRunning || !oauthAccountUsers().length) return;
  let res;
  try {
    res = await oauthLogin.check({
      pythonPath: pythonExe,
      repoRoot,
      extraEnv: pipelineEnv(),
      accountsJson: accountsPayload(),
    });
  } catch (err) {
    console.warn("[oauth] 토큰 상태 확인 실패:", err.message);
    return;
  }
  const bad = (res.statuses || []).filter((s) => s.status !== "ok");
  if (!bad.length) return;

  const last = cfg.oauthNagAt ? Date.parse(cfg.oauthNagAt) : 0;
  if (Date.now() - last < OAUTH_NAG_THROTTLE_MS) {
    console.log("[oauth] 재로그인 필요하지만 알림 throttle 중:", bad.map((s) => s.user).join(", "));
    return;
  }
  cfg = config.update({ oauthNagAt: new Date().toISOString() });

  const missing = bad.some((s) => s.status === "missing");
  const users = bad.map((s) => s.user).join(", ");
  try {
    if (!Notification.isSupported()) return;
    const n = new Notification({
      title: "메일 재로그인 필요",
      body: `${users} — 토큰이 ${missing ? "없습니다" : "만료/폐기됐습니다"}. 눌러서 로그인하세요.`,
    });
    n.on("click", () => runOAuthLogin());
    n.show();
  } catch {
    /* 알림 실패는 무시 */
  }
}

// --- 미적용 액션 정리 ---
// 스케줄러는 /sync(dry-run)만 자동 실행하므로, 처리 대상(save/trash/read)인데 아직
// INBOX 에 그대로인 메일이 쌓인다. 트레이에서 건수를 보여주고 원클릭으로 적용한다
// (최근 30일치 - `mail_app.apply_pending` 기본값. 자동 실행은 안 함: --apply 는 명시 요청만).

let pendingActions = { total: 0, by_action: {} };
let applyPendingRunning = false;

function pendingOpts() {
  return { pythonPath: pythonExe, repoRoot, extraEnv: pipelineEnv(), accountsJson: accountsPayload() };
}

/** 미적용 액션 건수를 갱신하고 트레이를 다시 그린다. 조용히 실패해도 무방. */
async function refreshPendingCount() {
  if (applyPendingRunning) return;
  try {
    const res = await applyPending.count(pendingOpts());
    if (res.error) {
      console.warn("[apply-pending] 건수 확인 실패:", res.error);
      return;
    }
    pendingActions = { total: res.total || 0, by_action: res.by_action || {} };
    refreshTray();
  } catch (err) {
    console.warn("[apply-pending] 건수 확인 예외:", err.message);
  }
}

function pendingBreakdown(by) {
  const L = { trash: "휴지통 이동", save: "보관", read: "읽음 표시" };
  return Object.entries(by || {})
    .map(([a, n]) => `  · ${L[a] || a}: ${n}건`)
    .join("\n");
}

/** 트레이 "미적용 액션 정리". 내역을 확인시키고 실제 IMAP 적용 → 알림 + 대시보드 새로고침. */
async function runApplyPending() {
  if (applyPendingRunning || pendingActions.total === 0) return;
  const choice = dialog.showMessageBoxSync({
    type: "warning",
    noLink: true,
    title: "미적용 액션 정리",
    message: `${pendingActions.total}건을 실제로 메일함에 반영합니다.`,
    detail:
      pendingBreakdown(pendingActions.by_action) +
      `\n\n최근 30일 내 메일 중 카테고리 규칙상 처리 대상인데 아직 받은편지함에\n` +
      `그대로인 것들입니다. 보관/휴지통 이동은 정리함(/vault)에서 되돌릴 수 있습니다.`,
    buttons: ["적용", "취소"],
    defaultId: 1,
    cancelId: 1,
  });
  if (choice !== 0) return;

  applyPendingRunning = true;
  refreshTray();
  try {
    const res = await applyPending.apply(pendingOpts());
    if (res.error) {
      dialog.showErrorBox("미적용 액션 정리 실패", res.error);
      return;
    }
    try {
      if (Notification.isSupported()) {
        new Notification({
          title: "미적용 액션 정리 완료",
          body: `적용 ${res.applied}건${res.failed ? ` · 실패 ${res.failed}건` : ""}`,
        }).show();
      }
    } catch {
      /* 알림 실패는 무시 */
    }
    if (mainWindow) mainWindow.webContents.reload();
  } finally {
    applyPendingRunning = false;
    await refreshPendingCount(); // 트레이 라벨/표시 갱신
  }
}

function createSettingsWindow() {
  settingsWindow = new BrowserWindow({
    width: 640,
    height: 640,
    title: "계정 설정 - ai-mail-agent",
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

  // 비밀번호는 렌더러로 돌려보내지 않는다 - 목록 표시에 필요 없다.
  ipcMain.handle("accounts:list", () =>
    (vault.isAvailable() ? vault.list() : []).map((a) => ({
      type: a.type,
      user: a.user,
      auth: a.auth || "",
      alias: a.alias || "",
    })),
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
    ...(pendingActions.total > 0
      ? [
          {
            label: applyPendingRunning
              ? "정리 적용 중…"
              : `미적용 액션 정리 (${pendingActions.total}건)…`,
            enabled: !applyPendingRunning,
            click: () => runApplyPending(),
          },
        ]
      : []),
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
    ...(oauthAccountUsers().length
      ? [
          {
            label: oauthLoginRunning ? "메일 로그인 중…" : "메일 로그인 (OAuth)…",
            enabled: !oauthLoginRunning,
            click: () => runOAuthLogin(),
          },
        ]
      : []),
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

// 번들 Python 의 Lib/ 에 admin_ui 패키지가 vendor 됐는지 (prepare_python.py 산출물).
const BUNDLED_PKG_MARKER = path.join(process.resourcesPath || "", "python", "Lib", "admin_ui", "admin_app.py");

/**
 * 실행 모드를 정한다:
 *  - dev       : `npm start` - desktop/.. 의 packages/*, cfg.pythonPath (dev-install.bat 로
 *                editable 설치했거나 flask.js 가 PYTHONPATH 로 잡아줌), repo/data
 *  - checkout  : 패키징 exe + cfg.repoPath 지정 - 그 체크아웃의 packages/*, 그쪽 data (파워유저)
 *  - bundled   : 패키징 exe 기본 - 번들 Python(Lib/ 에 3패키지 vendor), 데이터는 userData/data
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
  if (fs.existsSync(BUNDLED_PKG_MARKER) && fs.existsSync(BUNDLED_PY)) {
    runtimeMode = "bundled";
    repoRoot = null; // 저장소 없음 - flask.js 가 PYTHONPATH 없이 번들 Python 으로 실행
    pythonExe = BUNDLED_PY;
    dataDir = path.join(app.getPath("userData"), "data");
    fs.mkdirSync(dataDir, { recursive: true });
    await maybeImportDatabase();
    await applyEfsEncryption(dataDir); // app.db/dashboard.html 을 저장 시 암호화 (Windows EFS)
    return true;
  }

  dialog.showErrorBox(
    "번들이 손상되었습니다",
    "앱에 포함된 Python/파이프라인 패키지를 찾지 못했습니다. 앱을 다시 설치하세요.\n" +
      `python: ${BUNDLED_PY}\npackage: ${BUNDLED_PKG_MARKER}`,
  );
  return false;
}

/**
 * 데이터 폴더에 Windows EFS(파일 시스템 암호화)를 건다 - app.db/dashboard.html 이 이
 * Windows 계정으로만 복호화되도록. safeStorage(자격증명)와 같은 신뢰 모델.
 *  - 한 번만 실행(config.efsApplied 플래그).
 *  - `cipher /e <dir>`: 폴더에 암호화 속성 → 이후 그 안에 만들어지는 파일(app.db,
 *    dashboard.html, SQLite 저널)이 자동으로 암호화된다. 자식 프로세스가 만든 파일도 상속.
 *  - Windows Home 에디션 등 EFS 불가 환경이면 조용히 스킵(cipher 가 non-zero 로 끝남).
 *  - 비차단: 부팅을 막지 않는다.
 */
function applyEfsEncryption(dir) {
  if (process.platform !== "win32") return Promise.resolve();
  if (SMOKE && !process.env.MAIL_AGENT_TEST_EFS) return Promise.resolve();
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
        console.warn(`[efs] cipher 종료 코드 ${code} - EFS 미지원 환경으로 보임(스킵). ${out.trim().slice(-200)}`);
      }
      resolve();
    });
  });
}

/** 독립 실행 첫 부팅: userData/data/app.db 가 없으면 기존 DB 가져오기를 제안. */
async function maybeImportDatabase() {
  const target = path.join(dataDir, "app.db");
  if (fs.existsSync(target)) return;
  if (SMOKE) return; // 스모크: 다이얼로그 스킵(빈 상태로 시작)

  const choice = dialog.showMessageBoxSync({
    type: "question",
    buttons: ["기존 app.db 가져오기…", "빈 상태로 시작"],
    defaultId: 0,
    cancelId: 1,
    title: "데이터 가져오기",
    message: "기존 ai-mail-agent 데이터(app.db)를 가져올까요?",
    detail:
      "예전에 CLI/개발 버전을 썼다면 그때의 data/app.db 를 선택하세요.\n" +
      "처음이라면 '빈 상태로 시작'을 누르세요 - 앱에서 계정·카테고리를 추가하면 됩니다.",
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
    console.log("[vault] 볼트 키 생성 불가 - accounts.yaml 폴백 사용");
  }
  registerAccountIpc();

  const extraEnv = pipelineEnv();
  const accountsJson = accountsPayload();
  console.log("[main] 자격증명 소스:", accountsJson ? "암호화 볼트(stdin 주입)" : "accounts.yaml");

  try {
    flaskInfo = await flask.start({ pythonPath: pythonExe, port: cfg.flaskPort, repoRoot, extraEnv, accountsJson });
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

  // 스케줄러를 트레이보다 먼저 초기화한다 - buildTrayMenu()가 scheduler 라벨을 읽으므로.
  scheduler.start({
    port: cfg.flaskPort,
    getConfig: () => cfg,
    saveConfig: (partial) => {
      cfg = config.update(partial);
      refreshTray();
    },
    onChange: refreshTray,
    afterRun: () => {
      checkOAuthTokens();
      refreshPendingCount();
    },
  });

  createWindow();
  createTray();

  // 시작 후 한 번: OAuth 토큰 상태 알림 + 미적용 액션 건수.
  if (!SMOKE) {
    setTimeout(() => checkOAuthTokens(), 8000);
    setTimeout(() => refreshPendingCount(), 9000);
  }

  // 부팅 후 조용히 1회 업데이트 확인 (패키징 실행만, 스모크 제외).
  if (app.isPackaged && !SMOKE) {
    setTimeout(() => {
      updater.checkAndNotify({ silent: true }).catch((e) => console.warn("[updater]", e.message));
    }, 30000);
  }

  // 스모크 테스트: MAIL_AGENT_SMOKE=<초> 이면 그 시간 뒤 정상 종료 경로로 빠져나간다
  // (Flask 자식 정리까지 실제로 타는지 확인용). 평상시엔 설정 안 함.
  // 통합 테스트 훅: 계정 add → Flask 재시작 → remove → Flask 재시작 왕복 확인.
  if (TEST_HOOKS && process.env.MAIL_AGENT_TEST_CRUD) {
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

  if (SMOKE_SECONDS > 0) {
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
      console.log("[main] smoke timeout - quitting");
      app.isQuitting = true;
      app.quit();
    }, SMOKE_SECONDS * 1000);
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
