// The popup previews browser metadata locally and asks the worker to persist/send the capture.
import { characterCount, isPageCaptureAllowed } from "./capture.js";
import { MAX_USER_NOTE_CHARS } from "./limits.js";
import { setPlainText } from "./ui-render.js";

const elements = {
  pageTitle: document.getElementById("page-title"),
  pageUrl: document.getElementById("page-url"),
  note: document.getElementById("user-note"),
  noteCount: document.getElementById("note-count"),
  save: document.getElementById("save-page"),
  message: document.getElementById("capture-message"),
  connection: document.getElementById("connection-status"),
  pending: document.getElementById("pending-count"),
  latest: document.getElementById("latest-status"),
  options: document.getElementById("open-options"),
};

let currentPageUrl = "";
let currentPageTitle = "";

function connectionLabel(status) {
  // Popup показывает известные состояния, не отображая тело ошибок или конфигурационные секреты.
  const labels = {
    CONNECTED: "Подключено",
    AUTH_FAILED: "Проверьте токен в настройках",
    AUTH_REQUIRED: "Проверьте токен в настройках",
    SERVER_UNREACHABLE: "Сервер недоступен",
    SERVER_UNHEALTHY: "Сервер не прошёл проверку",
    PERMISSION_REQUIRED: "Разрешите доступ к серверу в настройках",
    UNTESTED: "Настройки сохранены, но не проверены",
    NOT_CONFIGURED: "Не настроено",
    UNCONFIGURED: "Не настроено",
  };
  return labels[status] ?? "Статус соединения недоступен";
}

function setMessage(message, kind = "") {
  // Текст динамических состояний остаётся инертным даже при ошибке сервера или странице.
  setPlainText(elements.message, message);
  elements.message.className = `message${kind ? ` ${kind}` : ""}`;
}

function renderState(state) {
  // Это только UI-проекция; локальная очередь и канонические Item здесь не изменяются.
  setPlainText(elements.connection, connectionLabel(state.connection_status));
  setPlainText(elements.pending, state.pending_count);
  if (state.latest_item) {
    setPlainText(elements.latest, `#${state.latest_item.item_id} · ${state.latest_item.processing_status}`);
  } else if (state.terminal_captures?.length) {
    setPlainText(elements.latest, `Capture failed · ${state.terminal_captures[0].last_error_code}`);
  } else {
    setPlainText(elements.latest, "Пока нет сохранённых Item");
  }
  if (state.last_notice) {
    const noticeMessages = {
      CAPTURE_TEXT_TOO_LARGE: "Выделение слишком большое. Сохраните вместо него ссылку на страницу.",
      REQUEST_TOO_LARGE: "Запрос превышает допустимый размер AIInbox.",
      LOCAL_QUEUE_FULL: "Локальная очередь заполнена. Восстановите соединение или удалите ошибки в настройках.",
      UNSUPPORTED_PAGE_URL: "Эту страницу нельзя сохранить. Откройте обычную страницу HTTP(S).",
      CAPTURE_FAILED: "Не удалось сохранить запись из контекстного меню.",
    };
    setMessage(noticeMessages[state.last_notice.code] ?? "Последнее действие не выполнено.", "error");
  }
}

function updateCaptureControls() {
  // Кнопка отключается по тем же ограничениям, которые worker проверит до записи в очередь.
  const noteLength = characterCount(elements.note.value);
  setPlainText(elements.noteCount, `${noteLength} / ${MAX_USER_NOTE_CHARS}`);
  elements.save.disabled = !isPageCaptureAllowed(currentPageUrl, elements.note.value);
  if (noteLength > MAX_USER_NOTE_CHARS) {
    setMessage("Заметка должна содержать не более 2 000 символов.", "error");
  }
}

async function loadPagePreview() {
  // activeTab даёт временный доступ после клика; заголовок остаётся только локальным превью.
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  currentPageUrl = tab?.url ?? "";
  currentPageTitle = tab?.title ?? "";
  setPlainText(elements.pageTitle, currentPageTitle || "Страница без заголовка");
  setPlainText(elements.pageUrl, currentPageUrl);
  updateCaptureControls();
  if (!isPageCaptureAllowed(currentPageUrl)) {
    setMessage("Можно сохранять только обычные страницы HTTP(S).", "error");
  }
}

async function loadState() {
  // Статусы читаются у worker; открытие popup просит разовый refresh без постоянного polling.
  const response = await chrome.runtime.sendMessage({ type: "GET_STATE" });
  if (response?.ok) {
    renderState(response.state);
    void chrome.runtime.sendMessage({ type: "REFRESH_STATUS" }).then((refreshed) => {
      if (refreshed?.ok) renderState(refreshed.state);
    });
  }
}

elements.note.addEventListener("input", updateCaptureControls);
elements.options.addEventListener("click", () => chrome.runtime.openOptionsPage());
elements.save.addEventListener("click", async () => {
  elements.save.disabled = true;
  setMessage("Сначала сохраняю запрос локально…");
  try {
    const response = await chrome.runtime.sendMessage({
      type: "CAPTURE_PAGE",
      page_url: currentPageUrl,
      user_note: elements.note.value,
    });
    if (!response?.ok) {
      const messages = {
        LOCAL_QUEUE_FULL: "Локальная очередь заполнена. Восстановите соединение или удалите ошибки в настройках.",
        CAPTURE_TEXT_TOO_LARGE: "Выделение слишком большое. Сохраните вместо него ссылку на страницу.",
        NOTE_TOO_LARGE: "Заметка должна содержать не более 2 000 символов.",
      };
      setMessage(messages[response?.code] ?? "Не удалось сохранить страницу. Проверьте URL и настройки.", "error");
      return;
    }
    renderState(response.state);
    setMessage(response.capture_status === "ACCEPTED"
      ? "AIInbox принял запрос. Статус обработки показан ниже."
      : response.capture_status === "TERMINAL_ERROR"
        ? "Запись сохранена локально с постоянной ошибкой. Подробности — в настройках."
        : "Сохранено локально — ожидаю AIInbox.", response.capture_status === "TERMINAL_ERROR" ? "error" : "success");
    elements.note.value = "";
    updateCaptureControls();
  } catch {
    setMessage("Не удалось сохранить страницу: service worker расширения недоступен.", "error");
  } finally {
    updateCaptureControls();
  }
});

void Promise.all([loadPagePreview(), loadState()]).catch(() => {
  setMessage("Не удалось прочитать вкладку. Откройте обычную страницу HTTP(S).", "error");
});
