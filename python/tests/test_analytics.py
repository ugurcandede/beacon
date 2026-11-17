import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from beacon import analytics


@pytest.fixture
def server():
    received, status = [], [204]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(status[0])
            self.end_headers()

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/v1/events", received, status
    httpd.shutdown()


def test_flush_sends_queue_with_context(tmp_path, server):
    url, received, _ = server
    a = analytics.Analytics("lumea", "1.4.0", tmp_path, context={"qt": "6.8"}, endpoint=url)
    a.track("effect_changed", {"effect": "rainbow"})
    assert a.flush()
    [batch] = received
    assert batch["app"] == "lumea" and batch["install_id"] == a.install_id
    assert batch["context"]["app_version"] == "1.4.0" and batch["context"]["qt"] == "6.8"
    assert batch["events"][0]["name"] == "effect_changed"
    assert batch["events"][0]["props"] == {"effect": "rainbow"}
    assert not a.flush() or len(received) == 1  # nothing left to send


def test_queue_survives_restart_while_offline(tmp_path):
    a = analytics.Analytics("lumea", "1.4.0", tmp_path, endpoint="http://127.0.0.1:9/")
    a.track("x")
    assert not a.flush(timeout=1)
    b = analytics.Analytics("lumea", "1.4.0", tmp_path)
    assert b.install_id == a.install_id
    assert [e["name"] for e in b._state["queue"]] == ["x"]


def test_rejected_batch_is_dropped(tmp_path, server):
    url, received, status = server
    status[0] = 400
    a = analytics.Analytics("lumea", "1.4.0", tmp_path, endpoint=url)
    a.track("x")
    assert a.flush()
    assert a._state["queue"] == []


def test_disabling_clears_and_stops_recording(tmp_path):
    a = analytics.Analytics("lumea", "1.4.0", tmp_path)
    a.track("x")
    a.enabled = False
    a.track("y")
    assert a._state["queue"] == []


def test_session_id_is_uuid7():
    import uuid
    session = uuid.UUID(analytics._uuid7())
    assert session.version == 7 and session.variant == uuid.RFC_4122


def test_concurrent_flushes_send_each_event_once(tmp_path, server):
    url, received, _ = server
    a = analytics.Analytics("lumea", "1.4.0", tmp_path, endpoint=url)
    for i in range(5):
        a.track(f"e{i}")
    threads = [threading.Thread(target=a.flush) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    sent = [e["uuid"] for batch in received for e in batch["events"]]
    assert len(sent) == 5 and len(set(sent)) == 5


OFFLINE = "http://127.0.0.1:9/"  # start() begins sending; keep tests off the network


def names(a):
    return [e["name"] for e in a._state["queue"]]


def test_reports_first_launch_then_app_updated(tmp_path):
    first = analytics.Analytics("lumea", "1.0.0", tmp_path, endpoint=OFFLINE)
    first.start()
    assert names(first)[:2] == ["first_launch", "app_open"]
    first.flush_before_quit(timeout=0.1)  # a clean quit, or the next start reports unclean_exit
    with first._sending:  # no send in flight: disabling empties the whole queue
        first.enabled = False
        first.enabled = True
    updated = analytics.Analytics("lumea", "1.1.0", tmp_path, endpoint=OFFLINE)
    updated.start()
    # first's sender thread may have queued its daily_active in between.
    queue = [e for e in updated._state["queue"] if e["name"] != "daily_active"]
    assert [e["name"] for e in queue][:2] == ["app_updated", "app_open"]
    assert queue[0]["props"] == {"from": "1.0.0"}


def test_migrating_app_is_not_a_first_launch(tmp_path):
    a = analytics.Analytics("lumea", "1.0.0", tmp_path, install_id="existing-ga-id", endpoint=OFFLINE)
    a.start()
    assert names(a)[0] == "app_open" and a.install_id == "existing-ga-id"


def test_batch_has_the_protocol_fields(tmp_path, server):
    """protocol/vectors/events.json lists the fields every client sends."""
    fields = json.loads((Path(__file__).resolve().parents[2] / "protocol" / "vectors" / "events.json").read_text())
    url, received, _ = server
    a = analytics.Analytics("lumea", "1.4.0", tmp_path, endpoint=url)
    a.track("x")
    assert a.flush()
    [batch] = received
    assert sorted(batch) == sorted(fields["batch_fields"])
    assert sorted(batch["events"][0]) == sorted(fields["event_fields"])
    expected = fields["context_fields"] + (fields["context_fields_macos"] if batch["context"]["os"] == "macOS" else [])
    assert sorted(batch["context"]) == sorted(expected)


def test_screen_is_a_posthog_screen_view(tmp_path):
    a = analytics.Analytics("lumea", "1.0.0", tmp_path, endpoint=OFFLINE)
    a.screen("Settings", {"tab": "theme"})
    assert a._state["queue"][-1]["name"] == "$screen"
    assert a._state["queue"][-1]["props"] == {"$screen_name": "Settings", "tab": "theme"}


def test_quit_records_uptime_and_sends(tmp_path, server, monkeypatch):
    url, received, _ = server
    a = analytics.Analytics("lumea", "1.0.0", tmp_path, endpoint=url)
    monkeypatch.setattr(analytics.time, "monotonic", lambda: a._started + 90.4)
    assert a.flush_before_quit()
    [batch] = received
    assert batch["events"][-1]["name"] == "app_quit"
    assert batch["events"][-1]["props"] == {"uptime_s": 90}


def test_batch_says_when_it_was_sent_and_which_machine(tmp_path, server):
    url, received, _ = server
    a = analytics.Analytics("lumea", "1.0.0", tmp_path, endpoint=url)
    a.track("x")
    before = int(analytics.time.time() * 1000)
    assert a.flush()
    [batch] = received
    assert before <= batch["sent_at"] <= int(analytics.time.time() * 1000)
    assert batch["machine_id"] == analytics.system.machine_id()


def heartbeat(a):
    a.track("heartbeat", {"duration_s": 300})


def test_heartbeats_fold_while_unsent(tmp_path):
    a = analytics.Analytics("lumea", "1.0.0", tmp_path, endpoint=OFFLINE)
    heartbeat(a), heartbeat(a), heartbeat(a)
    beats = [e for e in a._state["queue"] if e["name"] == "heartbeat"]
    assert len(beats) == 1 and beats[0]["props"]["duration_s"] == 900


def test_heartbeat_after_another_event_starts_a_new_stretch(tmp_path):
    a = analytics.Analytics("lumea", "1.0.0", tmp_path, endpoint=OFFLINE)
    heartbeat(a), a.track("x"), heartbeat(a)
    assert names(a) == ["heartbeat", "x", "heartbeat"]


def test_heartbeat_being_sent_is_not_changed(tmp_path):
    a = analytics.Analytics("lumea", "1.0.0", tmp_path, endpoint=OFFLINE)
    heartbeat(a)
    a._in_flight = 1  # a flush has it in its batch
    heartbeat(a)
    assert [e["props"]["duration_s"] for e in a._state["queue"]] == [300, 300]


def test_disabling_drops_everything_also_the_batch_being_sent(tmp_path):
    a = analytics.Analytics("lumea", "1.0.0", tmp_path, endpoint=OFFLINE)
    a.track("sending"), a.track("waiting")
    a._in_flight = 1
    a.enabled = False
    assert names(a) == []  # privacy.md: turning statistics off deletes what was waiting


def test_heartbeats_of_another_session_do_not_fold(tmp_path, monkeypatch):
    a = analytics.Analytics("lumea", "1.0.0", tmp_path, endpoint=OFFLINE)
    heartbeat(a)
    now = analytics.time.time()
    monkeypatch.setattr(analytics.time, "time", lambda: now + analytics._SESSION_IDLE_SECONDS + 1)
    heartbeat(a)
    beats = a._state["queue"]
    assert len(beats) == 2 and beats[0]["session_id"] != beats[1]["session_id"]


@pytest.mark.parametrize("gap,new_session", [
    (60, False),
    (analytics._SESSION_IDLE_SECONDS + 1, True),  # the machine slept
])
def test_session_ends_after_idle_gap(tmp_path, monkeypatch, gap, new_session):
    a = analytics.Analytics("lumea", "1.0.0", tmp_path, endpoint=OFFLINE)
    a.track("x")
    now = analytics.time.time()
    monkeypatch.setattr(analytics.time, "time", lambda: now + gap)
    a.track("y")
    first, second = a._state["queue"][-2:]
    assert (first["session_id"] != second["session_id"]) is new_session


def test_session_ends_after_a_day_even_when_busy(tmp_path, monkeypatch):
    a = analytics.Analytics("lumea", "1.0.0", tmp_path, endpoint=OFFLINE)
    a.track("x")
    start, clock = analytics.time.time(), [0]
    monkeypatch.setattr(analytics.time, "time", lambda: start + clock[0])
    sessions = set()
    for minute in range(0, 25 * 60, 10):  # an event every 10 minutes for 25 hours
        clock[0] = minute * 60
        a._event("x", {})
        sessions.add(a._session)
    assert len(sessions) == 2


def test_running_app_sends_heartbeats(tmp_path, monkeypatch):
    monkeypatch.setattr(analytics, "_HEARTBEAT_SECONDS", 0.05)
    monkeypatch.setattr(analytics, "_FLUSH_SECONDS", 0.02)
    # Offline without the network: on Windows a refused connection takes ~2 s.
    monkeypatch.setattr(analytics.Analytics, "_send", lambda self, events, timeout: False)
    a = analytics.Analytics("lumea", "1.0.0", tmp_path, endpoint=OFFLINE)
    a.start()
    analytics.time.sleep(0.4)
    beats = [e for e in a._state["queue"] if e["name"] == "heartbeat"]
    # Offline: all of them folded into one stretch.
    assert len(beats) == 1 and beats[0]["props"]["duration_s"] >= 0.05 * 3


def test_first_run_takes_the_old_opt_out(tmp_path):
    a = analytics.Analytics("lumea", "1.0.0", tmp_path, install_id="old", enabled=False, endpoint=OFFLINE)
    a.start()
    assert not a.enabled and a._state["queue"] == []
    a.enabled = True  # the user turns it back on: later runs keep that
    again = analytics.Analytics("lumea", "1.0.0", tmp_path, install_id="old", enabled=False, endpoint=OFFLINE)
    assert again.enabled
