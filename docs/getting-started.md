# Getting started

Connecting an app takes four steps: keys, a `beacon.toml`, a few lines in the app, and a release
workflow. Function details are in [api.md](api.md).

## 1. Keys

```sh
python -m beacon.release keygen      # the signing key pair
python -m beacon.release keygen      # the next pair, kept offline for a key rotation
```

- The current private key goes into the app repo's `BEACON_SIGNING_KEY` secret.
- Both public keys go into the app (`Updater(..., [current, next])`). The current one also goes
  into `beacon.toml` as `public_key`, so a release signed with the wrong key fails.

macOS code signing is set up once for all apps:
`python -m beacon.release codesign-cert --name "..." --out DIR` prints the
`BEACON_CODESIGN_P12` and `BEACON_CODESIGN_PASSWORD` secrets. Every build signed with this
certificate is the same app to macOS, so permissions such as Accessibility survive updates.

## 2. beacon.toml

At the app's root. The build and the release workflow read it.

```toml
name = "MyApp"                       # bundle and file names
id = "com.example.myapp"             # bundle id
company = "example"                  # optional; part of the data folder and the Windows unpack folder
public_key = "<current public key>"
cask = "myapp"                       # optional: the Homebrew cask that installs it
data = ["assets/icon.png"]           # shipped files, read with beacon.system.resource(...)
version_file = "version.txt"         # optional: written with the version at build time

[python]                             # a Python app; a Swift app has [swift] product = "<executable>"
entry = "main.py"
plugins = ["pyside6"]

[macos]
icon = "assets/icon.png"             # .png or .icns
[macos.plist]                        # merged into Info.plist
NSBluetoothAlwaysUsageDescription = "..."

[windows]
icon = "assets/icon.ico"
include_modules = []                 # modules Nuitka cannot see, on this OS only
```

`python -m beacon.build --version 1.0.0` builds for the machine it runs on:
`<name>-windows-x64.exe` and `<name>-macos-arm64.zip` for Python, one universal
`<name>-macos.zip` for Swift.

The app's name must also be in the Worker's `APPS` (`worker/wrangler.jsonc`) and, for its own
dashboard, in `PRODUCTS` in `posthog/setup.py`.

## 3. In the app

### Python (PySide6)

```python
from beacon import log, qt, system
from beacon.analytics import Analytics
from beacon.flags import Flags
from beacon.update import Updater

data_dir = system.data_dir("MyApp", "example")
analytics = Analytics("myapp", VERSION, data_dir, require_consent=True)
log.setup("myapp", data_dir, analytics)          # log file, uncaught errors, crash reports

if not qt.single_instance("myapp", on_activate=window.show_and_raise):
    sys.exit(0)                                  # the running copy shows itself
analytics.start()
window.shown.connect(analytics.ready)            # launch time; before the consent prompt
qt.ask_consent(analytics, "MyApp", PRIVACY_URL, window)

updater = Updater("me/myapp", VERSION, [PUBLIC_KEY, NEXT_PUBLIC_KEY], data_dir,
                  cask="myapp", analytics=analytics)
updater.cleanup()
updates = qt.QtUpdater(updater)                  # runs off the UI thread, answers with signals
updates.found.connect(show_banner)               # a Release, or None
updates.installed.connect(app.quit)              # the new build is starting
updates.check()                                  # check(manual=True) from "Check for updates"
# banner: updates.install(release), updater.skip(release); no skip when release.required

flags = qt.QtFlags(Flags("myapp", data_dir))
flags.refreshed.connect(apply_flags)
flags.refresh()

analytics.track("export_done", {"format": "png"})
app.aboutToQuit.connect(analytics.flush_before_quit)
```

Also available: `analytics.screen(name)`, `log.report(exc)`, `log.diagnostics(...)` (a support
zip), `autostart.enable(label)`, `updater.channel = "beta"`, `updater.release_notes(version)` for a
"what's new" when `analytics.updated_from` is set.

### Swift (macOS)

```swift
// Package.swift
.package(url: "https://github.com/ugurcandede/beacon", from: "1.0.0")
.product(name: "Beacon", package: "beacon")
```

```swift
import Beacon

let dataDir = dataDirectory("myapp")
let analytics = Analytics(app: "myapp", version: version, dataDirectory: dataDir, requireConsent: true)
Log.setup(app: "myapp", dataDirectory: dataDir, analytics: analytics)
analytics.start()
analytics.ready()
// while analytics.consentNeeded: ConsentView(analytics: analytics, appName: "MyApp", privacyURL: url)

let updater = try Updater(repo: "me/myapp", version: version, publicKeys: [publicKey, nextPublicKey],
                          dataDirectory: dataDir, cask: "myapp", analytics: analytics)
updater.cleanup()
let updates = UpdateModel(updater: updater)      // in a view: UpdateBanner(model: updates)
await updates.check()

let flags = Flags(app: "myapp", dataDirectory: dataDir)
await flags.refresh()
// applicationWillTerminate: analytics.flushBeforeQuit()
```

Also available: `Permissions` (Accessibility, Input Monitoring, Microphone), `LaunchAtLogin`,
`Log.report(error)`, `Log.diagnostics(...)`, `updater.releaseNotes(for:)`.

## 4. Releasing

`.github/workflows/release.yml`; every push to main is a release:

```yaml
name: Release
on:
  push:
    branches: [main]
    paths-ignore: ["**/*.md", "docs/**"]
concurrency: { group: release, cancel-in-progress: false }

jobs:
  release:
    uses: ugurcandede/beacon/.github/workflows/app-release.yml@v1
    permissions: { contents: write }
    with:
      platforms: '["macos", "windows"]'          # Swift apps: the default, '["macos"]'
    secrets:
      signing-key: ${{ secrets.BEACON_SIGNING_KEY }}
      codesign-p12: ${{ secrets.BEACON_CODESIGN_P12 }}
      codesign-password: ${{ secrets.BEACON_CODESIGN_PASSWORD }}
      tap-token: ${{ secrets.HOMEBREW_TAP_TOKEN }}         # optional: bumps the cask
      annotation-key: ${{ secrets.POSTHOG_ANNOTATION_KEY }}  # optional: marks the release in PostHog
```

The version comes from the commit messages: `feat:` is a minor, `BREAKING:` a major, anything
else a patch. Other inputs: `rollout`, `min-version`, `channel: beta`, `path`, `pre-build`
([api.md](api.md#build-and-release)).

`.github/workflows/release-control.yml`, to pause or widen a release from GitHub:

```yaml
name: Release control
on:
  workflow_dispatch:
    inputs:
      action: { type: choice, options: [pause, resume, rollout, min-version] }
      value: { description: "percent for rollout, version for min-version", default: "" }
      channel: { type: choice, options: [stable, beta], default: stable }
permissions: { contents: write }
jobs:
  control:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: ugurcandede/beacon/.github/actions/control@v1
        with:
          action: ${{ inputs.action }}
          value: ${{ inputs.value }}
          channel: ${{ inputs.channel }}
          signing-key: ${{ secrets.BEACON_SIGNING_KEY }}
```

Before the first release, run through [the end-to-end checklist](testing.md#an-app-end-to-end)
on a test build.
