/**
 * mail_core.crypto(Python) 와 바이트 호환되는 AES-256-GCM 유틸 - electron 의존 없음.
 *
 * 포맷 계약(양쪽이 반드시 같아야 함):
 *   키 파일 secret.key = base64url(32바이트)
 *   토큰 = base64url(nonce(12B) + ciphertext + tag(16B))
 * 계약은 packages/mail-core/tests/test_crypto_interop.py 가 Python<->Node 왕복 +
 * 고정 벡터로 강제한다. 한쪽 포맷을 바꾸면 그 테스트가 깨진다.
 */
const fs = require("node:fs");
const crypto = require("node:crypto");

function _urlsafeB64Decode(s) {
  let b64 = s.replace(/-/g, "+").replace(/_/g, "/");
  while (b64.length % 4) b64 += "=";
  return Buffer.from(b64, "base64");
}

function _loadPythonSecretKey(secretKeyPath) {
  const b64 = fs.readFileSync(secretKeyPath, "utf8").trim();
  const key = _urlsafeB64Decode(b64);
  if (key.length !== 32) throw new Error("secret.key 길이가 32바이트가 아닙니다.");
  return key;
}

function decryptPasswordEnc(token, secretKeyPath) {
  const key = _loadPythonSecretKey(secretKeyPath);
  const raw = _urlsafeB64Decode(token);
  const nonce = raw.subarray(0, 12);
  const rest = raw.subarray(12);
  const tag = rest.subarray(rest.length - 16);
  const ct = rest.subarray(0, rest.length - 16);
  const decipher = crypto.createDecipheriv("aes-256-gcm", key, nonce);
  decipher.setAuthTag(tag);
  return Buffer.concat([decipher.update(ct), decipher.final()]).toString("utf8");
}

function encryptPasswordEnc(plaintext, secretKeyPath) {
  const key = _loadPythonSecretKey(secretKeyPath);
  const nonce = crypto.randomBytes(12);
  const cipher = crypto.createCipheriv("aes-256-gcm", key, nonce);
  const ct = Buffer.concat([cipher.update(plaintext, "utf8"), cipher.final()]);
  const raw = Buffer.concat([nonce, ct, cipher.getAuthTag()]);
  return raw.toString("base64").replace(/\+/g, "-").replace(/\//g, "_");
}

module.exports = { decryptPasswordEnc, encryptPasswordEnc };

// CLI: node pycrypto.js <decrypt|encrypt> <secret.key path> <text>  (interop 테스트용)
if (require.main === module) {
  const [mode, keyPath, text] = process.argv.slice(2);
  const fn = mode === "encrypt" ? encryptPasswordEnc : decryptPasswordEnc;
  process.stdout.write(fn(text, keyPath));
}
