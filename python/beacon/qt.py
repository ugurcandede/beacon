"""beacon for Qt (PySide6) apps: the update and single-instance pieces as signals.

    from beacon import qt

    if not qt.single_instance("lumea", on_activate=window.show_and_raise):
        sys.exit(0)                      # the running copy was asked to show itself

    updates = qt.QtUpdater(updater)
    updates.found.connect(show_banner)   # a Release, or None when up to date
    updates.failed.connect(show_error)   # a short reason
    updates.progress.connect(set_bar)    # done, total bytes
    updates.installed.connect(app.quit)  # the new build is starting
    updates.check()                      # updates.check(manual=True) from "Check for updates"

    flags = qt.QtFlags(Flags("lumea", data_dir))
    flags.refreshed.connect(apply_flags) # read flags.flags again: the answer is in
    flags.refresh()

check() and install() run on a thread of their own; the signals arrive on the
thread the QtUpdater lives on (the UI thread), so slots can touch widgets and
the app needs no threads of its own.
"""

import getpass
import logging
import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QMessageBox

from . import system
from .update import UpdateError


class QtUpdater(QObject):
    found = Signal(object)  # Release or None
    failed = Signal(str)
    progress = Signal(int, int)
    installed = Signal()

    def __init__(self, updater, parent=None):
        super().__init__(parent)
        self.updater = updater

    def check(self, manual=False):
        self._run(lambda: self.found.emit(self.updater.check(manual=manual)))

    def install(self, release):
        def work():
            self.updater.install(release, lambda done, total: self.progress.emit(done, total))
            self.installed.emit()

        self._run(work)

    def _run(self, work):
        def guarded():
            try:
                work()
            except UpdateError as e:
                self.failed.emit(str(e))
            except Exception as e:  # the UI is waiting for an answer: it gets one
                self.failed.emit(f"unexpected: {e!r}")

        threading.Thread(target=guarded, daemon=True, name="beacon-update").start()


class QtFlags(QObject):
    """Flags.refresh() off the UI thread; `refreshed(ok)` arrives on it, the
    moment to read the flags again (the cached answer is there before it)."""
    refreshed = Signal(bool)

    def __init__(self, flags, parent=None):
        super().__init__(parent)
        self.flags = flags

    def refresh(self):
        threading.Thread(target=lambda: self.refreshed.emit(self.flags.refresh()), daemon=True,
                         name="beacon-flags").start()


def ask_consent(analytics, app_name, privacy_url, parent=None):
    """Asks once whether usage statistics may be sent (Analytics with
    require_consent); returns the answer. Does nothing once answered."""
    if not analytics.consent_needed:
        return analytics.enabled
    box = QMessageBox(parent)
    box.setWindowTitle(app_name)
    box.setText(f"Help improve {app_name}?")
    box.setInformativeText(
        f'{CONSENT_TEXT} <a href="{privacy_url}">What is collected</a>. You can change this in the settings at any time.')
    box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    box.setDefaultButton(QMessageBox.StandardButton.No)  # a stray Enter at startup is not a yes
    granted = box.exec() == QMessageBox.StandardButton.Yes
    analytics.set_consent(granted)
    return granted


CONSENT_TEXT = ("Send anonymous usage statistics and error reports: how the app is used and which version, "
                "an id for this machine, its model, OS and screen size, your country, region and city, "
                "and the text of errors. Never your files, what you type, or your name.")

_server = None


def _server_name(name):
    # Per user: the pipe is machine-wide on Windows, and two users may run the app.
    return f"beacon-{name}-{getpass.getuser()}"


def single_instance(name, on_activate=None):
    """True if this is the only running copy; then a second launch calls
    `on_activate` here (show and raise the window). False if another copy runs;
    it has been asked to show itself, and this one should exit."""
    global _server
    server_name = _server_name(name)
    if not system.single_instance(name):
        socket = QLocalSocket()
        socket.connectToServer(server_name)
        if socket.waitForConnected(1000):
            socket.write(b"activate")
            socket.waitForBytesWritten(1000)
            socket.disconnectFromServer()
        return False
    QLocalServer.removeServer(server_name)  # one left by a copy that crashed
    _server = QLocalServer()
    if on_activate is not None:
        def accept():
            while _server.hasPendingConnections():
                _server.nextPendingConnection().deleteLater()
                on_activate()

        _server.newConnection.connect(accept)
    if not _server.listen(server_name):
        # Still the only copy; a second launch just cannot ask it to show itself.
        logging.getLogger("beacon").warning("single instance: cannot listen on %s: %s", server_name, _server.errorString())
    return True
