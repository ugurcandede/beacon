"""Anonymous usage events, sent in batches to the beacon Worker, which adds the
location Cloudflare sees and forwards them to the analytics backend.

Every batch carries the machine id (the same in every beacon app on the
machine), a random install id and the machine context (OS, version, arch,
locale, screens, plus whatever the app adds); events carry what the app passes
to track() -- never names, paths or content. Nothing is recorded while disabled.

Recorded on its own: first_launch / app_updated, app_open, daily_active, a
heartbeat every 5 minutes while the app runs (what "open now" is counted from),
app_quit with the uptime, from flush_before_quit(), and unclean_exit when the
last run ended without it (a crash, or the process was killed). ready() adds
app_ready with how long the launch took; heartbeats carry the peak memory.

With require_consent, nothing is sent until the user answered (set_consent);
until then events wait on the machine, and a "no" drops them.

Events wait in <data_dir>/beacon-analytics.json until the Worker accepts them,
so time offline loses nothing: each keeps the time it happened, and the batch
says when it was sent, so the backend can correct a wrong clock. While nothing
gets through, heartbeats fold into one event per stretch instead of piling up.
track() only appends; one daemon thread does the sending.
"""

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import date
from pathlib import Path

from . import system

log = logging.getLogger(__name__)

ENDPOINT = "https://beacon-api.ugurcan-dede.workers.dev/v1/events"
_FLUSH_SECONDS = 30
_HEARTBEAT_SECONDS = 300
_SESSION_IDLE_SECONDS = 30 * 60  # a gap this long (the machine slept) starts a new session
_SESSION_MAX_SECONDS = 24 * 3600
_BATCH = 100
_MAX_QUEUE = 1000


class Analytics:
    def __init__(self, app, version, data_dir, *, context=None, install_id=None, enabled=None, endpoint=ENDPOINT,
                 require_consent=False):
        """`install_id` and `enabled` are used only on the first run, so an app
        moving to beacon keeps the id its users were counted under and the
        choice of those who had turned analytics off (which also counts as their
        answer when `require_consent` is set)."""
        self._app = app
        self._endpoint = endpoint
        self._path = Path(data_dir) / "beacon-analytics.json"
        self._lock = threading.Lock()
        self._sending = threading.Lock()  # one flush at a time, or both send the same batch
        self._in_flight = 0  # events at the head of the queue a flush is sending: not to be touched
        self._wake = threading.Event()
        self._started = time.monotonic()
        self._session, self._session_start, self._last_event = _uuid7(), time.time(), time.time()
        self._machine_id = system.machine_id()
        self._context = {**system.context(), "app_version": version, **(context or {})}
        self._require_consent = require_consent
        self.updated_from = None  # the previous version, on the first run after an update ("what's new")
        self._state = {"install_id": install_id or str(uuid.uuid4()), "enabled": True if enabled is None else bool(enabled),
                       "queue": [], "active_day": None, "consent": None if enabled is None else bool(enabled)}
        # A new install, not an app that already counted this user before it moved to beacon.
        self._new_install = install_id is None
        try:
            self._state.update(json.loads(self._path.read_text(encoding="utf-8")))
            self._new_install = False
        except (OSError, ValueError):
            pass

    @property
    def install_id(self) -> str:
        return self._state["install_id"]

    @property
    def enabled(self) -> bool:
        return self._state["enabled"]

    @enabled.setter
    def enabled(self, value: bool):
        with self._lock:
            self._state["enabled"] = bool(value)
            if not value:
                del self._state["queue"][self._in_flight:]
            self._save()

    @property
    def consent_needed(self) -> bool:
        """True until the user answered, when the app asks first (require_consent)."""
        return self._require_consent and self._state.get("consent") is None

    def set_consent(self, granted: bool):
        """The user's answer: yes sends what waited, no drops it and turns analytics off."""
        with self._lock:
            self._state["consent"] = bool(granted)
        self.enabled = bool(granted)
        if granted:
            self._wake.set()

    def ready(self, props=None):
        """Call when the app is ready for the user: app_ready with how long the launch took."""
        started = system.process_start()
        extra = {"launch_ms": int((time.time() - started) * 1000)} if started else {}
        self.track("app_ready", {**extra, **(props or {})})

    def start(self):
        """Records app_open (and first_launch or app_updated) and starts the sender thread."""
        version, previous = self._context["app_version"], self._state.get("last_version")
        if self._state.get("running"):
            self.track("unclean_exit", {"version": previous or version})
        if self._new_install:
            self.track("first_launch")
        elif previous and previous != version:
            self.updated_from = previous
            self.track("app_updated", {"from": previous})
        with self._lock:
            self._state["last_version"] = version
            self._state["running"] = True
            self._save()
        self.track("app_open")
        threading.Thread(target=self._run, daemon=True, name="beacon-analytics").start()

    def track(self, name, props=None):
        if not self.enabled:
            return
        with self._lock:
            self._append(self._event(name, props))

    def screen(self, name, props=None):
        """A window, page or panel the user is looking at: PostHog's $screen."""
        self.track("$screen", {"$screen_name": name, **(props or {})})

    def flush_before_quit(self, timeout=2) -> bool:
        """Records app_quit with the uptime and sends what is queued. Call it on
        quit -- an app that ends with os._exit() skips atexit."""
        self.track("app_quit", {"uptime_s": int(time.monotonic() - self._started)})
        with self._lock:
            self._state["running"] = False
            self._save()
        return self.flush(timeout)

    def flush(self, timeout=10) -> bool:
        """Sends what is queued (nothing before the user's consent, when asked for)."""
        if self.consent_needed:
            return False
        if not self._sending.acquire(timeout=timeout):
            return False
        try:
            return self._flush(timeout)
        finally:
            self._sending.release()

    # ---- under self._lock ------------------------------------------------------

    def _event(self, name, props):
        now = time.time()
        if now - self._last_event > _SESSION_IDLE_SECONDS or now - self._session_start > _SESSION_MAX_SECONDS:
            self._session, self._session_start = _uuid7(), now
        self._last_event = now
        # The version goes with each event: what is queued before an update is
        # sent by the new version. uuid lets the backend drop an event sent
        # twice -- when the Worker took it but the answer never came back.
        return {"name": name, "props": props or {}, "ts": int(now * 1000), "session_id": self._session,
                "app_version": self._context["app_version"], "uuid": _uuid7()}

    def _append(self, event):
        queue = self._state["queue"]
        last = queue[-1] if len(queue) > self._in_flight else None
        if (event["name"] == "heartbeat" and last and last["name"] == "heartbeat"
                and last["session_id"] == event["session_id"]):
            # Not sent yet: the one waiting stretches to cover this one too.
            last["props"]["duration_s"] += event["props"]["duration_s"]
            if "memory_peak_mb" in event["props"]:
                last["props"]["memory_peak_mb"] = max(last["props"].get("memory_peak_mb", 0), event["props"]["memory_peak_mb"])
        elif len(queue) < _MAX_QUEUE:
            # Full means the Worker has been unreachable for a long time; the
            # oldest events are what gets sent first, so drop the new one.
            queue.append(event)
        else:
            return
        self._save()

    # ---- sender ------------------------------------------------------------------

    def _flush(self, timeout):
        while True:
            with self._lock:
                batch = [dict(e, props=dict(e["props"])) for e in self._state["queue"][:_BATCH]]
                self._in_flight = len(batch)
            if not batch:
                return True
            try:
                if not self._send(batch, timeout):
                    return False
            finally:
                with self._lock:
                    self._in_flight = 0
            with self._lock:
                del self._state["queue"][:len(batch)]
                self._save()

    def _run(self):
        next_heartbeat = time.monotonic() + _HEARTBEAT_SECONDS
        while True:
            # A tray app can run for weeks without being touched; this keeps it
            # counted as a daily active user.
            today = date.today().isoformat()
            if self._state["active_day"] != today:
                self.track("daily_active")
                with self._lock:
                    self._state["active_day"] = today
                    self._save()
            if time.monotonic() >= next_heartbeat:
                self.track("heartbeat", {"duration_s": _HEARTBEAT_SECONDS, "memory_peak_mb": system.memory_peak_mb()})
                next_heartbeat += _HEARTBEAT_SECONDS
                if next_heartbeat < time.monotonic():  # slept through some: no burst after waking
                    next_heartbeat = time.monotonic() + _HEARTBEAT_SECONDS
            self.flush()
            self._wake.wait(_FLUSH_SECONDS)

    def _send(self, events, timeout) -> bool:
        body = {"app": self._app, "install_id": self.install_id, "machine_id": self._machine_id,
                "sent_at": int(time.time() * 1000), "context": self._context, "events": events}
        request = urllib.request.Request(
            self._endpoint, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=system.ssl_context()):
                return True
        except urllib.error.HTTPError as e:
            # A batch the Worker rejects would be rejected forever; drop it
            # rather than let it block everything queued behind it.
            log.debug("analytics batch rejected: %s", e)
            return 400 <= e.code < 500
        except OSError as e:
            log.debug("analytics flush failed: %s", e)
            return False

    def _save(self):
        tmp = self._path.with_suffix(".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self._state), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as e:
            log.debug("analytics state not saved: %s", e)


def _uuid7():
    """A UUIDv7 (time-ordered), the form PostHog expects for $session_id.
    uuid.uuid7() only arrived in Python 3.14."""
    value = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    value = (value & ~(0xF << 76)) | (0x7 << 76)  # version
    value = (value & ~(0x3 << 62)) | (0x2 << 62)  # variant
    return str(uuid.UUID(int=value))
