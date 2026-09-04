"use strict";
/*
 * 가벼운 업데이트 확인 — GitHub Releases API 만 본다(외부 npm 패키지 없음).
 *
 * `electron-updater`는 델타/서명검증/자동 재설치까지 하지만 transitive 의존성이 크다.
 * 단일 사용자 포터블 도구에는 "새 버전 있으면 알림 + 릴리스 페이지 열기"로 충분하다.
 * 실제 릴리스는 `npm run release`(electron-builder --publish always)가
 * `latest.yml` + `ai-mail-agent-<v>-portable.exe`를 GitHub Releases 에 올린다.
 *
 * owner/repo 는 package.json 의 `build.publish` 또는 `repository.url` 에서 읽는다.
 */
const https = require("node:https");
const path = require("node:path");
const { app, shell, Notification } = require("electron");

function repoSlug() {
  let pkg;
  try {
    pkg = require(path.join(__dirname, "package.json"));
  } catch {
    return null;
  }
  const pub = pkg.build && pkg.build.publish;
  if (pub && pub.owner && pub.repo && pub.owner !== "OWNER") {
    return `${pub.owner}/${pub.repo}`;
  }
  const url = pkg.repository && (typeof pkg.repository === "string" ? pkg.repository : pkg.repository.url);
  const m = url && url.match(/github\.com[/:]([^/]+)\/([^/.]+)/i);
  if (m && m[1] !== "OWNER") return `${m[1]}/${m[2]}`;
  return null; // owner 미설정 — 릴리스 준비 안 됨
}

/** "1.2.3" > "1.2.0" 비교. a>b면 1, 같으면 0, a<b면 -1. */
function cmpVersion(a, b) {
  const pa = String(a).replace(/^v/, "").split(".").map((n) => parseInt(n, 10) || 0);
  const pb = String(b).replace(/^v/, "").split(".").map((n) => parseInt(n, 10) || 0);
  for (let i = 0; i < 3; i++) {
    if ((pa[i] || 0) > (pb[i] || 0)) return 1;
    if ((pa[i] || 0) < (pb[i] || 0)) return -1;
  }
  return 0;
}

function fetchLatestRelease(slug) {
  return new Promise((resolve, reject) => {
    const req = https.get(
      {
        host: "api.github.com",
        path: `/repos/${slug}/releases/latest`,
        headers: { "User-Agent": "ai-mail-agent-updater", Accept: "application/vnd.github+json" },
        timeout: 8000,
      },
      (res) => {
        if (res.statusCode === 404) return resolve(null); // 릴리스 없음
        if (res.statusCode !== 200) return reject(new Error(`GitHub API ${res.statusCode}`));
        let body = "";
        res.on("data", (d) => (body += d));
        res.on("end", () => {
          try {
            resolve(JSON.parse(body));
          } catch (e) {
            reject(e);
          }
        });
      },
    );
    req.on("error", reject);
    req.on("timeout", () => req.destroy(new Error("timeout")));
  });
}

/**
 * @returns {Promise<{available:boolean, reason?:string, version?:string, htmlUrl?:string, exeUrl?:string}>}
 */
async function checkForUpdate() {
  const slug = repoSlug();
  if (!slug) return { available: false, reason: "릴리스 저장소 미설정 (package.json build.publish.owner)" };

  let rel;
  try {
    rel = await fetchLatestRelease(slug);
  } catch (err) {
    return { available: false, reason: `업데이트 확인 실패: ${err.message}` };
  }
  if (!rel || !rel.tag_name) return { available: false, reason: "게시된 릴리스 없음" };

  const latest = rel.tag_name.replace(/^v/, "");
  if (cmpVersion(latest, app.getVersion()) <= 0) {
    return { available: false, reason: `최신 버전 (v${app.getVersion()})` };
  }
  const exe = (rel.assets || []).find((a) => /portable\.exe$/i.test(a.name));
  return {
    available: true,
    version: latest,
    htmlUrl: rel.html_url,
    exeUrl: exe ? exe.browser_download_url : rel.html_url,
  };
}

/**
 * 업데이트가 있으면 알림 + 릴리스 페이지를 연다.
 * @param {{silent?: boolean}} opts silent=true면 업데이트 없을 때 아무 것도 안 함(부팅 시 자동 체크용)
 */
async function checkAndNotify({ silent = false } = {}) {
  const r = await checkForUpdate();
  if (r.available) {
    try {
      if (Notification.isSupported()) {
        new Notification({
          title: `새 버전 v${r.version}`,
          body: "클릭하면 다운로드 페이지가 열립니다. 새 exe 를 받아 기존 것과 교체하세요.",
        })
          .on("click", () => shell.openExternal(r.htmlUrl))
          .show();
      }
    } catch {
      /* 알림 실패 무시 */
    }
    shell.openExternal(r.htmlUrl);
  } else if (!silent) {
    const { dialog } = require("electron");
    dialog.showMessageBox({ type: "info", title: "업데이트 확인", message: r.reason || "최신 버전입니다." });
  }
  return r;
}

module.exports = { checkForUpdate, checkAndNotify, cmpVersion, repoSlug };
