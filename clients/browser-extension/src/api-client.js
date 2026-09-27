// Only the trusted service worker imports this module, keeping Bearer transport off page code.
import { API_TIMEOUT_MS } from "./limits.js";
import { buildApiUrl, normalizeApiOrigin } from "./url-policy.js";

function apiError(code) {
  // На границе fetch наружу выходит тип ошибки, но не текст ответа или параметры запроса.
  const error = new Error(code);
  error.code = code;
  return error;
}

/** Perform one bounded same-origin request with explicit credentials and redirect policy. */
export async function apiFetch({
  apiOrigin,
  path,
  token = null,
  method = "GET",
  body,
  idempotencyKey = null,
  fetchImpl = fetch,
  timeoutMs = API_TIMEOUT_MS,
}) {
  const origin = normalizeApiOrigin(apiOrigin);
  const url = buildApiUrl(origin, path);
  if (new URL(url).origin !== origin) throw apiError("ORIGIN_MISMATCH");

  const headers = { Accept: "application/json" };
  if (token !== null) headers.Authorization = `Bearer ${token}`;
  if (idempotencyKey !== null) headers["Idempotency-Key"] = idempotencyKey;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const controller = new AbortController();
  let timedOut = false;
  const timeout = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  try {
    return await fetchImpl(url, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "omit",
      redirect: "error",
      cache: "no-store",
      signal: controller.signal,
    });
  } catch (error) {
    throw apiError(timedOut || error?.name === "AbortError" ? "TIMEOUT" : "NETWORK_ERROR");
  } finally {
    clearTimeout(timeout);
  }
}

/** Send the stored request unchanged so every retry keeps its server identity. */
export async function postCapture({ apiOrigin, token, record, fetchImpl = fetch, timeoutMs }) {
  const response = await apiFetch({
    apiOrigin,
    path: "/v1/items",
    token,
    method: "POST",
    body: { text: record.text, user_note: record.user_note },
    idempotencyKey: record.idempotency_key,
    fetchImpl,
    timeoutMs,
  });
  if (response.status !== 202) {
    const errorCode = response.status === 409
      ? "IDEMPOTENCY_CONFLICT"
      : response.status === 413
        ? "REQUEST_TOO_LARGE"
        : response.status === 422
          ? "VALIDATION_ERROR"
          : `HTTP_${response.status}`;
    return {
      kind: "HTTP_ERROR",
      status: response.status,
      error_code: errorCode,
      retry_after: response.headers.get("Retry-After"),
    };
  }

  let payload;
  try {
    payload = await response.json();
  } catch {
    throw apiError("INVALID_ACCEPTANCE_RESPONSE");
  }
  if (
    !payload ||
    !Number.isSafeInteger(payload.id) ||
    payload.id <= 0 ||
    typeof payload.processing_status !== "string" ||
    typeof payload.state !== "string"
  ) {
    throw apiError("INVALID_ACCEPTANCE_RESPONSE");
  }
  return {
    kind: "ACCEPTED",
    item: { id: payload.id, processing_status: payload.processing_status, state: payload.state },
  };
}

/** Poll the existing owner-scoped PM-18 detail route without returning raw response data. */
export async function getItemStatus({ apiOrigin, token, itemId, fetchImpl = fetch, timeoutMs }) {
  if (!Number.isSafeInteger(itemId) || itemId <= 0) throw apiError("INVALID_ITEM_ID");
  const response = await apiFetch({
    apiOrigin,
    path: `/v1/items/${itemId}`,
    token,
    fetchImpl,
    timeoutMs,
  });
  if (!response.ok) return { kind: "HTTP_ERROR", status: response.status };
  let payload;
  try {
    payload = await response.json();
  } catch {
    throw apiError("INVALID_STATUS_RESPONSE");
  }
  if (
    !payload ||
    payload.id !== itemId ||
    typeof payload.processing_status !== "string" ||
    typeof payload.state !== "string"
  ) {
    throw apiError("INVALID_STATUS_RESPONSE");
  }
  return {
    kind: "STATUS",
    item: { id: itemId, processing_status: payload.processing_status, state: payload.state },
  };
}

/** Check health first, then authentication on the bounded lightweight Item list route. */
export async function testApiConnection({ apiOrigin, token, fetchImpl = fetch, timeoutMs }) {
  try {
    const health = await apiFetch({ apiOrigin, path: "/healthz", fetchImpl, timeoutMs });
    if (!health.ok) return { status: "SERVER_UNHEALTHY" };
    const healthBody = await health.json();
    if (healthBody?.status !== "ok") return { status: "SERVER_UNHEALTHY" };
  } catch (error) {
    return { status: error.code === "NETWORK_ERROR" || error.code === "TIMEOUT" ? "SERVER_UNREACHABLE" : "SERVER_UNHEALTHY" };
  }

  try {
    const authenticated = await apiFetch({
      apiOrigin,
      path: "/v1/items?limit=1",
      token,
      fetchImpl,
      timeoutMs,
    });
    if (authenticated.status === 401) return { status: "AUTH_FAILED" };
    return authenticated.ok ? { status: "CONNECTED" } : { status: "SERVER_UNHEALTHY" };
  } catch (error) {
    return { status: error.code === "NETWORK_ERROR" || error.code === "TIMEOUT" ? "SERVER_UNREACHABLE" : "SERVER_UNHEALTHY" };
  }
}
