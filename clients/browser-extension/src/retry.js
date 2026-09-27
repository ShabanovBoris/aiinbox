// Retry policy is deterministic except for injected jitter, which keeps tests stable.
import {
  BASE_RETRY_DELAY_MS,
  MAX_AUTOMATIC_ATTEMPTS,
  MAX_RETRY_AFTER_MS,
  MAX_RETRY_DELAY_MS,
  MIN_ALARM_DELAY_MS,
} from "./limits.js";

/** Classify PM-18 HTTP outcomes without retrying permanent request errors. */
export function classifyHttpStatus(status) {
  if (status === 202) return "ACCEPTED";
  if (status === 401) return "AUTH_REQUIRED";
  if (status === 408 || status === 429 || status >= 500) return "RETRY";
  return "TERMINAL";
}

/** Parse Retry-After seconds or HTTP-date and cap provider-controlled delays. */
export function parseRetryAfter(value, nowMs = Date.now()) {
  if (typeof value !== "string" || !value.trim()) return null;
  const raw = value.trim();
  let delay;
  if (/^\d+$/.test(raw)) {
    delay = Number(raw) * 1_000;
  } else {
    const dateMs = Date.parse(raw);
    if (!Number.isFinite(dateMs)) return null;
    delay = dateMs - nowMs;
  }
  if (!Number.isFinite(delay) || delay < 0) return null;
  return Math.max(MIN_ALARM_DELAY_MS, Math.min(delay, MAX_RETRY_AFTER_MS));
}

/** Schedule bounded exponential retry, adding small jitter to avoid synchronized retries. */
export function nextRetryAt({ attempt, nowMs = Date.now(), retryAfter = null, random = Math.random }) {
  const providerDelay = parseRetryAfter(retryAfter, nowMs);
  if (providerDelay !== null) return new Date(nowMs + providerDelay).toISOString();

  const exponent = Math.max(0, Math.min(attempt - 1, 20));
  const target = Math.min(MAX_RETRY_DELAY_MS, BASE_RETRY_DELAY_MS * 2 ** exponent);
  const jitter = 0.8 + Math.max(0, Math.min(random(), 1)) * 0.4;
  const delay = Math.max(MIN_ALARM_DELAY_MS, Math.min(MAX_RETRY_DELAY_MS, Math.round(target * jitter)));
  return new Date(nowMs + delay).toISOString();
}

/** Mark terminal processing statuses so accepted Items are never resubmitted. */
export function isTerminalProcessingStatus(status) {
  return status === "READY" || status === "FAILED";
}

/** Return whether a queue record still has an automatic send attempt available. */
export function hasAutomaticAttemptsRemaining(attempts) {
  return attempts < MAX_AUTOMATIC_ATTEMPTS;
}
