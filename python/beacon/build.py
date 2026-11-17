"""Builds an app the same way everywhere, from its beacon.toml:

    python -m beacon.build --version 1.4.0 [--config beacon.toml] [--out dist]

Builds for the machine it runs on and prints one PLATFORM=PATH line per asset
(what the release action's `assets` takes); also written to <out>/assets.txt.

- [python] apps: Nuitka. Windows: one exe that unpacks once per version into
  a kept folder (fast starts; an update cannot run from a folder its
  predecessor deletes). macOS: an .app, zipped.
- [swift] apps: one universal (arm64 + x86_64) .app from SwiftPM, zipped.

Every .app gets the plist keys from [macos.plist], its data files in
Contents/Resources (beacon.system.resource finds them), a signature that is
checked to survive `xattr -cr` (what the cask and unzip do), and a zip made
with ditto.

Signing: with BEACON_CODESIGN_IDENTITY set (the codesign action sets it from a
certificate made by `beacon.release codesign-cert`), every build is signed by
the same certificate, so macOS sees each version as the same app and keeps its
permissions (Accessibility, Input Monitoring) across updates. Without it, the
signature is ad-hoc: each build is a new app to macOS and loses them.

beacon.toml:

    name = "Lumea"                    # bundle and file names
    id = "com.ugurcandede.lumea"      # bundle id
    company = "ugurcandede"           # Windows unpack folder: <cache>/<company>/<name>/<version>
    data = ["assets/icon.png"]        # files the app reads at run time
    version_file = "version.txt"      # optional: written with the version, shipped as data
    public_key = "..."                # the update key built into the app; the release action checks it
    cask = "lumea"                    # optional: the Homebrew cask; the cask action updates it

    [python]
    entry = "main.py"
    plugins = ["pyside6"]
    include_modules = []              # imported out of Nuitka's sight, on every OS
    exclude_modules = []              # left out on purpose

    [swift]                           # instead of [python]
    product = "TapLockApp"            # the SwiftPM executable
    info_plist = "Resources/Info.plist"   # optional; else one is made

    [macos]
    icon = "assets/icon.png"          # .png (made into .icns) or .icns
    [macos.plist]                     # merged into Info.plist
    NSBluetoothAlwaysUsageDescription = "..."

    [windows]
    icon = "assets/icon.ico"
    include_modules = []              # this OS only (also under [macos])

Needs Python 3.11+ (tomllib) and, for Python apps, Nuitka (pip install
"beacon[build]").
"""

import argparse
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import system

NUITKA = "nuitka==4.2.2"  # 4.3 builds apps that segfault at start (Nuitka_Function_New)


class ConfigError(Exception):
    pass


def load(path):
    import tomllib

    path = Path(path)
    config = tomllib.loads(path.read_text(encoding="utf-8"))
    config["root"] = path.resolve().parent
    check(config)
    return config


def check(config):
    for key in ("name", "id"):
        if not isinstance(config.get(key), str) or not config[key]:
            raise ConfigError(f"beacon.toml needs `{key}`")
    if ("python" in config) == ("swift" in config):
        raise ConfigError("beacon.toml needs exactly one of [python] or [swift]")
    if "python" in config and not config["python"].get("entry"):
        raise ConfigError("[python] needs `entry`")
    if "swift" in config and not config["swift"].get("product"):
        raise ConfigError("[swift] needs `product`")
    icon = config.get("windows", {}).get("icon")
    if icon and not icon.endswith(".ico"):
        raise ConfigError("[windows] icon must be an .ico")
    icon = config.get("macos", {}).get("icon")
    if icon and not icon.endswith((".png", ".icns")):
        raise ConfigError("[macos] icon must be a .png or an .icns")


def data_files(config):
    files = list(config.get("data", []))
    if config.get("version_file"):
        files.append(config["version_file"])
    return files


def asset_name(config, platform_key):
    return f"{config['name']}-{platform_key}" + (".exe" if platform_key.startswith("windows") else ".zip")


# ---- Nuitka ---------------------------------------------------------------------


def nuitka_args(config, version, os_name, out):
    """The Nuitka command line for `os_name` ("windows" or "macos")."""
    py, name, root = config["python"], config["name"], config["root"]
    args = [sys.executable, "-m", "nuitka", str(root / py["entry"]), "--assume-yes-for-downloads",
            f"--output-dir={out}", f"--output-filename={name}.exe" if os_name == "windows" else f"--output-filename={name}"]
    args += [f"--enable-plugins={p}" for p in py.get("plugins", [])]
    # [python] include_modules on every OS, [windows] / [macos] include_modules on that one only
    # (a module that exists on one OS, like Lumea's winrt, breaks the other's build).
    modules = py.get("include_modules", []) + config.get(os_name, {}).get("include_modules", [])
    args += [f"--include-module={m}" for m in modules]
    args += [f"--nofollow-import-to={m}" for m in py.get("exclude_modules", [])]
    args += [f"--include-data-files={root / f}={f}" for f in data_files(config)]
    if os_name == "windows":
        company = config.get("company", name)
        args += ["--mode=onefile", "--msvc=latest", "--windows-console-mode=disable",
                 f"--company-name={company}", f"--product-name={name}",
                 f"--file-version={version}", f"--product-version={version}",
                 "--onefile-tempdir-spec={CACHE_DIR}/{COMPANY}/{PRODUCT}/{VERSION}",
                 "--onefile-cache-mode=cached"]
        if config.get("windows", {}).get("icon"):
            args.append(f"--windows-icon-from-ico={root / config['windows']['icon']}")
    else:
        args += ["--mode=app", f"--macos-app-name={name}", f"--macos-signed-app-name={config['id']}",
                 f"--macos-app-version={version}"]
    return args


def build_python(config, version, out):
    os_name = {"Windows": "windows", "Darwin": "macos"}.get(platform.system())
    if os_name is None:
        raise ConfigError(f"no Python build for {platform.system()}")
    key = f"{os_name}-{system.arch()}"
    work = Path(tempfile.mkdtemp(prefix="beacon-build-"))
    args = nuitka_args(config, version, os_name, work)
    if os_name == "macos" and config.get("macos", {}).get("icon"):
        args.append(f"--macos-app-icon={icns(config, work)}")
    run(args)
    if os_name == "windows":
        target = out / asset_name(config, key)
        shutil.move(str(work / f"{config['name']}.exe"), target)
        return {key: target}
    built = next(work.glob("*.app"))
    app = work / f"{config['name']}.app"
    if built != app:
        built.rename(app)
    move_resources(app)
    finish_app(config, version, app)
    return {key: package(app, out / asset_name(config, key))}


MACH_O = {b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce"}


def move_resources(app):
    """Every file in Contents/MacOS that is not code goes to Contents/Resources,
    with a relative symlink left behind. codesign seals such files in MacOS
    through extended attributes, which unzip and the cask's `xattr -cr` strip,
    so the app no longer verifies. The symlink keeps paths working for packages
    that find their files next to their module (certifi's cacert.pem);
    beacon.system.resource looks in Resources directly."""
    macos, resources = app / "Contents/MacOS", app / "Contents/Resources"
    for path in sorted(macos.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        with open(path, "rb") as f:
            if f.read(4) in MACH_O:
                continue
        rel = path.relative_to(macos)
        dest = resources / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), dest)
        path.symlink_to(os.path.relpath(dest, path.parent))


# ---- Swift ----------------------------------------------------------------------


def swift_build_args(arch_flags=("arm64", "x86_64")):
    args = ["swift", "build", "-c", "release"]
    for arch in arch_flags:
        args += ["--arch", arch]
    return args


def build_swift(config, version, out):
    if platform.system() != "Darwin":
        raise ConfigError("Swift apps build on macOS only")
    root, product, name = config["root"], config["swift"]["product"], config["name"]
    run(swift_build_args(), cwd=root)
    bin_dir = Path(subprocess.run(swift_build_args() + ["--show-bin-path"], cwd=root, capture_output=True,
                                  text=True, check=True).stdout.strip())
    work = Path(tempfile.mkdtemp(prefix="beacon-build-"))
    app = work / f"{name}.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/Resources").mkdir()
    shutil.copy2(bin_dir / product, app / "Contents/MacOS" / product)
    plist = {}
    if config["swift"].get("info_plist"):
        plist = plistlib.loads((root / config["swift"]["info_plist"]).read_bytes())
    plist.setdefault("CFBundleName", name)
    plist.setdefault("CFBundlePackageType", "APPL")
    plist.setdefault("NSPrincipalClass", "NSApplication")
    plist["CFBundleExecutable"] = product
    with open(app / "Contents/Info.plist", "wb") as f:
        plistlib.dump(plist, f)
    if config.get("macos", {}).get("icon"):
        shutil.copy2(icns(config, work), app / "Contents/Resources/AppIcon.icns")
        plist_set(app, {"CFBundleIconFile": "AppIcon"})
    for f in data_files(config):
        dest = app / "Contents/Resources" / f
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / f, dest)
    finish_app(config, version, app)
    # One universal zip serves both Mac platforms.
    zip_path = package(app, out / f"{name}-macos.zip")
    return {"macos-arm64": zip_path, "macos-x64": zip_path}


# ---- macOS bundle ---------------------------------------------------------------


def plist_values(config, version):
    """What every .app's Info.plist gets: identity, version, [macos.plist]."""
    return {"CFBundleIdentifier": config["id"], "CFBundleShortVersionString": version,
            "CFBundleVersion": version, **config.get("macos", {}).get("plist", {})}


def plist_set(app, values):
    path = app / "Contents/Info.plist"
    plist = plistlib.loads(path.read_bytes())
    plist.update(values)
    path.write_bytes(plistlib.dumps(plist))


def codesign_identity():
    return os.environ.get("BEACON_CODESIGN_IDENTITY") or "-"


def codesign_args(config, app):
    return ["codesign", "--force", "--deep", "--sign", codesign_identity(), "--identifier", config["id"], str(app)]


def finish_app(config, version, app):
    plist_set(app, plist_values(config, version))
    run(codesign_args(config, app))
    # Users and the cask strip extended attributes (xattr -cr); the seal must hold without them.
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / app.name
        run(["ditto", str(app), str(copy)])
        run(["xattr", "-cr", str(copy)])
        run(["codesign", "--verify", "--deep", "--strict", str(copy)])


def package(app, zip_path):
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    zip_path.unlink(missing_ok=True)
    run(["ditto", "-c", "-k", "--keepParent", str(app), str(zip_path)])
    return zip_path


def icns(config, work):
    """The macOS icon as .icns: as given, or made from a PNG with sips and iconutil."""
    icon = config["root"] / config["macos"]["icon"]
    if icon.suffix == ".icns":
        return icon
    iconset = work / "icon.iconset"
    iconset.mkdir(exist_ok=True)
    for size in (16, 32, 128, 256, 512):
        for scale, suffix in ((1, ""), (2, "@2x")):
            px = size * scale
            run(["sips", "-z", str(px), str(px), str(icon), "--out", str(iconset / f"icon_{size}x{size}{suffix}.png")])
    out = work / "AppIcon.icns"
    run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)])
    return out


def run(args, cwd=None):
    result = subprocess.run([str(a) for a in args], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if result.returncode != 0:
        sys.stderr.write(result.stdout[-4000:])
        raise SystemExit(f"{Path(str(args[0])).name} failed ({result.returncode}): {' '.join(map(str, args[:6]))} ...")


# ---- CLI --------------------------------------------------------------------------


def build(config, version, out):
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if config.get("version_file"):
        (config["root"] / config["version_file"]).write_text(version + "\n", encoding="utf-8")
    assets = build_python(config, version, out) if "python" in config else build_swift(config, version, out)
    lines = [f"{key}={path}" for key, path in assets.items()]
    (out / "assets.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return lines


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m beacon.build")
    parser.add_argument("--version", required=True)
    parser.add_argument("--config", default="beacon.toml")
    parser.add_argument("--out", default="dist")
    args = parser.parse_args(argv)
    try:
        lines = build(load(args.config), args.version, args.out)
    except ConfigError as e:
        raise SystemExit(f"beacon.build: {e}")
    print("\n".join(lines))


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    main()
