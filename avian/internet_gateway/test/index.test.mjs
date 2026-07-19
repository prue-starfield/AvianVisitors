import assert from "node:assert/strict";
import test from "node:test";

import gateway from "../src/index.js";

function environment(handler) {
  const calls = [];
  return {
    calls,
    env: {
      AVIAN_ORIGIN: {
        async fetch(request) {
          calls.push(request);
          return handler(request);
        },
      },
    },
  };
}

test("GET forwards only allow-listed headers, path, and query", async () => {
  const { calls, env } = environment(async () => new Response("garden", {
    headers: {
      "content-security-policy": "default-src 'self'",
      "content-type": "text/plain",
      location: "http://127.0.0.1:9137/private",
      refresh: "0; url=file:///Users/prue/private",
      server: "private-origin",
      "set-cookie": "origin-secret=yes",
      "x-backend-token": "must-not-cross",
      "x-frame-options": "DENY",
      "x-internal-path": "/Users/prue/private/archive",
    },
  }));
  const request = new Request("https://birds.example/api/today?day=2026-07-19", {
    headers: {
      accept: "application/json",
      "accept-language": "en-GB",
      authorization: "Bearer must-not-cross",
      "cf-access-jwt-assertion": "must-not-cross",
      "cf-connecting-ip": "192.0.2.10",
      cookie: "CF_Authorization=must-not-cross",
      "if-modified-since": "Sun, 19 Jul 2026 10:00:00 GMT",
      "if-none-match": "test-etag",
      "if-range": "range-etag",
      origin: "https://attacker.example",
      range: "bytes=0-99",
      referer: "https://attacker.example/private",
      "user-agent": "gateway-test",
      "x-forwarded-for": "192.0.2.11",
      "x-forwarded-host": "private.example",
      "x-real-ip": "192.0.2.12",
      "x-injected": "must-not-cross",
    },
  });

  const response = await gateway.fetch(request, env);
  assert.equal(response.status, 200);
  assert.equal(await response.text(), "garden");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "http://avian-origin.internal/api/today?day=2026-07-19");
  for (const [name, value] of Object.entries({
    accept: "application/json",
    "accept-language": "en-GB",
    "if-modified-since": "Sun, 19 Jul 2026 10:00:00 GMT",
    "if-none-match": "test-etag",
    "if-range": "range-etag",
    range: "bytes=0-99",
    "user-agent": "gateway-test",
  })) {
    assert.equal(calls[0].headers.get(name), value, `${name} should cross`);
  }
  for (const name of [
    "authorization",
    "cf-access-jwt-assertion",
    "cf-connecting-ip",
    "cookie",
    "origin",
    "referer",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-real-ip",
    "x-injected",
  ]) {
    assert.equal(calls[0].headers.get(name), null, `${name} must not cross`);
  }
  assert.equal(response.headers.get("content-security-policy"), "default-src 'self'");
  assert.equal(response.headers.get("content-type"), "text/plain");
  assert.equal(response.headers.get("x-frame-options"), "DENY");
  for (const name of [
    "location",
    "refresh",
    "server",
    "set-cookie",
    "x-backend-token",
    "x-internal-path",
  ]) {
    assert.equal(response.headers.get(name), null, `${name} must not cross`);
  }
  assert.equal(response.headers.get("cache-control"), "private, no-store");
  assert.equal(response.headers.get("x-robots-tag"), "noindex, nofollow, noarchive");
});

test("HEAD returns no body and preserves origin range metadata", async () => {
  const { env } = environment(async () => new Response(null, {
    status: 206,
    headers: {
      "accept-ranges": "bytes",
      "content-range": "bytes 0-99/1000",
    },
  }));
  const response = await gateway.fetch(
    new Request("https://birds.example/api/audio/abc", { method: "HEAD" }),
    env,
  );
  assert.equal(response.status, 206);
  assert.equal(await response.text(), "");
  assert.equal(response.headers.get("accept-ranges"), "bytes");
  assert.equal(response.headers.get("content-range"), "bytes 0-99/1000");
});

test("origin redirects fail closed without exposing Location", async (t) => {
  for (const status of [301, 302, 303, 307, 308]) {
    await t.test(String(status), async () => {
      const { calls, env } = environment(async () => new Response(null, {
        status,
        headers: {
          location: "http://127.0.0.1:9137/Users/prue/private",
          "x-internal-path": "/Users/prue/private/archive",
        },
      }));
      const response = await gateway.fetch(new Request("https://birds.example/redirect"), env);
      assert.equal(response.status, 502);
      assert.equal(await response.text(), "Archive temporarily unavailable\n");
      assert.equal(response.headers.get("location"), null);
      assert.equal(response.headers.get("x-internal-path"), null);
      assert.equal(calls.length, 1);
    });
  }
});

test("304 cache validation is preserved without arbitrary metadata", async () => {
  const { env } = environment(async () => new Response(null, {
    status: 304,
    headers: {
      etag: "safe-etag",
      location: "file:///Users/prue/private",
      "x-internal-path": "/Users/prue/private/archive",
    },
  }));
  const response = await gateway.fetch(new Request("https://birds.example/app.js"), env);
  assert.equal(response.status, 304);
  assert.equal(response.headers.get("etag"), "safe-etag");
  assert.equal(response.headers.get("location"), null);
  assert.equal(response.headers.get("x-internal-path"), null);
});

test("non-read methods fail closed without touching the origin", async () => {
  const { calls, env } = environment(async () => new Response("unexpected"));
  const response = await gateway.fetch(
    new Request("https://birds.example/api/today", { method: "POST" }),
    env,
  );
  assert.equal(response.status, 405);
  assert.equal(response.headers.get("allow"), "GET, HEAD");
  assert.equal(calls.length, 0);
});

test("origin failures return a generic non-cacheable response", async () => {
  const { env } = environment(async () => {
    throw new Error("private detail");
  });
  const originalError = console.error;
  console.error = () => {};
  try {
    const response = await gateway.fetch(new Request("https://birds.example/"), env);
    assert.equal(response.status, 502);
    assert.equal(await response.text(), "Archive temporarily unavailable\n");
    assert.equal(response.headers.get("cache-control"), "private, no-store");
  } finally {
    console.error = originalError;
  }
});
