// Receives event batches from the apps' beacon clients and forwards them to
// PostHog. Location comes from what Cloudflare sees for the request and goes
// out as PostHog's $geoip_* properties, with PostHog's own GeoIP lookup turned
// off: the IP itself goes no further than this Worker. Moving to another
// backend is a change to batch.ts only.
//
// /ingest/... is a pass-through to PostHog for posthog-js on the sites (proxy.ts);
// GET /v1/flags answers the apps' feature flags (flags.ts).

import { type Env, toPostHog, valid } from "./batch";
import { flags } from "./flags";
import { PREFIX, proxy } from "./proxy";

const MAX_BYTES = 1 << 20; // 100 events with stack traces fit well under this

export default {
  async fetch(request, env): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname.startsWith(PREFIX + "/")) {
      return proxy(request, env.POSTHOG_HOST); // not rate limited: a browsing visitor sends often
    }
    const isFlags = request.method === "GET" && url.pathname === "/v1/flags";
    if (!isFlags && (request.method !== "POST" || url.pathname !== "/v1/events")) {
      return new Response(null, { status: 404 });
    }
    // Keeps one misbehaving client from flooding the project (a limit on what
    // PostHog accepts is set in PostHog: the project token is public). 503,
    // not 429: a client drops a batch on a 4xx, and a real user's events
    // should wait and go again.
    const ip = request.headers.get("cf-connecting-ip") ?? "unknown";
    if (!(await env.LIMITER.limit({ key: ip })).success) {
      return new Response("slow down", { status: 503, headers: { "Retry-After": "60" } });
    }
    if (isFlags) return flags(url, env);
    if (Number(request.headers.get("content-length") ?? 0) > MAX_BYTES) {
      return new Response("batch too large", { status: 413 });
    }
    const bytes = await request.arrayBuffer();
    if (bytes.byteLength > MAX_BYTES) {
      return new Response("batch too large", { status: 413 });
    }
    let batch: unknown = null;
    try {
      batch = JSON.parse(new TextDecoder().decode(bytes));
    } catch {}
    if (!valid(batch, env)) {
      return new Response("invalid batch", { status: 400 });
    }
    let status = 0;
    try {
      const response = await fetch(`${env.POSTHOG_HOST}/batch/`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ api_key: env.POSTHOG_KEY, ...toPostHog(batch, request.cf) }),
      });
      status = response.status;
    } catch {} // PostHog unreachable: retried, like a 5xx
    if (status >= 200 && status < 300) return new Response(null, { status: 204 });
    // A batch PostHog says is wrong will be wrong tomorrow: the client drops it
    // (400). Anything else (its key, its limits, an outage) is not the batch's
    // fault: 502 makes the client keep it and retry later.
    return new Response(null, { status: status === 400 ? 400 : 502 });
  },
} satisfies ExportedHandler<Env>;
