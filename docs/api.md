# API

Python and Swift side by side; the two keep the same names on purpose. Wire formats:
[protocol/README.md](../protocol/README.md).

## Analytics

| | Python `beacon.analytics.Analytics` | Swift `Analytics` |
|---|---|---|
| Create | `Analytics(app, version, data_dir, *, context=None, install_id=None, enabled=None, endpoint=ENDPOINT, require_consent=False)` | `Analytics(app:version:dataDirectory:context:installID:enabled:endpoint:requireConsent:)` |
| Start | `start()` | `start()` |
| Event | `track(name, props=None)` | `track(_:_:)` |
| Screen | `screen(name, props=None)` | `screen(_:_:)` |
| Ready | `ready(props=None)` | `ready(_:)` |
| Consent | `consent_needed`, `set_consent(granted)` | `consentNeeded`, `setConsent(_:)` |
| Opt out | `enabled` | `enabled` |
| After an update | `updated_from` | `updatedFrom` |
| Quit | `flush_before_quit(timeout=2)` | `flushBeforeQuit(timeout:)` |

- `app` must be in the Worker's `APPS`.
- `install_id` and `enabled` are read on the first run only, to carry over an older analytics.
- `require_consent`: nothing is sent until the user answers; a "no" drops what waited. Prompts:
  `qt.ask_consent(analytics, app_name, privacy_url, parent)`, `ConsentView(analytics:appName:privacyURL:onAnswer:)`.
- `ready()` records the launch time; call it before a consent prompt.

## Updates

| | Python `beacon.update.Updater` | Swift `Updater` |
|---|---|---|
| Create | `Updater(repo, version, public_key, data_dir, *, cask=None, relaunch_args=(), base_url=None, analytics=None, channel="stable")` | `Updater(repo:version:publicKeys:dataDirectory:cask:baseURL:bundleURL:analytics:channel:)` |
| Check | `check(manual=False)` -> `Release` or None | `check(manual:) async throws -> Release?` |
| Install | `install(release, progress=None)`; the caller quits after | `install(_:) async throws` |
| Skip | `skip(release)` | `skip(_:)` |
| Install kind | `install_kind()`: "exe", "bundle", "brew" or None | `installKind`: `.bundle`, `.brew` or nil |
| Clean up | `cleanup()`, at launch | `cleanup()` |
| Channel | `channel`: "stable" or "beta" | `channel`: `.stable`, `.beta` |
| Release notes | `release_notes(version)` | `releaseNotes(for:)` |
| Log | `log_path` | `logURL` |

- `public_key`: one base64 key, or a list (the current and the next).
- `cask`: the Homebrew cask, if any; a brew install then updates through `brew upgrade`.
- `manual=True`: the user asked, so a skipped version is offered again.
- `Release`: `version`, `url`, `size`, `sha256`, `notes_url`, `required` (below the manifest's
  `min_version`; cannot be skipped).
- `install()` asks the server again first; a release paused or replaced since it was offered fails
  with step `withdrawn`.
- `UpdateError.step`: `check`, `withdrawn`, `download`, `verify`, `install`, `brew_behind`.
- Python: `update.wait_for_previous()` before a single-instance check, on Windows (the new exe
  starts while the old one exits).

UI helpers: Python `qt.QtUpdater(updater)` (signals `found`, `failed`, `progress`, `installed`);
Swift `UpdateModel(updater:canInstall:)` and `UpdateBanner(model:)`.

## Logs and errors

| | Python `beacon.log` | Swift `Log` |
|---|---|---|
| Set up | `setup(app, data_dir, analytics=None, level=INFO)` | `Log.setup(app:dataDirectory:analytics:)` |
| Write | the `logging` module | `Log.info`, `Log.warning`, `Log.error` |
| Report a caught error | `report(exc, handled=True, **props)` | `Log.report(_:handled:_:)` |
| Support zip | `diagnostics(app, data_dir, dest)` | `Log.diagnostics(app:dataDirectory:to:)` |

`setup` writes a line per launch, sends uncaught errors to error tracking, and on macOS sends the
crash reports the system wrote since the last launch. Python native crashes go to `<app>.crash.log`.

## Feature flags

| | Python `beacon.flags.Flags` | Swift `Flags` |
|---|---|---|
| Create | `Flags(app, data_dir, *, endpoint=ENDPOINT)` | `Flags(app:dataDirectory:endpoint:)` |
| Fetch | `refresh(timeout=5)` | `refresh(timeout:) async` |
| Read | `enabled(key)`, `value(key, default=None)`, `payload(key, default=None)` | `enabled(_:)`, `value(_:)`, `payload(_:)` |

The last answer is kept and used offline. Qt: `qt.QtFlags(flags)` refreshes off the UI thread and
emits `refreshed(ok)`.

## App lifecycle

| | Python | Swift |
|---|---|---|
| Data folder | `system.data_dir(app, company=None)` | `dataDirectory(_:)` |
| Single instance | `system.single_instance(name)`; Qt: `qt.single_instance(name, on_activate)` | (macOS opens one copy) |
| Start at login | `autostart.enable(label)`, `disable(label)`, `is_enabled(label)` | `LaunchAtLogin.isEnabled`, `LaunchAtLogin.set(_:)` |
| Shipped files | `system.resource(path)` | `Bundle.main` |
| Permissions | | `Permissions`: `accessibility`, `inputMonitoring`, `microphone`, `request...()`, `openSettings(_:)`, `waitFor(...)` |

## Build and release

Commands:

| Command | Does |
|---|---|
| `python -m beacon.build --version V [--config F] [--out DIR]` | builds for this machine; prints `PLATFORM=PATH` lines |
| `python -m beacon.release keygen` | a signing key pair |
| `python -m beacon.release manifest --repo R --tag T --out DIR [--rollout N] [--min-version V] [--channel beta] PLATFORM=PATH...` | the signed `update.json` (needs `BEACON_SIGNING_KEY`) |
| `python -m beacon.release control DIR [--pause \| --resume] [--rollout N] [--min-version V] [--channel beta]` | changes a published manifest and signs it again |
| `python -m beacon.release next-version [--since SHA]` | the next version from the commit messages |
| `python -m beacon.release codesign-cert --name N --out DIR` | the macOS code signing certificate |
| `python -m beacon.release cask FILE --version V --sha256 H` | points a cask at a release |
| `python -m beacon.release annotate --app A --version V` | marks a release in PostHog (needs `POSTHOG_ANNOTATION_KEY`) |

GitHub Actions, `ugurcandede/beacon/.github/actions/<name>@v1`:

| Action | Inputs |
|---|---|
| `version` | `since` -> outputs `tag`, `version` |
| `codesign` | `p12`, `password` |
| `release` | `tag`, `assets`, `signing-key`, `public-key`, `rollout`, `min-version`, `channel`, `out` |
| `control` | `action` (pause, resume, rollout, min-version), `value`, `channel`, `tag`, `signing-key` |
| `cask` | `cask`, `version`, `asset`, `token` |
| `annotate` | `app`, `version`, `key` |

`public-key` and `cask` default to `beacon.toml`.

Reusable workflow `ugurcandede/beacon/.github/workflows/app-release.yml@v1`:
inputs `path`, `platforms`, `python-version`, `requirements`, `pre-build`, `rollout`, `min-version`,
`channel`, `publish`; secrets `signing-key`, `codesign-p12`, `codesign-password`, `tap-token`,
`annotation-key`.
