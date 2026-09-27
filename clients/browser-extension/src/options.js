// Options is the only place that requests a server host grant and submits credentials to the worker.
import { setPlainText } from "./ui-render.js";
import { apiHostPermission, normalizeApiOrigin } from "./url-policy.js";

const elements = {
  apiOrigin: document.getElementById("api-origin"),
  token: document.getElementById("api-token"),
  tokenState: document.getElementById("token-state"),
  save: document.getElementById("save-config"),
  test: document.getElementById("test-connection"),
  disconnect: document.getElementById("disconnect"),
  message: document.getElementById("connection-message"),
  pending: document.getElementById("pending-count"),
  otherOriginCount: document.getElementById("other-origin-count"),
  captureList: document.getElementById("capture-list"),
};

let currentState = null;

function setMessage(message, kind = "") {
  // Все сообщения проходят через textContent и не могут превратить ответ API в разметку.
  setPlainText(elements.message, message);
  elements.message.className = `message${kind ? ` ${kind}` : ""}`;
}

function connectionLabel(status) {
  // UI использует короткие локальные подписи, а не сырые HTTP-тела или исключения.
  const labels = {
    CONNECTED: "Соединение установлено.",
    AUTH_FAILED: "Ошибка авторизации. Обновите токен и повторите проверку.",
    AUTH_REQUIRED: "Требуется авторизация. Обновите токен и повторите проверку.",
    SERVER_UNREACHABLE: "Сервер недоступен. Проверьте адрес и сеть.",
    SERVER_UNHEALTHY: "Проверка состояния сервера завершилась ошибкой.",
    PERMISSION_REQUIRED: "Разрешите доступ к настроенному серверу.",
    UNTESTED: "Настройки сохранены. Проверьте соединение.",
    NOT_CONFIGURED: "Не настроено.",
    UNCONFIGURED: "Не настроено.",
  };
  return labels[status] ?? "Статус соединения недоступен.";
}

function addDiscardRow(list, { local_id, created_at, last_error_code, api_origin, reason }) {
  // В списке видны только идентификатор ошибки и origin, чтобы не показывать сохранённый текст.
  const row = document.createElement("li");
  const detail = document.createElement("span");
  setPlainText(detail, `${reason}: ${last_error_code ?? "destination changed"} · ${created_at ?? ""} · ${api_origin ?? ""}`);
  const discard = document.createElement("button");
  discard.type = "button";
  discard.className = "danger";
  setPlainText(discard, "Удалить");
  discard.addEventListener("click", async () => {
    if (!window.confirm("Удалить эту локальную запись? Она не будет отправлена в AIInbox.")) return;
    const response = await chrome.runtime.sendMessage({ type: "DISCARD_CAPTURE", local_id });
    if (!response?.ok) {
      setMessage("Эта запись ещё активна и пока не может быть удалена.", "error");
      return;
    }
    await refreshState();
    setMessage("Локальная запись удалена.", "success");
  });
  row.append(detail, discard);
  list.append(row);
}

function renderState(state) {
  // Options получает только публичную проекцию и никогда не подставляет токен в поле.
  currentState = state;
  elements.apiOrigin.value = state.api_origin ?? "";
  setPlainText(elements.tokenState, state.token_configured
    ? "Токен настроен. Оставьте поле пустым, чтобы сохранить его для текущего адреса."
    : "Токен не настроен. Введите Bearer-токен PM-18.");
  setPlainText(elements.pending, state.pending_count);
  setPlainText(elements.otherOriginCount, state.other_origin_captures.length);
  elements.captureList.replaceChildren();
  for (const capture of state.terminal_captures) {
    addDiscardRow(elements.captureList, { ...capture, reason: "Постоянная ошибка" });
  }
  for (const capture of state.other_origin_captures) {
    if (state.terminal_captures.some((terminal) => terminal.local_id === capture.local_id)) continue;
    addDiscardRow(elements.captureList, { ...capture, reason: "Другой API origin" });
  }
  setMessage(connectionLabel(state.connection_status));
}

async function refreshState() {
  // Состояние читается через worker, который остаётся единственным владельцем storage.
  const response = await chrome.runtime.sendMessage({ type: "GET_STATE" });
  if (response?.ok) renderState(response.state);
}

function setBusy(busy) {
  // Одновременная отправка нескольких конфигураций могла бы переставить origin и token местами.
  elements.save.disabled = busy;
  elements.test.disabled = busy;
  elements.disconnect.disabled = busy;
}

/** Request permission synchronously from this button gesture before saving configuration. */
async function prepareConfiguration(testAfterSave) {
  // Разрешение запрашивается синхронно в обработчике кнопки до фиксации новой конфигурации.
  let origin;
  try {
    origin = normalizeApiOrigin(elements.apiOrigin.value);
  } catch (error) {
    const messages = {
      INVALID_API_URL: "Введите origin API без пути, логина, пароля, query или fragment.",
      INSECURE_API_URL: "Для удалённого API используйте HTTPS. HTTP разрешён только для localhost и 127.0.0.1.",
    };
    setMessage(messages[error.code] ?? "Проверьте адрес API.", "error");
    return;
  }

  const token = elements.token.value;
  if (!token.trim() && (!currentState?.token_configured || currentState.api_origin !== origin)) {
    setMessage("Введите токен этого сервера до запроса доступа.", "error");
    return;
  }

  setBusy(true);
  setMessage("Запрашиваю доступ к выбранному серверу AIInbox…");
  try {
    const permission = apiHostPermission(origin);
    const permissionRequest = chrome.permissions.request({ origins: [permission] });
    const granted = await permissionRequest;
    if (!granted) {
      setMessage("Доступ к серверу не предоставлен; настройки не изменены.", "error");
      return;
    }

    const saved = await chrome.runtime.sendMessage({
      type: "SAVE_CONFIG",
      api_origin: origin,
      token,
    });
    if (!saved?.ok) {
      setMessage(saved?.code === "TOKEN_REQUIRED"
        ? "Введите токен этого сервера AIInbox."
        : "Не удалось сохранить настройки. Проверьте URL API и токен.", "error");
      return;
    }
    elements.token.value = "";
    await refreshState();
    if (!testAfterSave) {
      setMessage("Настройки сохранены. Проверьте соединение, чтобы проверить токен.", "success");
      return;
    }

    setMessage("Проверяю состояние сервера и токен…");
    const tested = await chrome.runtime.sendMessage({ type: "TEST_CONNECTION" });
    if (!tested?.ok) {
      setMessage("Не удалось выполнить проверку соединения.", "error");
      return;
    }
    setMessage(connectionLabel(tested.status), tested.status === "CONNECTED" ? "success" : "error");
    await refreshState();
  } catch {
    setMessage("Не удалось сохранить настройки или проверить соединение.", "error");
  } finally {
    setBusy(false);
  }
}

elements.save.addEventListener("click", () => void prepareConfiguration(false));
elements.test.addEventListener("click", () => void prepareConfiguration(true));
elements.disconnect.addEventListener("click", async () => {
  setBusy(true);
  try {
    const response = await chrome.runtime.sendMessage({ type: "DISCONNECT" });
    if (response?.ok) {
      elements.apiOrigin.value = "";
      elements.token.value = "";
      await refreshState();
      setMessage(`Учётные данные удалены. Записей в очереди сохранено: ${response.pending_count}.`, "success");
    } else {
      setMessage("Не удалось удалить учётные данные.", "error");
    }
  } catch {
    setMessage("Не удалось удалить учётные данные.", "error");
  } finally {
    setBusy(false);
  }
});

void refreshState().catch(() => setMessage("Не удалось прочитать настройки расширения.", "error"));
