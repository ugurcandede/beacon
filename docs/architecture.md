# Architecture

```
 app (Python / Swift, beacon library)
   ├── update.json + .sig  <──  GitHub Releases  <──  app-release workflow (build, sign, publish)
   ├── events, flags  ──>  Cloudflare Worker (beacon-api)  ──>  PostHog (EU)
   └── crash reports, logs (on the machine; sent as events or a support zip)

 website (posthog-js)  ──>  Worker /ingest  ──>  PostHog
```

## Choices

| Part | Choice | Why |
|---|---|---|
| Update hosting | GitHub Releases | already where builds go; `releases/latest/download/` has no API rate limit |
| Update signing | Ed25519; pure Python, CryptoKit in Swift | small keys, no dependency in the apps |
| Python builds | Nuitka | a Windows exe that unpacks once per version: fast starts, safe updates |
| Swift builds | SwiftPM, universal binary | one zip for both Mac architectures, no Xcode project |
| Code signing | one self-signed certificate | free; permissions survive updates (ad-hoc signing loses them) |
| Homebrew | casks in a tap | how the Mac apps are installed; brew-installed copies update through brew |
| Event relay | Cloudflare Worker | no server; adds location and drops the IP; keeps the PostHog key out of the apps |
| Analytics | PostHog, EU | product and web analytics, replay, error tracking and SQL in one place; set up as code |
| Releases | a reusable GitHub workflow and composite actions | every app releases the same way; a fix reaches all of them |
| Updates to the apps | Renovate | a PR in each app for each beacon release |
| Client dependencies | Python: `certifi`. Swift: system frameworks | nothing that grows the apps or needs its own updates |

## Compatibility

- The Worker keeps accepting what older clients send; a breaking change is a new route.
- Files in an app's data folder only gain fields, so any version can read them.
- beacon follows semver: anything that makes an app change its code is a major version.
