"""Behavior found missing by the 1.0.0 review: bad input, broken files and
unexpected errors must not break the app, leak a user name or undo a choice."""

import base64
import http.client
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from beacon import _ed25519, analytics, flags, log, release, update

OFFLINE = "http://127.0.0.1:9/"
SECRET = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
PUBLIC = base64.b64encode(_ed25519.public_key(SECRET)).decode()


def make(tmp_path, **kw):
    return analytics.Analytics("lumea", "1.0.0", tmp_path, endpoint=OFFLINE, **kw)


# ---- props that are not JSON ---------------------------------------------------------


def test_a_prop_that_is_not_json_is_sent_as_text_and_breaks_nothing(tmp_path):
    a = make(tmp_path)
    a.track("opened", {"file": Path("/tmp/x"), "when": {1, 2}.__class__, "ratio": float("nan"), "n": 3})
    a.track("next")  # the queue still works
    first, second = a._state["queue"]
    assert first["props"]["file"] == str(Path("/tmp/x")) and first["props"]["n"] == 3
    assert first["props"]["ratio"] == "nan"
    assert second["name"] == "next"
    json.loads((tmp_path / "beacon-analytics.json").read_text())  # and it was saved as valid JSON


# ---- a state file that cannot be trusted ---------------------------------------------


@pytest.mark.parametrize("content", ["null", "123", "[]", '{"queue": null}', '{"queue": "x", "enabled": "yes"}', '{"enab'])
def test_a_broken_state_file_does_not_break_the_app(tmp_path, content):
    (tmp_path / "beacon-analytics.json").write_text(content)
    a = make(tmp_path)
    a.track("x")  # does not raise
    assert isinstance(a._state["queue"], list)


@pytest.mark.parametrize("content", ["null", '{"enab'])
def test_a_broken_state_file_never_turns_statistics_on(tmp_path, content):
    """The user's choice was in that file: without it, nothing is sent until they choose again."""
    (tmp_path / "beacon-analytics.json").write_text(content)
    assert make(tmp_path).enabled is False
    asking = make(tmp_path, require_consent=True)
    assert asking.consent_needed  # an app that asks, asks again


@pytest.mark.skipif(sys.platform == "win32", reason="chmod")
def test_an_unreadable_state_file_is_left_alone_and_nothing_is_sent(tmp_path):
    path = tmp_path / "beacon-analytics.json"
    path.write_text(json.dumps({"install_id": "keep-me", "enabled": False, "queue": []}))
    path.chmod(0)
    try:
        a = make(tmp_path)
        assert a.enabled is False
        a.enabled = True  # even then: this run cannot know what it would overwrite
        a.track("x")
    finally:
        path.chmod(0o600)
    assert json.loads(path.read_text())["install_id"] == "keep-me"


@pytest.mark.parametrize("content", ["[]", "null", '{"flags": null, "payloads": 3}'])
def test_a_broken_flags_cache_reads_as_no_flags(tmp_path, content):
    (tmp_path / "beacon-flags.json").write_text(content)
    f = flags.Flags("lumea", tmp_path, endpoint=OFFLINE)
    assert f.enabled("x") is False and f.value("x", 1) == 1 and f.payload("x") is None


# ---- network errors that are not OSError ---------------------------------------------


def test_a_broken_http_answer_is_a_failed_send_not_an_exception(tmp_path, monkeypatch):
    a = make(tmp_path)
    a.track("x")

    def broken(*args, **kwargs):
        raise http.client.BadStatusLine("garbage")

    monkeypatch.setattr(analytics.urllib.request, "urlopen", broken)
    assert a.flush(timeout=1) is False
    assert len(a._state["queue"]) == 1  # kept for the next try
    monkeypatch.setattr(flags.urllib.request, "urlopen", broken)
    assert flags.Flags("lumea", tmp_path, endpoint=OFFLINE).refresh() is False


def test_a_flags_answer_that_is_not_an_object_is_a_failed_refresh(tmp_path, monkeypatch):
    class Answer:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"[1, 2]"

    monkeypatch.setattr(flags.urllib.request, "urlopen", lambda *a, **k: Answer())
    f = flags.Flags("lumea", tmp_path, endpoint=OFFLINE)
    assert f.refresh() is False and f.enabled("x") is False


# ---- the home folder in reports ------------------------------------------------------


@pytest.mark.parametrize("home, text, expected", [
    (r"C:\Users\Jane Doe", str(FileNotFoundError(2, "No such file", r"C:\Users\Jane Doe\Documents\x.txt")),
     r"[Errno 2] No such file: '~\\Documents\\x.txt'"),
    (r"C:\Users\Jane Doe", r"open c:/users/jane doe/x.txt failed", "open ~/x.txt failed"),
    (r"C:\Users\Jane Doe", r"at C:\Users\Jane Doe\app\main.py", r"at ~\app\main.py"),
    ("/Users/jane", "bad file /Users/jane/secret.txt", "bad file ~/secret.txt"),
    ("/Users/jane", "bad file /Users/janet/secret.txt", "bad file /Users/janet/secret.txt"),  # another user's folder
])
def test_the_home_folder_is_hidden_however_the_path_is_written(monkeypatch, home, text, expected):
    monkeypatch.setattr(log.os.path, "expanduser", lambda p: home)
    assert log._scrub(text) == expected


# ---- the uncaught-exception hook in an app that keeps running (Qt) --------------------


def test_the_exception_hook_does_not_send_on_the_callers_thread(tmp_path, monkeypatch):
    a = make(tmp_path)
    monkeypatch.setattr(log, "_analytics", a)
    monkeypatch.setattr(a, "flush", lambda *args, **kw: pytest.fail("the hook sent on the caller's thread"))
    monkeypatch.setattr(log, "_reported", {}, raising=False)
    try:
        raise ValueError("slot failed")
    except ValueError as e:
        log._uncaught(type(e), e, e.__traceback__)
    assert [e["name"] for e in a._state["queue"]] == ["$exception"]
    assert a._wake.is_set()  # the sender thread is asked to send it


def test_the_same_exception_again_and_again_is_reported_once(tmp_path, monkeypatch):
    a = make(tmp_path)
    monkeypatch.setattr(log, "_analytics", a)
    monkeypatch.setattr(log, "_reported", {}, raising=False)

    def tick():
        raise ValueError("timer slot failed")

    for _ in range(50):  # a slot on a timer
        try:
            tick()
        except ValueError as e:
            log._uncaught(type(e), e, e.__traceback__)
    try:
        raise KeyError("another")
    except KeyError as e:
        log._uncaught(type(e), e, e.__traceback__)
    assert len([e for e in a._state["queue"] if e["name"] == "$exception"]) == 2


# ---- a crash report this code does not understand -------------------------------------


@pytest.mark.parametrize("body", ["[1, 2]", '{"exception": null, "threads": [], "faultingThread": 0}',
                                  '{"faultingThread": 0, "threads": [{"frames": [{"imageIndex": "x"}]}], "usedImages": []}'])
def test_an_odd_crash_report_is_skipped_not_raised(tmp_path, body):
    reports, data = tmp_path / "reports", tmp_path / "data"
    reports.mkdir()
    a = make(data)
    log.report_crashes("App", data, a, reports)  # first look: marks the time
    time.sleep(0.05)
    (reports / "App-2026.ips").write_text('{"app_name": "App"}\n' + body)
    log.report_crashes("App", data, a, reports)  # does not raise


# ---- the updater and errors it did not expect ----------------------------------------


def updater(tmp_path, monkeypatch, get):
    u = update.Updater("me/app", "1.0.0", PUBLIC, tmp_path)
    monkeypatch.setattr(u, "_get", get)
    return u


@pytest.mark.parametrize("error", [http.client.IncompleteRead(b""), TypeError("odd manifest"), AttributeError("x")])
def test_check_turns_any_error_into_an_update_error(tmp_path, monkeypatch, error):
    def get(url):
        raise error

    u = updater(tmp_path, monkeypatch, get)
    with pytest.raises(update.UpdateError) as e:
        u.check()
    assert e.value.step == "check"
    assert "check failed" in u.log_path.read_text()


def test_install_turns_any_error_into_an_update_error_and_reports_it(tmp_path, monkeypatch):
    import subprocess

    class Recorder:
        events = []

        def track(self, name, props=None):
            self.events.append((name, props))

    u = update.Updater("me/app", "1.0.0", PUBLIC, tmp_path, analytics=Recorder())
    found = update.Release("2.0.0", "", 1, "", "")
    monkeypatch.setattr(u, "check", lambda manual=False: found)
    monkeypatch.setattr(u, "install_kind", lambda: "bundle")

    def ditto_failed(*args):
        raise subprocess.CalledProcessError(1, "ditto")

    monkeypatch.setattr(u, "_install_bundle", ditto_failed)
    with pytest.raises(update.UpdateError) as e:
        u.install(found)
    assert e.value.step == "install"
    assert [name for name, _ in Recorder.events] == ["update_started", "update_failed"]


def test_qt_updater_reports_an_unexpected_error_instead_of_going_quiet():
    QtCore = pytest.importorskip("PySide6.QtCore")
    from beacon import qt

    app = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])

    class Broken:
        def check(self, manual=False):
            raise RuntimeError("not an UpdateError")

    failed = []
    u = qt.QtUpdater(Broken())
    u.failed.connect(failed.append)
    u.check()
    deadline = time.monotonic() + 5
    while not failed and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert failed and "not an UpdateError" in failed[0]


# ---- release control signs only what was signed before --------------------------------


def published(tmp_path, monkeypatch):
    asset = tmp_path / "App.zip"
    asset.write_bytes(b"zip")
    monkeypatch.setenv("BEACON_SIGNING_KEY", base64.b64encode(SECRET).decode())
    out = tmp_path / "release"
    release.main(["manifest", "--repo", "me/app", "--tag", "v1.2.0", "--out", str(out), "--public-key", PUBLIC,
                  f"macos-arm64={asset}"])
    return out


def test_control_refuses_a_manifest_whose_signature_does_not_match(tmp_path, monkeypatch):
    out = published(tmp_path, monkeypatch)
    manifest = out / "update.json"
    before_sig = (out / "update.json.sig").read_text()
    manifest.write_text(manifest.read_text().replace("github.com/me/app", "evil.example/app"))
    with pytest.raises(SystemExit) as e:
        release.main(["control", str(out), "--pause", "--public-key", PUBLIC])
    assert "signature" in str(e.value)
    assert (out / "update.json.sig").read_text() == before_sig  # nothing was signed
    assert "paused" not in manifest.read_text()


def test_control_refuses_a_manifest_without_a_signature(tmp_path, monkeypatch):
    out = published(tmp_path, monkeypatch)
    (out / "update.json.sig").unlink()
    with pytest.raises(SystemExit):
        release.main(["control", str(out), "--pause"])
    assert not (out / "update.json.sig").exists()


def test_control_still_changes_a_properly_signed_manifest(tmp_path, monkeypatch):
    out = published(tmp_path, monkeypatch)
    release.main(["control", str(out), "--rollout", "30"])
    data = (out / "update.json").read_bytes()
    assert json.loads(data)["rollout"] == 30
    assert _ed25519.verify(_ed25519.public_key(SECRET), data, base64.b64decode((out / "update.json.sig").read_text()))


@pytest.mark.parametrize("key", ["", "c2hvcnQ=", "not base64!"])
def test_signing_refuses_a_key_that_is_not_a_signing_key(tmp_path, monkeypatch, key):
    asset = tmp_path / "App.zip"
    asset.write_bytes(b"zip")
    monkeypatch.setenv("BEACON_SIGNING_KEY", key)
    with pytest.raises(SystemExit) as e:
        release.main(["manifest", "--repo", "me/app", "--tag", "v1.0.0", "--out", str(tmp_path / "o"), f"macos-arm64={asset}"])
    assert "BEACON_SIGNING_KEY" in str(e.value)


# ---- turning statistics off ----------------------------------------------------------


def test_turning_statistics_off_drops_even_what_is_being_sent(tmp_path, monkeypatch):
    a = make(tmp_path)
    a.track("sending"), a.track("waiting")
    started, release_send = threading.Event(), threading.Event()

    def slow_send(events, timeout):
        started.set()
        release_send.wait(5)
        return False  # the network is down: the batch would stay queued

    monkeypatch.setattr(a, "_send", slow_send)
    sender = threading.Thread(target=a.flush)
    sender.start()
    assert started.wait(5)
    a.enabled = False  # while the batch is on its way
    release_send.set()
    sender.join(5)
    assert a._state["queue"] == []
    assert json.loads((tmp_path / "beacon-analytics.json").read_text())["queue"] == []
    sent = []
    monkeypatch.setattr(a, "_send", lambda events, timeout: sent.append(events) or True)
    a.flush()
    assert sent == []  # and nothing goes out while it is off


def test_events_recorded_after_turning_back_on_survive_the_old_send(tmp_path, monkeypatch):
    a = make(tmp_path)
    a.track("old")
    started, release_send = threading.Event(), threading.Event()

    def slow_send(events, timeout):
        started.set()
        release_send.wait(5)
        return True

    monkeypatch.setattr(a, "_send", slow_send)
    sender = threading.Thread(target=a.flush)
    sender.start()
    assert started.wait(5)
    a.enabled = False
    a.enabled = True
    a.track("new")
    release_send.set()
    sender.join(5)
    assert [e["name"] for e in a._state["queue"]] == ["new"]  # the finished send did not delete it


# ---- which answers drop a batch ------------------------------------------------------


@pytest.mark.parametrize("status, kept", [(400, 0), (413, 0), (422, 0), (403, 3), (407, 3), (408, 3), (429, 3), (500, 3), (503, 3)])
def test_only_an_answer_about_the_batch_itself_drops_it(tmp_path, monkeypatch, status, kept):
    a = make(tmp_path)
    for name in "abc":
        a.track(name)

    def answer(request, **kwargs):
        raise analytics.urllib.error.HTTPError(request.full_url, status, "x", {}, None)

    monkeypatch.setattr(analytics.urllib.request, "urlopen", answer)
    a.flush(timeout=1)
    assert len(a._state["queue"]) == kept


# ---- feature flags and consent -------------------------------------------------------


def test_flags_are_not_asked_for_before_consent_or_when_statistics_are_off(tmp_path, monkeypatch):
    asked = []
    monkeypatch.setattr(flags.urllib.request, "urlopen", lambda url, **k: asked.append(url) or (_ for _ in ()).throw(OSError("x")))
    waiting = make(tmp_path / "a", require_consent=True)
    assert flags.Flags("lumea", tmp_path / "a", endpoint=OFFLINE, analytics=waiting).refresh() is False
    off = make(tmp_path / "b", enabled=False)
    assert flags.Flags("lumea", tmp_path / "b", endpoint=OFFLINE, analytics=off).refresh() is False
    assert asked == []
    flags.Flags("lumea", tmp_path / "c", endpoint=OFFLINE, analytics=make(tmp_path / "c")).refresh()
    flags.Flags("lumea", tmp_path / "d", endpoint=OFFLINE).refresh()  # an app without analytics still gets flags
    assert len(asked) == 2


def test_the_consent_prompt_says_what_is_sent():
    pytest.importorskip("PySide6.QtWidgets")
    from beacon import qt

    text = qt.CONSENT_TEXT.lower()
    for word in ("machine", "city", "error", "never"):
        assert word in text


# ---- the updater ---------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "darwin", reason="the macOS relaunch")
def test_the_relaunch_passes_paths_as_arguments_not_as_script_text(tmp_path, monkeypatch):
    import subprocess

    calls = []
    monkeypatch.setattr(update.subprocess, "Popen", lambda args, **kw: calls.append(args))
    u = update.Updater("me/app", "1.0.0", PUBLIC, tmp_path, relaunch_args=["--open", "it's.txt"])
    bundle = tmp_path / "Tom's Apps" / "Demo.app"
    u._relaunch_mac(bundle)
    [args] = calls
    script = args[2]
    assert "Tom" not in script and "it's" not in script
    assert str(bundle) in args and "it's.txt" in args and "--open" in args
    monkeypatch.undo()  # the real Popen back
    assert subprocess.run(["/bin/sh", "-n", "-c", script]).returncode == 0  # the script itself parses
    # And run for real with a stand-in `open`: the arguments arrive whole.
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "open").write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$OUT"\n')
    (fake / "open").chmod(0o755)
    out = tmp_path / "args.txt"
    subprocess.run(["/bin/sh", "-c", script, "999999", str(bundle), "--open", "it's.txt"], check=True,
                   env={"PATH": f"{fake}:/usr/bin:/bin", "OUT": str(out)})
    assert out.read_text().splitlines() == [str(bundle), "--args", "--open", "it's.txt"]


def test_cleanup_only_removes_folders_of_older_versions(tmp_path, monkeypatch):
    here = tmp_path / "1.1.0"
    for name in ("1.0.0", "1.1.0", "2.0.0", "1.10.0", "notes"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(update.sys, "platform", "win32")
    monkeypatch.setattr(update, "frozen", lambda: True)
    monkeypatch.setattr(update.system, "_resource_root", lambda: here)
    u = update.Updater("me/app", "1.1.0", PUBLIC, tmp_path / "data")
    assert [p.name for p in u._old_unpack_folders()] == ["1.0.0"]


def test_a_download_larger_than_the_manifest_said_is_stopped(tmp_path, monkeypatch):
    import hashlib

    asset = tmp_path / "asset"
    asset.write_bytes(b"x" * 300_000)
    monkeypatch.setattr(update.system, "ssl_context", lambda: None)
    u = update.Updater("me/app", "1.0.0", PUBLIC, tmp_path)
    lying = update.Release("2.0.0", asset.as_uri(), 10, hashlib.sha256(asset.read_bytes()).hexdigest(), "")
    with pytest.raises(update.UpdateError) as e:
        u._download(lying, tmp_path / "out", None)
    assert e.value.step == "verify" and not (tmp_path / "out").exists()
    short = update.Release("2.0.0", asset.as_uri(), 300_001, hashlib.sha256(asset.read_bytes()).hexdigest(), "")
    with pytest.raises(update.UpdateError):
        u._download(short, tmp_path / "out", None)


def test_install_uses_what_the_server_offers_now_not_what_the_caller_held(tmp_path, monkeypatch):
    u = update.Updater("me/app", "1.0.0", PUBLIC, tmp_path)
    held = update.Release("2.0.0", "https://old.example/a.zip", 1, "old", "")
    now = update.Release("2.0.0", "https://new.example/a.zip", 2, "new", "")
    installed = []
    monkeypatch.setattr(u, "check", lambda manual=False: now)
    monkeypatch.setattr(u, "install_kind", lambda: "bundle")
    monkeypatch.setattr(u, "_install_bundle", lambda release, progress: installed.append(release))
    u.install(held)
    assert installed == [now]


def test_a_new_exe_that_does_not_start_leaves_the_old_one_in_place(tmp_path, monkeypatch):
    exe = tmp_path / "App.exe"
    exe.write_text("old")
    monkeypatch.setattr(update, "app_path", lambda: exe)
    monkeypatch.setattr(update.subprocess, "DETACHED_PROCESS", 0, raising=False)
    monkeypatch.setattr(update.subprocess, "CREATE_NEW_PROCESS_GROUP", 0, raising=False)
    u = update.Updater("me/app", "1.0.0", PUBLIC, tmp_path / "data")
    monkeypatch.setattr(u, "_download", lambda release, dest, progress: dest.write_text("new"))

    def blocked(*args, **kwargs):
        raise OSError("blocked by antivirus")

    monkeypatch.setattr(update.subprocess, "Popen", blocked)
    with pytest.raises(OSError):
        u._install_exe(update.Release("2.0.0", "", 1, "", ""), None)
    assert exe.read_text() == "old"
    assert not exe.with_name("App.exe.old").exists()


@pytest.mark.parametrize("argv", [["app", "--beacon-after", "x"], ["app", "--beacon-after", ""], ["app", "--beacon-after", "-5"]])
def test_a_bad_pid_after_the_update_flag_does_not_stop_the_app(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", list(argv))
    update.wait_for_previous(timeout=0.1)
    assert sys.argv == ["app"]


# ---- release tools -------------------------------------------------------------------


@pytest.mark.parametrize("subjects, expected", [
    (["feat(ui): a button"], "1.3.0"),
    (["fix(core): x", "feat(swift): y"], "1.3.0"),
    (["feat!: drop the old API"], "2.0.0"),
    (["refactor(api)!: rename"], "2.0.0"),
    (["BREAKING: x"], "2.0.0"),
    (["bump: x"], "2.0.0"),
    (["Bump actions/checkout from 6 to 7"], "1.2.4"),  # a dependency bot's subject
    (["build(deps): bump vitest"], "1.2.4"),
    (["feature flags doc"], "1.2.4"),
])
def test_next_version_reads_scoped_and_breaking_subjects(subjects, expected):
    assert release.next_version(["v1.2.3"], subjects) == expected


@pytest.mark.parametrize("heading", ["## 1.4.0.1", "## 1.4.0-rc1", "## 1.4.01"])
def test_check_version_wants_the_exact_changelog_heading(tmp_path, heading):
    (tmp_path / "python/beacon").mkdir(parents=True)
    (tmp_path / "swift/Sources/Beacon").mkdir(parents=True)
    (tmp_path / "python/beacon/__init__.py").write_text('__version__ = "1.4.0"\n')
    (tmp_path / "swift/Sources/Beacon/System.swift").write_text('static let libVersion = "1.4.0"\n')
    (tmp_path / "CHANGELOG.md").write_text(f"# Changelog\n\n{heading}\n\n- something\n")
    problems, _ = release.check_version("v1.4.0", tmp_path)
    assert problems and "CHANGELOG" in problems[0]
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\n## 1.4.0\n\n- something\n")
    assert release.check_version("v1.4.0", tmp_path) == ([], "- something")


def test_a_failed_certificate_export_leaves_no_private_key_behind(tmp_path, monkeypatch):
    import subprocess

    real = subprocess.run

    def run(args, **kwargs):
        if "pkcs12" in args and "-export" in args:
            raise subprocess.CalledProcessError(1, args)
        return real(args, **kwargs)

    monkeypatch.setattr(release.subprocess, "run", run)
    with pytest.raises(subprocess.CalledProcessError):
        release.codesign_cert("Test", tmp_path)
    assert not (tmp_path / "codesign.key").exists()


# ---- the machine ---------------------------------------------------------------------


def test_the_machine_id_is_read_once_per_process(monkeypatch):
    from beacon import system

    calls = []
    monkeypatch.setattr(system, "_hardware_uuid", lambda: calls.append(1) or "ABC")
    system.machine_id.cache_clear()
    try:
        assert system.machine_id() == system.machine_id() == system.hashed_machine_id("ABC")
        assert len(calls) == 1
    finally:
        system.machine_id.cache_clear()


@pytest.mark.parametrize("data, enabled", [(bytes([2] + [0] * 11), True), (bytes([6] + [0] * 11), True),
                                           (bytes([3] + [0] * 11), False), (bytes([7] + [0] * 11), False), (b"", True)])
def test_a_startup_entry_switched_off_in_task_manager_reads_as_off(data, enabled):
    from beacon import autostart

    assert autostart._approved(data) is enabled


def test_the_single_instance_server_is_per_user(monkeypatch):
    pytest.importorskip("PySide6.QtNetwork")
    from beacon import qt

    monkeypatch.setattr(qt.getpass, "getuser", lambda: "jane")
    assert qt._server_name("lumea") == "beacon-lumea-jane"


def test_flags_on_a_machine_without_an_id_use_the_install_id_or_do_not_ask(tmp_path, monkeypatch):
    asked = []

    def record(url, **kwargs):
        asked.append(url)
        raise OSError("offline")

    monkeypatch.setattr(flags.urllib.request, "urlopen", record)
    monkeypatch.setattr(flags.system, "machine_id", lambda: "")
    assert flags.Flags("lumea", tmp_path, endpoint=OFFLINE).refresh() is False
    assert asked == []  # nobody to ask for: the Worker would answer 400
    a = make(tmp_path, install_id="install-1")
    flags.Flags("lumea", tmp_path, endpoint=OFFLINE, analytics=a).refresh()
    assert asked and asked[0].endswith("id=install-1")  # the id its events are counted under


# ---- the next version, from the repository -------------------------------------------


def git_repo(tmp_path, monkeypatch):
    import subprocess

    def git(*args):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
                              cwd=tmp_path, check=True, capture_output=True, text=True).stdout.strip()

    git("init", "-q", "-b", "main")
    monkeypatch.chdir(tmp_path)
    return git


def next_version_output(capsys, *args):
    release.main(["next-version", *args])
    return dict(line.split("=") for line in capsys.readouterr().out.split())


def test_next_version_counts_every_commit_since_the_last_release_not_only_the_last_push(tmp_path, monkeypatch, capsys):
    git = git_repo(tmp_path, monkeypatch)
    git("commit", "-q", "--allow-empty", "-m", "fix: first")
    git("tag", "v1.2.3")
    git("commit", "-q", "--allow-empty", "-m", "feat: a feature whose release run failed")
    before = git("rev-parse", "HEAD")
    git("commit", "-q", "--allow-empty", "-m", "fix: the push that releases")
    # The push only holds the fix; the feature is unreleased all the same.
    assert next_version_output(capsys, "--since", before)["version"] == "1.3.0"


def test_next_version_is_the_same_again_when_this_commit_is_already_tagged(tmp_path, monkeypatch, capsys):
    """A re-run of a release whose tag exists must not make the next number for the same commit."""
    git = git_repo(tmp_path, monkeypatch)
    git("commit", "-q", "--allow-empty", "-m", "feat: x")
    git("tag", "v1.3.0")
    assert next_version_output(capsys) == {"tag": "v1.3.0", "version": "1.3.0"}
    git("commit", "-q", "--allow-empty", "-m", "fix: y")
    assert next_version_output(capsys)["version"] == "1.3.1"


def test_next_version_of_a_repository_without_tags(tmp_path, monkeypatch, capsys):
    git = git_repo(tmp_path, monkeypatch)
    git("commit", "-q", "--allow-empty", "-m", "feat: the first")
    assert next_version_output(capsys)["version"] == "0.1.0"


# ---- the local end-to-end script -----------------------------------------------------


@pytest.mark.skipif(sys.platform != "darwin", reason="run-local.sh is the macOS script")
def test_run_local_refuses_to_delete_a_folder_it_did_not_make(tmp_path):
    import subprocess

    script = Path(__file__).resolve().parents[2] / "examples/run-local.sh"
    precious = tmp_path / "Desktop"
    precious.mkdir()
    (precious / "thesis.docx").write_text("years of work")
    result = subprocess.run(["/bin/bash", str(script), str(precious), "/usr/bin/false"], capture_output=True, text=True)
    assert result.returncode != 0 and "refus" in (result.stderr + result.stdout).lower()
    assert (precious / "thesis.docx").read_text() == "years of work"
