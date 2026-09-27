// These bounds mirror PM-18 and keep local retries within browser storage limits.
export const MAX_CAPTURE_TEXT_CHARS = 20_000;
export const MAX_USER_NOTE_CHARS = 2_000;
export const MAX_HTTP_BODY_BYTES = 64 * 1024;
export const MAX_PENDING_CAPTURES = 100;
export const MAX_PENDING_PAYLOAD_CHARS = 1_000_000;
export const MAX_AUTOMATIC_ATTEMPTS = 8;
export const MAX_SENDS_PER_WAKE = 5;
export const API_TIMEOUT_MS = 15_000;
export const BASE_RETRY_DELAY_MS = 30_000;
export const MAX_RETRY_DELAY_MS = 60 * 60 * 1_000;
export const MAX_RETRY_AFTER_MS = 6 * 60 * 60 * 1_000;
export const MIN_ALARM_DELAY_MS = 30_000;
export const STATUS_POLL_INTERVAL_MS = 60_000;
export const STATUS_POLL_HORIZON_MS = 30 * 60 * 1_000;
export const RECENT_STATUS_RETENTION_MS = 24 * 60 * 60 * 1_000;
export const MAX_RECENT_ITEMS = 20;
