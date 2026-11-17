# Operations

## Secrets

| Secret | Where | What |
|---|---|---|
| `BEACON_SIGNING_KEY` | each app repo | signs `update.json` (`beacon.release keygen`) |
| next signing key | offline | replaces the current one if it is lost or leaked |
| `BEACON_CODESIGN_P12`, `BEACON_CODESIGN_PASSWORD` | each macOS app repo | the code signing certificate, one for all apps |
| `HOMEBREW_TAP_TOKEN` | apps with a cask | push access to the tap repo only |
| `POSTHOG_ANNOTATION_KEY` | each app repo | a PostHog key with only `annotation:write` |
| `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` | this repo | deploys the Worker |
| `POSTHOG_KEY` | your shell, while running `posthog/setup.py` | a personal PostHog key; revoke after use |

The public signing keys and the PostHog project token in `worker/wrangler.jsonc` are not secret.

## Controlling a release

Installed apps read `update.json` from the latest GitHub release at launch and once a day, so
changing that file changes what every copy sees next.

From GitHub: the app's **Actions -> Release control** (see [getting-started.md](getting-started.md#4-releasing)).
By hand:

```sh
gh release download vX.Y.Z -R me/myapp -p 'update.json*' -D out
BEACON_SIGNING_KEY=... python -m beacon.release control out --pause    # or --resume, --rollout 50
gh release upload vX.Y.Z -R me/myapp out/update.json out/update.json.sig --clobber
```

- A release run that failed after publishing (the cask, the chart marker) can be re-run: it
  finishes that release and makes no other.
- **A bad release**: pause it. No one else gets it, not even a copy already showing the update;
  copies that installed it keep it. Then release a fix, or resume.
- **A staged release**: release with `rollout: "10"`, watch the "Güncellemeler" and
  "Hatalar ve sağlık" dashboards, then widen to 50 and 100.
- **A beta**: release with `channel: beta`. It becomes a prerelease, and its `update-beta.json`
  goes onto the latest stable release, where installed apps look.
- **A forced update**: `min-version: X.Y.Z` makes the release required for anything older.

## Rotating the signing key

1. Put the next private key in `BEACON_SIGNING_KEY` and the next public key in `beacon.toml`.
2. Release. Installed copies accept it because they already ship the next public key.
3. Make a new next pair, ship its public key, keep its private key offline.

## Code signing

One self-signed certificate signs every Mac build, so macOS keeps permissions such as
Accessibility across updates. Losing it means every app asks again for every permission: keep the
`.p12` and its password.

## The Worker

`https://beacon-api.ugurcan-dede.workers.dev`, deployed by CI from main.

- `APPS` in `worker/wrangler.jsonc` lists the app names it accepts; a new app goes there first.
- 60 requests a minute per IP, 1 MB per request.
- Locally: `cd worker && npx wrangler dev --port 8787 --var APPS:myapp-test`.
- Older clients must keep working; a breaking change is a new `/v2` route.

## PostHog

Project 323268, EU cloud. Everything in it is made by `posthog/setup.py`; change it there.

```sh
POSTHOG_KEY=phx_... python posthog/setup.py --check   # runs every query, changes nothing
POSTHOG_KEY=phx_... python posthog/setup.py
```

- A new app: add it to `PRODUCTS` in `setup.py` and run it.
- Test builds report as `<app>-test`, and test sites run on `localhost`; reports leave both out.
- Alerts (e-mail): errors, failed updates, and for each product in `LIVE` (`setup.py`: the ones
  whose released app is on beacon) a day without users. Add a product to `LIVE` when it ships.
- A run of `setup.py` replaces the tiles it made (by name) and leaves ones added by hand; it makes
  the new tiles before it removes the old.
- The limit on what PostHog takes in is set in PostHog (billing limits): the project token is
  public, so the Worker's rate limit only stops a client gone wrong.

## When an alert arrives

| Alert | Look at | Likely cause |
|---|---|---|
| Failed updates | "Hatalar ve sağlık" -> failed updates (step, error) | `verify`: wrong key or renamed asset; `download`: missing asset; `brew_behind`: the cask was not bumped; `withdrawn`: a paused release (expected) |
| More errors | Error tracking, by version | a new release: pause it |
| A product is silent | "Şu an", then the Worker | the Worker rejecting (`APPS`, rate limit), a release that cannot start |

Ask the user for the support zip, or for `update.log` and `<app>.log` from the app's data folder.
