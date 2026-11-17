"""Starting the app at login, for Python apps (Swift apps use SMAppService).

    from beacon import autostart
    autostart.enable("com.ugurcandede.lumea")    # the running build, at login
    autostart.is_enabled("com.ugurcandede.lumea")
    autostart.disable("com.ugurcandede.lumea")

Windows: a value under HKCU\\...\\CurrentVersion\\Run. macOS: a LaunchAgent in
~/Library/LaunchAgents that opens the .app. Both point at the build's path, so
an update in place keeps working; a moved app needs enable() again.
"""

import plistlib
import sys
from pathlib import Path

from . import update

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
# Where Task Manager's Startup tab keeps "disabled": the entry stays in Run.
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"


def _approved(data: bytes) -> bool:
    """Whether a StartupApproved value leaves the entry on: its first byte is even
    (2, 6) when enabled, odd (3, 7) when the user switched it off."""
    return not data or data[0] % 2 == 0
AGENTS = Path.home() / "Library/LaunchAgents"


def enable(label, path=None, agents=AGENTS):
    path = Path(path or update.app_path())
    if sys.platform == "win32":
        import winreg

        # A fresh profile may have no Run key yet.
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, label, 0, winreg.REG_SZ, f'"{path}"')
        return
    bundle = next((p for p in [path, *path.parents] if p.suffix == ".app"), None)
    program = ["/usr/bin/open", "-a", str(bundle)] if bundle else [str(path)]
    agents.mkdir(parents=True, exist_ok=True)
    (agents / f"{label}.plist").write_bytes(plistlib.dumps(
        {"Label": label, "ProgramArguments": program, "RunAtLoad": True, "ProcessType": "Interactive"}))


def disable(label, agents=AGENTS):
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, label)
        except FileNotFoundError:  # no Run key, or no value: already off
            pass
        return
    (agents / f"{label}.plist").unlink(missing_ok=True)


def is_enabled(label, agents=AGENTS):
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
                winreg.QueryValueEx(key, label)
        except FileNotFoundError:  # no Run key, or no value
            return False
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, APPROVED_KEY) as key:
                return _approved(winreg.QueryValueEx(key, label)[0])
        except FileNotFoundError:  # never touched in Task Manager
            return True
    return (agents / f"{label}.plist").is_file()
