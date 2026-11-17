import json
import logging
import os
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

STAMP = r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d[+-]\d\d:\d\d"

from beacon import analytics, log, system

OFFLINE = "http://127.0.0.1:9/"
PYTHON_DIR = str(Path(analytics.__file__).resolve().parents[1])


def boom():
    raise ValueError(f"bad file {os.path.expanduser('~')}/secret.txt")


def test_exception_props_are_posthogs_format_without_the_home_folder():
    try:
        boom()
    except ValueError as e:
        props = log.exception_props(e, handled=True, where="settings")
    [exc] = props["$exception_list"]
    assert exc["type"] == "ValueError"
    assert exc["value"] == "bad file ~/secret.txt"
    assert exc["mechanism"]["handled"] is True
    frames = exc["stacktrace"]["frames"]
    assert frames[-1]["function"] == "boom" and frames[-1]["in_app"]
    assert all(not f["filename"].startswith(os.path.expanduser("~")) for f in frames)
    assert props["$exception_level"] == "error" and props["where"] == "settings"


def test_report_sends_a_caught_exception(tmp_path, monkeypatch):
    a = analytics.Analytics("lumea", "1.0.0", tmp_path, endpoint=OFFLINE)
    monkeypatch.setattr(log, "_analytics", a)
    try:
        boom()
    except ValueError as e:
        log.report(e)
    assert a._state["queue"][-1]["name"] == "$exception"


def test_setup_writes_a_rotating_log_and_reports_uncaught_thread_errors(tmp_path, monkeypatch):
    a = analytics.Analytics("lumea", "1.0.0", tmp_path, endpoint=OFFLINE)
    root = logging.getLogger()
    handlers = list(root.handlers)
    hooks = sys.excepthook, threading.excepthook
    try:
        path = log.setup("lumea", tmp_path, a)
        logging.getLogger("lumea.ui").warning("hello")
        try:
            boom()
        except ValueError as e:
            log.report(e)
        t = threading.Thread(target=boom, name="worker")
        t.start(), t.join()
        for h in root.handlers:
            h.flush()
        text = path.read_text()
        assert "WARNING lumea.ui: hello" in text and "uncaught in worker" in text and "ValueError" in text
        assert f"INFO beacon: start lumea 1.0.0 on {system.context()['os']}" in text  # every launch
        assert re.match(STAMP + " INFO beacon: start", text)  # local time with its offset
        assert "ERROR beacon: reported ValueError" in text and "in boom" in text  # a caught error, with its stack
        [event] = [e for e in a._state["queue"] if e["name"] == "$exception" and e["props"].get("thread")]
        assert event["props"]["$exception_level"] == "fatal" and event["props"]["thread"] == "worker"
        assert isinstance(next(h for h in root.handlers if h not in handlers), logging.handlers.RotatingFileHandler)
    finally:
        for h in root.handlers[:]:
            if h not in handlers:
                root.removeHandler(h)
                h.close()
        sys.excepthook, threading.excepthook = hooks


def test_an_uncaught_main_thread_error_is_logged_reported_and_still_ends_the_app(tmp_path):
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {PYTHON_DIR!r})
        from beacon import analytics, log
        a = analytics.Analytics("lumea", "1.0.0", {str(tmp_path)!r}, endpoint={OFFLINE!r})
        log.setup("lumea", {str(tmp_path)!r}, a)
        raise RuntimeError("main thread")
    """)
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 1
    assert "uncaught in main thread" in (tmp_path / "lumea.log").read_text()
    queued = (tmp_path / "beacon-analytics.json").read_text()
    assert '"$exception"' in queued and "main thread" in queued


def test_a_killed_process_is_an_unclean_exit_next_time(tmp_path):
    # os._exit runs no handler, as a crash or a kill. (A real segfault would
    # make macOS show its "quit unexpectedly" dialog on every test run.)
    script = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {PYTHON_DIR!r})
        from beacon import analytics
        a = analytics.Analytics("lumea", "1.0.0", {str(tmp_path)!r}, endpoint={OFFLINE!r})
        a.start()
        os._exit(9)
    """)
    assert subprocess.run([sys.executable, "-c", script]).returncode == 9
    after = analytics.Analytics("lumea", "1.0.0", tmp_path, endpoint=OFFLINE)
    after.start()
    unclean = [e for e in after._state["queue"] if e["name"] == "unclean_exit"]
    assert len(unclean) == 1 and unclean[0]["props"] == {"version": "1.0.0"}


def test_native_crashes_go_to_the_crash_log(tmp_path):
    import faulthandler

    root, handlers, hooks = logging.getLogger(), list(logging.getLogger().handlers), (sys.excepthook, threading.excepthook)
    try:
        log.setup("lumea", tmp_path)
        assert faulthandler.is_enabled()
        assert log._fault_file.name == str(tmp_path / "lumea.crash.log")
    finally:
        faulthandler.disable()
        log._fault_file.close()
        for h in root.handlers[:]:
            if h not in handlers:
                root.removeHandler(h)
                h.close()
        sys.excepthook, threading.excepthook = hooks


def test_a_clean_quit_is_not_an_unclean_exit(tmp_path):
    a = analytics.Analytics("lumea", "1.0.0", tmp_path, endpoint=OFFLINE)
    a.start()
    a.flush_before_quit(timeout=0.1)
    again = analytics.Analytics("lumea", "1.0.0", tmp_path, endpoint=OFFLINE)
    again.start()
    assert not [e for e in again._state["queue"] if e["name"] == "unclean_exit"]


def test_update_log_lines_have_the_same_time_format(tmp_path):
    from beacon import update
    u = update.Updater("me/app", "1.0.0", "11qYAYKxCrfVS/7TyWQHOg7hcvPapiMlrwIaaPcHURo=", tmp_path)
    u.skip(update.Release("1.1.0", "", 0, "", ""))
    assert re.fullmatch(STAMP + " skip 1.1.0\n", u.log_path.read_text())


CRASH = Path(__file__).resolve().parents[2] / "protocol/vectors/crash"


@pytest.mark.parametrize("case", json.loads((CRASH / "cases.json").read_text())["cases"], ids=lambda c: c["file"])
def test_crash_report_props_match_the_vector(case):
    assert log.crash_report_props(CRASH / case["file"]) == case["expected"]


def test_crash_reports_are_sent_once_and_only_new_ones(tmp_path):
    reports, data = tmp_path / "reports", tmp_path / "data"
    reports.mkdir()
    a = analytics.Analytics("lumea", "1.0.0", data, endpoint=OFFLINE)
    old = reports / "TapLockApp-old.ips"
    shutil.copy(CRASH / "TapLockApp-2026-11-16-154836.ips", old)
    os.utime(old, (time.time() - 3600,) * 2)  # written before; file and wall clocks can differ by ms
    log.report_crashes("TapLockApp", data, a, reports)  # first look: only marks the time
    assert not a._state["queue"]
    time.sleep(0.05)
    shutil.copy(CRASH / "TapLockApp-2026-11-16-154836.ips", reports / "TapLockApp-new.ips")
    shutil.copy(CRASH / "TapLockApp-2026-11-16-154836.ips", reports / "OtherApp-new.ips")
    log.report_crashes("TapLockApp", data, a, reports)
    assert [e["name"] for e in a._state["queue"]] == ["$exception"]
    log.report_crashes("TapLockApp", data, a, reports)  # already sent
    assert len(a._state["queue"]) == 1
