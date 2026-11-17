"""The app's log file and its error reports.

    from beacon import log
    log.setup("lumea", data_dir, analytics)   # first thing in main()

- Logging goes to <data_dir>/<app>.log, rotated at 1 MB, three files kept: the
  file to ask a user for. A source run also logs to stderr.
- An exception nothing caught -- on the main thread or any other -- is logged
  and reported as PostHog's `$exception` (Error tracking), then the app ends as
  it would have.
- A native crash (segfault, abort) runs no Python; faulthandler writes its
  stack into <app>.crash.log, Analytics reports `unclean_exit` on the next
  start and, on macOS, the crash report the system wrote is sent as a fatal
  `$exception` (type, signal, the crashed thread's frames).

`report(exc)` sends a caught exception the same way. Paths in reports have the
home folder replaced by ~, so no user name leaves the machine.
"""

import faulthandler
import json
import logging
import logging.handlers
import os
import sys
import threading
import time
import traceback
from pathlib import Path

from . import system

_analytics = None
_fault_file = None
_MAX_FRAMES = 30


def setup(app, data_dir, analytics=None, level=logging.INFO):
    global _analytics, _fault_file
    _analytics = analytics
    path = Path(data_dir) / f"{app}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=1 << 20, backupCount=3, encoding="utf-8")
    handler.setFormatter(_Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
    if not system.frozen():
        root.addHandler(logging.StreamHandler())
    # A line per launch: a log is still worth sending when the app itself logs nothing.
    ctx = system.context()
    logging.getLogger("beacon").info("start %s %s on %s %s %s, beacon %s", app,
                                     analytics._context["app_version"] if analytics is not None else "?",
                                     ctx["os"], ctx["os_version"], ctx["arch"], ctx["lib_version"])
    # Kept open for the life of the process: faulthandler writes to it from a
    # signal handler, when nothing else can run.
    _fault_file = open(path.with_suffix(".crash.log"), "a", encoding="utf-8")
    faulthandler.enable(_fault_file)
    if analytics is not None and sys.platform == "darwin" and system.frozen():
        report_crashes(Path(sys.argv[0]).name, Path(data_dir), analytics)
    sys.excepthook = _excepthook
    threading.excepthook = lambda args: _uncaught(args.exc_type, args.exc_value, args.exc_traceback,
                                                  thread=args.thread.name if args.thread else None)
    return path


def diagnostics(app, data_dir, dest):
    """A zip for support, made when the user asks: the update log, the app's
    logs and the machine context, with the home folder replaced by ~. Returns its path."""
    import json
    import zipfile

    data_dir, dest = Path(data_dir), Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    path = dest / f"{app}-diagnostics-{time.strftime('%Y%m%d-%H%M%S')}.zip"
    names = ["update.log", f"{app}.crash.log", *(f"{app}.log" + suffix for suffix in ("", ".1", ".2", ".3"))]
    for handler in logging.getLogger().handlers:
        handler.flush()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in names:
            if (data_dir / name).is_file():
                z.writestr(name, _scrub((data_dir / name).read_text(encoding="utf-8", errors="replace")))
        from . import __version__
        z.writestr("system.json", json.dumps({**system.context(), "beacon": __version__,
                                              "platform": system.platform_key()}, indent=2))
    return path


def report(exc, handled=True, **props):
    """Logs `exc` and sends it to Error tracking; for an exception the app caught and carried on from."""
    logging.getLogger("beacon").error("reported %s: %s", type(exc).__name__, exc, exc_info=exc)
    if _analytics is not None:
        _analytics.track("$exception", exception_props(exc, handled, **props))


def exception_props(exc, handled=True, **props):
    """PostHog's $exception properties for `exc`, home folder hidden."""
    frames = traceback.extract_tb(exc.__traceback__)[-_MAX_FRAMES:]
    return {
        "$exception_list": [{
            "type": type(exc).__name__,
            "value": _scrub(str(exc)),
            "mechanism": {"handled": handled, "synthetic": False},
            "stacktrace": {"type": "raw", "frames": [
                {"filename": _scrub(f.filename), "lineno": f.lineno, "function": f.name, "platform": "python",
                 "in_app": "site-packages" not in f.filename and "lib/python" not in f.filename}
                for f in frames]},
        }],
        "$exception_level": "error" if handled else "fatal",
        **props,
    }


CRASH_REPORTS = Path.home() / "Library/Logs/DiagnosticReports"


def report_crashes(process, data_dir, analytics, reports=CRASH_REPORTS):
    """Sends the crash reports macOS wrote for `process` since the last look.
    The first time, only marks where to start: older crashes are not news."""
    mark = data_dir / "beacon-crash-check"
    try:
        since = float(mark.read_text())
    except (OSError, ValueError):
        since = None
    data_dir.mkdir(parents=True, exist_ok=True)
    mark.write_text(str(time.time()))
    if since is None:
        return
    for path in sorted(reports.glob(f"{process}-*.ips")):
        try:
            if path.stat().st_mtime > since:
                analytics.track("$exception", crash_report_props(path))
        except (OSError, ValueError, KeyError, IndexError) as e:
            logging.getLogger("beacon").warning("crash report %s unreadable: %s", path.name, e)


def crash_report_props(path):
    """PostHog's $exception properties for a macOS .ips crash report."""
    header, body = Path(path).read_text(encoding="utf-8").split("\n", 1)
    report = json.loads(body)
    images = [image.get("name", "") for image in report.get("usedImages", [])]
    thread = report["threads"][report["faultingThread"]]
    frames = []
    for frame in thread["frames"][:_MAX_FRAMES]:
        module = images[frame["imageIndex"]] if frame.get("imageIndex", -1) < len(images) else ""
        out = {"function": frame.get("symbol", "?"), "module": module, "in_app": module == report.get("procName"),
               "platform": "native"}
        if frame.get("sourceFile"):
            out.update(filename=frame["sourceFile"], lineno=frame.get("sourceLine"))
        frames.append(out)
    exception, termination = report.get("exception", {}), report.get("termination", {})
    kind = exception.get("type", "crash") + (f" ({exception['signal']})" if exception.get("signal") else "")
    return {
        "$exception_list": [{"type": kind, "value": termination.get("indicator") or exception.get("subtype")
                                                    or exception.get("signal", ""),
                             "mechanism": {"handled": False, "synthetic": False},
                             "stacktrace": {"type": "raw", "frames": frames}}],
        "$exception_level": "fatal",
        "crash_report": True,
        "crashed_version": report.get("bundleInfo", {}).get("CFBundleShortVersionString", ""),
        "crashed_at": report.get("captureTime", ""),
    }


class _Formatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        return system.timestamp(record.created)  # local time and offset, like update.log


def _scrub(text):
    home = os.path.expanduser("~")
    return text.replace(home, "~") if home and home != "~" else text


def _excepthook(exc_type, exc, tb):
    _uncaught(exc_type, exc, tb)


def _uncaught(exc_type, exc, tb, thread=None):
    if exc_type is SystemExit or exc_type is KeyboardInterrupt:
        return sys.__excepthook__(exc_type, exc, tb)
    logging.getLogger("beacon").critical("uncaught in %s", thread or "main thread", exc_info=(exc_type, exc, tb))
    if exc is not None and exc.__traceback__ is None:
        exc = exc.with_traceback(tb)
    if _analytics is not None and exc is not None:
        _analytics.track("$exception", exception_props(exc, handled=False, **({"thread": thread} if thread else {})))
        if thread is None:  # the main thread: the app is about to end
            _analytics.flush(timeout=2)
