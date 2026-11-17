# beacon

Shared self-update, release signing, builds, analytics, logs and error reports for the owner's
apps (Python, Swift, later Kotlin).

- Formats every client and the Worker agree on: `protocol/README.md`.
- The public API, both languages: `docs/api.md`. How an app uses it: `docs/getting-started.md`.
- Running it (keys, secrets, releases, PostHog): `docs/operations.md`. What is collected: `docs/privacy.md`.
- Tests: `docs/testing.md`. The parts and why: `docs/architecture.md`.

## Rules

- A change to what clients send or read starts in `protocol/`: spec and `vectors/` first, then
  every language until `python`, `swift` and `worker` tests pass. Never change one client alone.
- Keep the clients' public API the same across languages (same names, same arguments); a change to
  it updates `docs/api.md` in the same commit.
- A change to what is sent or kept updates `docs/privacy.md`; a new secret or step to run, `docs/operations.md`.
- The Worker must keep accepting every batch older clients send; a breaking change is a new `/v2` route.
- Semver: anything that makes an app change its code is a major, written up in `CHANGELOG.md`.
- Add an `Unreleased` entry to `CHANGELOG.md` for every user-visible change.
- Every feature added or changed comes with its tests, in every language it touches.
- No test may crash a process for real (a segfault shows macOS's crash dialog); simulate with os._exit.
- No test may call a permission request (it shows a system prompt); test reading the status only.
- PostHog is changed through `posthog/setup.py`, never by hand; test data goes under a `-test` app name.
- Files in `protocol/vectors/` are compared byte for byte; the signed manifest must not be reformatted.
  Regenerate the signed ones with `protocol/vectors/generate.py` if they have to change.

## Tests

```sh
cd python && pytest -q
cd swift && swift test
cd worker && npm test && npx tsc -p .
cd posthog && pytest -q          # POSTHOG_KEY=... python setup.py --check runs every query
examples/run-local.sh            # macOS self-update end to end (INSTALL=brew for the Homebrew path)
```

CI is one staged pipeline, `.github/workflows/ci.yml`: fast tests, then heavy checks, then the
Worker deploy on main. Keep new jobs in the right stage.
