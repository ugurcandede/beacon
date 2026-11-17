# Privacy

What an app on beacon sends, what it keeps on the machine, and what it never collects. Apps base
their privacy text on this page.

## Sent from the apps

Only with usage statistics on (an app can ask first and send nothing until the user agrees).
Events go through a Cloudflare Worker to PostHog (EU).

- **Who**: no account, name or e-mail. A machine id (a salted hash of the hardware UUID, which
  itself never leaves the machine) and a random id per install.
- **The machine**: OS and version, CPU architecture and count, memory, Mac model, screen size,
  locale, the app's and beacon's versions.
- **Where**: country, region and city as Cloudflare sees the request; no postal code, no
  coordinates. The IP address is dropped before PostHog.
- **What happens**: the app opening and quitting, how long it runs, updates, windows opened,
  launch time and memory use, and the app's own events.
- **Errors**: the type and the text of an error, and where in the code it happened (function and
  file names). The home folder is replaced by `~`, so the user name is not in it. An error's text
  can name what it was about, for example a file that could not be opened.
- **Never collected**: file contents, audio, key presses, account names or passwords. The one
  place a value from the app can show up is the text of an error.

Events wait on the machine while it is offline (at most 1000) and are sent later with the time
they happened. Turning statistics off deletes what was waiting.

Feature flags: the app asks the Worker which features are on for the machine id and the app,
only once the user has agreed to statistics and while they are on.

## Sent from the websites

posthog-js: page views, clicks (also clicks that do nothing), downloads and copied install
commands, session recordings with inputs masked, heatmaps, page speed, JavaScript errors and
console messages of the page, browser and device, referrer, location (IPs anonymized).
Recordings are kept 30 days.

## Kept on the machine

In the app's data folder (`~/Library/Application Support/...` on macOS, `%APPDATA%\...` on Windows):

| File | What |
|---|---|
| `beacon-analytics.json` | the install id, the user's choice, events not yet sent |
| `beacon-update.json` | a version the user skipped |
| `beacon-flags.json` | the last feature flags |
| `update.log`, `<app>.log`, `<app>.crash.log` | logs, for support; they stay on the machine |

A support zip leaves the machine only when the user makes it and sends it.

## Deletion

Data for one machine can be deleted in PostHog by its machine id. Nothing is shared beyond
Cloudflare (transport) and PostHog (storage).
