import assert from "node:assert/strict";
import test from "node:test";
import { createCaptureRecord } from "../src/capture.js";

let workerImportId = 0;

function event() {
  const listeners = [];
  return { listeners, addListener(listener) { listeners.push(listener); } };
}

function createHarness(fetchHandler) {
  const values = new Map();
  const runtimeOnMessage = event();
  const alarmsOnAlarm = event();
  const contextMenuOnClicked = event();
  const runtimeOnStartup = event();
  const createdAlarms = [];
  let accessLevel = null;
  let currentFetchHandler = fetchHandler;
  const removedPermissions = [];
  const revokedOrigins = new Set();
  const clone = (value) => structuredClone(value);
  const local = {
    async get(keys) {
      const list = Array.isArray(keys) ? keys : [keys];
      return Object.fromEntries(list.filter((key) => values.has(key)).map((key) => [key, clone(values.get(key))]));
    },
    async set(entries) {
      for (const [key, value] of Object.entries(entries)) values.set(key, clone(value));
    },
    async remove(keys) {
      for (const key of keys) values.delete(key);
    },
    async setAccessLevel({ accessLevel: value }) { accessLevel = value; },
  };
  const api = {
    runtime: { id: "extension-test-id", onInstalled: event(), onStartup: runtimeOnStartup, onMessage: runtimeOnMessage },
    storage: { local },
    alarms: {
      onAlarm: alarmsOnAlarm,
      async create(name, info) { createdAlarms.push({ name, info }); },
      async clear() { return true; },
    },
    contextMenus: {
      onClicked: contextMenuOnClicked,
      removeAll(callback) { callback(); },
      create() {},
    },
    permissions: {
      async contains(permission) {
        return (permission.origins ?? []).every((origin) => !revokedOrigins.has(origin));
      },
      async remove(permission) {
        removedPermissions.push(permission);
        for (const origin of permission.origins ?? []) revokedOrigins.add(origin);
        return true;
      },
      async request(permission) {
        for (const origin of permission.origins ?? []) revokedOrigins.delete(origin);
        return true;
      },
    },
    action: { async setBadgeBackgroundColor() {}, async setBadgeText() {} },
  };

  globalThis.chrome = api;
  globalThis.fetch = (...args) => currentFetchHandler(...args);

  return {
    api,
    local,
    setFetchHandler(handler) { currentFetchHandler = handler; },
    async importWorker() {
      workerImportId += 1;
      await import(new URL(`../src/service-worker.js?worker=${workerImportId}`, import.meta.url).href);
    },
    sendMessage(message) {
      return new Promise((resolve) => {
        const listener = runtimeOnMessage.listeners[0];
        const keptOpen = listener(message, { id: api.runtime.id }, resolve);
        assert.equal(keptOpen, true);
      });
    },
    async read(key) {
      const result = await local.get([key]);
      return result[key];
    },
    fireAlarm(name = "aibox-queue-v1") {
      for (const listener of alarmsOnAlarm.listeners) listener({ name });
    },
    get accessLevel() { return accessLevel; },
    grantPermission(origin) { revokedOrigins.delete(origin); },
    eventCounts: {
      message: runtimeOnMessage.listeners,
      alarm: alarmsOnAlarm.listeners,
      startup: runtimeOnStartup.listeners,
      menu: contextMenuOnClicked.listeners,
    },
    createdAlarms,
    removedPermissions,
  };
}

async function waitFor(predicate) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  assert.fail("Timed out waiting for the service worker event.");
}

async function waitForAsync(predicate) {
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (await predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  assert.fail("Timed out waiting for the service worker state.");
}

test("startup recomputes an alarm from the durable retry timestamp", async () => {
  let networkCalls = 0;
  const harness = createHarness(async () => {
    networkCalls += 1;
    throw new Error("A future retry must not run early.");
  });

  try {
    await harness.importWorker();
    const origin = "https://api.example";
    const dueAt = Date.now() + 60_000;
    const record = createCaptureRecord(
      { text: "https://example.com/retry", user_note: null },
      { id: "restart-stable-key", apiOrigin: origin },
    );
    await harness.local.set({
      api_config_v1: { origin },
      auth_token_v1: "s".repeat(32),
      connection_state_v1: "CONNECTED",
      capture_queue_v1: [{ ...record, attempts: 1, next_attempt_at: new Date(dueAt).toISOString() }],
    });
    for (const listener of harness.eventCounts.startup) listener();
    await waitFor(() => harness.createdAlarms.length > 0);
    assert.equal(harness.createdAlarms.at(-1).name, "aibox-queue-v1");
    assert.ok(harness.createdAlarms.at(-1).info.when >= dueAt);
    assert.equal(networkCalls, 0);
  } finally {
    delete globalThis.chrome;
    delete globalThis.fetch;
  }
});

test("worker persists before POST, removes private payload on 202, and stops at READY", async () => {
  const calls = [];
  let itemPoll = 0;
  const harness = createHarness(async (url, options) => {
    const parsed = new URL(url);
    calls.push({ url, options });
    if (options.method === "POST") {
      const stored = await harness.read("capture_queue_v1");
      assert.equal(stored.length, 1);
      assert.equal(stored[0].text, "https://example.com/article");
      assert.equal(stored[0].idempotency_key, options.headers["Idempotency-Key"]);
      assert.equal(options.headers.Authorization, `Bearer ${"s".repeat(32)}`);
      return new Response(JSON.stringify({ id: 17, processing_status: "QUEUED", state: "ACTIVE" }), { status: 202 });
    }
    if (parsed.pathname === "/v1/items/17") {
      itemPoll += 1;
      const status = itemPoll === 1 ? "PROCESSING" : "READY";
      return new Response(JSON.stringify({ id: 17, processing_status: status, state: "ACTIVE" }), { status: 200 });
    }
    throw new Error(`Unexpected test request: ${url}`);
  });

  try {
    await harness.importWorker();
    assert.equal(harness.accessLevel, "TRUSTED_CONTEXTS");
    assert.equal(harness.eventCounts.message.length, 1);
    assert.equal(harness.eventCounts.alarm.length, 1);
    assert.equal(harness.eventCounts.menu.length, 1);
    await harness.sendMessage({ type: "SAVE_CONFIG", api_origin: "https://api.example", token: "s".repeat(32) });

    const captured = await harness.sendMessage({ type: "CAPTURE_PAGE", page_url: "https://example.com/article" });
    assert.equal(captured.ok, true);
    assert.equal(captured.capture_status, "ACCEPTED");
    assert.equal(captured.state.pending_count, 0);
    assert.equal((await harness.read("capture_queue_v1")).length, 0);
    assert.equal((await harness.read("recent_items_v1"))[0].item_id, 17);
    assert.equal("text" in (await harness.read("recent_items_v1"))[0], false);

    await harness.sendMessage({ type: "REFRESH_STATUS" });
    assert.equal((await harness.read("recent_items_v1"))[0].processing_status, "PROCESSING");
    await harness.sendMessage({ type: "REFRESH_STATUS" });
    assert.equal((await harness.read("recent_items_v1"))[0].processing_status, "READY");
    await harness.sendMessage({ type: "REFRESH_STATUS" });
    assert.equal(calls.filter((call) => call.options.method === "POST").length, 1);
    assert.equal(itemPoll, 2);
  } finally {
    delete globalThis.chrome;
    delete globalThis.fetch;
  }
});

test("processing FAILED updates status and never resubmits the accepted capture", async () => {
  let posts = 0;
  let polls = 0;
  const harness = createHarness(async (url, options) => {
    if (options.method === "POST") {
      posts += 1;
      return new Response(JSON.stringify({ id: 71, processing_status: "QUEUED", state: "ACTIVE" }), { status: 202 });
    }
    if (new URL(url).pathname === "/v1/items/71") {
      polls += 1;
      return new Response(JSON.stringify({ id: 71, processing_status: "FAILED", state: "ACTIVE" }), { status: 200 });
    }
    throw new Error(`Unexpected test request: ${url}`);
  });

  try {
    await harness.importWorker();
    await harness.sendMessage({ type: "SAVE_CONFIG", api_origin: "https://api.example", token: "s".repeat(32) });
    await harness.sendMessage({ type: "CAPTURE_PAGE", page_url: "https://example.com/failed" });
    await harness.sendMessage({ type: "REFRESH_STATUS" });
    await harness.sendMessage({ type: "REFRESH_STATUS" });
    assert.equal((await harness.read("recent_items_v1"))[0].processing_status, "FAILED");
    assert.equal(posts, 1);
    assert.equal(polls, 1);
  } finally {
    delete globalThis.chrome;
    delete globalThis.fetch;
  }
});

test("alarm retry reuses the same request body and idempotency key", async () => {
  const posts = [];
  let sendCount = 0;
  const harness = createHarness(async (url, options) => {
    if (options.method !== "POST") throw new Error(`Unexpected test request: ${url}`);
    posts.push({ url, body: options.body, key: options.headers["Idempotency-Key"] });
    sendCount += 1;
    if (sendCount === 1) throw new TypeError("offline");
    return new Response(JSON.stringify({ id: 29, processing_status: "QUEUED", state: "ACTIVE" }), { status: 202 });
  });

  try {
    await harness.importWorker();
    await harness.sendMessage({ type: "SAVE_CONFIG", api_origin: "https://api.example", token: "s".repeat(32) });
    const captured = await harness.sendMessage({
      type: "CAPTURE_PAGE",
      page_url: "https://example.com/retry",
      user_note: "same body",
    });
    assert.equal(captured.capture_status, "PENDING");
    const firstQueue = await harness.read("capture_queue_v1");
    assert.equal(firstQueue[0].attempts, 1);

    await harness.local.set({
      capture_queue_v1: firstQueue.map((record) => ({ ...record, next_attempt_at: new Date(0).toISOString() })),
    });
    harness.fireAlarm();
    await waitFor(() => sendCount === 2);

    assert.equal(posts[1].key, posts[0].key);
    assert.equal(posts[1].body, posts[0].body);
    assert.equal((await harness.read("capture_queue_v1")).length, 0);
    assert.equal((await harness.read("recent_items_v1"))[0].item_id, 29);
  } finally {
    delete globalThis.chrome;
    delete globalThis.fetch;
  }
});

test("401 preserves the capture and blocks retries until a successful connection test", async () => {
  let postCount = 0;
  let authChecks = 0;
  const postKeys = [];
  const harness = createHarness(async (url, options) => {
    const parsed = new URL(url);
    if (options.method === "POST") {
      postCount += 1;
      postKeys.push(options.headers["Idempotency-Key"]);
      if (postCount === 1) return new Response(JSON.stringify({ error: { code: "UNAUTHORIZED" } }), { status: 401 });
      return new Response(JSON.stringify({ id: 31, processing_status: "QUEUED", state: "ACTIVE" }), { status: 202 });
    }
    if (parsed.pathname === "/healthz") return new Response(JSON.stringify({ status: "ok" }), { status: 200 });
    if (parsed.pathname === "/v1/items") {
      authChecks += 1;
      return new Response(JSON.stringify({ items: [], next_cursor: null }), { status: 200 });
    }
    throw new Error(`Unexpected test request: ${url}`);
  });

  try {
    await harness.importWorker();
    await harness.sendMessage({ type: "SAVE_CONFIG", api_origin: "https://api.example", token: "s".repeat(32) });
    const captured = await harness.sendMessage({ type: "CAPTURE_PAGE", page_url: "https://example.com/private" });
    assert.equal(captured.capture_status, "PENDING");
    const queueAfterAuthFailure = await harness.read("capture_queue_v1");
    assert.equal(queueAfterAuthFailure.length, 1);
    assert.equal(queueAfterAuthFailure[0].text, "https://example.com/private");
    assert.equal(queueAfterAuthFailure[0].attempts, 0);
    assert.equal(captured.state.connection_status, "AUTH_FAILED");

    harness.fireAlarm();
    await new Promise((resolve) => setTimeout(resolve, 20));
    assert.equal(postCount, 1);

    await harness.sendMessage({ type: "SAVE_CONFIG", api_origin: "https://api.example", token: "t".repeat(32) });
    assert.equal(postCount, 1);
    assert.equal((await harness.sendMessage({ type: "GET_STATE" })).state.connection_status, "AUTH_FAILED");

    const tested = await harness.sendMessage({ type: "TEST_CONNECTION" });
    assert.equal(tested.status, "CONNECTED");
    assert.equal(authChecks, 1);
    assert.equal(postCount, 2);
    assert.equal(postKeys[0], postKeys[1]);
    assert.equal((await harness.read("capture_queue_v1")).length, 0);
  } finally {
    delete globalThis.chrome;
    delete globalThis.fetch;
  }
});

test("context-menu selection is source text and oversized input creates only a bounded local notice", async () => {
  let networkCalls = 0;
  const harness = createHarness(async () => {
    networkCalls += 1;
    throw new Error("No API should be called before configuration.");
  });

  try {
    await harness.importWorker();
    const onClicked = harness.eventCounts.menu[0];
    onClicked({
      menuItemId: "aiinbox-save-selection",
      selectionText: "Selected source text",
      pageUrl: "https://example.com/source",
    });
    await waitForAsync(async () => (await harness.read("capture_queue_v1"))?.length === 1);
    const stored = await harness.read("capture_queue_v1");
    assert.equal(stored[0].text, "Selected source text\n\nhttps://example.com/source");
    assert.equal(stored[0].user_note, null);

    onClicked({
      menuItemId: "aiinbox-save-selection",
      selectionText: "x".repeat(20_000),
      pageUrl: "https://example.com/source",
    });
    await waitForAsync(async () => (await harness.read("ui_notice_v1"))?.code === "CAPTURE_TEXT_TOO_LARGE");
    assert.equal((await harness.read("capture_queue_v1")).length, 1);
    assert.equal(networkCalls, 0);
  } finally {
    delete globalThis.chrome;
    delete globalThis.fetch;
  }
});

test("409, 413, and 422 become terminal and are never automatically resubmitted", async () => {
  let statusIndex = 0;
  let posts = 0;
  const statuses = [409, 413, 422];
  const harness = createHarness(async (_url, options) => {
    if (options.method !== "POST") throw new Error("Unexpected test request.");
    posts += 1;
    const status = statuses[statusIndex++];
    return new Response(JSON.stringify({ error: { code: `HTTP_${status}` } }), { status });
  });

  try {
    await harness.importWorker();
    await harness.sendMessage({ type: "SAVE_CONFIG", api_origin: "https://api.example", token: "s".repeat(32) });
    for (const path of ["one", "two", "three"]) {
      const response = await harness.sendMessage({ type: "CAPTURE_PAGE", page_url: `https://example.com/${path}` });
      assert.equal(response.capture_status, "TERMINAL_ERROR");
    }
    await harness.sendMessage({ type: "GET_STATE" });
    assert.equal(posts, 3);
    assert.deepEqual((await harness.read("capture_queue_v1")).map((record) => record.state), [
      "TERMINAL_ERROR",
      "TERMINAL_ERROR",
      "TERMINAL_ERROR",
    ]);
  } finally {
    delete globalThis.chrome;
    delete globalThis.fetch;
  }
});

test("the eighth transient attempt is terminal and there is no ninth automatic POST", async () => {
  let posts = 0;
  const harness = createHarness(async () => {
    posts += 1;
    throw new TypeError("offline");
  });

  try {
    await harness.importWorker();
    await harness.sendMessage({ type: "SAVE_CONFIG", api_origin: "https://api.example", token: "s".repeat(32) });
    await harness.sendMessage({ type: "CAPTURE_PAGE", page_url: "https://example.com/eight" });
    const queue = await harness.read("capture_queue_v1");
    await harness.local.set({
      capture_queue_v1: queue.map((record) => ({
        ...record,
        attempts: 7,
        next_attempt_at: new Date(0).toISOString(),
      })),
    });

    const eighth = await harness.sendMessage({ type: "GET_STATE" });
    assert.equal(posts, 2);
    assert.equal(eighth.state.terminal_captures[0].last_error_code, "RETRY_EXHAUSTED");
    await harness.sendMessage({ type: "GET_STATE" });
    assert.equal(posts, 2);
  } finally {
    delete globalThis.chrome;
    delete globalThis.fetch;
  }
});

test("disconnect clears credentials but preserves unsent captures", async () => {
  const harness = createHarness(async () => { throw new TypeError("offline"); });

  try {
    await harness.importWorker();
    await harness.sendMessage({ type: "SAVE_CONFIG", api_origin: "https://api.example", token: "s".repeat(32) });
    await harness.sendMessage({ type: "CAPTURE_PAGE", page_url: "https://example.com/keep" });
    const before = await harness.read("capture_queue_v1");
    assert.equal(before.length, 1);

    const disconnected = await harness.sendMessage({ type: "DISCONNECT" });
    const state = await harness.read("api_config_v1");
    const token = await harness.read("auth_token_v1");
    assert.equal(disconnected.pending_count, 1);
    assert.equal(state, undefined);
    assert.equal(token, undefined);
    assert.equal((await harness.read("capture_queue_v1"))[0].text, "https://example.com/keep");
  } finally {
    delete globalThis.chrome;
    delete globalThis.fetch;
  }
});

test("changing API origin preserves old queued identity and removes the unused host grant", async () => {
  const calls = [];
  const keys = [];
  let apiARequests = 0;
  const harness = createHarness(async (url, options) => {
    const origin = new URL(url).origin;
    calls.push(origin);
    if (options.method !== "POST") throw new Error(`Unexpected test request: ${url}`);
    keys.push(options.headers["Idempotency-Key"]);
    if (origin === "https://api-a.example" && apiARequests++ === 0) throw new TypeError("offline");
    if (origin !== "https://api-a.example") throw new Error("Old capture was sent to a new API origin.");
    return new Response(JSON.stringify({ id: 81, processing_status: "QUEUED", state: "ACTIVE" }), { status: 202 });
  });

  try {
    await harness.importWorker();
    await harness.sendMessage({ type: "SAVE_CONFIG", api_origin: "https://api-a.example", token: "a".repeat(32) });
    await harness.sendMessage({ type: "CAPTURE_PAGE", page_url: "https://example.com/origin" });
    const queue = await harness.read("capture_queue_v1");
    const originalKey = queue[0].idempotency_key;

    await harness.sendMessage({ type: "SAVE_CONFIG", api_origin: "https://api-b.example", token: "b".repeat(32) });
    const switched = await harness.sendMessage({ type: "GET_STATE" });
    assert.equal(switched.state.other_origin_captures.length, 1);
    assert.equal((await harness.read("capture_queue_v1"))[0].api_origin, "https://api-a.example");
    assert.deepEqual(calls, ["https://api-a.example"]);
    assert.ok(harness.removedPermissions.some((permission) => permission.origins?.[0] === "https://api-a.example:443/*"));

    // A config change does not bypass the durable retry backoff.
    const waiting = await harness.read("capture_queue_v1");
    await harness.local.set({
      capture_queue_v1: waiting.map((record) => ({ ...record, next_attempt_at: new Date(0).toISOString() })),
    });
    harness.grantPermission("https://api-a.example:443/*");
    await harness.sendMessage({ type: "SAVE_CONFIG", api_origin: "https://api-a.example", token: "a".repeat(32) });
    assert.equal((await harness.read("capture_queue_v1")).length, 0);
    assert.equal(keys[0], originalKey);
    assert.equal(keys[1], originalKey);
    assert.ok(harness.removedPermissions.some((permission) => permission.origins?.[0] === "https://api-b.example:443/*"));
    assert.deepEqual(calls, ["https://api-a.example", "https://api-a.example"]);
  } finally {
    delete globalThis.chrome;
    delete globalThis.fetch;
  }
});

test("a late status failure from the previous API cannot reschedule a same-ID Item", async () => {
  let statusRequestStarted;
  const started = new Promise((resolve) => { statusRequestStarted = resolve; });
  let finishStatusRequest;
  const harness = createHarness(async () => {
    statusRequestStarted();
    return await new Promise((resolve) => { finishStatusRequest = resolve; });
  });

  try {
    await harness.importWorker();
    const checkedAt = new Date(Date.now() - 60_000).toISOString();
    const nextCheckAt = new Date(Date.now() - 1_000).toISOString();
    await harness.local.set({
      api_config_v1: { origin: "https://api-a.example" },
      auth_token_v1: "a".repeat(32),
      connection_state_v1: "CONNECTED",
      capture_queue_v1: [],
      recent_items_v1: [
        {
          item_id: 17,
          api_origin: "https://api-a.example",
          accepted_at: checkedAt,
          processing_status: "PROCESSING",
          state: "ACTIVE",
          last_checked_at: checkedAt,
          next_check_at: nextCheckAt,
          poll_until: new Date(Date.now() + 10 * 60_000).toISOString(),
        },
        {
          item_id: 17,
          api_origin: "https://api-b.example",
          accepted_at: checkedAt,
          processing_status: "PROCESSING",
          state: "ACTIVE",
          last_checked_at: null,
          next_check_at: "2030-01-01T00:00:00.000Z",
          poll_until: "2030-01-01T00:30:00.000Z",
        },
      ],
    });

    const refresh = harness.sendMessage({ type: "REFRESH_STATUS" });
    await started;
    const saveNewOrigin = harness.sendMessage({
      type: "SAVE_CONFIG",
      api_origin: "https://api-b.example",
      token: "b".repeat(32),
    });
    await waitForAsync(async () => (await harness.read("api_config_v1"))?.origin === "https://api-b.example");
    finishStatusRequest(new Response(JSON.stringify({ error: { code: "UNAVAILABLE" } }), { status: 503 }));
    await refresh;
    await saveNewOrigin;

    const sameIdOnNewOrigin = (await harness.read("recent_items_v1")).find((item) => item.api_origin === "https://api-b.example");
    assert.equal(sameIdOnNewOrigin.last_checked_at, null);
    assert.equal(sameIdOnNewOrigin.next_check_at, "2030-01-01T00:00:00.000Z");
  } finally {
    delete globalThis.chrome;
    delete globalThis.fetch;
  }
});
