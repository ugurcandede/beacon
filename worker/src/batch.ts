// The batch the clients send (protocol/README.md) and its PostHog form.

export interface Env {
  POSTHOG_HOST: string;
  POSTHOG_KEY: string; // project token: write-only, not a secret
  APPS: string; // comma-separated app names accepted
  LIMITER: RateLimit; // per client IP
}

export interface AppEvent {
  name: string;
  props: Record<string, unknown>;
  ts: number; // epoch ms, client clock
  session_id: string; // UUIDv7, what PostHog groups sessions by
  app_version?: string; // the version that recorded it; the sender may be newer
  uuid?: string; // PostHog drops a second event with the same uuid
}

export interface Batch {
  app: string;
  install_id: string;
  machine_id?: string; // the same in every app on a machine; 0.1 clients have none
  sent_at?: number; // epoch ms, client clock, when this batch was sent
  context: Record<string, unknown>;
  events: AppEvent[];
}

const MAX_EVENTS = 100;

export function valid(batch: Batch | null, env: Env): batch is Batch {
  return (
    batch !== null &&
    env.APPS.split(",").includes(batch.app) &&
    typeof batch.install_id === "string" &&
    (batch.machine_id === undefined || typeof batch.machine_id === "string") &&
    (batch.sent_at === undefined || typeof batch.sent_at === "number") &&
    typeof batch.context === "object" &&
    Array.isArray(batch.events) &&
    batch.events.length <= MAX_EVENTS &&
    batch.events.every((e) => typeof e.name === "string" && typeof e.ts === "number")
  );
}

/** The body for PostHog's /batch/, without the api_key. With sent_at, PostHog
 * shifts every timestamp by how far the client clock is off, so events kept
 * offline for days land at the right time even on a wrong clock. */
export function toPostHog(batch: Batch, cf: IncomingRequestCfProperties | undefined) {
  const ctx = batch.context;
  const common = {
    app: batch.app, // kept for the insights made before product/channel
    product: batch.app, // the websites send the same two, so reports split by product
    channel: "app", // and by app vs site
    $app_name: batch.app,
    install_id: batch.install_id,
    $os: ctx.os,
    $os_version: ctx.os_version,
    $device_type: "Desktop",
    $device_model: ctx.model,
    $locale: ctx.locale,
    $screen_width: ctx.screen_width,
    $screen_height: ctx.screen_height,
    $lib: ctx.lib,
    $lib_version: ctx.lib_version,
    ...geo(cf),
    // The machine context also lands on the person, so people can be broken
    // down by any of it without an event filter.
    $set: { ...ctx, last_app: batch.app },
    $set_once: { first_app: batch.app },
  };
  return {
    sent_at: batch.sent_at === undefined ? undefined : iso(batch.sent_at),
    batch: batch.events.map((e) => ({
      event: e.name,
      uuid: e.uuid,
      distinct_id: batch.machine_id || batch.install_id, // "" when the OS would not say
      timestamp: iso(e.ts),
      properties: { ...e.props, ...common, $app_version: e.app_version ?? ctx.app_version, $session_id: e.session_id },
    })),
  };
}

function iso(ms: number) {
  return new Date(ms).toISOString();
}

function geo(cf: IncomingRequestCfProperties | undefined) {
  if (!cf) return { $geoip_disable: true };
  return {
    $geoip_disable: true,
    $geoip_country_code: cf.country,
    $geoip_continent_code: cf.continent,
    $geoip_subdivision_1_code: cf.regionCode,
    $geoip_subdivision_1_name: cf.region,
    $geoip_city_name: cf.city,
    $geoip_postal_code: cf.postalCode,
    $geoip_latitude: cf.latitude ? Number(cf.latitude) : undefined,
    $geoip_longitude: cf.longitude ? Number(cf.longitude) : undefined,
    $geoip_time_zone: cf.timezone,
  };
}
