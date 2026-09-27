import assert from "node:assert/strict";
import test from "node:test";

import { createCaptureRecord } from "../src/capture.js";
import {
  acceptCapture,
  appendCapture,
  loadStoredState,
  publicState,
  retainRecentItems,
  storeQueue,
} from "../src/storage.js";

function fakeStorage() {
  const values = new Map();
  return {
    async get(keys) {
      return Object.fromEntries(keys.filter((key) => values.has(key)).map((key) => [key, structuredClone(values.get(key))]));
    },
    async set(record) {
      for (const [key, value] of Object.entries(record)) values.set(key, structuredClone(value));
    },
    async remove(keys) {
      for (const key of keys) values.delete(key);
    },
  };
}

test("persisted local captures survive a new worker-like state read", async () => {
  const storage = fakeStorage();
  const record = createCaptureRecord({ text: "https://example.com", user_note: null }, { id: "stable-id" });
  await storeQueue(storage, appendCapture([], record));

  const afterRestart = await loadStoredState(storage);
  assert.equal(afterRestart.queue.length, 1);
  assert.equal(afterRestart.queue[0].idempotency_key, "stable-id");
  assert.equal(afterRestart.queue[0].text, "https://example.com");
});

test("queue bounds reject without evicting older captures", () => {
  const record = (id, text = "x") => createCaptureRecord({ text, user_note: null }, { id });
  const hundred = Array.from({ length: 100 }, (_, index) => record(`id-${index}`));
  assert.throws(() => appendCapture(hundred, record("new")), { code: "LOCAL_QUEUE_FULL" });
  const fullPayload = Array.from({ length: 50 }, (_, index) => record(`long-${index}`, "x".repeat(20_000)));
  assert.throws(() => appendCapture(fullPayload, record("extra")), { code: "LOCAL_QUEUE_FULL" });
  assert.equal(hundred.length, 100);
  assert.equal(hundred[0].local_id, "id-0");
});

test("202 removes private capture payload and retains a bounded status projection", () => {
  const record = createCaptureRecord({ text: "private selection", user_note: "private note" }, { id: "local" });
  const result = acceptCapture([record], [], "local", {
    id: 42,
    processing_status: "QUEUED",
    state: "ACTIVE",
  }, 1_800_000_000_000);
  assert.deepEqual(result.queue, []);
  assert.equal(result.recent[0].item_id, 42);
  assert.equal(result.recent[0].api_origin, null);
  assert.equal("text" in result.recent[0], false);
  assert.equal("user_note" in result.recent[0], false);
  assert.equal("idempotency_key" in result.recent[0], false);
});

test("public state never exposes the bearer token or capture body", () => {
  const record = createCaptureRecord({ text: "secret page selection", user_note: "secret note" }, { id: "local" });
  const projection = publicState({
    queue: [record],
    config: { origin: "https://example.com" },
    token: "bearer-secret-token-value",
    connection: "CONNECTED",
    recent: [],
  });
  const serialized = JSON.stringify(projection);
  assert.equal(serialized.includes("bearer-secret"), false);
  assert.equal(serialized.includes("secret page selection"), false);
  assert.equal(serialized.includes("secret note"), false);
  assert.equal(projection.pending_count, 1);
});

test("recent status retention expires old entries and removes terminal rows first at the 20-item bound", () => {
  const now = Date.parse("2026-01-02T00:00:00.000Z");
  const rows = Array.from({ length: 21 }, (_, index) => ({
    item_id: index + 1,
    accepted_at: new Date(now - (21 - index) * 60_000).toISOString(),
    processing_status: index === 0 ? "READY" : "PROCESSING",
  }));
  rows.push({
    item_id: 99,
    accepted_at: new Date(now - 25 * 60 * 60 * 1_000).toISOString(),
    processing_status: "READY",
  });

  const retained = retainRecentItems(rows, now);
  assert.equal(retained.length, 20);
  assert.equal(retained.some((row) => row.item_id === 1), false);
  assert.equal(retained.some((row) => row.item_id === 99), false);
});
