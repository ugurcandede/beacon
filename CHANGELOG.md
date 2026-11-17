# Changelog

beacon follows semver: a minor or patch release never needs a change in the apps; a major says
here what to change.

## 1.1.0

A full review of 1.0.0. The fixes below come with tests, most written first and seen to fail.

### Added

- `Flags(..., analytics=)`: flags are asked for only with the user's consent and while statistics are on.
- Actions `release`, `control`, `cask`, `annotate`: a `config` input for a beacon.toml outside the repository root.
- An app's own person properties (`$set` in an event's props) reach PostHog.

### Changed

- Python 3.11 or newer.
- Location is country, region and city only: no postal code, no coordinates.
- The consent prompts say what is sent, and "yes" is no longer the default button.
- The next version counts every commit since the last release and reads `feat(scope):` and `feat!:`.
- A batch is dropped only on 400, 413 or 422; any other answer keeps it for later.
- An automatic update check that fails says nothing (`UpdateModel`); only a check the user asked for does.

### Fixed

- **Crashes**: a prop that is not JSON (a date, a path, NaN) is sent as text instead of crashing
  the app (Swift) or breaking the queue (Python); writing a log line on a full disk no longer ends
  a Swift app; a broken state file, flags cache or crash report no longer stops an app from starting.
- **Privacy**: a broken or unreadable state file never turns statistics on; turning statistics
  off also drops the batch being sent; the user name is removed from Windows paths however they
  are written; privacy.md says that the text of errors is sent.
- **Updates**: `release control` refuses a manifest whose signature does not match, and an empty
  or malformed signing key; the relaunch survives a quote in the app's path; an install uses what
  the server offers at that moment and checks the size; a new exe that does not start is rolled
  back; cleanup leaves newer versions alone; `check()` and `install()` report every error.
- **Qt**: an exception in a slot no longer blocks the UI on a send, and is reported once a minute.
- **Swift**: `updatedFrom` is set when `start()` returns; a check cannot reset an install under
  way; an HTTP error during download is `download`, not `verify`; an uncaught exception is no
  longer recorded as a clean quit; the sources compile in Swift 6 mode.
- **Worker**: malformed input answers 400 instead of failing; a batch PostHog rejects is dropped
  by the client instead of retried forever; a flags answer PostHog could not compute keeps the
  client's last one; the proxy passes only what posthog-js needs; events only PostHog may send are dropped.
- **Releases**: a release run that failed after publishing can be re-run; only the release job can
  write; the major tag never moves back; the demo releases run on every push.
- **PostHog setup**: a run removes only the tiles it made, after the new ones exist; a product's
  "silent" alert is on once the product is in `LIVE`.
- The local end-to-end scripts refuse to wipe a folder they did not make.

## 1.0.0

### Added

- **Updates**: several trusted keys for key rotation; pause, staged rollout and forced updates
  (`min_version`) in the manifest; skip and manual checks; a beta channel; release notes for a
  "what's new"; install refuses a release withdrawn after it was offered.
- **Analytics**: one person per machine; consent before sending; heartbeats, sessions, launch time
  and memory; offline queue with correct timestamps; `unclean_exit`.
- **Logs and errors**: a rotating log file; uncaught errors and macOS crash reports in PostHog
  error tracking; a support zip.
- **Feature flags** through the Worker, cached for offline use.
- **App plumbing**: data folder, single instance, start at login, Qt helpers (Python); update
  banner, consent view, permissions, launch at login (Swift).
- **Builds**: `python -m beacon.build` from `beacon.toml`: Nuitka for Python, a universal binary for
  Swift, code signing with one certificate.
- **Releases**: a reusable app release workflow; actions for the version, code signing, the signed
  manifest, release control, the Homebrew cask and PostHog annotations.
- **Worker**: product and channel on every event, location without the IP, rate limits, feature
  flags, a posthog-js proxy for websites.
- **PostHog as code**: dashboards, alerts, test data filtered out.
- **Protocol**: shared formats and test vectors every client checks.

### Changed

- The Worker is `beacon-api`; clients default to `https://beacon-api.ugurcan-dede.workers.dev`.
- Every log line starts with the local time and its offset.
- `Package.swift` is at the repository root, so apps depend on the Swift package by URL.
- Python: `flush_before_quit()` on quit; `system.frozen()` replaces `update.frozen`.

### Removed

- PyInstaller support: Nuitka is the one Python build.

## 0.1.0

- First version: signed self-update (exe, .app, Homebrew) and analytics for Python and Swift.
