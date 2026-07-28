'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');

const MAX_SESSION_ID_LENGTH = 128;

function sanitizeSessionId(value) {
  if (typeof value !== 'string') return null;
  const trimmed = value.trim();
  if (!trimmed) return null;
  const sanitized = trimmed
    .replace(/[^A-Za-z0-9._-]/g, '_')
    .slice(0, MAX_SESSION_ID_LENGTH);
  return sanitized || null;
}

function bridgePath(sessionId) {
  const safeSessionId = sanitizeSessionId(sessionId);
  if (!safeSessionId) return null;
  return path.join(os.tmpdir(), `ecc-metrics-${safeSessionId}.json`);
}

function readBridge(sessionId) {
  const filePath = bridgePath(sessionId);
  if (!filePath) return null;
  try {
    const value = JSON.parse(fs.readFileSync(filePath, 'utf8'));
    return value && typeof value === 'object' && !Array.isArray(value) ? value : null;
  } catch {
    return null;
  }
}

function writeBridgeAtomic(sessionId, value) {
  const filePath = bridgePath(sessionId);
  if (!filePath) throw new TypeError('A valid session id is required');
  const tempPath = `${filePath}.${process.pid}.${Date.now()}.tmp`;
  try {
    fs.writeFileSync(tempPath, JSON.stringify(value), 'utf8');
    fs.renameSync(tempPath, filePath);
  } finally {
    try {
      if (fs.existsSync(tempPath)) fs.unlinkSync(tempPath);
    } catch {
      // Best-effort cleanup; callers treat bridge persistence as non-blocking.
    }
  }
}

module.exports = { readBridge, sanitizeSessionId, writeBridgeAtomic };
