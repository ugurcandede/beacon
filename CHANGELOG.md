# Changelog

beacon follows semver: a minor or patch release never needs a change in the apps; a major says
here what to change.

## Unreleased

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
