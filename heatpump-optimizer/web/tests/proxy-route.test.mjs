import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { test } from "node:test";

const runtimeRequire = createRequire(import.meta.url);
const ts = runtimeRequire("typescript");
const { NextRequest } = runtimeRequire("next/server");

test("planning settings proxy forwards scope and revision without exposing its server token", async () => {
  const previousUrl = process.env.INTERNAL_API_URL;
  const previousToken = process.env.INTERNAL_API_TOKEN;
  const previousFetch = globalThis.fetch;
  const canary = "p21-proxy-canary-token";
  process.env.INTERNAL_API_URL = "http://isolated-upstream.invalid";
  process.env.INTERNAL_API_TOKEN = canary;

  let upstreamUrl = "";
  let upstreamAuthorization = null;
  let upstreamIfMatch = null;
  globalThis.fetch = async (input, init) => {
    upstreamUrl = String(input);
    const headers = new Headers(init?.headers);
    upstreamAuthorization = headers.get("authorization");
    upstreamIfMatch = headers.get("if-match");
    return new Response(JSON.stringify({ ordinary_writes_allowed: false }), {
      status: 200,
      headers: {
        "content-type": "application/json",
        "X-Installation-ID": "00000000-0000-0000-0000-000000000017",
        "X-Contract-Revision": "7",
      },
    });
  };

  try {
    const routePath = new URL("../app/api/[...path]/route.ts", import.meta.url);
    const source = await readFile(routePath, "utf8");
    const compiled = ts.transpileModule(source, {
      compilerOptions: {
        module: ts.ModuleKind.CommonJS,
        target: ts.ScriptTarget.ES2022,
        esModuleInterop: true,
      },
    }).outputText;
    const proxyModule = { exports: {} };
    new Function("require", "module", "exports", compiled)(
      runtimeRequire,
      proxyModule,
      proxyModule.exports,
    );

    const request = new NextRequest(
      "http://127.0.0.1/api/installations/00000000-0000-0000-0000-000000000017/planning-settings",
      { headers: { "If-Match": '"7"', Authorization: "Bearer browser-supplied-token" } },
    );
    const response = await proxyModule.exports.GET(request, {
      params: Promise.resolve({
        path: ["installations", "00000000-0000-0000-0000-000000000017", "planning-settings"],
      }),
    });
    const body = await response.text();

    assert.equal(
      upstreamUrl,
      "http://isolated-upstream.invalid/api/installations/00000000-0000-0000-0000-000000000017/planning-settings",
    );
    assert.equal(upstreamAuthorization, `Bearer ${canary}`);
    assert.equal(upstreamIfMatch, '"7"');
    assert.equal(response.headers.get("X-Contract-Revision"), "7");
    assert.equal(body.includes(canary), false);
    assert.equal(response.headers.has("authorization"), false);
  } finally {
    globalThis.fetch = previousFetch;
    if (previousUrl === undefined) delete process.env.INTERNAL_API_URL;
    else process.env.INTERNAL_API_URL = previousUrl;
    if (previousToken === undefined) delete process.env.INTERNAL_API_TOKEN;
    else process.env.INTERNAL_API_TOKEN = previousToken;
  }
});
