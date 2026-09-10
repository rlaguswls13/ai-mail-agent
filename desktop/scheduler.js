"use strict";
/*
 * 앱 내부 스케줄러 (Phase 2) - 외부 패키지(node-cron 등) 없이 자체 구현.
 *
 * 동작:
 *  - 1분 간격 폴링 방식. "오늘의 예정 시각이 지났고, 그 시각 이후로 아직 안 돌았으면"
 *    실행한다. setTimeout 방식보다 노트북 절전/복귀·시계 변경에 강하다(놓친 실행을
 *    다음 tick이 자동으로 잡는다).
 *  - 실행 = admin_app.py 의 POST /sync (dry-run: fetch_mail.py → generate_html.py).
 *    실제 메일함을 바꾸는 --apply 는 하지 않는다(하드 규칙 - dry-run 자동화만 허용).
 *  - 완료/실패 시 Electron Notification.
 *  - 마지막 실행 시각은 config.json 의 lastRunAt 에 영속.
 *  - 시작 시 자가복구: lastRunAt 이 없거나 STALE_HOURS 초과면 (예정 시각 전이어도) 1회 실행.
 */
const http = require("node:http");
const { Notification } = require("electron");

const TICK_MS = 60 * 1000;
const STALE_HOURS = 24;
const SYNC_TIMEOUT_MS = 11 * 60 * 1000; // /sync 는 fetch(≤300s)+generate(≤300s) 동기 실행

let opts = null; // { port, getConfig, saveConfig, onChange }
let timer = null;
let recoverTimer = null;
let running = false; // 중복 실행 방지

/** @param {Date} now @param {{hour:number,minute:number}} sched */
function scheduledTimeOn(now, sched) {
  const d = new Date(now);
  d.setHours(sched.hour, sched.minute, 0, 0);
  return d;
}

/** 다음 실행 예정 시각(Date). 스케줄이 꺼져 있으면 null. */
function nextRunAt() {
  if (!opts) return null;
  const cfg = opts.getConfig();
  if (!cfg.schedule || !cfg.schedule.enabled) return null;
  const now = new Date();
  let next = scheduledTimeOn(now, cfg.schedule);
  if (next <= now) next.setDate(next.getDate() + 1);
  return next;
}

/** 트레이 라벨용 문자열. */
function nextRunLabel() {
  if (!opts) return "자동 실행: 준비 중";
  const cfg = opts.getConfig();
  if (!cfg.schedule || !cfg.schedule.enabled) return "자동 실행: 꺼짐";
  const at = nextRunAt();
  const hh = String(at.getHours()).padStart(2, "0");
  const mm = String(at.getMinutes()).padStart(2, "0");
  const today = at.toDateString() === new Date().toDateString();
  return `다음 실행: ${today ? "오늘" : "내일"} ${hh}:${mm}`;
}

function lastRunLabel() {
  if (!opts) return "마지막 실행: 없음";
  const cfg = opts.getConfig();
  if (!cfg.lastRunAt) return "마지막 실행: 없음";
  const d = new Date(cfg.lastRunAt);
  if (Number.isNaN(d.getTime())) return "마지막 실행: 없음";
  return `마지막 실행: ${d.toLocaleString()}`;
}

function notify(title, body) {
  try {
    if (Notification.isSupported()) new Notification({ title, body }).show();
  } catch {
    /* 알림 실패는 무시 */
  }
}

/** POST /sync (form-encoded, range=daily). resolve=성공, reject=실패. */
function postSync() {
  return new Promise((resolve, reject) => {
    const cfg = opts.getConfig();
    const payload = "range=daily";
    const req = http.request(
      {
        host: "127.0.0.1",
        port: cfg.flaskPort || opts.port,
        path: "/sync",
        method: "POST",
        headers: {
          "Content-Type": "application/x-www-form-urlencoded",
          "Content-Length": Buffer.byteLength(payload),
        },
        timeout: SYNC_TIMEOUT_MS,
      },
      (res) => {
        res.resume();
        // /sync 는 성공 시 302 리다이렉트.
        if (res.statusCode >= 200 && res.statusCode < 400) resolve();
        else reject(new Error(`/sync HTTP ${res.statusCode}`));
      },
    );
    req.on("error", reject);
    req.on("timeout", () => {
      req.destroy(new Error("/sync 응답 타임아웃"));
    });
    req.write(payload);
    req.end();
  });
}

/**
 * 실제 실행. reason 은 로그/알림 문구용("정기 실행" | "놓친 실행 보정" | "수동").
 * @returns {Promise<boolean>} 성공 여부
 */
async function runNow(reason = "정기 실행") {
  if (running) {
    console.log("[scheduler] 이미 실행 중 - 건너뜀");
    return false;
  }
  running = true;
  opts.onChange && opts.onChange();
  console.log(`[scheduler] ${reason} 시작`);
  try {
    await postSync();
    const at = new Date().toISOString();
    opts.saveConfig({ lastRunAt: at });
    console.log(`[scheduler] ${reason} 완료 @ ${at}`);
    notify("ai-mail-agent 동기화 완료", `${reason} - 대시보드가 최신 상태입니다.`);
    return true;
  } catch (err) {
    console.error(`[scheduler] ${reason} 실패:`, err.message);
    notify("ai-mail-agent 동기화 실패", `${reason} - ${err.message}`);
    return false;
  } finally {
    running = false;
    opts.onChange && opts.onChange();
  }
}

/** 1분 tick: 예정 시각이 지났고 그 이후로 아직 안 돌았으면 실행. */
function tick() {
  const cfg = opts.getConfig();
  if (!cfg.schedule || !cfg.schedule.enabled || running) return;

  const now = new Date();
  const todaySched = scheduledTimeOn(now, cfg.schedule);
  if (now < todaySched) return; // 아직 예정 시각 전

  const last = cfg.lastRunAt ? new Date(cfg.lastRunAt) : null;
  const ranSinceSchedule = last && !Number.isNaN(last.getTime()) && last >= todaySched;
  if (ranSinceSchedule) return; // 오늘 예정분은 이미 실행됨

  runNow("정기 실행");
}

/** 시작 시 자가복구: 마지막 실행이 너무 오래됐으면 즉시 1회. */
function recoverIfStale() {
  const cfg = opts.getConfig();
  if (!cfg.runOnStartupIfStale) return;
  if (!cfg.schedule || !cfg.schedule.enabled) return;

  const last = cfg.lastRunAt ? new Date(cfg.lastRunAt) : null;
  const stale =
    !last ||
    Number.isNaN(last.getTime()) ||
    Date.now() - last.getTime() > STALE_HOURS * 3600 * 1000;
  if (stale) {
    console.log("[scheduler] 마지막 실행이 오래됨 - 시작 시 보정 실행");
    // Flask 가 막 떴을 수 있으니 잠깐 뒤에.
    recoverTimer = setTimeout(() => {
      recoverTimer = null;
      runNow("놓친 실행 보정");
    }, 3000);
  }
}

/** @param {{port:number, getConfig:Function, saveConfig:Function, onChange?:Function}} o */
function start(o) {
  opts = o;
  stop();
  timer = setInterval(tick, TICK_MS);
  recoverIfStale();
  console.log("[scheduler] 시작 -", nextRunLabel());
}

function stop() {
  if (timer) {
    clearInterval(timer);
    timer = null;
  }
  if (recoverTimer) {
    clearTimeout(recoverTimer);
    recoverTimer = null;
  }
}

/** 트레이 "자동 실행" 토글. */
function setEnabled(enabled) {
  const cfg = opts.getConfig();
  opts.saveConfig({ schedule: { ...cfg.schedule, enabled: !!enabled } });
  console.log("[scheduler] 자동 실행:", enabled ? "켬" : "끔", "-", nextRunLabel());
  opts.onChange && opts.onChange();
}

function isRunning() {
  return running;
}

module.exports = {
  start,
  stop,
  runNow,
  setEnabled,
  isRunning,
  nextRunAt,
  nextRunLabel,
  lastRunLabel,
  _tick: tick, // 테스트용: 1분 tick 로직을 즉시 호출
};
