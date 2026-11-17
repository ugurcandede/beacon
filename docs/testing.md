# Testing

```sh
cd python && pytest -q                 # Python
swift test                             # Swift (Package.swift at the root)
cd worker && npm test && npx tsc -p .  # Worker
cd posthog && pytest -q                # dashboards; POSTHOG_KEY=... python setup.py --check runs every query
examples/run-local.sh <work-dir>       # macOS self-update end to end (INSTALL=brew for Homebrew); wipes <work-dir>, so only an empty one or its own
```

The clients also check the shared vectors in `protocol/vectors/`, so a language that falls
behind the protocol fails its tests.

## CI

`.github/workflows/ci.yml`, on every push that changes more than docs:

1. **Tests**: Python (macOS, Windows), Swift (also type-checked in Swift 6 mode), Worker, dashboards.
2. **Release checks**: every release action as an app runs it; the reusable app release on both
   demos (built and signed, nothing published); the Windows
   self-update end to end.
3. **Deploy**: the Worker, from main.

## Rules

- Every change comes with tests, in every language it touches.
- A protocol change starts in `protocol/` and its vectors.
- No test crashes a process for real or asks for a permission (both show system dialogs).
- Tests stay off the network and do not sleep on timers.

## An app end to end

Before an app ships on beacon, run it once by hand as a separate test app:

1. Build it with a test `name` and `id` (`myapp-test`), so the real app's settings are untouched.
2. Point it at a local update server and `wrangler dev --var APPS:myapp-test`.
3. Build three versions, publish the second, install the first.

| Try | Expect |
|---|---|
| Open it | the consent prompt; events in PostHog |
| Skip the update, then "check for updates" | offered again |
| Update | relaunches as the new version; "what's new" |
| Publish below the machine's rollout bucket, then above | not offered, then offered |
| Pause it, then press update on the banner | refused (`withdrawn`) |
| A beta with `min-version` | offered on the beta channel, without "skip" |
| A release signed with the next key | installs |
| Send a test error, crash it, reopen | both in Error tracking; `unclean_exit` |
| Make a support zip | the logs, home folder as `~` |

Clean up: the test app, its data folder, preferences and crash reports. Leave the events in
PostHog; reports already leave `-test` apps out.

A website works the same way: serve a copy locally with PostHog's `api_host` set to the local
Worker's `/ingest`.
