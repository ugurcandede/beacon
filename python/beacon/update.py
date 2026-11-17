"""Self-update from a signed manifest published with every GitHub release.

Each release carries update.json -- the version and, per platform, the asset's
URL, size and SHA-256 -- and update.json.sig, its Ed25519 signature (see
beacon.release). check() fetches both through releases/latest/download, a plain
download rather than an API call, so GitHub's 60-an-hour API limit does not
apply, and believes the manifest only if the signature matches the public key
built into the app. install() downloads the asset, checks it against the
manifest's hash, puts it in place and starts the new build; the app then quits.

How it is put in place (install_kind()):

- "exe", Windows: the running exe is renamed to <exe>.old (Windows refuses to
  overwrite a running exe but allows renaming it) and the download takes its
  name. cleanup() removes the .old on the next launch.
- "brew", macOS, running from the bundle a Homebrew cask installed: `brew
  upgrade --cask`, so brew's own records stay right.
- "bundle", any other macOS .app: the zip is unpacked and swapped in for the
  running bundle.
- None, a source run: no install; show the release page.

Every step goes to <data_dir>/update.log, which is what to ask a user for when
an update fails on their machine. check() and install() block -- run them off
the UI thread (beacon.qt.QtUpdater does that for Qt apps).

The new exe on Windows starts while this one is still exiting; an app with a
single-instance guard calls wait_for_previous() before taking it
(beacon.system.single_instance does). skip() hides a release from automatic
checks; a check the user asked for (manual=True) still offers it.
"""

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import _ed25519, system

_TIMEOUT = 30  # per socket operation, so a slow download is fine and a stalled one is not
_WORK_PREFIX = "beacon-update-"
_AFTER = "--beacon-after"  # <pid>: the build that started this one, still exiting
_VERSION_DIR = re.compile(r"\d+(\.\d+)+")


@dataclass(frozen=True)
class Release:
    version: str
    url: str
    size: int
    sha256: str
    notes_url: str
    required: bool = False  # the running version is below the manifest's min_version: not to be skipped


class UpdateError(Exception):
    def __init__(self, step, detail):
        super().__init__(f"{step}: {detail}")
        self.step = step  # "check" | "withdrawn" | "download" | "verify" | "install" | "brew_behind"


def is_newer(candidate, current):
    """Numeric dot-separated comparison: 1.10.0 is newer than 1.9.3. Non-numeric
    parts ("dev") count as 0, so source runs are always offered an update."""

    def parts(text):
        return [int(p) if p.isdigit() else 0 for p in text.split(".")]

    a, b = parts(candidate), parts(current)
    width = max(len(a), len(b))
    return a + [0] * (width - len(a)) > b + [0] * (width - len(b))


def rollout_bucket(machine_id, version):
    """0-99, fixed per machine and release: a release with rollout N reaches the
    machines below N, and a different set each release."""
    return int(hashlib.sha256(f"{machine_id}:{version}".encode()).hexdigest()[:8], 16) % 100


def parse_manifest(data, signature, public_keys, current, machine_id=""):
    """The Release for this platform in a manifest, if its signature matches one
    of `public_keys` (the app's current and next key, for rotation), it is newer
    than `current`, not paused, and this machine is within its rollout."""
    keys = [public_keys] if isinstance(public_keys, bytes) else public_keys
    if not any(_ed25519.verify(key, data, signature) for key in keys):
        raise UpdateError("verify", "manifest signature does not match")
    manifest = json.loads(data)
    asset = manifest["assets"].get(system.platform_key())
    if asset is None or not is_newer(manifest["version"], current) or manifest.get("paused"):
        return None
    if rollout_bucket(machine_id, manifest["version"]) >= manifest.get("rollout", 100):
        return None
    required = "min_version" in manifest and is_newer(manifest["min_version"], current)
    return Release(manifest["version"], asset["url"], asset["size"], asset["sha256"], manifest["notes_url"], required)


frozen = system.frozen


def wait_for_previous(timeout=15):
    """When an update started this process, waits (up to `timeout` seconds) for
    the build that started it to exit, and takes the flag that said so out of
    sys.argv. Returns at once otherwise. Call it before a single-instance check:
    the old build may still hold the lock."""
    if _AFTER not in sys.argv[:-1]:
        return
    i = sys.argv.index(_AFTER)
    value = sys.argv[i + 1]
    del sys.argv[i:i + 2]
    if not value.isdigit():
        return  # not from the updater: nothing to wait for
    pid = int(value)
    deadline = time.monotonic() + timeout
    while _running(pid) and time.monotonic() < deadline:
        time.sleep(0.1)


def _running(pid):
    if sys.platform == "win32":
        import ctypes

        synchronize, still_active = 0x00100000, 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(synchronize | 0x1000, False, pid)  # | PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def app_path():
    """The program the user launched. Not sys.executable: in Nuitka's onefile that
    is the payload unpacked into a cache folder."""
    return Path(sys.argv[0]).resolve()


class Updater:
    def __init__(self, repo, version, public_key, data_dir, *, cask=None, relaunch_args=(), base_url=None,
                 analytics=None, channel="stable"):
        """`public_key` is the base64 key beacon.release keygen printed, or a list
        of them: the current and the next, so the signing key can change without
        a release no installed copy accepts. `cask`
        names the Homebrew cask, if there is one. `relaunch_args` go to the new
        build -- e.g. a flag telling it to wait for this instance to exit.
        `base_url` replaces GitHub's releases/latest/download/ -- for local tests.
        With `analytics`, installs are reported as update_started / update_failed;
        the new build reports app_updated itself. `channel` "beta" also reads
        update-beta.json and takes whichever release is newer; it can change at
        run time (a "get beta updates" switch)."""
        self._repo = repo
        self._base = base_url or f"https://github.com/{repo}/releases/latest/download/"
        self._version = version
        self._keys = [base64.b64decode(k) for k in ([public_key] if isinstance(public_key, (str, bytes)) else public_key)]
        self._data_dir = Path(data_dir)
        self._cask = cask
        self._relaunch_args = list(relaunch_args)
        self.log_path = self._data_dir / "update.log"
        self._analytics = analytics
        self.channel = channel

    def check(self, manual=False):
        """The newer Release for this platform, or None when up to date -- or,
        unless `manual` (the user asked) or the release is required, when the
        user skipped that version."""
        try:
            release = self._offered("update.json")
            if self.channel == "beta":
                beta = self._offered("update-beta.json", missing_ok=True)
                if beta and (release is None or is_newer(beta.version, release.version)):
                    release = beta
        except UpdateError as e:
            self._log(f"check failed: {e}")
            raise
        except Exception as e:  # a broken answer or an odd manifest: still a failed check, never a crash
            self._log(f"check failed: {e!r}")
            raise UpdateError("check", _reason(e)) from e
        self._log(f"check: running {self._version} ({self.channel}), offered "
                  f"{release.version + (' (required)' if release.required else '') if release else 'nothing'}")
        if release and not manual and not release.required and release.version == self._state().get("skipped"):
            self._log(f"check: {release.version} was skipped")
            return None
        return release

    def _offered(self, name, missing_ok=False):
        """The release a manifest offers this build. With `missing_ok`, a manifest
        that is not there (no beta out) offers nothing."""
        try:
            data = self._get(self._base + name)
        except urllib.error.HTTPError as e:
            if missing_ok and e.code == 404:
                return None
            raise
        signature = base64.b64decode(self._get(self._base + name + ".sig"))
        return parse_manifest(data, signature, self._keys, self._version, system.machine_id())

    def release_notes(self, version):
        """The GitHub release text of `version` (Markdown), for a "what's new"
        after an update (Analytics.updated_from), or None if it cannot be had."""
        url = f"https://api.github.com/repos/{self._repo}/releases/tags/v{version}"
        try:
            return json.loads(self._get(url)).get("body") or None
        except (OSError, ValueError) as e:
            self._log(f"release notes for {version}: {e!r}")
            return None

    def skip(self, release):
        """Automatic checks stop offering `release`; a newer one is offered again.
        A required release cannot be skipped: nothing changes."""
        if release.required:
            self._log(f"skip {release.version}: required, ignored")
            return
        self._log(f"skip {release.version}")
        self._save_state({**self._state(), "skipped": release.version})
        self._track("update_skipped", release, self.install_kind())

    def install_kind(self):
        if not frozen():
            return None
        if sys.platform == "win32":
            return "exe"
        if sys.platform == "darwin" and self._bundle():
            return "brew" if self._brew_prefix() else "bundle"
        return None

    def install(self, release, progress=None):
        """Downloads, verifies and installs `release`, then starts it. Returns once
        the new build is launching; the caller quits. `progress(done, total)` is
        called from this thread as bytes arrive. Asks the server first: a
        release paused or replaced since it was offered is not installed
        (UpdateError "withdrawn")."""
        kind = self.install_kind()
        self._log(f"install {release.version} as {kind} over {app_path()}")
        self._track("update_started", release, kind)
        try:
            offered = self.check(manual=True)
            if offered is None or offered.version != release.version:
                raise UpdateError("withdrawn", f"{release.version} is no longer offered")
            release = offered  # what the server signs now, not what the caller has held since
            if kind == "exe":
                self._install_exe(release, progress)
            elif kind == "bundle":
                self._install_bundle(release, progress)
            elif kind == "brew":
                self._install_brew(release)
            else:
                raise UpdateError("install", "this build cannot update itself")
        except UpdateError as e:
            self._log(f"install failed: {e}")
            self._track("update_failed", release, kind, e)
            raise
        except Exception as e:  # ditto failing, a broken download: still reported, never a crash
            self._log(f"install failed: {e!r}")
            error = UpdateError("install", _reason(e))
            self._track("update_failed", release, kind, error)
            raise error from e
        self._log("install done, relaunching")

    def cleanup(self):
        """Call at launch: removes what a previous update left behind."""
        if sys.platform == "win32" and frozen():
            old = app_path().with_name(app_path().name + ".old")
            try:
                old.unlink(missing_ok=True)
            except OSError:
                pass  # the old process may still be exiting; next launch
        for work in self._data_dir.glob(_WORK_PREFIX + "*"):
            shutil.rmtree(work, ignore_errors=True)
        for folder in self._old_unpack_folders():
            self._log(f"removing {folder.name}, the unpack folder of an older version")
            shutil.rmtree(folder, ignore_errors=True)  # one still running keeps its files; next launch

    def _old_unpack_folders(self):
        """A Nuitka onefile exe unpacks once per version into <cache>/<company>/<name>/<version>
        (beacon.build); the folders of the versions before this one."""
        if sys.platform != "win32" or not frozen():
            return []
        here = system._resource_root()
        if here.name != self._version:
            return []  # not the layout beacon.build makes: touch nothing
        # Older ones only: an old exe started by hand must not delete the installed newer version's.
        return [p for p in here.parent.iterdir() if p.is_dir() and _VERSION_DIR.fullmatch(p.name)
                and is_newer(self._version, p.name)]

    # ---- steps ---------------------------------------------------------------

    def _install_exe(self, release, progress):
        exe = app_path()
        new, old = exe.with_name(exe.name + ".new"), exe.with_name(exe.name + ".old")
        self._download(release, new, progress)
        old.unlink(missing_ok=True)
        os.rename(exe, old)
        self._log(f"renamed running exe to {old.name}")
        try:
            os.rename(new, exe)
        except OSError:
            os.rename(old, exe)
            raise
        self._log(f"new exe in place, starting {exe}")
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            subprocess.Popen(self._relaunch_command(exe), creationflags=flags, close_fds=True, env=_clean_environment())
        except OSError:
            # It will not start (blocked, damaged): the build that works goes back.
            self._log("the new exe did not start, putting the old one back")
            exe.unlink(missing_ok=True)
            os.rename(old, exe)
            raise

    def _relaunch_command(self, exe):
        # This process is still running when the new one starts: tell it to wait.
        return [str(exe), *self._relaunch_args, _AFTER, str(os.getpid())]

    def _install_bundle(self, release, progress):
        bundle = self._bundle()
        self._data_dir.mkdir(parents=True, exist_ok=True)
        work = Path(tempfile.mkdtemp(prefix=_WORK_PREFIX, dir=self._data_dir))
        archive = work / "update.zip"
        self._download(release, archive, progress)
        subprocess.run(["/usr/bin/ditto", "-x", "-k", str(archive), str(work / "new")], check=True)
        new = next((work / "new").glob("*.app"), None)
        if new is None:
            raise UpdateError("install", "no .app in the archive")
        # shutil.move renames within a volume and copies across volumes.
        old = work / "old.app"
        shutil.move(bundle, old)
        self._log(f"moved running bundle to {old}")
        try:
            shutil.move(new, bundle)
        except OSError:
            shutil.move(old, bundle)
            raise
        self._log(f"new bundle in place at {bundle}")
        self._relaunch_mac(bundle)

    def _install_brew(self, release):
        prefix, bundle = self._brew_prefix(), self._bundle()
        with open(self.log_path, "a", encoding="utf-8") as out:
            # brew refreshes its taps at most once a day on its own, so the cask
            # may still name the previous version.
            subprocess.run([f"{prefix}/bin/brew", "update", "--quiet"], stdout=out, stderr=out)
            result = subprocess.run([f"{prefix}/bin/brew", "upgrade", "--cask", self._cask], stdout=out, stderr=out)
        if result.returncode != 0:
            raise UpdateError("install", f"brew upgrade exited with {result.returncode}")
        # The cask is bumped a few minutes after the GitHub release; until then
        # brew succeeds without changing anything.
        if not Path(prefix, "Caskroom", self._cask, release.version).is_dir():
            raise UpdateError("brew_behind", f"the {self._cask} cask is not at {release.version} yet")
        self._relaunch_mac(bundle)

    def _download(self, release, dest, progress):
        digest, done = hashlib.sha256(), 0
        self._log(f"downloading {release.url} ({release.size} bytes)")
        try:
            with urllib.request.urlopen(release.url, timeout=_TIMEOUT, context=system.ssl_context()) as response, \
                    open(dest, "wb") as out:
                while chunk := response.read(1 << 16):
                    out.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if done > release.size:
                        break  # more than the signed manifest says: not that file, stop taking it
                    if progress:
                        progress(done, release.size)
        except OSError as e:
            Path(dest).unlink(missing_ok=True)
            self._log(f"download failed after {done} bytes: {e!r}")
            raise UpdateError("download", _reason(e)) from e
        if done != release.size or digest.hexdigest() != release.sha256:
            Path(dest).unlink(missing_ok=True)
            what = "sha256 mismatch" if done == release.size else f"size is not the manifest's {release.size}"
            raise UpdateError("verify", f"{what} after {done} bytes")
        self._log("download verified")

    # ---- helpers -------------------------------------------------------------

    def _bundle(self):
        # X.app/Contents/MacOS/X -> X.app
        bundle = app_path().parents[2]
        return bundle if bundle.suffix == ".app" else None

    def _brew_prefix(self):
        """The Homebrew prefix whose cask installed the bundle this process runs from."""
        if not self._cask:
            return None
        for prefix in ("/opt/homebrew", "/usr/local"):
            caskroom = Path(prefix, "Caskroom", self._cask)
            if not os.access(f"{prefix}/bin/brew", os.X_OK) or not caskroom.is_dir():
                continue
            if any(app.resolve() == self._bundle() for app in caskroom.glob("*/*.app")):
                return prefix
        return None

    def _relaunch_mac(self, bundle):
        # Reopen by path once this process is gone: another copy with the same
        # bundle id may be registered with LaunchServices.
        # The pid, the path and the app's arguments go in as arguments ($0, $1, the
        # rest), never into the script text: a quote in a folder name cannot break it.
        script = 'while kill -0 "$0" 2>/dev/null; do sleep 0.2; done; bundle=$1; shift; open "$bundle" --args "$@"'
        subprocess.Popen(["/bin/sh", "-c", script, str(os.getpid()), str(bundle), *map(str, self._relaunch_args)],
                         start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _track(self, name, release, kind, error=None):
        if self._analytics is None:
            return
        props = {"to": release.version, "kind": kind or "none"}
        if error:
            props.update(step=error.step, error=str(error)[:200])
        self._analytics.track(name, props)

    def _state(self):
        try:
            return json.loads((self._data_dir / "beacon-update.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self, state):
        self._data_dir.mkdir(parents=True, exist_ok=True)
        (self._data_dir / "beacon-update.json").write_text(json.dumps(state), encoding="utf-8")

    def _get(self, url):
        with urllib.request.urlopen(url, timeout=_TIMEOUT, context=system.ssl_context()) as response:
            return response.read()

    def _log(self, message):
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as out:
                out.write(f"{system.timestamp()} {message}\n")
        except OSError:
            pass


def _reason(error):
    """A short why for UpdateError: urllib wraps the cause ("[Errno 61]
    Connection refused") in noise. The full repr goes to the log."""
    reason = getattr(error, "reason", None) or getattr(error, "strerror", None) or str(error)
    return str(reason)


def _clean_environment():
    """This process's environment minus the NUITKA_* variables the onefile
    bootloader leaves for its own children. The new exe sits at the same path
    as this one; seeing them it would take itself for our child and run from
    our unpack folder instead of unpacking its own version."""
    return {k: v for k, v in os.environ.items() if not k.startswith("NUITKA_")}
