# beacon

The shared foundation of my desktop apps: signed self-updates, builds and releases, usage
analytics, logs and crash reports, feature flags. One library for Python (PySide6) and Swift
(macOS), one release workflow, one backend.

## What an app gets

- **Self-update**: signed manifests on GitHub Releases; Windows exe, macOS .app and Homebrew
  installs; skip, beta channel, staged rollout, pause, forced updates, signing key rotation.
- **Builds and releases**: `beacon.toml` describes the app; one reusable workflow builds it
  (Nuitka for Python, a universal binary for Swift), signs it and publishes the release.
- **Analytics**: anonymous, opt-in events through a Cloudflare Worker to PostHog; works offline,
  counts a machine once across apps.
- **Errors**: a rotating log file, uncaught errors and macOS crash reports in PostHog's error
  tracking, a support bundle the user can send.
- **App plumbing**: data folder, single instance, start at login, permissions (macOS), feature flags.

## Quick start

```toml
# beacon.toml
name = "MyApp"
id = "com.example.myapp"
public_key = "<from: python -m beacon.release keygen>"

[python]
entry = "main.py"
plugins = ["pyside6"]
```

```python
from beacon import log, system
from beacon.analytics import Analytics
from beacon.update import Updater

data_dir = system.data_dir("MyApp")
analytics = Analytics("myapp", VERSION, data_dir, require_consent=True)
log.setup("myapp", data_dir, analytics)
analytics.start()

updater = Updater("me/myapp", VERSION, [PUBLIC_KEY, NEXT_PUBLIC_KEY], data_dir, analytics=analytics)
release = updater.check()          # a Release, or None
```

```yaml
# .github/workflows/release.yml: every push to main is a release
jobs:
  release:
    uses: ugurcandede/beacon/.github/workflows/app-release.yml@v1
    permissions: { contents: write }
    secrets: { signing-key: "${{ secrets.BEACON_SIGNING_KEY }}" }
```

The full setup, Swift included: [docs/getting-started.md](docs/getting-started.md).

## Docs

| | |
|---|---|
| [Getting started](docs/getting-started.md) | keys, `beacon.toml`, the code in the app, the release workflow |
| [API](docs/api.md) | every function, Python and Swift side by side; commands and actions |
| [Operations](docs/operations.md) | controlling a release, keys and secrets, the Worker, PostHog |
| [Privacy](docs/privacy.md) | what is collected and kept |
| [Architecture](docs/architecture.md) | the parts and why they were chosen |
| [Testing](docs/testing.md) | tests, CI, the end-to-end checklist |
| [Protocol](protocol/README.md) | the formats every client and the Worker share |

## Layout

```
python/      the Python library (beacon.update, .analytics, .log, .flags, .qt, .build, .release, ...)
swift/       the Swift package Beacon (macOS)
worker/      the Cloudflare Worker: events, feature flags, a proxy for posthog-js
protocol/    the shared formats and the test vectors every language checks
posthog/     the PostHog project as code: dashboards, alerts, filters
.github/     the release actions and the reusable app release workflow
examples/    a PySide6 and a SwiftUI demo, and scripts that watch them update themselves
```

## Status

Pre-1.0. Built and tested end to end on test builds of two apps; no app ships on it yet.

## License

All rights reserved. The source is public to read, not to use; see [LICENSE](LICENSE).
