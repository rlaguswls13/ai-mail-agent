"use strict";
/*
 * 자격증명 볼트 (Phase 3) - Electron safeStorage(Windows: DPAPI) 로 암호화.
 *
 *  - 정본: userData/accounts.enc (safeStorage.encryptString 결과 바이너리)
 *  - 형식: [{ type: "gmail"|"naver"|"outlook", user, password, auth?: "xoauth2", alias?: string }, ...]
 *    alias 는 화면 표시 이름(별칭) - 같은 제공자를 여러 개 등록해도 구분되게. 없으면
 *    화면은 제공자 이름(Gmail/Naver/Outlook)을 대신 쓴다(mail_core.accounts.account_label).
 *    auth:"xoauth2" 계정(Outlook 전부, 선택 시 Gmail)은 password 가 비어도 된다 -
 *    IMAP 인증을 OAuth 토큰으로 하므로. 최초 로그인은 트레이 "메일 로그인 (OAuth)".
 *  - Python 파이프라인에는 main.js 가 이 배열을 JSON 으로 admin_ui 자식 stdin 첫 줄에
 *    실어 주입한다(env 아님 → 환경 블록에 평문 비밀번호 방지. → fetch_mail.py 는 env 상속).
 *    평문 파일 불필요.
 *  - safeStorage 암호화가 불가능한 환경이면(드묾) 볼트를 쓰지 않고 accounts.yaml 폴백.
 */
const fs = require("node:fs");
const path = require("node:path");
const { app, safeStorage } = require("electron");

const VALID_TYPES = new Set(["gmail", "naver", "outlook"]);

function vaultPath() {
  return path.join(app.getPath("userData"), "accounts.enc");
}

function isAvailable() {
  try {
    return safeStorage.isEncryptionAvailable();
  } catch {
    return false;
  }
}

/** 계정 dict 를 정규화(허용 필드만, 타입 검증). 유효하지 않으면 null. */
function normalize(a) {
  if (!a || typeof a !== "object") return null;
  const type = String(a.type || "").trim();
  const user = String(a.user || "").trim();
  const password = String(a.password || "");
  const authRaw = String(a.auth || "").trim().toLowerCase();
  const auth = authRaw === "xoauth2" || authRaw === "password" ? authRaw : "";
  const alias = String(a.alias || "").trim();
  // 실효 인증 방식이 xoauth2 면(명시했거나 Outlook 기본) 비밀번호는 없어도 된다.
  const usesOAuth = auth === "xoauth2" || (auth === "" && type === "outlook");
  if (!VALID_TYPES.has(type) || !user) return null;
  if (!usesOAuth && !password) return null;
  const out = { type, user, password };
  if (auth) out.auth = auth;
  if (alias) out.alias = alias;
  return out;
}

/** 볼트에서 계정 배열을 읽는다. 파일 없음/복호화 불가/손상이면 null. */
function read() {
  if (!isAvailable()) return null;
  let buf;
  try {
    buf = fs.readFileSync(vaultPath());
  } catch {
    return null; // 파일 없음 = 아직 마이그레이션 전
  }
  try {
    const json = safeStorage.decryptString(buf);
    const parsed = JSON.parse(json);
    if (!Array.isArray(parsed)) return [];
    return parsed.map(normalize).filter(Boolean);
  } catch (err) {
    console.error("[vault] 복호화/파싱 실패:", err.message);
    return null;
  }
}

/** 계정 배열을 암호화해서 볼트에 저장. safeStorage 불가면 throw. */
function write(accounts) {
  if (!isAvailable()) {
    throw new Error("이 환경에서는 safeStorage 암호화를 쓸 수 없습니다.");
  }
  const clean = (Array.isArray(accounts) ? accounts : []).map(normalize).filter(Boolean);
  const enc = safeStorage.encryptString(JSON.stringify(clean));
  const p = vaultPath();
  fs.mkdirSync(path.dirname(p), { recursive: true });
  // 원자적 교체: 임시 파일에 쓰고 rename.
  const tmp = p + ".tmp";
  fs.writeFileSync(tmp, enc);
  fs.renameSync(tmp, p);
  return clean;
}

function exists() {
  return fs.existsSync(vaultPath());
}

// --- CRUD (전부 read → 수정 → write) ---

function list() {
  return read() || [];
}

function add(account) {
  const acc = normalize(account);
  if (!acc) throw new Error("계정 정보가 올바르지 않습니다 (종류/이메일, OAuth 아니면 비밀번호 확인).");
  const all = list();
  if (all.some((a) => a.user === acc.user)) {
    throw new Error(`이미 있는 계정입니다: ${acc.user}`);
  }
  all.push(acc);
  return write(all);
}

function update(originalUser, account) {
  const acc = normalize(account);
  if (!acc) throw new Error("계정 정보가 올바르지 않습니다.");
  const all = list();
  const idx = all.findIndex((a) => a.user === originalUser);
  if (idx === -1) throw new Error(`없는 계정입니다: ${originalUser}`);
  // 이메일을 바꾸는 경우 다른 계정과 충돌 방지
  if (acc.user !== originalUser && all.some((a) => a.user === acc.user)) {
    throw new Error(`이미 있는 계정입니다: ${acc.user}`);
  }
  all[idx] = acc;
  return write(all);
}

function remove(user) {
  const all = list();
  const next = all.filter((a) => a.user !== user);
  return write(next);
}

// --- accounts.yaml → 볼트 최초 마이그레이션 ---

/** mail_core/accounts.py 의 최소 파서와 같은 규칙(주석 제거, "- " 항목, key: value). */
function parseYaml(text) {
  const accounts = [];
  let current = null;
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.split("#")[0].replace(/\s+$/, "");
    let stripped = line.trim();
    if (!stripped || stripped === "accounts:") continue;
    if (stripped.startsWith("- ")) {
      if (current) accounts.push(current);
      current = {};
      stripped = stripped.slice(2).trim();
    }
    if (current === null || !stripped.includes(":")) continue;
    const i = stripped.indexOf(":");
    const key = stripped.slice(0, i).trim();
    let value = stripped.slice(i + 1).trim();
    value = value.replace(/^["']|["']$/g, "");
    if (key && value) current[key] = value;
  }
  if (current) accounts.push(current);
  return accounts.map(normalize).filter(Boolean);
}

/**
 * 볼트가 비어 있고 accounts.yaml 에 계정이 있으면 볼트로 암호화해 옮긴다.
 * yaml 은 지우지 않는다(백업). @returns {{migrated:number, reason?:string}}
 */
function migrateFromYaml(yamlPath) {
  if (!isAvailable()) return { migrated: 0, reason: "safeStorage 불가" };
  if (exists() && (read() || []).length > 0) return { migrated: 0, reason: "볼트에 이미 계정 있음" };
  let text;
  try {
    text = fs.readFileSync(yamlPath, "utf-8");
  } catch {
    return { migrated: 0, reason: "accounts.yaml 없음" };
  }
  const accounts = parseYaml(text);
  if (!accounts.length) return { migrated: 0, reason: "accounts.yaml 에 유효한 계정 없음" };
  write(accounts);
  return { migrated: accounts.length };
}

module.exports = {
  vaultPath,
  isAvailable,
  exists,
  read,
  write,
  list,
  add,
  update,
  remove,
  migrateFromYaml,
  parseYaml,
  normalize,
};
