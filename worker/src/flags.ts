// GET /v1/flags?app=<app>&id=<machine_id>: what PostHog's feature flags say for
// this machine and app, simplified for the clients (protocol/vectors/flags.json).
// The project token stays here; flags can target by product, since the app goes
// to PostHog as the person property `product`.

import type { Env } from "./batch";

interface PostHogFlag {
  enabled: boolean;
  variant: string | null;
  metadata?: { payload?: string | null };
}

export function simplify(answer: { flags?: Record<string, PostHogFlag> }) {
  const flags: Record<string, string | boolean> = {};
  const payloads: Record<string, unknown> = {};
  for (const [key, flag] of Object.entries(answer.flags ?? {})) {
    flags[key] = flag.variant ?? flag.enabled;
    const payload = flag.metadata?.payload;
    if (payload != null) {
      try {
        payloads[key] = JSON.parse(payload);
      } catch {
        payloads[key] = payload;
      }
    }
  }
  return { flags, payloads };
}

export async function flags(url: URL, env: Env): Promise<Response> {
  const app = url.searchParams.get("app") ?? "";
  const id = url.searchParams.get("id") ?? "";
  if (!env.APPS.split(",").includes(app) || !id) {
    return new Response("unknown app or no id", { status: 400 });
  }
  // 502 whenever the answer is not a full one: the client then keeps its last
  // answer, instead of replacing it with flags that are missing or all off.
  const failed = new Response(null, { status: 502 });
  try {
    const response = await fetch(`${env.POSTHOG_HOST}/flags/?v=2`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ api_key: env.POSTHOG_KEY, distinct_id: id, person_properties: { product: app } }),
    });
    if (!response.ok) return failed;
    const answer = (await response.json()) as { errorsWhileComputingFlags?: boolean; quotaLimited?: string[] };
    if (answer.errorsWhileComputingFlags || answer.quotaLimited?.includes("feature_flags")) return failed;
    return Response.json(simplify(answer as never));
  } catch {
    return failed; // unreachable, or not JSON
  }
}
