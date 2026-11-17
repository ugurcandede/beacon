import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from beacon import flags, system

VECTOR = json.loads((Path(__file__).resolve().parents[2] / "protocol/vectors/flags.json").read_text())


@pytest.fixture
def server():
    state = {"status": 200, "body": VECTOR["worker"], "asked": []}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["asked"].append(self.path)
            self.send_response(state["status"])
            self.end_headers()
            if state["status"] == 200:
                self.wfile.write(json.dumps(state["body"]).encode())

        def log_message(self, *args):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}/v1/flags", state
    httpd.shutdown()


@pytest.mark.parametrize("case", VECTOR["client"], ids=lambda c: c["key"])
def test_flags_read_as_the_vector_says(tmp_path, server, case):
    url, _ = server
    f = flags.Flags("lumea", tmp_path, endpoint=url)
    assert f.refresh()
    assert f.enabled(case["key"]) is case["enabled"]
    assert f.value(case["key"]) == case["value"]
    assert f.payload(case["key"]) == case.get("payload")


def test_refresh_asks_for_this_machine_and_app(tmp_path, server):
    url, state = server
    flags.Flags("lumea", tmp_path, endpoint=url).refresh()
    assert state["asked"] == [f"/v1/flags?app=lumea&id={system.machine_id()}"]


def test_the_last_answer_is_kept_offline_and_on_errors(tmp_path, server):
    url, state = server
    assert flags.Flags("lumea", tmp_path, endpoint=url).refresh()
    state["status"] = 502
    f = flags.Flags("lumea", tmp_path, endpoint=url)  # a later launch
    assert not f.refresh()
    assert f.value("theme") == "dark"
    offline = flags.Flags("lumea", tmp_path, endpoint="http://127.0.0.1:9/v1/flags")
    assert not offline.refresh(timeout=1) and offline.enabled("new-editor")


def test_nothing_known_yet_means_defaults(tmp_path):
    f = flags.Flags("lumea", tmp_path, endpoint="http://127.0.0.1:9/v1/flags")
    assert not f.enabled("anything") and f.value("theme", "light") == "light" and f.payload("x", {}) == {}
