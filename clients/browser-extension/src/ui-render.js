// UI text crosses from web pages and HTTP errors into extension pages; render it as text only.
/** Assign dynamic display values through textContent so markup remains inert. */
export function setPlainText(element, value) {
  element.textContent = value == null ? "" : String(value);
}
