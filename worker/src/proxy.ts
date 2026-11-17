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

export async function proxy(request: Request, posthogHost: string): Promise<Response> {
  const headers = new Headers(request.headers);
  headers.delete("cookie");
  headers.delete("host");
  const ip = request.headers.get("cf-connecting-ip");
  if (ip) headers.set("x-forwarded-for", ip);
  const body = request.method === "GET" || request.method === "HEAD" ? undefined : await request.arrayBuffer();
  return fetch(proxyTarget(new URL(request.url), posthogHost), { method: request.method, headers, body });
}
