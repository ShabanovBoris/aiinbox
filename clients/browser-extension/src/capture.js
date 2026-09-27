// Capture composition stays in the extension boundary; AIInbox owns extraction and analysis.
import {
  MAX_CAPTURE_TEXT_CHARS,
  MAX_HTTP_BODY_BYTES,
  MAX_USER_NOTE_CHARS,
} from "./limits.js";
import { normalizeCapturePageUrl } from "./url-policy.js";

function captureError(code, message) {
  // Код отделяет локальную валидацию от сетевого отказа и не содержит введённых данных.
  const error = new Error(message);
  error.code = code;
  return error;
}

/** Count Unicode code points like the PM-18 Pydantic character limits do. */
export function characterCount(value) {
  return Array.from(value).length;
}

/** Enforce the server's exact text, note, and request-body bounds before enqueueing. */
export function validateCaptureBody(body) {
  if (!body || typeof body.text !== "string" || !body.text.trim()) {
    throw captureError("EMPTY_CAPTURE", "Нет данных для сохранения.");
  }
  if (characterCount(body.text) > MAX_CAPTURE_TEXT_CHARS) {
    throw captureError("CAPTURE_TEXT_TOO_LARGE", "Выделение слишком большое. Сохраните вместо него ссылку на страницу.");
  }
  if (body.user_note !== null && typeof body.user_note !== "string") {
    throw captureError("INVALID_NOTE", "Заметка должна быть текстом.");
  }
  if (body.user_note !== null && characterCount(body.user_note) > MAX_USER_NOTE_CHARS) {
    throw captureError("NOTE_TOO_LARGE", "Заметка должна содержать не более 2 000 символов.");
  }
  const bodyBytes = new TextEncoder().encode(JSON.stringify(body)).byteLength;
  if (bodyBytes > MAX_HTTP_BODY_BYTES) {
    throw captureError("REQUEST_TOO_LARGE", "Запрос превышает допустимый размер AIInbox.");
  }
  return body;
}

/** Create a page-URL capture; the browser title is deliberately not part of the body. */
export function composePageCapture(pageUrl, userNote = "") {
  const text = normalizeCapturePageUrl(pageUrl);
  const note = userNote === "" ? null : userNote;
  return validateCaptureBody({ text, user_note: note });
}

/** Keep popup Save disabled until the same validation used by durable capture succeeds. */
export function isPageCaptureAllowed(pageUrl, userNote = "") {
  try {
    composePageCapture(pageUrl, userNote);
    return true;
  } catch {
    return false;
  }
}

/** Preserve selected text as source material and fall back to the page URL if blank. */
export function composeSelectionCapture(selectionText, pageUrl) {
  const url = normalizeCapturePageUrl(pageUrl);
  if (typeof selectionText !== "string" || !selectionText.trim()) {
    return validateCaptureBody({ text: url, user_note: null });
  }
  return validateCaptureBody({ text: `${selectionText}\n\n${url}`, user_note: null });
}

/** Create one durable identity for the exact request body and its API destination. */
export function createCaptureRecord(body, { apiOrigin = null, now = new Date(), id = crypto.randomUUID() } = {}) {
  validateCaptureBody(body);
  return {
    local_id: id,
    idempotency_key: id,
    text: body.text,
    user_note: body.user_note,
    api_origin: apiOrigin,
    created_at: now.toISOString(),
    attempts: 0,
    next_attempt_at: null,
    state: "PENDING",
    last_error_code: null,
  };
}
