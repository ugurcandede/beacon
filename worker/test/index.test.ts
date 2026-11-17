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
    expect(await new Response(init.body as ReadableStream).text()).toBe("payload"); // streamed through, not held
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

describe("hardening", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("answers 400, not an exception, to a body it cannot read as a batch", async () => {
    vi.stubGlobal("fetch", async () => new Response(null, { status: 200 }));
    for (const body of [{ ...vectors.valid, events: [null] }, { ...vectors.valid, context: null }, [], "x", 5, null]) {
      expect((await handle(post(body))).status).toBe(400);
    }
  });

  it("answers 502 when PostHog cannot be reached, so the client retries", async () => {
    vi.stubGlobal("fetch", async () => {
      throw new TypeError("network");
    });
    expect((await handle(post(vectors.valid))).status).toBe(502);
  });

  it("answers 400 when PostHog rejects the batch, so the client drops it instead of retrying forever", async () => {
    vi.stubGlobal("fetch", async () => new Response("bad", { status: 400 }));
    expect((await handle(post(vectors.valid))).status).toBe(400);
    for (const status of [401, 429, 500]) {
      vi.stubGlobal("fetch", async () => new Response(null, { status }));
      expect((await handle(post(vectors.valid))).status).toBe(502);
    }
  });

  it("sends PostHog nothing of the request but the batch: no client headers, no IP", async () => {
    const upstream = vi.fn(async () => new Response(null, { status: 200 }));
    vi.stubGlobal("fetch", upstream);
    const request = new Request("https://beacon.test/v1/events", {
      method: "POST",
      body: JSON.stringify(vectors.valid),
      headers: { "cf-connecting-ip": "203.0.113.9", "x-forwarded-for": "203.0.113.9", cookie: "a=b" },
    });
    await handle(request);
    const init = (upstream.mock.calls[0] as unknown as [string, RequestInit])[1];
    expect(Object.keys(init.headers as object)).toEqual(["Content-Type"]);
    expect(init.body as string).not.toContain("203.0.113.9");
  });

  it("measures the batch in bytes", async () => {
    vi.stubGlobal("fetch", async () => new Response(null, { status: 200 }));
    const big = { ...vectors.valid, context: { note: "€".repeat(400_000) } }; // 1.2 MB as UTF-8, 400k characters
    expect((await handle(post(big))).status).toBe(413);
  });

  it("drops events and props only PostHog itself may send", () => {
    const batch = structuredClone(vectors.valid) as never as { events: { name: string; props: Record<string, unknown> }[] };
    batch.events = [
      { ...batch.events[0], name: "$create_alias", props: { alias: "someone-else" } },
      { ...batch.events[0], name: "$identify", props: {} },
      { ...batch.events[0], name: "$screen", props: { $screen_name: "Main" } },
      { ...batch.events[0], name: "$exception", props: {} },
      { ...batch.events[0], name: "saved", props: { $unset: ["os"], $groups: { x: 1 }, $ip: "1.2.3.4", product: "other", channel: "web", ok: 1 } },
    ];
    const out = toPostHog(batch as never, cf).batch;
    expect(out.map((e) => e.event)).toEqual(["$screen", "$exception", "saved"]);
    const props = out[2].properties as Record<string, unknown>;
    expect(props.ok).toBe(1);
    expect(props.$unset).toBeUndefined();
    expect(props.$groups).toBeUndefined();
    expect(props.$ip).toBeUndefined();
    expect(props.product).toBe("lumea"); // the app cannot pass for another product
    const withSet = toPostHog({ ...(batch as object), events: [{ ...batch.events[4], props: { $set: { mode: "hold", os: "fake" } } }] } as never, cf);
    const set = (withSet.batch[0].properties as { $set: Record<string, unknown> }).$set;
    expect(set.mode).toBe("hold"); // an app's own person properties are kept
    expect(set.os).toBe(vectors.valid.context.os); // the machine context wins
    expect(props.channel).toBe("app");
  });

  it("answers 502 to a flags answer PostHog could not compute, so the client keeps its last one", async () => {
    const answer = (extra: object) => async () => Response.json({ flags: { a: { key: "a", enabled: true } }, ...extra });
    vi.stubGlobal("fetch", answer({ errorsWhileComputingFlags: true }));
    const get = () => handle(new Request("https://beacon.test/v1/flags?app=lumea&id=m1"));
    expect((await get()).status).toBe(502);
    vi.stubGlobal("fetch", answer({ quotaLimited: ["feature_flags"] }));
    expect((await get()).status).toBe(502);
    vi.stubGlobal("fetch", async () => new Response("not json", { status: 200 }));
    expect((await get()).status).toBe(502);
    vi.stubGlobal("fetch", answer({ errorsWhileComputingFlags: false }));
    expect((await get()).status).toBe(200);
  });

  it("proxies only what posthog-js sends: GET, POST and OPTIONS, without credentials", async () => {
    const upstream = vi.fn(async () => new Response("ok"));
    vi.stubGlobal("fetch", upstream);
    for (const method of ["PUT", "DELETE", "PATCH"]) {
      expect((await handle(new Request("https://beacon.test/ingest/e/", { method }))).status).toBe(405);
    }
    expect(upstream).not.toHaveBeenCalled();
    await handle(new Request("https://beacon.test/ingest/e/?ip=0", {
      method: "POST",
      body: "{}",
      headers: { authorization: "Bearer phx_secret", cookie: "a=b", "content-type": "text/plain", "user-agent": "UA" },
    }));
    const [target, init] = upstream.mock.calls[0] as unknown as [string, RequestInit];
    expect(target).toBe("https://posthog.invalid/e/?ip=0");
    const headers = new Headers(init.headers);
    expect(headers.get("authorization")).toBeNull();
    expect(headers.get("cookie")).toBeNull();
    expect(headers.get("content-type")).toBe("text/plain");
    expect(headers.get("user-agent")).toBe("UA");
  });

  it("keeps the proxy on the PostHog host whatever the path", () => {
    for (const path of ["/ingest/../admin", "/ingest//evil.example/x", "/ingest/@evil.example", "/ingest/%2e%2e/x"]) {
      const target = new URL(proxyTarget(new URL("https://beacon.test" + path), "https://posthog.invalid"));
      expect(target.host).toBe("posthog.invalid");
    }
  });
});
