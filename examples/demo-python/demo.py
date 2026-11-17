"""beacon demo app (PySide6): shows its version, checks for an update and
installs it, and sends analytics events. examples/run-local.sh builds it twice
(1.0.0 and 1.1.0) to watch a self-update end to end.

Settings come from demo_config.json, which the run-local scripts write next to
this file and beacon.build ships as data (beacon.toml): version, public_key, base_url (where update.json is served),
endpoint (the Worker), auto_update (install without a click, for the script),
cask (the Homebrew cask it was installed with, if any).
"""

import json
import sys
import threading

from beacon import log, system
from beacon.analytics import Analytics
from beacon.update import UpdateError, Updater
from PySide6.QtCore import QObject, QStandardPaths, QTimer, Signal
from PySide6.QtWidgets import QApplication, QCheckBox, QLabel, QPushButton, QVBoxLayout, QWidget


def config():
    return json.loads(system.resource("demo_config.json").read_text())


class Bridge(QObject):
    """Carries results from worker threads back to the UI thread."""
    found = Signal(object)
    failed = Signal(str)
    installed = Signal()


class Window(QWidget):
    def __init__(self, cfg):
        super().__init__()
        # The run-local scripts give one under their work folder, so a test run leaves nothing behind.
        data_dir = cfg.get("data_dir") or QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
        self.analytics = Analytics("beacon-demo-py", cfg["version"], data_dir, endpoint=cfg["endpoint"])
        log.setup("beacon-demo-py", data_dir, self.analytics)
        self.analytics.start()
        self.updater = Updater("ugurcandede/beacon-demo", cfg["version"], cfg["public_key"], data_dir,
                               base_url=cfg["base_url"], cask=cfg.get("cask"), analytics=self.analytics)
        self.updater.cleanup()
        self.auto = cfg.get("auto_update", False)
        self.release = None

        self.bridge = Bridge()
        self.bridge.found.connect(self.on_found)
        self.bridge.failed.connect(lambda why: self.status.setText(f"Update failed ({why}).\nDetails: update.log"))
        self.bridge.installed.connect(QApplication.quit)

        self.setWindowTitle(f"beacon demo (Python) {cfg['version']}")
        self.status = QLabel(f"Version {cfg['version']} ({self.updater.install_kind() or 'source run'})")
        check = QPushButton("Check for update", clicked=self.check)
        self.install = QPushButton("Install", clicked=self.install_update, enabled=False)
        event = QPushButton("Send 'button_clicked' event",
                            clicked=lambda: self.analytics.track("button_clicked", {"button": "demo"}))
        stats = QCheckBox("Send anonymous usage stats", checked=self.analytics.enabled)
        stats.toggled.connect(lambda on: setattr(self.analytics, "enabled", on))
        layout = QVBoxLayout(self)
        for widget in (self.status, check, self.install, event, stats):
            layout.addWidget(widget)
        QTimer.singleShot(0, self.check)

    def check(self):
        self.status.setText("Checking…")
        self.run(lambda: self.bridge.found.emit(self.updater.check()))

    def on_found(self, release):
        self.release = release
        self.install.setEnabled(release is not None and self.updater.install_kind() is not None)
        self.status.setText(f"{release.version} is available" if release else "Up to date")
        if release and self.auto and self.install.isEnabled():
            self.install_update()

    def install_update(self):
        self.status.setText(f"Installing {self.release.version}…")

        def work():
            self.updater.install(self.release)
            self.bridge.installed.emit()
        self.run(work)

    def run(self, work):
        def guarded():
            try:
                work()
            except UpdateError as e:
                self.bridge.failed.emit(str(e))
        threading.Thread(target=guarded, daemon=True).start()


def main():
    app = QApplication(sys.argv)
    app.setOrganizationName("ugurcandede")
    app.setApplicationName("beacon-demo-py")
    window = Window(config())
    window.show()
    window.analytics.screen("Main")
    app.aboutToQuit.connect(window.analytics.flush_before_quit)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
