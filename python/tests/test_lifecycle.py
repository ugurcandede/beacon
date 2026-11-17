"""Skip, manual checks, the relaunch handshake, unpack cleanup, data dir,
single instance, autostart and the Qt adapter."""

import base64
import os
import plistlib
import subprocess
import sys
import textwrap
import time
import uuid
from pathlib import Path

import pytest

from beacon import autostart, system, update

VECTORS = Path(__file__).resolve().parents[2] / "protocol/vectors/manifest"
KEY = base64.b64encode(bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")).decode()
PYTHON_DIR = str(Path(update.__file__).resolve().parents[1])


def updater(tmp_path, monkeypatch, version="1.1.9"):
    u = update.Updater("me/app", version, KEY, tmp_path)
    files = {"update.json": (VECTORS / "update.json").read_bytes(), "update.json.sig": (VECTORS / "update.json.sig").read_bytes()}
    monkeypatch.setattr(u, "_get", lambda url: files[url.rsplit("/", 1)[1]])
    return u


# ---- skip ---------------------------------------------------------------------------


def test_a_skipped_release_is_not_offered_again_automatically(tmp_path, monkeypatch):
    u = updater(tmp_path, monkeypatch)
    release = u.check()
    assert release.version == "1.2.0"
    u.skip(release)
    assert u.check() is None
    assert u.check(manual=True).version == "1.2.0"  # the user asked
    again = updater(tmp_path, monkeypatch)  # the next run remembers
    assert again.check() is None


def test_skipping_one_version_does_not_skip_the_next(tmp_path, monkeypatch):
    u = updater(tmp_path, monkeypatch)
    u.skip(update.Release("1.1.95", "", 0, "", ""))
    assert u.check().version == "1.2.0"


# ---- relaunch handshake -------------------------------------------------------------


def test_the_new_exe_is_told_which_process_to_wait_for(tmp_path):
    u = update.Updater("me/app", "1.0", KEY, tmp_path, relaunch_args=["--minimized"])
    assert u._relaunch_command(Path("App.exe")) == ["App.exe", "--minimized", "--beacon-after", str(os.getpid())]


def test_wait_for_previous_waits_for_the_old_process_and_cleans_argv(monkeypatch):
    old = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.6)"])
    monkeypatch.setattr(sys, "argv", ["app", "--minimized", "--beacon-after", str(old.pid)])
    start = time.monotonic()
    update.wait_for_previous(timeout=10)
    old.wait()
    assert time.monotonic() - start >= 0.4
    assert sys.argv == ["app", "--minimized"]


def test_wait_for_previous_returns_at_once_without_the_flag(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["app"])
    start = time.monotonic()
    update.wait_for_previous()
    assert time.monotonic() - start < 0.1


# ---- unpack folders -------------------------------------------------------------------


def test_cleanup_removes_older_versions_unpack_folders(tmp_path, monkeypatch):
    product = tmp_path / "cache/ugurcandede/Lumea"
    for name in ("1.3.0", "1.3.1", "1.4.0", "logs"):
        (product / name).mkdir(parents=True)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(update, "frozen", lambda: True)
    monkeypatch.setattr(system, "_resource_root", lambda: product / "1.4.0")
    update.Updater("me/app", "1.4.0", KEY, tmp_path / "data").cleanup()
    assert sorted(p.name for p in product.iterdir()) == ["1.4.0", "logs"]


def test_cleanup_leaves_folders_it_does_not_recognise(tmp_path, monkeypatch):
    folder = tmp_path / "somewhere/else"
    (folder.parent / "1.0.0").mkdir(parents=True)
    folder.mkdir()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(update, "frozen", lambda: True)
    monkeypatch.setattr(system, "_resource_root", lambda: folder)  # not <name>/<version>
    update.Updater("me/app", "1.4.0", KEY, tmp_path / "data").cleanup()
    assert (folder.parent / "1.0.0").is_dir()


# ---- data dir and single instance ------------------------------------------------------


@pytest.mark.parametrize("os_name,expected", [
    ("Darwin", "Library/Application Support/ugurcandede/Lumea"),
    ("Windows", "Roaming/ugurcandede/Lumea"),
])
def test_data_dir(monkeypatch, tmp_path, os_name, expected):
    monkeypatch.setattr(system.platform, "system", lambda: os_name)
    monkeypatch.setattr(system.Path, "home", lambda: tmp_path)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    assert system.data_dir("Lumea", "ugurcandede") == tmp_path / expected


def test_single_instance(tmp_path):
    name = f"test-{uuid.uuid4().hex[:8]}"
    holder = subprocess.Popen([sys.executable, "-c", textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {PYTHON_DIR!r})
        from beacon import system
        assert system.single_instance({name!r})
        print("held", flush=True)
        time.sleep(30)
    """)], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        assert not system.single_instance(name)
    finally:
        holder.kill()
        holder.wait()
    assert system.single_instance(name)  # the holder died; the OS let go of its lock


# ---- autostart ------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="LaunchAgents are macOS")
def test_autostart_launch_agent_opens_the_app(tmp_path):
    label = "com.example.test"
    app = tmp_path / "Lumea.app/Contents/MacOS/Lumea"
    assert not autostart.is_enabled(label, agents=tmp_path / "agents")
    autostart.enable(label, app, agents=tmp_path / "agents")
    assert autostart.is_enabled(label, agents=tmp_path / "agents")
    plist = plistlib.loads((tmp_path / "agents" / f"{label}.plist").read_bytes())
    assert plist["ProgramArguments"] == ["/usr/bin/open", "-a", str(tmp_path / "Lumea.app")] and plist["RunAtLoad"]
    autostart.disable(label, agents=tmp_path / "agents")
    assert not autostart.is_enabled(label, agents=tmp_path / "agents")


@pytest.mark.skipif(sys.platform != "win32", reason="the Run key is Windows")
def test_autostart_run_key():
    label = f"beacon-test-{uuid.uuid4().hex[:8]}"
    assert not autostart.is_enabled(label)  # also when the profile has no Run key yet
    autostart.disable(label)  # nothing to remove: no error
    try:
        autostart.enable(label, r"C:\Apps\Lumea.exe")
        assert autostart.is_enabled(label)
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, autostart.RUN_KEY) as key:
            assert winreg.QueryValueEx(key, label)[0] == r'"C:\Apps\Lumea.exe"'
    finally:
        autostart.disable(label)
    assert not autostart.is_enabled(label)


# ---- Qt -------------------------------------------------------------------------------


@pytest.fixture
def qt_app():
    QtCore = pytest.importorskip("PySide6.QtCore")
    return QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])


def wait_for(app, condition, timeout=5):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    return condition()


class FakeUpdater:
    def __init__(self, fail=None):
        self.fail, self.manual = fail, None

    def check(self, manual=False):
        self.manual = manual
        if self.fail:
            raise update.UpdateError("check", self.fail)
        return update.Release("2.0.0", "", 10, "", "")

    def install(self, release, progress):
        progress(5, 10)
        progress(10, 10)


def test_qt_updater_reports_through_signals(qt_app):
    from beacon import qt

    found, progress, installed = [], [], []
    u = qt.QtUpdater(FakeUpdater())
    u.found.connect(found.append)
    u.progress.connect(lambda done, total: progress.append((done, total)))
    u.installed.connect(lambda: installed.append(True))
    u.check(manual=True)
    assert wait_for(qt_app, lambda: found)
    assert found[0].version == "2.0.0" and u.updater.manual is True
    u.install(found[0])
    assert wait_for(qt_app, lambda: installed)
    assert progress == [(5, 10), (10, 10)]


def test_qt_updater_reports_failures(qt_app):
    from beacon import qt

    failed = []
    u = qt.QtUpdater(FakeUpdater(fail="offline"))
    u.failed.connect(failed.append)
    u.check()
    assert wait_for(qt_app, lambda: failed)
    assert failed == ["check: offline"]


def test_qt_flags_say_when_the_answer_is_in(qt_app):
    from beacon import qt

    class FakeFlags:
        def refresh(self):
            return True

    refreshed = []
    f = qt.QtFlags(FakeFlags())
    f.refreshed.connect(refreshed.append)
    f.refresh()
    assert wait_for(qt_app, lambda: refreshed)
    assert refreshed == [True]


def test_qt_single_instance_asks_the_running_copy_to_show_itself(qt_app):
    from beacon import qt

    name = f"test-{uuid.uuid4().hex[:8]}"
    shown = []
    assert qt.single_instance(name, on_activate=lambda: shown.append(True))
    # A second launch: here the same process stands in for it.
    assert not qt.single_instance(name)
    assert wait_for(qt_app, lambda: shown)


# ---- channels and forced updates ------------------------------------------------------

SECRET = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")


def served(tmp_path, monkeypatch, manifests, channel="stable", version="1.1.0"):
    """An Updater whose server has `manifests`: name -> manifest fields (signed here)."""
    from beacon import _ed25519, release
    import json

    asset = tmp_path / "App"
    asset.write_bytes(b"x")
    files = {}
    for name, fields in manifests.items():
        body = release.manifest("me/app", f"v{fields['version']}", {system.platform_key(): asset})
        body.update(fields)
        data = json.dumps(body).encode()
        files[name], files[name + ".sig"] = data, base64.b64encode(_ed25519.sign(SECRET, data))
    u = update.Updater("me/app", version, KEY, tmp_path, channel=channel)

    def get(url):
        name = url.rsplit("/", 1)[1]
        if name not in files:
            raise update.urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return files[name]

    monkeypatch.setattr(u, "_get", get)
    return u


def test_stable_channel_never_reads_the_beta_manifest(tmp_path, monkeypatch):
    u = served(tmp_path, monkeypatch, {"update.json": {"version": "1.2.0"}, "update-beta.json": {"version": "1.3.0"}})
    assert u.check().version == "1.2.0"


def test_beta_channel_takes_the_newer_release(tmp_path, monkeypatch):
    both = {"update.json": {"version": "1.2.0"}, "update-beta.json": {"version": "1.3.0"}}
    assert served(tmp_path, monkeypatch, both, channel="beta").check().version == "1.3.0"
    older_beta = {"update.json": {"version": "1.2.0"}, "update-beta.json": {"version": "1.1.5"}}
    assert served(tmp_path, monkeypatch, older_beta, channel="beta").check().version == "1.2.0"


def test_beta_channel_without_a_beta_out_gets_stable(tmp_path, monkeypatch):
    u = served(tmp_path, monkeypatch, {"update.json": {"version": "1.2.0"}}, channel="beta")
    assert u.check().version == "1.2.0"


def test_channel_can_change_at_run_time(tmp_path, monkeypatch):
    u = served(tmp_path, monkeypatch, {"update.json": {"version": "1.2.0"}, "update-beta.json": {"version": "1.3.0"}})
    assert u.check().version == "1.2.0"
    u.channel = "beta"  # the user turned on beta updates
    assert u.check().version == "1.3.0"


def test_a_required_release_cannot_be_skipped(tmp_path, monkeypatch):
    u = served(tmp_path, monkeypatch, {"update.json": {"version": "1.2.0", "min_version": "1.1.5"}}, version="1.1.0")
    release = u.check()
    assert release.required
    u.skip(release)
    assert u.check().version == "1.2.0"  # offered again, skip or not


def sub(tmp_path, name):
    (tmp_path / name).mkdir()
    return tmp_path / name


class Recorder:
    def __init__(self):
        self.events = []

    def track(self, name, props=None):
        self.events.append((name, props))


def test_skipping_is_reported_and_a_required_skip_is_not(tmp_path, monkeypatch):
    u = served(tmp_path, monkeypatch, {"update.json": {"version": "1.2.0"}})
    u._analytics = Recorder()
    u.skip(u.check())
    assert u._analytics.events == [("update_skipped", {"to": "1.2.0", "kind": "none"})]
    required = served(sub(tmp_path, "r"), monkeypatch, {"update.json": {"version": "1.2.0", "min_version": "1.1.5"}})
    required._analytics = Recorder()
    required.skip(required.check())
    assert required._analytics.events == []


@pytest.mark.parametrize("now", [
    {"version": "1.2.0", "paused": True},   # paused after it was offered
    {"version": "1.2.0", "rollout": 0},     # rolled back to nobody
    {"version": "1.2.1"},                   # replaced by a fix
])
def test_install_refuses_a_release_no_longer_offered(tmp_path, monkeypatch, now):
    u = served(tmp_path, monkeypatch, {"update.json": {"version": "1.2.0"}})
    offered = u.check()
    u._analytics = Recorder()
    later = served(sub(tmp_path, "later"), monkeypatch, {"update.json": now})
    monkeypatch.setattr(u, "_get", later._get)
    monkeypatch.setattr(u, "_install_bundle", lambda *a: pytest.fail("installed"))
    monkeypatch.setattr(u, "_install_exe", lambda *a: pytest.fail("installed"))
    with pytest.raises(update.UpdateError) as e:
        u.install(offered)
    assert e.value.step == "withdrawn"
    assert [name for name, _ in u._analytics.events] == ["update_started", "update_failed"]
    assert u._analytics.events[1][1]["step"] == "withdrawn"


# ---- consent, what's new, diagnostics, launch time ---------------------------------------

from beacon import analytics as analytics_module, log as log_module  # noqa: E402

OFFLINE = "http://127.0.0.1:9/"


def test_nothing_is_sent_before_consent_and_a_no_drops_it(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(analytics_module.Analytics, "_send", lambda self, events, timeout: sent.extend(events) or True)
    a = analytics_module.Analytics("lumea", "1.0.0", tmp_path, endpoint=OFFLINE, require_consent=True)
    assert a.consent_needed
    a.track("first_thing")
    assert a.flush() is False and not sent  # waits on the machine
    a.set_consent(True)
    assert not a.consent_needed and a.flush() and [e["name"] for e in sent] == ["first_thing"]
    b = analytics_module.Analytics("lumea", "1.0.0", tmp_path / "b", endpoint=OFFLINE, require_consent=True)
    b.track("x")
    b.set_consent(False)
    assert not b.enabled and b._state["queue"] == []
    again = analytics_module.Analytics("lumea", "1.0.0", tmp_path / "b", endpoint=OFFLINE, require_consent=True)
    assert not again.consent_needed  # asked once


def test_a_carried_over_choice_counts_as_the_answer(tmp_path):
    a = analytics_module.Analytics("lumea", "1.0.0", tmp_path, install_id="old", enabled=True,
                                   endpoint=OFFLINE, require_consent=True)
    assert not a.consent_needed and a.enabled


def test_updated_from_says_what_the_last_version_was(tmp_path):
    first = analytics_module.Analytics("lumea", "1.0.0", tmp_path, endpoint=OFFLINE)
    first.start()
    assert first.updated_from is None
    first.flush_before_quit(timeout=0.1)
    second = analytics_module.Analytics("lumea", "1.1.0", tmp_path, endpoint=OFFLINE)
    second.start()
    assert second.updated_from == "1.0.0"


def test_ready_reports_the_launch_time(tmp_path):
    a = analytics_module.Analytics("lumea", "1.0.0", tmp_path, endpoint=OFFLINE)
    a.ready({"window": "main"})
    event = a._state["queue"][-1]
    assert event["name"] == "app_ready" and event["props"]["window"] == "main"
    if system.process_start():  # macOS and Windows say; a test run is younger than an hour
        assert 0 < event["props"]["launch_ms"] < 3_600_000


def test_heartbeats_keep_the_highest_memory_when_folded(tmp_path):
    a = analytics_module.Analytics("lumea", "1.0.0", tmp_path, endpoint=OFFLINE)
    a.track("heartbeat", {"duration_s": 300, "memory_peak_mb": 80})
    a.track("heartbeat", {"duration_s": 300, "memory_peak_mb": 120})
    assert a._state["queue"][-1]["props"] == {"duration_s": 600, "memory_peak_mb": 120}


def test_release_notes_come_from_the_github_release(tmp_path, monkeypatch):
    u = update.Updater("me/app", "1.1.0", KEY, tmp_path)
    asked = []
    monkeypatch.setattr(u, "_get", lambda url: asked.append(url) or b'{"body": "- Faster startup"}')
    assert u.release_notes("1.1.0") == "- Faster startup"
    assert asked == ["https://api.github.com/repos/me/app/releases/tags/v1.1.0"]
    monkeypatch.setattr(u, "_get", lambda url: (_ for _ in ()).throw(OSError("offline")))
    assert u.release_notes("1.1.0") is None


def test_diagnostics_zip_has_the_logs_and_the_machine_without_the_home_folder(tmp_path):
    import zipfile

    data = tmp_path / "data"
    data.mkdir()
    (data / "update.log").write_text(f"check: from {Path.home()}/x\n")
    (data / "lumea.log").write_text("hello\n")
    path = log_module.diagnostics("lumea", data, tmp_path / "out")
    with zipfile.ZipFile(path) as z:
        assert sorted(z.namelist()) == ["lumea.log", "system.json", "update.log"]
        assert z.read("update.log").decode() == "check: from ~/x\n"
        assert b'"platform"' in z.read("system.json")


def test_qt_consent_prompt_is_not_shown_once_answered(tmp_path):
    pytest.importorskip("PySide6.QtWidgets")
    from beacon import qt

    a = analytics_module.Analytics("lumea", "1.0.0", tmp_path, endpoint=OFFLINE, require_consent=True)
    a.set_consent(True)
    assert qt.ask_consent(a, "Lumea", "https://example.com/privacy") is True  # no dialog: already answered
