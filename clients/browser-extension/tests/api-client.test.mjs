import assert from "node:assert/strict";
import test from "node:test";

import { apiFetch, postCapture, testApiConnection } from "../src/api-client.js";

function jsonResponse(body, status = 200, headers = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

test("capture retries preserve request body and Idempotency-Key and use extension fetch policy", async () => {
  const calls = [];
  const fetchImpl = async (url, options) => {
    calls.push({ url, options });
    if (calls.length === 1) throw new TypeError("offline");
    return jsonResponse({ id: 14, processing_status: "QUEUED", state: "ACTIVE" }, 202);
  };
  const record = {
    text: "https://example.com/article",
    user_note: "Read later",
    idempotency_key: "same-key",
  };
  await assert.rejects(postCapture({ apiOrigin: "https://inbox.example", token: "t".repeat(32), record, fetchImpl }), {
    code: "NETWORK_ERROR",
  });
  const accepted = await postCapture({ apiOrigin: "https://inbox.example", token: "t".repeat(32), record, fetchImpl });

  assert.equal(accepted.kind, "ACCEPTED");
  assert.equal(calls[0].url, "https://inbox.example/v1/items");
  assert.equal(calls[0].options.headers.Authorization, `Bearer ${"t".repeat(32)}`);
  assert.equal(calls[0].options.headers["Idempotency-Key"], "same-key");
  assert.equal(calls[0].options.credentials, "omit");
  assert.equal(calls[0].options.redirect, "error");
  assert.equal(calls[0].options.body, calls[1].options.body);
  assert.equal(calls[0].options.headers["Idempotency-Key"], calls[1].options.headers["Idempotency-Key"]);
});

test("API client rejects cross-origin routes before attaching credentials", async () => {
  let called = false;
  await assert.rejects(apiFetch({
    apiOrigin: "https://inbox.example",
    path: "//attacker.example/v1/items",
    token: "t".repeat(32),
    fetchImpl: async () => { called = true; },
  }));
  assert.equal(called, false);
});

test("API timeout aborts the fetch", async () => {
  let observedAbort = false;
  const fetchImpl = (_url, { signal }) => new Promise((_resolve, reject) => {
    signal.addEventListener("abort", () => {
      observedAbort = true;
      const error = new Error("aborted");
      error.name = "AbortError";
      reject(error);
    });
  });
  await assert.rejects(apiFetch({
    apiOrigin: "https://inbox.example",
    path: "/healthz",
    fetchImpl,
    timeoutMs: 5,
  }), { code: "TIMEOUT" });
  assert.equal(observedAbort, true);
});

test("connection check probes health without auth and validates auth on the list route", async () => {
  const calls = [];
  const result = await testApiConnection({
    apiOrigin: "http://localhost:8080",
    token: "secret-token-value",
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return calls.length === 1
        ? jsonResponse({ status: "ok" })
        : jsonResponse({ items: [], next_cursor: null });
    },
  });
  assert.equal(result.status, "CONNECTED");
  assert.equal(calls[0].url, "http://localhost:8080/healthz");
  assert.equal(calls[0].options.headers.Authorization, undefined);
  assert.equal(calls[1].url, "http://localhost:8080/v1/items?limit=1");
  assert.equal(calls[1].options.headers.Authorization, "Bearer secret-token-value");
});

test("connection test distinguishes unauthenticated tokens", async () => {
  const result = await testApiConnection({
    apiOrigin: "https://inbox.example",
    token: "secret-token-value",
    fetchImpl: async (_url, options) => options.headers.Authorization
      ? jsonResponse({ error: { code: "UNAUTHORIZED" } }, 401)
      : jsonResponse({ status: "ok" }),
  });
  assert.equal(result.status, "AUTH_FAILED");
});
