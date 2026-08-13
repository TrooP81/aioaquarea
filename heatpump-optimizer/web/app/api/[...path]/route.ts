import { NextRequest } from "next/server";

const INTERNAL_API_URL = process.env.INTERNAL_API_URL || "http://api:8500";
const INTERNAL_API_TOKEN = process.env.INTERNAL_API_TOKEN || "";

/**
 * Server-side API gateway for the dashboard.
 *
 * The dashboard never receives the production API token. Instead this route
 * adds the container-only credential while forwarding the browser request to
 * FastAPI over the internal Docker network.
 */
async function proxy(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
) {
  // Next.js 15 resolves dynamic route parameters asynchronously.
  const { path } = await context.params;
  const requestedPath = path.join("/");
  // FastAPI keeps operational probes outside its /api namespace. Expose them
  // through the same browser gateway so local monitoring never needs the
  // container-only API port.
  const upstreamPath = requestedPath === "health" || requestedPath.startsWith("health/")
    ? `/${requestedPath}`
    : `/api/${requestedPath}`;
  const target = new URL(upstreamPath, INTERNAL_API_URL);
  target.search = request.nextUrl.search;

  const headers = new Headers();
  for (const name of ["accept", "content-type", "x-request-id"]) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  if (INTERNAL_API_TOKEN && INTERNAL_API_TOKEN !== "disabled") {
    headers.set("authorization", `Bearer ${INTERNAL_API_TOKEN}`);
  } else {
    const authorization = request.headers.get("authorization");
    if (authorization) headers.set("authorization", authorization);
  }

  const init: RequestInit = {
    method: request.method,
    headers,
    cache: "no-store",
    redirect: "manual",
  };
  if (request.method !== "GET" && request.method !== "HEAD") {
    init.body = await request.arrayBuffer();
  }

  const upstream = await fetch(target, init);
  const responseHeaders = new Headers(upstream.headers);
  responseHeaders.delete("connection");
  responseHeaders.delete("content-encoding");
  responseHeaders.delete("content-length");
  responseHeaders.set("cache-control", "no-store");
  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: responseHeaders,
  });
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
export const OPTIONS = proxy;
