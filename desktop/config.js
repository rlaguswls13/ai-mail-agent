"use strict";
// 앱 설정 영속 저장 - 외부 패키지(electron-store 등) 없이 userData 아래 JSON 파일 하나.
// 이 프로젝트의 "과도한 의존성 금지" 원칙에 맞춰 직접 구현한다.
const fs = require("node:fs");
const path = require("node:path");
const { app } = require("electron");

const DEFAULTS = {
  pythonPath: "python", // PATH의 python. 스토어 스텁이거나 다른 배포판을 쓰면 설정 화면에서 전체 경로로 변경.
  repoPath: null, // ai-mail-agent 체크아웃 경로. 개발 실행 땐 자동(desktop/..), 패키징 exe에선 첫 실행 시 폴더 선택. (Phase 5에서 번들링으로 제거)
  flaskPort: 5000,
  schedule: { enabled: true, hour: 6, minute: 0 }, // 매일 06:00 파이프라인 실행
  runOnStartupIfStale: true, // 마지막 실행이 24h+ 전이면 시작 시 1회 실행
  autoLaunch: false, // 로그인 시 자동 실행 (app.setLoginItemSettings)
  efsApplied: false, // 번들 실행에서 데이터 폴더에 Windows EFS 암호화를 이미 걸었는가
  lastRunAt: null, // ISO 문자열
  oauthNagAt: null, // "메일 재로그인 필요" 알림을 마지막으로 띄운 시각(ISO) - 하루 1회 throttle
};

function configPath() {
  return path.join(app.getPath("userData"), "config.json");
}

function load() {
  try {
    const raw = fs.readFileSync(configPath(), "utf-8");
    const parsed = JSON.parse(raw);
    return { ...DEFAULTS, ...parsed, schedule: { ...DEFAULTS.schedule, ...(parsed.schedule || {}) } };
  } catch {
    return { ...DEFAULTS };
  }
}

function save(cfg) {
  const merged = { ...DEFAULTS, ...cfg, schedule: { ...DEFAULTS.schedule, ...(cfg.schedule || {}) } };
  fs.mkdirSync(path.dirname(configPath()), { recursive: true });
  fs.writeFileSync(configPath(), JSON.stringify(merged, null, 2), "utf-8");
  return merged;
}

/** 현재 파일 내용에 partial 을 얕게 병합(schedule 은 한 단계 더)해서 저장. */
function update(partial) {
  const current = load();
  return save({
    ...current,
    ...partial,
    schedule: { ...current.schedule, ...(partial.schedule || {}) },
  });
}

module.exports = { DEFAULTS, configPath, load, save, update };
