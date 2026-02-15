/**
 * Kalshi API authentication via RSA-PSS signing.
 *
 * Each request requires:
 *   KALSHI-ACCESS-KEY: API key ID
 *   KALSHI-ACCESS-SIGNATURE: RSA-PSS(SHA256) of "timestamp + method + path"
 *   KALSHI-ACCESS-TIMESTAMP: Unix timestamp in milliseconds
 */

import crypto from "node:crypto";
import fs from "node:fs";
import { kalshiConfig } from "../config.js";

let _privateKey: crypto.KeyObject | null = null;

/**
 * Load the RSA private key from file or env var (lazy, cached).
 */
function getPrivateKey(): crypto.KeyObject {
  if (_privateKey) return _privateKey;

  let pem: string;

  if (kalshiConfig.privateKeyPem) {
    // Inline PEM from env (newlines encoded as \n)
    pem = kalshiConfig.privateKeyPem.replace(/\\n/g, "\n");
  } else if (kalshiConfig.privateKeyPath) {
    pem = fs.readFileSync(kalshiConfig.privateKeyPath, "utf-8");
  } else {
    throw new Error("No Kalshi private key configured (set KALSHI_PRIVATE_KEY or KALSHI_PRIVATE_KEY_PATH)");
  }

  _privateKey = crypto.createPrivateKey(pem);
  return _privateKey;
}

/**
 * Sign a message using RSA-PSS with SHA-256.
 */
function signPss(message: string): string {
  const key = getPrivateKey();
  const signature = crypto.sign("sha256", Buffer.from(message, "utf-8"), {
    key,
    padding: crypto.constants.RSA_PKCS1_PSS_PADDING,
    saltLength: crypto.constants.RSA_PSS_SALTLEN_DIGEST,
  });
  return signature.toString("base64");
}

/**
 * Generate authentication headers for a Kalshi API request.
 */
export function createAuthHeaders(
  method: string,
  path: string
): Record<string, string> {
  if (!kalshiConfig.hasAuth) {
    return {};
  }

  const timestamp = Date.now().toString();
  // Strip query params from path before signing
  const cleanPath = path.split("?")[0];
  const message = timestamp + method.toUpperCase() + cleanPath;
  const signature = signPss(message);

  return {
    "KALSHI-ACCESS-KEY": kalshiConfig.apiKeyId,
    "KALSHI-ACCESS-SIGNATURE": signature,
    "KALSHI-ACCESS-TIMESTAMP": timestamp,
  };
}

/**
 * Generate auth headers specifically for WebSocket connection.
 */
export function createWsAuthHeaders(): Record<string, string> {
  return createAuthHeaders("GET", "/trade-api/ws/v2");
}

/**
 * Check if authentication is available.
 */
export function isAuthAvailable(): boolean {
  if (!kalshiConfig.hasAuth) {
    // Log why hasAuth is false for debugging
    const { apiKeyId, privateKeyPath, privateKeyPem } = kalshiConfig;
    console.log(
      `[auth] hasAuth=false: apiKeyId.length=${apiKeyId.length}, ` +
      `privateKeyPath.length=${privateKeyPath.length}, ` +
      `privateKeyPem.length=${privateKeyPem.length}`
    );
    return false;
  }
  try {
    getPrivateKey();
    return true;
  } catch (err) {
    console.error("[auth] getPrivateKey() failed:", err);
    return false;
  }
}
