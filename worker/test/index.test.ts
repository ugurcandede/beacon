import { afterEach, describe, expect, it, vi } from "vitest";
import worker from "../src/index";
import { toPostHog, valid } from "../src/batch";
import { proxyTarget } from "../src/proxy";
import { simplify } from "../src/flags";
import flagVectors from "../../protocol/vectors/flags.json";
import vectors from "../../protocol/vectors/events.json";

const allow = { limit: async () => ({ success: true }) };
const env = { POSTHOG_HOST: "https://posthog.invalid", POSTHOG_KEY: "phc_test", APPS: "lumea,sotto", LIMITER: allow };
const cf = vectors.cf as unknown as IncomingRequestCfProperties;
const post = (body: unknown) =>
  new Request("https://beacon.test/v1/events", { method: "POST", body: JSON.stringify(body) });
const handle = (request: Request, e: object = env) => worker.fetch(request as never, e as never);

describe("protocol vectors", () => {
  it("accepts the valid batch", () => {
    expect(valid(structuredClone(vectors.valid) as never, env)).toBe(true);
  });

  it.each(vectors.invalid)("rejects: $why", ({ patch }) => {
    expect(valid({ ...structuredClone(vectors.valid), ...patch } as never, env)).toBe(false);
  });

  it("maps the batch to PostHog", () => {
    // Through JSON, as it is sent: undefined fields drop out.
    expect(JSON.parse(JSON.stringify(toPostHog(vectors.valid as never, cf)))).toEqual(vectors.posthog);
  });

  it("still takes a batch from a 0.1 client, keyed by its install id", () => {
    expect(valid(structuredClone(vectors.v1) as never, env)).toBe(true);
    const out = JSON.parse(JSON.stringify(toPostHog(vectors.v1 as never, undefined)));
    expect(out.sent_at).toBeUndefined();
    expect(out.batch[0].distinct_id).toBe(vectors.v1.install_id);
    expect(out.batch[0].properties.$app_version).toBe("1.0.0");
  });
});

describe("fetch", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("forwards a valid batch and answers 204", async () => {
    const upstream = vi.fn(async () => new Response(null, { status: 200 }));
    vi.stubGlobal("fetch", upstream);
    expect((await handle(post(vectors.valid))).status).toBe(204);
    const [url, init] = upstream.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://posthog.invalid/batch/");
    const sent = JSON.parse(init.body as string);
    expect(sent.api_key).toBe("phc_test");
    expect(sent.sent_at).toBe(vectors.posthog.sent_at);
    expect(sent.batch).toHaveLength(vectors.valid.events.length);
  });

  it("answers 502 when PostHog fails, so the client retries", async () => {
    vi.stubGlobal("fetch", async () => new Response(null, { status: 503 }));
    expect((await handle(post(vectors.valid))).status).toBe(502);
  });

  it("answers 503 to an address over its rate, keyed by the client IP", async () => {
    const keys: string[] = [];
    const deny = { limit: async ({ key }: { key: string }) => (keys.push(key), { success: false }) };
    const request = post(vectors.valid);
    request.headers.set("cf-connecting-ip", "203.0.113.7");
    const response = await handle(request, { ...env, LIMITER: deny });
    expect(response.status).toBe(503); // the client keeps the batch and retries
    expect(keys).toEqual(["203.0.113.7"]);
  });

  it("answers 413 to a batch over 1 MB", async () => {
    const big = { ...vectors.valid, context: { ...vectors.valid.context, padding: "x".repeat(1 << 20) } };
    expect((await handle(post(big))).status).toBe(413);
  });

  it("answers 400 to a body that is not JSON", async () => {
    const request = new Request("https://beacon.test/v1/events", { method: "POST", body: "{not json" });
    expect((await handle(request)).status).toBe(400);
  });

  it("answers 400 to a bad batch and 404 elsewhere", async () => {
    expect((await handle(post({ app: "lumea" }))).status).toBe(400);
    expect((await handle(new Request("https://beacon.test/"))).status).toBe(404);
  });
});

describe("posthog-js proxy", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("sends the library to the assets host and events to the ingest host", () => {
    const host = "https://eu.i.posthog.com";
    expect(proxyTarget(new URL("https://beacon.test/ingest/static/array.js"), host))
      .toBe("https://eu-assets.i.posthog.com/static/array.js");
    expect(proxyTarget(new URL("https://beacon.test/ingest/e/?ip=0&ver=1.2"), host))
      .toBe("https://eu.i.posthog.com/e/?ip=0&ver=1.2");
  });

  it("forwards method, body and the visitor's IP, drops cookies, skips the rate limit", async () => {
    const upstream = vi.fn(async () => new Response("ok", { status: 200, headers: { "access-control-allow-origin": "*" } }));
    vi.stubGlobal("fetch", upstream);
    const limited = { limit: vi.fn(async () => ({ success: false })) };
    const request = new Request("https://beacon.test/ingest/e/?compression=gzip-js", {
      method: "POST", body: "payload",
      headers: { "cf-connecting-ip": "203.0.113.9", cookie: "session=1", "content-type": "text/plain" },
    });
    const response = await handle(request, { ...env, LIMITER: limited });
    expect(response.status).toBe(200);
    expect(response.headers.get("access-control-allow-origin")).toBe("*");
    expect(limited.limit).not.toHaveBeenCalled();
    const [url, init] = upstream.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://posthog.invalid/e/?compression=gzip-js");
    const headers = new Headers(init.headers);
    expect(headers.get("x-forwarded-for")).toBe("203.0.113.9");
    expect(headers.get("cookie")).toBeNull();
    expect(new TextDecoder().decode(init.body as ArrayBuffer)).toBe("payload");
  });
});

describe("feature flags", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("simplifies PostHog's answer as the vector says", () => {
    expect(simplify(flagVectors.posthog as never)).toEqual(flagVectors.worker);
  });

  it("asks PostHog for this machine and product, and answers 400 to an unknown app", async () => {
    const upstream = vi.fn(async () => Response.json(flagVectors.posthog));
    vi.stubGlobal("fetch", upstream);
    const response = await handle(new Request("https://beacon.test/v1/flags?app=lumea&id=abc"));
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual(flagVectors.worker);
    const [url, init] = upstream.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("https://posthog.invalid/flags/?v=2");
    expect(JSON.parse(init.body as string)).toEqual({ api_key: "phc_test", distinct_id: "abc", person_properties: { product: "lumea" } });
    expect((await handle(new Request("https://beacon.test/v1/flags?app=nope&id=abc"))).status).toBe(400);
    expect((await handle(new Request("https://beacon.test/v1/flags?app=lumea"))).status).toBe(400);
  });

  it("answers 502 when PostHog fails, so the client keeps its last answer", async () => {
    vi.stubGlobal("fetch", async () => new Response(null, { status: 500 }));
    expect((await handle(new Request("https://beacon.test/v1/flags?app=lumea&id=abc"))).status).toBe(502);
  });
});
