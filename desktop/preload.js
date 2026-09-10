"use strict";
// 렌더러 ↔ main IPC 브리지.
// 메인 창은 Flask UI(별도 origin)를 로드하므로 이 preload 는 거기서 거의 안 쓰인다.
// renderer/settings.html(계정 설정창)만 아래 API 를 쓴다 - Phase 3.
const { contextBridge, ipcRenderer } = require("electron");

// 계정 CRUD API 는 file:// 로 로드되는 우리 설정창(renderer/settings.html)에만 노출한다.
// 메인 창은 http://127.0.0.1 의 Flask UI 라 이 API 가 필요 없다.
const isLocalRenderer = location.protocol === "file:";

const bridge = { phase: 3 };
if (isLocalRenderer) {
  bridge.accounts = {
    status: () => ipcRenderer.invoke("accounts:status"),
    list: () => ipcRenderer.invoke("accounts:list"),
    add: (account) => ipcRenderer.invoke("accounts:add", account),
    update: (originalUser, account) => ipcRenderer.invoke("accounts:update", { originalUser, account }),
    remove: (user) => ipcRenderer.invoke("accounts:remove", user),
  };
  bridge.closeSettings = () => ipcRenderer.send("settings:close");
}

contextBridge.exposeInMainWorld("mailAgent", bridge);
