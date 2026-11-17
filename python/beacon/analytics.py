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

import http.client
import json
import logging
import math
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
_REJECTED = (400, 413, 422)  # answers that are about the batch itself


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
        self._epoch = 0  # goes up when the queue is dropped, so a send that was under way deletes nothing after
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
        # The file is there but cannot be read (locked, no permission): this run
        # does not know the user's choice, so it sends nothing and saves nothing.
        self._frozen = False
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            pass  # the first run
        except OSError as e:
            log.warning("analytics state unreadable, off for this run: %s", e)
            self._frozen = True
        else:
            self._new_install = False
            loaded = _valid_state(text)
            if loaded is None:
                # Broken: the user's choice was in it. Off until they choose again
                # (an app that asks for consent asks again).
                log.warning("analytics state broken, starting over with statistics off")
                self._state.update(enabled=False, consent=None)
            else:
                self._state.update(loaded)

    @property
    def install_id(self) -> str:
        return self._state["install_id"]

    @property
    def enabled(self) -> bool:
        return self._state["enabled"] and not self._frozen

    @enabled.setter
    def enabled(self, value: bool):
        if self._frozen:
            return
        with self._lock:
            self._state["enabled"] = bool(value)
            if not value:
                # Everything, also what a send has on its way: if that send fails,
                # nothing is left to go out later.
                self._state["queue"].clear()
                self._in_flight = 0
                self._epoch += 1
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
            self._append(self._event(name, _jsonable(props) if isinstance(props, dict) else {}))

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
        """Sends what is queued (nothing before the user's consent, when asked
        for, and nothing while turned off)."""
        if self.consent_needed or not self.enabled:
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
                epoch = self._epoch
            if not batch:
                return True
            sent = False
            try:
                sent = self._send(batch, timeout)
            finally:
                # One step under the lock: the batch is taken off and no longer
                # counts as in flight together, so nothing can change in between.
                with self._lock:
                    self._in_flight = 0
                    if sent and epoch == self._epoch:  # turned off meanwhile: the queue was dropped already
                        del self._state["queue"][:len(batch)]
                        self._save()
            if not sent or epoch != self._epoch:
                return False

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
            # A batch the Worker says is wrong (400, too large 413, 422) would be
            # wrong forever: dropped, or it blocks everything behind it. Any other
            # answer (a proxy's 403 or 407, a 429, a 5xx) says nothing about the
            # batch: it waits and goes again.
            log.debug("analytics batch not accepted: %s", e)
            return e.code in _REJECTED
        except (OSError, http.client.HTTPException) as e:  # a broken answer is not an OSError
            log.debug("analytics flush failed: %s", e)
            return False

    def _save(self):
        if self._frozen:
            return
        tmp = self._path.with_suffix(".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self._state), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as e:
            log.debug("analytics state not saved: %s", e)


def _jsonable(value):
    """`value` as JSON can carry it: what is not JSON (a Path, a date, NaN) goes as
    text, so one odd prop cannot break the queue."""
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return str(value)


def _valid_state(text):
    """The state file's content with fields of the wrong type left out, or None
    when it is not a state at all (cut short, or not an object)."""
    try:
        state = json.loads(text)
    except ValueError:
        return None
    if not isinstance(state, dict):
        return None
    types = {"install_id": str, "enabled": bool, "queue": list, "active_day": (str, type(None)),
             "consent": (bool, type(None)), "last_version": (str, type(None)), "running": bool}
    good = {k: v for k, v in state.items() if k not in types or isinstance(v, types[k])}
    if "queue" in good:
        good["queue"] = [e for e in good["queue"] if isinstance(e, dict) and isinstance(e.get("props"), dict)
                         and isinstance(e.get("name"), str)]
    if "enabled" in state and "enabled" not in good:
        good["enabled"] = False  # a choice that cannot be read is not a yes
    return good


def _uuid7():
    """A UUIDv7 (time-ordered), the form PostHog expects for $session_id.
    uuid.uuid7() only arrived in Python 3.14."""
    value = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    value = (value & ~(0xF << 76)) | (0x7 << 76)  # version
    value = (value & ~(0x3 << 62)) | (0x2 << 62)  # variant
    return str(uuid.UUID(int=value))
