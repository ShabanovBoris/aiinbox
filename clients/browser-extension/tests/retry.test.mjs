import assert from "node:assert/strict";
import test from "node:test";

import {
  classifyHttpStatus,
  hasAutomaticAttemptsRemaining,
  isTerminalProcessingStatus,
  nextRetryAt,
  parseRetryAfter,
} from "../src/retry.js";

test("retry classification distinguishes temporary, auth, accepted, and terminal responses", () => {
  for (const status of [408, 429, 500, 502, 503, 504]) assert.equal(classifyHttpStatus(status), "RETRY");
  assert.equal(classifyHttpStatus(202), "ACCEPTED");
  assert.equal(classifyHttpStatus(401), "AUTH_REQUIRED");
  for (const status of [400, 404, 409, 413, 422]) assert.equal(classifyHttpStatus(status), "TERMINAL");
});

test("exponential backoff is bounded and jitter can be deterministic", () => {
  const now = 1_800_000_000_000;
  assert.equal(Date.parse(nextRetryAt({ attempt: 1, nowMs: now, random: () => 0.5 })), now + 30_000);
  assert.equal(Date.parse(nextRetryAt({ attempt: 2, nowMs: now, random: () => 0.5 })), now + 60_000);
  assert.ok(Date.parse(nextRetryAt({ attempt: 8, nowMs: now, random: () => 1 })) <= now + 3_600_000);
  assert.equal(hasAutomaticAttemptsRemaining(7), true);
  assert.equal(hasAutomaticAttemptsRemaining(8), false);
});

test("Retry-After supports seconds and HTTP dates with a six-hour cap", () => {
  const now = Date.parse("2026-01-01T00:00:00.000Z");
  assert.equal(parseRetryAfter("45", now), 45_000);
  assert.equal(parseRetryAfter("Thu, 01 Jan 2026 00:02:00 GMT", now), 120_000);
  assert.equal(parseRetryAfter("invalid", now), null);
  assert.equal(parseRetryAfter("999999", now), 6 * 60 * 60 * 1_000);
  assert.equal(parseRetryAfter("0", now), 30_000);
});

test("READY and FAILED stop status polling and never imply a capture retry", () => {
  assert.equal(isTerminalProcessingStatus("READY"), true);
  assert.equal(isTerminalProcessingStatus("FAILED"), true);
  assert.equal(isTerminalProcessingStatus("PROCESSING"), false);
});
