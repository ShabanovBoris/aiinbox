// The worker is the sole caller of these persistence functions; UI contexts get projections only.
import {
  MAX_PENDING_CAPTURES,
  MAX_PENDING_PAYLOAD_CHARS,
  MAX_RECENT_ITEMS,
  RECENT_STATUS_RETENTION_MS,
} from "./limits.js";
import { characterCount } from "./capture.js";
import { isTerminalProcessingStatus } from "./retry.js";

export const STORAGE_KEYS = Object.freeze({
  queue: "capture_queue_v1",
  config: "api_config_v1",
  token: "auth_token_v1",
  connection: "connection_state_v1",
  recent: "recent_items_v1",
  notice: "ui_notice_v1",
});

function stateError(code, message) {
  // Повреждённая локальная структура должна стать видимой ошибкой, а не пустой очередью.
  const error = new Error(message);
  error.code = code;
  return error;
}

function readArray(value, name) {
  // Отсутствующий ключ означает чистую установку, неверный тип не должен стирать очередь.
  if (value === undefined) return [];
  if (!Array.isArray(value)) {
    throw stateError("LOCAL_STATE_INVALID", `Stored ${name} state is invalid.`);
  }
  return value;
}

/** Load durable worker state after every wake; no queue data is held as canonical memory. */
export async function loadStoredState(storageArea) {
  const stored = await storageArea.get(Object.values(STORAGE_KEYS));
  return {
    queue: readArray(stored[STORAGE_KEYS.queue], "queue"),
    config: stored[STORAGE_KEYS.config] ?? null,
    token: stored[STORAGE_KEYS.token] ?? null,
    connection: stored[STORAGE_KEYS.connection] ?? "UNCONFIGURED",
    recent: readArray(stored[STORAGE_KEYS.recent], "recent status"),
    notice: stored[STORAGE_KEYS.notice] ?? null,
  };
}

/** Count only private capture text and explicit note toward the user's queue budget. */
export function pendingPayloadCharacters(queue) {
  return queue.reduce(
    (total, record) => total + characterCount(record.text) + characterCount(record.user_note ?? ""),
    0,
  );
}

/** Append without evicting unsent records when either durable queue bound is reached. */
export function appendCapture(queue, record) {
  if (
    queue.length >= MAX_PENDING_CAPTURES ||
    pendingPayloadCharacters(queue) + characterCount(record.text) + characterCount(record.user_note ?? "") >
      MAX_PENDING_PAYLOAD_CHARS
  ) {
    throw stateError("LOCAL_QUEUE_FULL", "The local queue is full. Restore the connection or discard failed captures.");
  }
  return [...queue, record];
}

/** Persist one queue value; callers serialize this read-modify-write in the worker. */
export async function storeQueue(storageArea, queue) {
  await storageArea.set({ [STORAGE_KEYS.queue]: queue });
}

/** Persist small accepted Item metadata after removing the private local request body. */
export function acceptCapture(queue, recent, localId, item, nowMs = Date.now()) {
  const remaining = queue.filter((record) => record.local_id !== localId);
  if (remaining.length === queue.length) return { queue, recent };

  const acceptedAt = new Date(nowMs).toISOString();
  const entry = {
    item_id: item.id,
    api_origin: queue.find((record) => record.local_id === localId)?.api_origin ?? null,
    accepted_at: acceptedAt,
    processing_status: item.processing_status,
    state: item.state,
    last_checked_at: null,
    next_check_at: new Date(nowMs + 60_000).toISOString(),
    poll_until: new Date(nowMs + 30 * 60_000).toISOString(),
  };
  return { queue: remaining, recent: retainRecentItems([entry, ...recent], nowMs) };
}

/** Keep the most recent 20 statuses, removing terminal entries before active work. */
export function retainRecentItems(items, nowMs = Date.now()) {
  const recent = items.filter((item) => {
    const accepted = Date.parse(item.accepted_at);
    return Number.isFinite(accepted) && accepted >= nowMs - RECENT_STATUS_RETENTION_MS;
  });
  while (recent.length > MAX_RECENT_ITEMS) {
    const terminalIndex = recent
      .map((item, index) => ({ item, index }))
      .filter(({ item }) => isTerminalProcessingStatus(item.processing_status))
      .sort((left, right) => Date.parse(left.item.accepted_at) - Date.parse(right.item.accepted_at))[0]
      ?.index;
    if (terminalIndex !== undefined) {
      recent.splice(terminalIndex, 1);
    } else {
      const oldestIndex = recent
        .map((item, index) => ({ item, index }))
        .sort((left, right) => Date.parse(left.item.accepted_at) - Date.parse(right.item.accepted_at))[0].index;
      recent.splice(oldestIndex, 1);
    }
  }
  return recent;
}

/** Replace one queue record immutably so concurrent worker handlers cannot lose fields. */
export function updateCapture(queue, localId, update) {
  let found = false;
  const next = queue.map((record) => {
    if (record.local_id !== localId) return record;
    found = true;
    return update(record);
  });
  return found ? next : queue;
}

/** Remove a single user-discarded terminal or destination-mismatched capture. */
export function discardCapture(queue, localId) {
  return queue.filter((record) => record.local_id !== localId);
}

/** Bind captures made before setup to the first API origin without changing their body or key. */
export function bindUnconfiguredCaptures(queue, apiOrigin) {
  return queue.map((record) =>
    record.api_origin === null ? { ...record, api_origin: apiOrigin } : record,
  );
}

/** Expose statuses and error codes only; never include token or pending capture text. */
export function publicState(state) {
  const origin = state.config?.origin ?? null;
  const terminal = state.queue
    .filter((record) => record.state === "TERMINAL_ERROR")
    .map(({ local_id, last_error_code, created_at }) => ({ local_id, last_error_code, created_at }));
  const otherOrigin = state.queue
    .filter((record) => record.api_origin && record.api_origin !== origin)
    .map(({ local_id, api_origin, created_at }) => ({ local_id, api_origin, created_at }));
  const recent = state.recent.map((item) => ({
    item_id: item.item_id,
    api_origin: item.api_origin,
    accepted_at: item.accepted_at,
    processing_status: item.processing_status,
    state: item.state,
    last_checked_at: item.last_checked_at,
  }));
  return {
    api_origin: origin,
    token_configured: Boolean(state.token),
    connection_status: state.connection === "AUTH_REQUIRED"
      ? "AUTH_FAILED"
      : state.config && state.token
        ? state.connection
        : "NOT_CONFIGURED",
    pending_count: state.queue.length,
    terminal_captures: terminal,
    other_origin_captures: otherOrigin,
    recent_items: recent,
    latest_item: recent.find((item) => item.api_origin === origin) ?? null,
    last_notice: state.notice ? { code: state.notice.code, created_at: state.notice.created_at } : null,
  };
}
