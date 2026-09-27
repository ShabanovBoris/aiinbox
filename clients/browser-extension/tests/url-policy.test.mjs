import assert from "node:assert/strict";
import test from "node:test";

import {
  apiHostPermission,
  buildApiUrl,
  normalizeApiOrigin,
  normalizeCapturePageUrl,
} from "../src/url-policy.js";

test("API policy accepts HTTPS and exact loopback HTTP origins", () => {
  assert.equal(normalizeApiOrigin("https://aiinbox.example.net/"), "https://aiinbox.example.net");
  assert.equal(normalizeApiOrigin("http://localhost:8080"), "http://localhost:8080");
  assert.equal(normalizeApiOrigin("http://127.0.0.1:8080"), "http://127.0.0.1:8080");
  assert.equal(apiHostPermission("https://aiinbox.example.net:8443"), "https://aiinbox.example.net:8443/*");
  assert.equal(apiHostPermission("http://localhost:8080"), "http://localhost:8080/*");
});

test("API policy rejects remote HTTP, credentials, paths, queries, and fragments", () => {
  for (const value of [
    "http://example.com",
    "http://192.168.1.8:8080",
    "http://localhost.evil.test",
    "https://user:password@example.com",
    "https://example.com/api",
    "https://example.com?token=x",
    "https://example.com#section",
    "file:///tmp/api",
  ]) {
    assert.throws(() => normalizeApiOrigin(value));
  }
});

test("page capture accepts only HTTP(S), preserving query-bearing page URLs", () => {
  assert.equal(
    normalizeCapturePageUrl("https://example.com/article?ref=home"),
    "https://example.com/article?ref=home",
  );
  for (const value of [
    "chrome://settings",
    "chrome-extension://id/page.html",
    "edge://history",
    "about:blank",
    "file:///tmp/article.html",
    "view-source:https://example.com",
    "data:text/plain,hello",
    "javascript:alert(1)",
  ]) {
    assert.throws(() => normalizeCapturePageUrl(value));
  }
});

test("PM-18 URL construction stays on the configured origin and known routes", () => {
  assert.equal(
    buildApiUrl("https://example.com", "/v1/items?limit=1"),
    "https://example.com/v1/items?limit=1",
  );
  assert.equal(buildApiUrl("http://localhost:8080", "/v1/items/42"), "http://localhost:8080/v1/items/42");
  assert.throws(() => buildApiUrl("https://example.com", "//attacker.example/v1/items"));
  assert.throws(() => buildApiUrl("https://example.com", "/v1/ask"));
  assert.throws(() => buildApiUrl("https://example.com", "/v1/items/../ask"));
  assert.throws(() => buildApiUrl("https://example.com", "/v1/items?limit=100"));
});
