// API origins and captured page URLs cross trust boundaries, so validate them here.
function policyError(code, message) {
  // Сохранение только кода позволяет UI показать безопасную локализованную ошибку.
  const error = new Error(message);
  error.code = code;
  return error;
}

/** Normalize an API base URL while rejecting paths and credential-bearing URLs. */
export function normalizeApiOrigin(value) {
  if (typeof value !== "string" || !value.trim()) {
    throw policyError("INVALID_API_URL", "Введите origin API AIInbox.");
  }

  const raw = value.trim();
  let url;
  try {
    url = new URL(raw);
  } catch {
    throw policyError("INVALID_API_URL", "Введите корректный origin API AIInbox.");
  }

  if (url.username || url.password) {
    throw policyError("INVALID_API_URL", "URL API не должен содержать логин или пароль.");
  }
  if (url.search || url.hash || raw.includes("?") || raw.includes("#")) {
    throw policyError("INVALID_API_URL", "URL API не должен содержать query или fragment.");
  }
  if (url.pathname !== "/") {
    throw policyError("INVALID_API_URL", "Укажите origin API без пути приложения.");
  }
  if (url.protocol !== "https:" && url.protocol !== "http:") {
    throw policyError("INVALID_API_URL", "URL API должен использовать HTTPS или HTTP на loopback.");
  }
  if (
    url.protocol === "http:" &&
    url.hostname !== "localhost" &&
    url.hostname !== "127.0.0.1"
  ) {
    throw policyError("INSECURE_API_URL", "HTTP разрешён только для localhost или 127.0.0.1.");
  }
  return url.origin;
}

/** Build the narrow host permission requested for one configured API origin. */
export function apiHostPermission(value) {
  const origin = normalizeApiOrigin(value);
  const url = new URL(origin);
  const port = url.port || (url.protocol === "https:" ? "443" : "80");
  return `${url.protocol}//${url.hostname}:${port}/*`;
}

/** Validate the only page URLs PM-33 sends to the canonical capture endpoint. */
export function normalizeCapturePageUrl(value) {
  if (typeof value !== "string" || !value.trim()) {
    throw policyError("UNSUPPORTED_PAGE_URL", "Эту страницу нельзя сохранить в AIInbox.");
  }

  let url;
  try {
    url = new URL(value);
  } catch {
    throw policyError("UNSUPPORTED_PAGE_URL", "Эту страницу нельзя сохранить в AIInbox.");
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw policyError("UNSUPPORTED_PAGE_URL", "Можно сохранять только страницы HTTP(S).");
  }
  return url.href;
}

/** Resolve fixed PM-18 routes and reject any URL construction that changes origin. */
export function buildApiUrl(apiOrigin, path) {
  const origin = normalizeApiOrigin(apiOrigin);
  if (typeof path !== "string" || !path.startsWith("/")) {
    throw policyError("INVALID_API_PATH", "Некорректный маршрут API AIInbox.");
  }
  const url = new URL(path, `${origin}/`);
  if (url.origin !== origin) {
    throw policyError("INVALID_API_PATH", "Маршрут API изменил origin.");
  }
  const isHealthCheck = url.pathname === "/healthz" && !url.search && !url.hash;
  const isItemList = url.pathname === "/v1/items" && ["", "?limit=1"].includes(url.search) && !url.hash;
  const isItemDetail = /^\/v1\/items\/[1-9]\d*$/.test(url.pathname) && !url.search && !url.hash;
  if (!isHealthCheck && !isItemList && !isItemDetail) {
    throw policyError("INVALID_API_PATH", "Маршрут API не поддерживается.");
  }
  return url.href;
}
