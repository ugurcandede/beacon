"""Feature flags and remote config from PostHog, through the beacon Worker.

    from beacon.flags import Flags
    flags = Flags("lumea", data_dir)
    flags.refresh()                      # at launch, off the UI thread; keeps the last answer offline
    if flags.enabled("new-editor"): ...
    theme = flags.value("theme", "light")          # a variant
    limits = flags.payload("sync", {"limit": 5})   # JSON set on the flag

A flag can be turned off in PostHog without a release, for every app or by
product (the person property `product`), or for a percentage of machines.
The answer is kept in <data_dir>/beacon-flags.json and used until the next
refresh, so a machine offline still gets the last known value.
"""

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import analytics, system

log = logging.getLogger(__name__)
ENDPOINT = analytics.ENDPOINT.replace("/v1/events", "/v1/flags")


class Flags:
    def __init__(self, app, data_dir, *, endpoint=ENDPOINT):
        self._app = app
        self._endpoint = endpoint
        self._path = Path(data_dir) / "beacon-flags.json"
        try:
            self._answer = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._answer = {"flags": {}, "payloads": {}}

    def refresh(self, timeout=5) -> bool:
        """Asks again; False (and the last answer kept) when it cannot."""
        query = urllib.parse.urlencode({"app": self._app, "id": system.machine_id()})
        try:
            with urllib.request.urlopen(f"{self._endpoint}?{query}", timeout=timeout,
                                        context=system.ssl_context()) as response:
                answer = json.loads(response.read())
        except (OSError, ValueError) as e:
            log.debug("flags not refreshed: %s", e)
            return False
        self._answer = {"flags": answer.get("flags", {}), "payloads": answer.get("payloads", {}),
                        "fetched_at": int(time.time())}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._answer), encoding="utf-8")
        except OSError as e:
            log.debug("flags not saved: %s", e)
        return True

    def enabled(self, key) -> bool:
        """On: true, or any variant."""
        return bool(self._answer["flags"].get(key, False))

    def value(self, key, default=None):
        """The variant, true or false; `default` for a flag PostHog does not know."""
        return self._answer["flags"].get(key, default)

    def payload(self, key, default=None):
        return self._answer["payloads"].get(key, default)
