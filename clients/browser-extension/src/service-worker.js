// The service worker is the only owner of credentials, durable queue writes, and PM-18 transport.
import { composePageCapture, composeSelectionCapture, createCaptureRecord } from "./capture.js";
import { getItemStatus, postCapture, testApiConnection } from "./api-client.js";
import {
  MAX_AUTOMATIC_ATTEMPTS,
  MAX_SENDS_PER_WAKE,
  MIN_ALARM_DELAY_MS,
  RECENT_STATUS_RETENTION_MS,
  STATUS_POLL_HORIZON_MS,
  STATUS_POLL_INTERVAL_MS,
} from "./limits.js";
import { classifyHttpStatus, isTerminalProcessingStatus, nextRetryAt } from "./retry.js";
import {
  STORAGE_KEYS,
  acceptCapture,
  appendCapture,
  bindUnconfiguredCaptures,
  discardCapture,
  loadStoredState,
  publicState,
  retainRecentItems,
  storeQueue,
  updateCapture,
} from "./storage.js";
import { apiHostPermission, normalizeApiOrigin } from "./url-policy.js";

const storageArea = chrome.storage.local;
const queueAlarmName = "aibox-queue-v1";
const pageMenuId = "aiinbox-save-page";
const selectionMenuId = "aiinbox-save-selection";
const transientApiErrors = new Set(["NETWORK_ERROR", "TIMEOUT", "INVALID_ACCEPTANCE_RESPONSE"]);
const visibleLocalNoticeCodes = new Set([
  "CAPTURE_TEXT_TOO_LARGE",
  "REQUEST_TOO_LARGE",
  "LOCAL_QUEUE_FULL",
  "UNSUPPORTED_PAGE_URL",
]);

// These promise chains serialize only this worker lifetime; chrome.storage.local remains canonical.
let queueMutationChain = Promise.resolve();
let workChain = Promise.resolve();
let initializationPromise = null;

/** Serialize durable read-modify-write operations across simultaneous extension events. */
function serializeQueueMutation(operation) {
  const result = queueMutationChain.then(operation, operation);
  queueMutationChain = result.catch(() => {});
  return result;
}

/** Keep network drains single-flight while allowing new captures to persist immediately. */
function serializeWorkerWork(operation) {
  const result = workChain.then(operation, operation);
  workChain = result.catch(() => {});
  return result;
}

function safeCode(error) {
  // UI и журналы получают только ограниченный код, чтобы исключение не утекло вместе с данными.
  return typeof error?.code === "string" && /^[A-Z0-9_]{1,48}$/.test(error.code)
    ? error.code
    : "LOCAL_OPERATION_FAILED";
}

function safeLog(event, fields = {}) {
  // Разрешённый список полей исключает токен, заметку, выделение и полный запрос.
  const allowed = ["local_id", "item_id", "status", "attempt", "queue_count"];
  const values = allowed.filter((key) => fields[key] !== undefined).map((key) => `${key}=${fields[key]}`);
  console.info(`PM-33 ${event}${values.length ? ` ${values.join(" ")}` : ""}`);
}

async function readState() {
  // Каноническое состояние перечитывается из storage, поскольку worker может быть пересоздан.
  return loadStoredState(storageArea);
}

async function storeConnection(status) {
  // Статус соединения меняется тем же сериализованным путём, что и очередь.
  await serializeQueueMutation(async () => {
    await storageArea.set({ [STORAGE_KEYS.connection]: status });
  });
}

async function updateBadge() {
  // Значок — только проекция очереди и ошибки, он не хранит бизнес-состояние.
  const state = await readState();
  const text = state.connection === "AUTH_REQUIRED" || state.notice
    ? "!"
    : state.queue.length > 99
      ? "99+"
      : state.queue.length
        ? String(state.queue.length)
        : "";
  await chrome.action.setBadgeBackgroundColor({ color: state.connection === "AUTH_REQUIRED" ? "#b42318" : "#245b76" });
  await chrome.action.setBadgeText({ text });
}

async function expireRecentStatuses() {
  // Удаляются только короткие статусы сервера; незавершённые capture-записи не истекают.
  await serializeQueueMutation(async () => {
    const state = await readState();
    const recent = retainRecentItems(state.recent);
    if (recent.length !== state.recent.length) {
      await storageArea.set({ [STORAGE_KEYS.recent]: recent });
    }
  });
}

/** Recreate one alarm from durable due work because service workers can restart. */
async function recomputeAlarm() {
  const state = await readState();
  const now = Date.now();
  let nextWake = null;
  for (const item of state.recent) {
    const retentionUntil = Date.parse(item.accepted_at) + RECENT_STATUS_RETENTION_MS;
    if (Number.isFinite(retentionUntil) && retentionUntil > now) {
      nextWake = nextWake === null ? retentionUntil : Math.min(nextWake, retentionUntil);
    }
  }
  if (
    state.config &&
    state.token &&
    state.connection !== "AUTH_REQUIRED" &&
    await chrome.permissions.contains({ origins: [apiHostPermission(state.config.origin)] })
  ) {
    for (const record of state.queue) {
      if (record.state !== "PENDING" || record.api_origin !== state.config.origin) continue;
      const dueAt = record.next_attempt_at ? Date.parse(record.next_attempt_at) : now;
      if (Number.isFinite(dueAt)) nextWake = nextWake === null ? dueAt : Math.min(nextWake, dueAt);
    }
    for (const item of state.recent) {
      if (item.api_origin !== state.config.origin) continue;
      if (isTerminalProcessingStatus(item.processing_status)) continue;
      const pollUntil = Date.parse(item.poll_until);
      if (!Number.isFinite(pollUntil) || now >= pollUntil) continue;
      const dueAt = item.next_check_at ? Date.parse(item.next_check_at) : now + STATUS_POLL_INTERVAL_MS;
      if (Number.isFinite(dueAt)) nextWake = nextWake === null ? dueAt : Math.min(nextWake, dueAt);
    }
  }

  if (nextWake === null) {
    await chrome.alarms.clear(queueAlarmName);
    return;
  }
  await chrome.alarms.create(queueAlarmName, {
    when: Math.max(nextWake, now + MIN_ALARM_DELAY_MS),
  });
}

async function initializeWorker() {
  // Сначала закрываем доступ к storage, затем восстанавливаем локальные сроки будильника.
  if (!initializationPromise) {
    initializationPromise = (async () => {
      await storageArea.setAccessLevel({ accessLevel: "TRUSTED_CONTEXTS" });
      await expireRecentStatuses();
      await updateBadge();
      await recomputeAlarm();
    })();
  }
  return initializationPromise;
}

/** Persist intent and stable request identity before any network path can run. */
async function persistCapture(body) {
  const record = await serializeQueueMutation(async () => {
    const state = await readState();
    const apiOrigin = state.config?.origin ?? null;
    const next = appendCapture(
      state.queue,
      createCaptureRecord(body, { apiOrigin }),
    );
    await storageArea.set({
      [STORAGE_KEYS.queue]: next,
      [STORAGE_KEYS.notice]: null,
    });
    return next[next.length - 1];
  });
  await updateBadge();
  return record;
}

/** Keep a bounded, non-content result for context-menu actions that have no popup response. */
async function storeLocalNotice(error) {
  const code = visibleLocalNoticeCodes.has(error?.code) ? error.code : "CAPTURE_FAILED";
  await serializeQueueMutation(async () => {
    await storageArea.set({
      [STORAGE_KEYS.notice]: { code, created_at: new Date().toISOString() },
    });
  });
  await updateBadge();
}

async function updateQueuedCapture(localId, reducer) {
  // Reducer обновляет одну запись, чтобы ответ на HTTP не перезаписал соседние capture.
  await serializeQueueMutation(async () => {
    const state = await readState();
    const queue = reducer(state.queue);
    if (queue !== state.queue) await storeQueue(storageArea, queue);
  });
  await updateBadge();
}

async function markAttemptStarted(localId) {
  // Счётчик фиксируется до fetch: перезапуск не превращает бесконечные обрывы в новые попытки.
  let attempted = null;
  await serializeQueueMutation(async () => {
    const state = await readState();
    const current = state.queue.find((record) => record.local_id === localId);
    if (!current || current.state !== "PENDING" || current.attempts >= MAX_AUTOMATIC_ATTEMPTS) return;
    attempted = { ...current, attempts: current.attempts + 1, next_attempt_at: null };
    await storeQueue(
      storageArea,
      updateCapture(state.queue, localId, () => attempted),
    );
  });
  return attempted;
}

async function persistAccepted(localId, item, apiOrigin, token) {
  // 202 атомарно переносит Item в краткий статус и удаляет локальное приватное тело.
  await serializeQueueMutation(async () => {
    const state = await readState();
    const accepted = acceptCapture(state.queue, state.recent, localId, item);
    await storageArea.set({
      [STORAGE_KEYS.queue]: accepted.queue,
      [STORAGE_KEYS.recent]: accepted.recent,
      [STORAGE_KEYS.connection]: state.config?.origin === apiOrigin && state.token === token
        ? "CONNECTED"
        : state.connection,
    });
  });
  await updateBadge();
}

async function recordHttpFailure(record, outcome, nowMs = Date.now(), token) {
  // Ответ API классифицируется здесь, чтобы постоянный 4xx не попал в бесконечную очередь.
  const classification = classifyHttpStatus(outcome.status);
  if (classification === "AUTH_REQUIRED") {
    await updateQueuedCapture(record.local_id, (queue) =>
      updateCapture(queue, record.local_id, (current) => ({
        ...current,
        attempts: Math.max(0, current.attempts - 1),
        next_attempt_at: null,
        last_error_code: "AUTH_REQUIRED",
      })),
    );
    const state = await readState();
    if (state.config?.origin === record.api_origin && state.token === token) {
      await storeConnection("AUTH_REQUIRED");
    }
    return;
  }

  if (classification === "RETRY") {
    if (record.attempts >= MAX_AUTOMATIC_ATTEMPTS) {
      await updateQueuedCapture(record.local_id, (queue) =>
        updateCapture(queue, record.local_id, (current) => ({
          ...current,
          state: "TERMINAL_ERROR",
          next_attempt_at: null,
          last_error_code: "RETRY_EXHAUSTED",
        })),
      );
      return;
    }
    const nextAttempt = nextRetryAt({
      attempt: record.attempts,
      nowMs,
      retryAfter: outcome.retry_after,
    });
    await updateQueuedCapture(record.local_id, (queue) =>
      updateCapture(queue, record.local_id, (current) => ({
        ...current,
        next_attempt_at: nextAttempt,
        last_error_code: outcome.error_code ?? "TRANSIENT_HTTP_ERROR",
      })),
    );
    if (outcome.status >= 500) {
      const state = await readState();
      if (state.config?.origin === record.api_origin && state.token === token) {
        await storeConnection("SERVER_UNHEALTHY");
      }
    }
    return;
  }

  await updateQueuedCapture(record.local_id, (queue) =>
    updateCapture(queue, record.local_id, (current) => ({
      ...current,
      state: "TERMINAL_ERROR",
      next_attempt_at: null,
      last_error_code: outcome.error_code ?? "REQUEST_REJECTED",
    })),
  );
}

async function recordTransportFailure(record, error, token, nowMs = Date.now()) {
  // Сетевой сбой повторяется с тем же телом и ключом, а не создаёт новую capture-операцию.
  if (!transientApiErrors.has(error.code)) throw error;
  let state = await readState();
  if (state.config?.origin === record.api_origin && state.token === token) {
    await storeConnection("SERVER_UNREACHABLE");
  }
  if (record.attempts >= MAX_AUTOMATIC_ATTEMPTS) {
    await updateQueuedCapture(record.local_id, (queue) =>
      updateCapture(queue, record.local_id, (current) => ({
        ...current,
        state: "TERMINAL_ERROR",
        next_attempt_at: null,
        last_error_code: "RETRY_EXHAUSTED",
      })),
    );
    return;
  }
  await updateQueuedCapture(record.local_id, (queue) =>
    updateCapture(queue, record.local_id, (current) => ({
      ...current,
      next_attempt_at: nextRetryAt({ attempt: record.attempts, nowMs }),
      last_error_code: error.code,
    })),
  );
}

async function attemptCapture(localId, config, token) {
  // Перед отправкой проверяется актуальная конфигурация, затем используется уже сохранённая identity.
  const state = await readState();
  if (
    state.config?.origin !== config.origin ||
    state.token !== token ||
    state.connection === "AUTH_REQUIRED"
  ) return false;
  if (!(await chrome.permissions.contains({ origins: [apiHostPermission(config.origin)] }))) return false;
  const record = await markAttemptStarted(localId);
  if (!record) return false;
  const origin = record.api_origin ?? config.origin;
  if (origin !== config.origin) return false;

  try {
    const outcome = await postCapture({ apiOrigin: origin, token, record });
    if (outcome.kind === "ACCEPTED") {
      await persistAccepted(localId, outcome.item, origin, token);
      safeLog("accepted", { local_id: localId, item_id: outcome.item.id, status: 202 });
      return true;
    }
    await recordHttpFailure(record, outcome, Date.now(), token);
    safeLog("http-outcome", { local_id: localId, status: outcome.status, attempt: record.attempts });
  } catch (error) {
    await recordTransportFailure(record, error, token);
    safeLog("transport-outcome", { local_id: localId, status: safeCode(error), attempt: record.attempts });
  }
  return true;
}

async function pollOneItem(item, config, token, nowMs = Date.now()) {
  // Polling меняет только локальную проекцию статуса и никогда не возвращает Item в capture-очередь.
  try {
    const result = await getItemStatus({
      apiOrigin: config.origin,
      token,
      itemId: item.item_id,
    });
    if (result.kind === "HTTP_ERROR") {
      if (result.status === 401) {
        const state = await readState();
        if (state.config?.origin === config.origin && state.token === token) {
          await storeConnection("AUTH_REQUIRED");
        }
      }
      await scheduleNextStatusCheck(item.item_id, config.origin, token, nowMs);
      return;
    }
    await serializeQueueMutation(async () => {
      const state = await readState();
      if (state.config?.origin !== config.origin || state.token !== token) return;
      const recent = state.recent.map((current) => {
        if (current.item_id !== item.item_id || current.api_origin !== config.origin) return current;
        const terminal = isTerminalProcessingStatus(result.item.processing_status);
        const pollUntil = Date.parse(current.poll_until);
        const keepPolling = !terminal && nowMs < pollUntil;
        return {
          ...current,
          processing_status: result.item.processing_status,
          state: result.item.state,
          last_checked_at: new Date(nowMs).toISOString(),
          next_check_at: keepPolling ? new Date(nowMs + STATUS_POLL_INTERVAL_MS).toISOString() : null,
        };
      });
      await storageArea.set({ [STORAGE_KEYS.recent]: recent });
    });
  } catch (error) {
    if (!transientApiErrors.has(error.code) && error.code !== "INVALID_STATUS_RESPONSE") throw error;
    await scheduleNextStatusCheck(item.item_id, config.origin, token, nowMs);
  }
}

async function scheduleNextStatusCheck(itemId, apiOrigin, token, nowMs) {
  // Поздний ответ старого сервера не должен менять статус одноимённого Item нового сервера.
  await serializeQueueMutation(async () => {
    const state = await readState();
    if (state.config?.origin !== apiOrigin || state.token !== token) return;
    const recent = state.recent.map((item) => {
      if (
        item.item_id !== itemId ||
        item.api_origin !== apiOrigin ||
        isTerminalProcessingStatus(item.processing_status)
      ) return item;
      const keepPolling = nowMs < Date.parse(item.poll_until);
      return {
        ...item,
        last_checked_at: new Date(nowMs).toISOString(),
        next_check_at: keepPolling ? new Date(nowMs + STATUS_POLL_INTERVAL_MS).toISOString() : null,
      };
    });
    await storageArea.set({ [STORAGE_KEYS.recent]: recent });
  });
}

async function pollStatuses({ explicit = false } = {}) {
  // Фоновый опрос ограничен активными Item и горизонтом; popup может запросить разовое обновление.
  const state = await readState();
  if (!state.config || !state.token || state.connection === "AUTH_REQUIRED") return;
  if (!(await chrome.permissions.contains({ origins: [apiHostPermission(state.config.origin)] }))) return;
  const now = Date.now();
  const candidates = state.recent.filter((item) => {
    if (item.api_origin !== state.config.origin) return false;
    if (isTerminalProcessingStatus(item.processing_status)) return false;
    if (explicit) return true;
    const pollUntil = Date.parse(item.poll_until);
    return now < pollUntil && (!item.next_check_at || Date.parse(item.next_check_at) <= now);
  });
  await Promise.all(candidates.map((item) => pollOneItem(item, state.config, state.token, now)));
}

async function processDueCaptures({ specificId = null, maxSends = MAX_SENDS_PER_WAKE } = {}) {
  // Одна активация worker отправляет только due-записи текущего API и ограниченное число запросов.
  const state = await readState();
  if (!state.config || !state.token || state.connection === "AUTH_REQUIRED") return;
  const permission = apiHostPermission(state.config.origin);
  if (!(await chrome.permissions.contains({ origins: [permission] }))) return;

  const now = Date.now();
  const due = state.queue.filter((record) => {
    if (record.state !== "PENDING") return false;
    if (record.api_origin !== null && record.api_origin !== state.config.origin) return false;
    if (specificId !== null && record.local_id !== specificId) return false;
    return !record.next_attempt_at || Date.parse(record.next_attempt_at) <= now;
  });

  let sends = 0;
  for (const pending of due) {
    if (sends >= maxSends) break;
    if (pending.attempts >= MAX_AUTOMATIC_ATTEMPTS) {
      await updateQueuedCapture(pending.local_id, (queue) =>
        updateCapture(queue, pending.local_id, (record) => ({
          ...record,
          state: "TERMINAL_ERROR",
          next_attempt_at: null,
          last_error_code: "RETRY_EXHAUSTED",
        })),
      );
      continue;
    }
    if (pending.api_origin === null) {
      await updateQueuedCapture(pending.local_id, (queue) =>
        updateCapture(queue, pending.local_id, (record) => ({
          ...record,
          api_origin: state.config.origin,
        })),
      );
    }
    const fresh = await readState();
    if (
      fresh.connection === "AUTH_REQUIRED" ||
      fresh.config?.origin !== state.config.origin ||
      fresh.token !== state.token
    ) break;
    if (await attemptCapture(pending.local_id, state.config, state.token)) sends += 1;
  }
}

/** Run a bounded queue batch and/or status refresh under one worker-local execution chain. */
async function processDueWork(options = {}) {
  return serializeWorkerWork(async () => {
    await expireRecentStatuses();
    await processDueCaptures(options);
    if (options.refreshStatuses) await pollStatuses({ explicit: options.explicitStatuses === true });
    await recomputeAlarm();
    await updateBadge();
  });
}

async function publicSnapshot() {
  // Общий ответ для UI строится как безопасная проекция без токена и тела очереди.
  await expireRecentStatuses();
  const state = await readState();
  return publicState(state);
}

async function ensureHostPermission(apiOrigin, requestToken) {
  // Worker повторно проверяет URL и permission, хотя запрос разрешения инициирует Options.
  const origin = normalizeApiOrigin(apiOrigin);
  const token = typeof requestToken === "string" ? requestToken.trim() : "";
  if (token && (token.length < 32 || /[\u0000-\u0020\u007f]/.test(token))) {
    const error = new Error("Enter a valid API token of at least 32 characters.");
    error.code = "INVALID_TOKEN";
    throw error;
  }

  const hasPermission = await chrome.permissions.contains({ origins: [apiHostPermission(origin)] });
  if (!hasPermission) {
    const error = new Error("Grant the configured AIInbox host permission first.");
    error.code = "HOST_PERMISSION_REQUIRED";
    throw error;
  }
  return { origin, token };
}

/** Commit the new API destination only after Options has obtained its host grant. */
async function saveConfiguration(message) {
  const { origin, token: enteredToken } = await ensureHostPermission(message.api_origin, message.token);
  const result = await serializeQueueMutation(async () => {
    const state = await readState();
    const token = enteredToken || (state.config?.origin === origin ? state.token : null);
    if (!token) {
      const error = new Error("Enter an API token for this AIInbox server.");
      error.code = "TOKEN_REQUIRED";
      throw error;
    }
    if (token.length < 32 || /[\u0000-\u0020\u007f]/.test(token)) {
      const error = new Error("Enter a valid API token of at least 32 characters.");
      error.code = "INVALID_TOKEN";
      throw error;
    }

    const unchanged = state.config?.origin === origin && state.token === token;
    const queue = bindUnconfiguredCaptures(state.queue, origin);
    const connection = state.connection === "AUTH_REQUIRED" && state.config?.origin === origin
      ? "AUTH_REQUIRED"
      : unchanged
        ? state.connection
        : "UNTESTED";
    await storageArea.set({
      [STORAGE_KEYS.config]: { origin },
      [STORAGE_KEYS.token]: token,
      [STORAGE_KEYS.queue]: queue,
      [STORAGE_KEYS.connection]: connection,
    });
    const previousPermission = state.config ? apiHostPermission(state.config.origin) : null;
    const nextPermission = apiHostPermission(origin);
    if (previousPermission && previousPermission !== nextPermission) {
      try {
        await chrome.permissions.remove({ origins: [previousPermission] });
      } catch {
        safeLog("old-host-permission-release-failed");
      }
    }
    return {
      previousOrigin: state.config?.origin ?? null,
      origin,
      changed: !unchanged,
    };
  });

  await updateBadge();
  if (result.changed) await processDueWork({ specificId: null, maxSends: 1 });
  await recomputeAlarm();
  return { api_origin: result.origin, token_configured: true };
}

/** Test health and token separately; successful auth releases queued captures. */
async function runConnectionTest() {
  const state = await readState();
  if (!state.config || !state.token) return { status: "NOT_CONFIGURED" };
  if (!(await chrome.permissions.contains({ origins: [apiHostPermission(state.config.origin)] }))) {
    return { status: "PERMISSION_REQUIRED" };
  }
  const result = await testApiConnection({ apiOrigin: state.config.origin, token: state.token });
  const fresh = await readState();
  if (fresh.config?.origin === state.config.origin && fresh.token === state.token) {
    await storeConnection(result.status === "AUTH_FAILED" ? "AUTH_REQUIRED" : result.status);
  }
  if (result.status === "CONNECTED") {
    await processDueWork({ maxSends: 1 });
  }
  await updateBadge();
  await recomputeAlarm();
  return result;
}

async function disconnect() {
  // Удаляются только настройки доступа; локальные несданные данные остаются доступны пользователю.
  let pendingCount = 0;
  await serializeQueueMutation(async () => {
    const state = await readState();
    pendingCount = state.queue.length;
    await storageArea.remove([STORAGE_KEYS.config, STORAGE_KEYS.token]);
    await storageArea.set({ [STORAGE_KEYS.connection]: "UNCONFIGURED" });
    if (state.config) {
      try {
        await chrome.permissions.remove({ origins: [apiHostPermission(state.config.origin)] });
      } catch {
        safeLog("host-permission-release-failed");
      }
    }
  });
  await updateBadge();
  await recomputeAlarm();
  return { disconnected: true, pending_count: pendingCount };
}

async function discardLocalCapture(localId) {
  // Удаление допускается только для постоянной ошибки или записи, привязанной к другому серверу.
  await serializeQueueMutation(async () => {
    const state = await readState();
    const record = state.queue.find((entry) => entry.local_id === localId);
    if (!record) return;
    const originMismatch = Boolean(record.api_origin && record.api_origin !== state.config?.origin);
    if (record.state !== "TERMINAL_ERROR" && !originMismatch) {
      const error = new Error("Only failed or other-server captures can be discarded.");
      error.code = "CAPTURE_NOT_DISCARDABLE";
      throw error;
    }
    await storeQueue(storageArea, discardCapture(state.queue, localId));
  });
  await updateBadge();
  await recomputeAlarm();
  return { discarded: true };
}

async function handleMessage(message) {
  // Popup и Options передают намерения; транспорт и мутации остаются внутри worker.
  await initializeWorker();
  switch (message?.type) {
    case "GET_STATE":
      await processDueWork({ maxSends: 1 });
      return { state: await publicSnapshot() };
    case "CAPTURE_PAGE": {
      const body = composePageCapture(message.page_url, message.user_note ?? "");
      const record = await persistCapture(body);
      await processDueWork({ specificId: record.local_id, maxSends: 1 });
      await recomputeAlarm();
      const state = await readState();
      const remaining = state.queue.find((entry) => entry.local_id === record.local_id);
      return {
        local_id: record.local_id,
        capture_status: remaining?.state ?? "ACCEPTED",
        state: publicState(state),
      };
    }
    case "CAPTURE_SELECTION": {
      const body = composeSelectionCapture(message.selection_text, message.page_url);
      const record = await persistCapture(body);
      await processDueWork({ specificId: record.local_id, maxSends: 1 });
      await recomputeAlarm();
      const state = await readState();
      const remaining = state.queue.find((entry) => entry.local_id === record.local_id);
      return {
        local_id: record.local_id,
        capture_status: remaining?.state ?? "ACCEPTED",
        state: publicState(state),
      };
    }
    case "SAVE_CONFIG":
      return await saveConfiguration(message);
    case "TEST_CONNECTION":
      return await runConnectionTest();
    case "DISCONNECT":
      return await disconnect();
    case "DISCARD_CAPTURE":
      return await discardLocalCapture(message.local_id);
    case "REFRESH_STATUS":
      await processDueWork({ refreshStatuses: true, explicitStatuses: true, maxSends: 0 });
      return { state: await publicSnapshot() };
    default:
      return { ignored: true };
  }
}

function registerContextMenus() {
  // Детерминированные id позволяют безопасно пересоздать оба пункта после обновления расширения.
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: pageMenuId,
      title: "Сохранить страницу в AIInbox",
      contexts: ["page"],
      documentUrlPatterns: ["http://*/*", "https://*/*"],
    });
    chrome.contextMenus.create({
      id: selectionMenuId,
      title: "Сохранить выделение в AIInbox",
      contexts: ["selection"],
      documentUrlPatterns: ["http://*/*", "https://*/*"],
    });
  });
}

// Register every event synchronously because MV3 may start this file for one event only.
chrome.runtime.onInstalled.addListener(() => {
  registerContextMenus();
  void initializeWorker().then(() => processDueWork()).catch((error) => safeLog("initialize-failed", { status: safeCode(error) }));
});

chrome.runtime.onStartup.addListener(() => {
  void initializeWorker().then(() => processDueWork()).catch((error) => safeLog("startup-failed", { status: safeCode(error) }));
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name !== queueAlarmName) return;
  void initializeWorker().then(() => processDueWork({ refreshStatuses: true })).catch((error) => safeLog("alarm-failed", { status: safeCode(error) }));
});

chrome.contextMenus.onClicked.addListener((info) => {
  if (info.menuItemId !== pageMenuId && info.menuItemId !== selectionMenuId) return;
  void initializeWorker().then(async () => {
    const body = info.menuItemId === selectionMenuId
      ? composeSelectionCapture(info.selectionText, info.pageUrl)
      : composePageCapture(info.pageUrl);
    const record = await persistCapture(body);
    await processDueWork({ specificId: record.local_id, maxSends: 1 });
    await recomputeAlarm();
  }).catch(async (error) => {
    try {
      await storeLocalNotice(error);
    } catch {
      safeLog("context-notice-save-failed");
    }
    safeLog("context-capture-failed", { status: safeCode(error) });
  });
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (sender.id !== chrome.runtime.id) return false;
  if (!new Set([
    "GET_STATE",
    "CAPTURE_PAGE",
    "CAPTURE_SELECTION",
    "SAVE_CONFIG",
    "TEST_CONNECTION",
    "DISCONNECT",
    "DISCARD_CAPTURE",
    "REFRESH_STATUS",
  ]).has(message?.type)) return false;

  void handleMessage(message).then(
    (response) => sendResponse({ ok: true, ...response }),
    (error) => sendResponse({ ok: false, code: safeCode(error) }),
  );
  return true;
});

// Recreate the alarm on every module start; startup and alarm events own bounded work batches.
void initializeWorker().catch((error) => safeLog("worker-start-failed", { status: safeCode(error) }));
