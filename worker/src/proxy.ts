// posthog-js on the sites talks to PostHog through here (/ingest/...): from the
// Worker's address, ad blockers that drop requests to posthog.com let it through.
// The visitor's IP goes on as X-Forwarded-For, so PostHog still finds where
// they are (and drops the IP: "anonymize IPs" is on for the project).

export const PREFIX = "/ingest";

export function proxyTarget(url: URL, posthogHost: string): string {
  const rest = url.pathname.slice(PREFIX.length) || "/";
  // The library and its add-ons (recorder, heatmaps) come from the assets host.
  const host = rest.startsWith("/static/") ? posthogHost.replace(".i.posthog.com", "-assets.i.posthog.com") : posthogHost;
  return host + rest + url.search;
}

// What posthog-js needs and nothing else: no cookies, no credentials.
const METHODS = new Set(["GET", "POST", "OPTIONS", "HEAD"]);
const HEADERS = ["accept", "accept-language", "content-encoding", "content-type", "origin", "referer", "user-agent",
  "access-control-request-method", "access-control-request-headers"];

export async function proxy(request: Request, posthogHost: string): Promise<Response> {
  if (!METHODS.has(request.method)) return new Response(null, { status: 405 });
  const headers = new Headers();
  for (const name of HEADERS) {
    const value = request.headers.get(name);
    if (value !== null) headers.set(name, value);
  }
  const ip = request.headers.get("cf-connecting-ip");
  if (ip) headers.set("x-forwarded-for", ip);
  // The body goes through as it arrives: nothing of any size is held here.
  const body = request.method === "POST" ? request.body : undefined;
  return fetch(proxyTarget(new URL(request.url), posthogHost), { method: request.method, headers, body, duplex: "half" } as RequestInit);
}
