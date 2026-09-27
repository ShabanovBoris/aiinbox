import assert from "node:assert/strict";
import test from "node:test";

import { setPlainText } from "../src/ui-render.js";

test("page titles and API errors render as inert text", () => {
  const element = { textContent: "", innerHTML: "unchanged" };
  const malicious = '<img src=x onerror=alert(1)>';
  setPlainText(element, malicious);
  assert.equal(element.textContent, malicious);
  assert.equal(element.innerHTML, "unchanged");
});
