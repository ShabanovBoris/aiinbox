import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const directory = new URL("../", import.meta.url);
const manifest = JSON.parse(await readFile(new URL("manifest.json", directory), "utf8"));

test("manifest uses MV3 with the bounded required permission set", () => {
  assert.equal(manifest.manifest_version, 3);
  assert.equal(manifest.minimum_chrome_version, "120");
  assert.deepEqual([...manifest.permissions].sort(), ["activeTab", "alarms", "contextMenus", "storage"]);
  assert.deepEqual(manifest.optional_host_permissions, [
    "https://*/*",
    "http://localhost/*",
    "http://127.0.0.1/*",
  ]);
  for (const forbidden of ["tabs", "scripting", "webRequest", "cookies", "history", "<all_urls>"]) {
    assert.equal(manifest.permissions.includes(forbidden), false);
    assert.equal(manifest.optional_host_permissions.includes(forbidden), false);
  }
  assert.equal("content_scripts" in manifest, false);
  assert.equal(manifest.background.type, "module");
});

test("extension pages ship local executable code under a restrictive CSP", async () => {
  assert.equal(manifest.content_security_policy.extension_pages, "script-src 'self'; object-src 'self'");
  for (const filename of ["popup.html", "options.html"]) {
    const html = await readFile(new URL(filename, directory), "utf8");
    assert.match(html, /<script type="module" src="src\/[a-z-]+\.js"><\/script>/);
    assert.doesNotMatch(html, /<script\s*>/i);
    assert.doesNotMatch(html, /https?:\/\/[^"']+\.js/i);
  }
});

test("runtime icon files referenced by the manifest are local", async () => {
  for (const filename of Object.values(manifest.icons)) {
    const bytes = await readFile(new URL(filename, directory));
    assert.ok(bytes.length > 30);
    assert.deepEqual([...bytes.subarray(0, 8)], [137, 80, 78, 71, 13, 10, 26, 10]);
  }
});
