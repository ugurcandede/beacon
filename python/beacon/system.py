"""What the apps share about the machine they run on, and the HTTPS context
they talk through."""

import hashlib
import locale
import os
import platform
import ssl
from datetime import datetime
import subprocess
import sys
import time
from pathlib import Path

import certifi

from . import __version__


def arch() -> str:
    machine = platform.machine().lower()
    return {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64"}.get(machine, machine)


def platform_key() -> str:
    """The manifest key for this build: windows-x64, macos-arm64, ..."""
    system = platform.system()
    name = {"Windows": "windows", "Darwin": "macos"}.get(system, system.lower())
    return f"{name}-{arch()}"


def context() -> dict:
    system = platform.system()
    if system == "Darwin":
        os_name, os_version = "macOS", platform.mac_ver()[0]
    elif system == "Windows":
        # 10.0.<build>: build 22000 and up is Windows 11.
        os_name, os_version = "Windows", platform.version()
    else:
        os_name, os_version = system, platform.release()
    return {
        "os": os_name,
        "os_version": os_version,
        "arch": arch(),
        "locale": _locale(),
        "cpu_count": os.cpu_count() or 0,
        "memory_gb": _memory() >> 30,
        **_screens(),
        "lib": "beacon-python",
        "lib_version": __version__,
        **({"model": _sysctl("hw.model")} if system == "Darwin" else {}),
    }


def timestamp(seconds=None) -> str:
    """Local time with its offset, as every beacon log writes it: 2026-11-17 19:58:15+03:00."""
    return datetime.fromtimestamp(time.time() if seconds is None else seconds).astimezone().isoformat(sep=" ", timespec="seconds")


def frozen() -> bool:
    # Nuitka leaves __compiled__ in every module it compiled.
    return "__compiled__" in globals()


def resource(relative) -> Path:
    """A file the build shipped with the app (beacon.toml `data`, `version_file`):
    Contents/Resources in a macOS .app, the unpack folder of a Windows onefile
    exe, or next to the entry script in a source run."""
    return _resource_root() / relative


def _resource_root() -> Path:
    if not frozen():
        return Path(sys.argv[0]).resolve().parent
    # Compiled, this module sits in <root>/beacon/; data files are put in <root>,
    # except in a .app, where beacon.build moves them to Contents/Resources
    # (codesign seals non-code files in Contents/MacOS through xattrs, which an
    # unzip or the cask's `xattr -cr` strips).
    root = Path(__file__).resolve().parent.parent
    return root.parent / "Resources" if root.name == "MacOS" else root


def process_start():
    """When this process started (epoch seconds), or None if the OS would not say."""
    try:
        if platform.system() == "Darwin":
            import ctypes
            import struct

            # sysctl kern.proc.pid.<pid>: a kinfo_proc, starting with p_starttime (a timeval).
            libc = ctypes.CDLL(None)
            mib = (ctypes.c_int * 4)(1, 14, 1, os.getpid())  # CTL_KERN, KERN_PROC, KERN_PROC_PID
            buf, size = ctypes.create_string_buffer(1024), ctypes.c_size_t(1024)
            if libc.sysctl(mib, 4, buf, ctypes.byref(size), None, 0) == 0:
                seconds, micros = struct.unpack_from("qi", buf.raw)
                return seconds + micros / 1e6
        elif platform.system() == "Windows":
            import ctypes

            created, exited, kernel, user = (ctypes.c_ulonglong() for _ in range(4))
            kernel32 = ctypes.windll.kernel32
            if kernel32.GetProcessTimes(kernel32.GetCurrentProcess(), ctypes.byref(created), ctypes.byref(exited),
                                        ctypes.byref(kernel), ctypes.byref(user)):
                return created.value / 1e7 - 11644473600  # FILETIME (100 ns since 1601) to epoch
    except (OSError, AttributeError, ValueError):
        pass
    return None


def memory_peak_mb() -> int:
    """The most memory this process has held so far, in MB (0 if unknown)."""
    try:
        if platform.system() == "Windows":
            import ctypes
            from ctypes import wintypes

            class Counters(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

            counters = Counters(cb=ctypes.sizeof(Counters))
            ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(),
                                                     ctypes.byref(counters), counters.cb)
            return counters.PeakWorkingSetSize >> 20
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # bytes on macOS, KB on Linux
        return peak >> 20 if platform.system() == "Darwin" else peak >> 10
    except (OSError, AttributeError, ImportError):
        return 0


def data_dir(app, company=None) -> Path:
    """Where an app keeps its files: ~/Library/Application Support/[company/]app on
    macOS, %APPDATA%\\[company\\]app on Windows -- what Qt's AppDataLocation gives
    with the same organization and application names."""
    parts = [company, app] if company else [app]
    if platform.system() == "Windows":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming", *parts)
    if platform.system() == "Darwin":
        return Path.home().joinpath("Library/Application Support", *parts)
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share", *parts)


_instance_lock = None


def single_instance(name) -> bool:
    """True if this is the only running copy of `name` (kept until the process
    exits), False if another holds it. Waits first for the build that started
    this one in an update, which may still be exiting (update.wait_for_previous).
    Windows: a named mutex -- what QLocalServer cannot tell there. Elsewhere: a
    lock on a file in the temp folder, which the OS drops when the holder dies."""
    global _instance_lock
    from .update import wait_for_previous

    wait_for_previous()
    if platform.system() == "Windows":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, f"Local\\beacon-{name}")
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            return False
        _instance_lock = handle
        return True
    import fcntl
    import tempfile

    lock = open(Path(tempfile.gettempdir()) / f"beacon-{name}.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        return False
    _instance_lock = lock
    return True


def machine_id() -> str:
    """The same id in every beacon app on this machine, so one person running
    three apps counts once. A salted hash: the hardware UUID never leaves the
    machine. Empty when the OS does not say."""
    raw = _hardware_uuid()
    return hashed_machine_id(raw) if raw else ""


def hashed_machine_id(raw: str) -> str:
    return hashlib.sha256(("beacon-machine:" + raw.strip().upper()).encode()).hexdigest()[:32]


def _hardware_uuid() -> str:
    try:
        if platform.system() == "Darwin":
            out = subprocess.run(["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                                 capture_output=True, text=True).stdout
            for line in out.splitlines():
                if "IOPlatformUUID" in line:
                    return line.split("=", 1)[1].strip().strip('"')
        elif platform.system() == "Windows":
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                                winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                return winreg.QueryValueEx(key, "MachineGuid")[0]
    except OSError:
        pass
    return ""


def _screens() -> dict:
    """The main display's size in points (what the user sees) and how many there are."""
    try:
        if platform.system() == "Darwin":
            import ctypes

            cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
            cg.CGMainDisplayID.restype = ctypes.c_uint32
            cg.CGDisplayPixelsWide.restype = cg.CGDisplayPixelsHigh.restype = ctypes.c_size_t
            cg.CGDisplayPixelsWide.argtypes = cg.CGDisplayPixelsHigh.argtypes = [ctypes.c_uint32]
            count = ctypes.c_uint32()
            cg.CGGetActiveDisplayList(0, None, ctypes.byref(count))
            main = cg.CGMainDisplayID()
            return {"screen_width": cg.CGDisplayPixelsWide(main), "screen_height": cg.CGDisplayPixelsHigh(main),
                    "screen_count": count.value}
        if platform.system() == "Windows":
            import ctypes

            metrics = ctypes.windll.user32.GetSystemMetrics
            return {"screen_width": metrics(0), "screen_height": metrics(1), "screen_count": metrics(80)}
    except (OSError, AttributeError):
        pass
    return {"screen_width": 0, "screen_height": 0, "screen_count": 0}


def _locale() -> str:
    """The user's locale as en_US. A GUI app often has no LANG to go by, so ask
    the OS."""
    if platform.system() == "Darwin":
        return _defaults_locale() or locale.getlocale()[0] or ""
    if platform.system() == "Windows":
        import ctypes

        buffer = ctypes.create_unicode_buffer(85)
        if ctypes.windll.kernel32.GetUserDefaultLocaleName(buffer, len(buffer)):
            return buffer.value.replace("-", "_")
    return locale.getlocale()[0] or ""


def _defaults_locale() -> str:
    try:
        out = subprocess.run(["/usr/bin/defaults", "read", "-g", "AppleLocale"], capture_output=True, text=True)
    except OSError:
        return ""
    return out.stdout.strip().split("@")[0]


def _sysctl(name) -> str:
    try:
        return subprocess.run(["/usr/sbin/sysctl", "-n", name], capture_output=True, text=True).stdout.strip()
    except OSError:
        return ""


def _memory() -> int:
    """Physical memory in bytes."""
    if platform.system() == "Windows":
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        status = MemoryStatus(dwLength=ctypes.sizeof(MemoryStatus))
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        return status.ullTotalPhys
    return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")


def ssl_context() -> ssl.SSLContext:
    # A frozen app on macOS has no CA file where Python's OpenSSL looks for one;
    # certifi ships its own and Nuitka bundles it.
    return ssl.create_default_context(cafile=certifi.where())
