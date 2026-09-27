import assert from "node:assert/strict";
import test from "node:test";

import {
  characterCount,
  composePageCapture,
  composeSelectionCapture,
  createCaptureRecord,
  isPageCaptureAllowed,
  validateCaptureBody,
} from "../src/capture.js";

test("page capture sends the URL and keeps the explicit note separate", () => {
  assert.deepEqual(composePageCapture("https://example.com/story", "Read for Android"), {
    text: "https://example.com/story",
    user_note: "Read for Android",
  });
  assert.deepEqual(composePageCapture("https://example.com/story"), {
    text: "https://example.com/story",
    user_note: null,
  });
});

test("selection capture includes provenance and blank selection falls back to URL", () => {
  assert.deepEqual(composeSelectionCapture("Useful paragraph", "https://example.com/story"), {
    text: "Useful paragraph\n\nhttps://example.com/story",
    user_note: null,
  });
  assert.deepEqual(composeSelectionCapture(" \n ", "https://example.com/story"), {
    text: "https://example.com/story",
    user_note: null,
  });
});

test("capture rejects oversized text, oversized notes, and UTF-8 bodies over PM-18 limit", () => {
  assert.equal(characterCount("😀😀"), 2);
  assert.throws(() => composeSelectionCapture("x".repeat(20_000), "https://e.test"), {
    code: "CAPTURE_TEXT_TOO_LARGE",
  });
  assert.throws(() => composePageCapture("https://e.test", "x".repeat(2_001)), {
    code: "NOTE_TOO_LARGE",
  });
  assert.throws(() => validateCaptureBody({ text: "😀".repeat(17_000), user_note: null }), {
    code: "REQUEST_TOO_LARGE",
  });
});

test("popup validation disables unsupported pages and oversized notes", () => {
  assert.equal(isPageCaptureAllowed("chrome://settings"), false);
  assert.equal(isPageCaptureAllowed("https://example.com", "x".repeat(2_001)), false);
  assert.equal(isPageCaptureAllowed("https://example.com", "keep this"), true);
});

test("one capture record gets one stable local and server request identity", () => {
  const record = createCaptureRecord(
    { text: "https://example.com", user_note: null },
    { id: "one-uuid", now: new Date("2026-01-01T00:00:00.000Z"), apiOrigin: "https://example.com" },
  );
  assert.equal(record.local_id, "one-uuid");
  assert.equal(record.idempotency_key, "one-uuid");
  assert.equal(record.api_origin, "https://example.com");
  assert.equal(record.attempts, 0);
});
