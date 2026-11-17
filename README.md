<div align="center">
  <img src="docs/assets/icon.svg" alt="beacon" width="80">
  <h1>beacon</h1>
  <p>Self-update, releases, analytics and crash reports for desktop apps, in Python and Swift.</p>
  <p><sub>The foundation of my own apps: the source is here to read, the setup it describes is mine.</sub></p>
  <br>
  <a href="https://github.com/ugurcandede/beacon/releases/latest"><img src="https://img.shields.io/github/v/release/ugurcandede/beacon?label=version&style=flat-square" alt="Version"></a>
  <a href="https://github.com/ugurcandede/beacon/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/ugurcandede/beacon/ci.yml?branch=main&style=flat-square" alt="CI"></a>
  <br>
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/Swift-6.0-F05138?style=flat-square&logo=swift&logoColor=white" alt="Swift">
  <img src="https://img.shields.io/badge/macOS-13%2B-000?style=flat-square&logo=apple&logoColor=white" alt="macOS">
  <img src="https://img.shields.io/badge/Windows-10%2B-0078D4?style=flat-square" alt="Windows">
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-All%20rights%20reserved-lightgrey?style=flat-square" alt="License"></a>
</div>

---

## Features

| | Feature |
|---|---|
| ⬆️ | **Self-update** — signed manifests on GitHub Releases; Windows exe, macOS app, Homebrew |
| 🎚️ | Staged rollout, pause, beta channel, forced updates, signing key rotation |
| 📦 | **Builds** — one `beacon.toml`; Nuitka for Python, universal binaries for Swift, code signing |
| 🚀 | **Releases** — one reusable GitHub workflow; release control from the Actions tab |
| 📊 | **Analytics** — anonymous and opt-in, offline queue, one person per machine, PostHog |
| 🐞 | **Crash reports** — log files, uncaught errors and macOS crash reports in error tracking |
| 🚩 | Feature flags, start at login, single instance, macOS permissions |

---

## Install

```bash
pip install "beacon @ git+https://github.com/ugurcandede/beacon@v1.1.0#subdirectory=python"
```

```swift
.package(url: "https://github.com/ugurcandede/beacon", exact: "1.1.0")
```

Setting up an app takes a key pair, a `beacon.toml`, a few lines of code and a release workflow:
**[Getting started](docs/getting-started.md)**.

---

## Docs

| | |
|---|---|
| [Getting started](docs/getting-started.md) | connect an app, release it |
| [API](docs/api.md) | Python and Swift side by side |
| [Operations](docs/operations.md) | pause or widen a release, keys, the backend |
| [Privacy](docs/privacy.md) | what is collected |
| [Architecture](docs/architecture.md) | the parts and why |
| [Testing](docs/testing.md) | tests and CI |
| [Protocol](protocol/README.md) | formats shared by every client |

---

## License

All rights reserved. The source is public to read, not to use. See [LICENSE](LICENSE).
