# Protocol

What every beacon client and the Worker agree on. A change here comes first: update this
file and `vectors/`, then each language until its tests pass again.

## Update manifest

Each GitHub release carries `update.json` and `update.json.sig` (Ed25519 over the exact bytes, base64):

```json
{
  "version": "1.4.0",
  "notes_url": "https://github.com/<repo>/releases/tag/v1.4.0",
  "assets": {
    "windows-x64": {"url": "...", "size": 123, "sha256": "..."},
    "macos-arm64": {"url": "...", "size": 456, "sha256": "..."}
  }
}
```

- Optional: `"paused": true` (offered to no one) and `"rollout": N` (0-100; offered to a machine when
  `int(first 8 hex of sha256("<machine_id>:<version>"), 16) % 100 < N`, so each release reaches a
  different first set). `beacon.release control` changes both on a published release.
- Optional `"min_version": "V"`: a client older than V gets the release as required (it cannot be
  skipped; the app should not offer to).
- Clients trust a list of public keys (the current and the next): to rotate, ship the next key in a
  release, then start signing with it.
- Beta channel: `update-beta.json` (+ `.sig`), the same format, uploaded to the latest stable
  release next to `update.json` (prereleases are not "latest"); its asset URLs point at the beta
  release. A client on the beta channel reads both and takes the newer release; a missing
  `update-beta.json` means no beta is out.
- Clients fetch both from `https://github.com/<repo>/releases/latest/download/` (no API rate limit).
- Platform keys: `macos-arm64`, `macos-x64`, `windows-x64`, `windows-arm64`.
- A release is offered when its version is newer: numeric, dot-separated, missing parts are 0,
  non-numeric parts ("dev") are 0. `1.10.0` > `1.9.3`; `1.2` = `1.2.0`.
- Release CI makes them with the release action (`python -m beacon.release manifest`).

## Event batch: `POST /v1/events`

```json
{
  "app": "lumea",
  "install_id": "uuid",
  "machine_id": "32 hex chars",
  "sent_at": 1761600060000,
  "context": {"app_version": "1.4.0", "os": "macOS", "os_version": "15.1", "arch": "arm64",
              "locale": "tr_TR", "cpu_count": 10, "memory_gb": 16, "model": "Mac15,6",
              "screen_width": 1512, "screen_height": 982, "screen_count": 2,
              "lib": "beacon-swift", "lib_version": "0.2.0"},
  "events": [{"name": "effect_changed", "props": {"effect": "rainbow"}, "ts": 1761600000000,
              "session_id": "<uuid7>", "app_version": "1.3.0", "uuid": "<uuid7>"}]
}
```

- `machine_id`: first 32 hex chars of sha256(`beacon-machine:` + hardware UUID, trimmed, upper case);
  IOPlatformUUID on macOS, MachineGuid on Windows; `""` when the OS does not say. The Worker
  uses it as the person, so one machine running several apps is one person; else `install_id`.
- `sent_at`: client clock when the batch went out. PostHog shifts every `ts` by
  `sent_at - its own now`, so events kept offline land at the right time on a wrong clock.
- `context` is the machine; `model` is macOS only; screens are the main display in points.
  Apps may add their own keys.
- `app_version` on an event is the version that recorded it; the one sending may be newer.
- `uuid` lets the backend drop an event sent twice.
- At most 100 events per batch. `app` must be in the Worker's `APPS`.
- 204 accepted, 4xx rejected (the client drops the batch), anything else: retried later.
- A 0.1 batch (no `machine_id`, `sent_at`, per-event `uuid`/`app_version`) is still accepted.

### Events every client records

| Event | When | Props |
|---|---|---|
| `first_launch` | first run of a new install (not one migrating with an install id) | |
| `app_updated` | first run of a new version | `from` |
| `app_open` | start | |
| `daily_active` | once a day while running | |
| `heartbeat` | every 5 min while running | `duration_s`, `memory_peak_mb` |
| `$screen` | the app calls `screen(name)` | `$screen_name` |
| `app_quit` | the app calls flush before quit | `uptime_s` |
| `app_ready` | the app calls `ready()` when it is ready for the user | `launch_ms` (from process start) |
| `update_started`, `update_failed` | Updater with analytics | `to`, `kind`; `step`, `error` |
| `update_skipped` | Updater with analytics, `skip()` (not for a required release) | `to`, `kind` |
| `unclean_exit` | start, when the last run did not reach flush before quit | `version` |
| `$exception` | an error reported or not caught; a macOS crash report found on start | PostHog's `$exception_list`, `$exception_level`; crash reports add `crash_report`, `crashed_version`, `crashed_at` |

Sessions: a new `session_id` after 30 minutes without events (the machine slept) or after 24 hours.
Offline: an unsent heartbeat followed by another of the same session becomes one, with the
durations added; nothing a send has in flight is changed.

## Feature flags: `GET /v1/flags?app=<app>&id=<machine_id>`

```json
{"flags": {"new-editor": true, "theme": "dark", "kill-sync": false}, "payloads": {"theme": {"accent": "#ff6600"}}}
```

The Worker asks PostHog for this machine (person property `product` = the app) and answers each
flag's value (the variant, else true or false) and its JSON payload, if any. 400 for an app not in
`APPS` or no id, 502 when PostHog fails: the client keeps its last answer.

## Website proxy: `/ingest/...`

A pass-through to PostHog for posthog-js on the sites (`/ingest/static/*` to the assets host,
the rest to the ingest host), with the visitor's IP as `X-Forwarded-For` and no cookies. Not
rate limited.

## Between two builds of the same app

- Windows: an update starts the new exe while the old one is still exiting, with
  `--beacon-after <old pid>` appended to its arguments. The new build waits for that process to
  end before taking its single-instance lock (`update.wait_for_previous()`), and removes the flag
  from its arguments. macOS needs nothing: the relaunch waits for the old process itself.
- The files in the app's data folder (listed in `docs/privacy.md`) are read by the next version:
  their names and fields only grow.

## Vectors

| File | Checked by |
|---|---|
| `vectors/versions.json` | `is_newer` / `isNewer` |
| `vectors/manifest/` | manifest parsing: signature, key rotation, version, tampering, paused, rollout, min_version |
| `vectors/events.json` | the batch each client sends (field names), the Worker's validation and PostHog mapping, a 0.1 batch |
| `vectors/machine_id.json` | the machine id hash |
| `vectors/rollout.json` | the rollout bucket |
| `vectors/crash/` | a macOS crash report and the `$exception` made from it |
| `vectors/flags.json` | PostHog's flags answer, the Worker's simplified one, what clients read from it |

`vectors/generate.py` writes the signed manifests (RFC 8032 test keys).

Files under `vectors/` are compared byte for byte (`.gitattributes` keeps line endings as they are).
