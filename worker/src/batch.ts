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

const MAX_TIME = 8.64e15; // the last millisecond a Date can hold
const isObject = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const isTime = (v: unknown) => typeof v === "number" && Number.isFinite(v) && Math.abs(v) <= MAX_TIME;

/** Whatever was posted is checked here in full: nothing after this may throw on it. */
export function valid(batch: unknown, env: Env): batch is Batch {
  if (!isObject(batch)) return false;
  const b = batch as Partial<Batch>;
  return (
    typeof b.app === "string" &&
    env.APPS.split(",").includes(b.app) &&
    typeof b.install_id === "string" &&
    (b.machine_id === undefined || typeof b.machine_id === "string") &&
    (b.machine_id || b.install_id) !== "" && // someone to count it under
    (b.sent_at === undefined || isTime(b.sent_at)) &&
    isObject(b.context) &&
    Array.isArray(b.events) &&
    b.events.length <= MAX_EVENTS &&
    b.events.every(
      (e) => isObject(e) && typeof e.name === "string" && e.name !== "" && isTime(e.ts) && (e.props === undefined || isObject(e.props)),
    )
  );
}

// PostHog reads these as instructions about people (merge two, forget a property),
// not as something that happened: an app's batch may not carry them.
const OWN_EVENTS = new Set(["$screen", "$exception"]);
const RESERVED_PROPS = ["$set", "$set_once", "$unset", "$groups", "$ip", "$anon_distinct_id", "$process_person_profile"];
// `$set` is the exception: an app may keep its own settings on the person; the machine context wins a clash.
const personProps = (props: Record<string, unknown> | undefined) => (isObject(props?.$set) ? props.$set : {});

function ownProps(props: Record<string, unknown> | undefined) {
  const out = { ...props };
  for (const key of RESERVED_PROPS) delete out[key];
  return out;
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
    batch: batch.events
      .filter((e) => !e.name.startsWith("$") || OWN_EVENTS.has(e.name))
      .map((e) => ({
        event: e.name,
        uuid: e.uuid,
        distinct_id: batch.machine_id || batch.install_id, // "" when the OS would not say
        timestamp: iso(e.ts),
        // `common` last: an app cannot pass for another product or channel.
        properties: {
          ...ownProps(e.props),
          ...common,
          $set: { ...personProps(e.props), ...common.$set },
          $app_version: e.app_version ?? ctx.app_version,
          $session_id: e.session_id,
        },
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
    // Country, region and city, as docs/privacy.md says: no postal code, no coordinates.
    $geoip_country_code: cf.country,
    $geoip_subdivision_1_code: cf.regionCode,
    $geoip_subdivision_1_name: cf.region,
    $geoip_city_name: cf.city,
  };
}
