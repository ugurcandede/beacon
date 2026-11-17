import os
import plistlib
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from beacon import build, system

ROOT = Path(__file__).resolve().parents[2]


def config(tmp_path, text):
    (tmp_path / "beacon.toml").write_text(text)
    return build.load(tmp_path / "beacon.toml")


PY = '''
name = "Lumea"
id = "com.ugurcandede.lumea"
company = "ugurcandede"
data = ["assets/icon.png"]
version_file = "version.txt"
[python]
entry = "main.py"
plugins = ["pyside6"]
include_modules = ["json"]
exclude_modules = ["PySide6.QtWebEngineCore"]
[macos.plist]
NSBluetoothAlwaysUsageDescription = "Lumea uses Bluetooth."
[windows]
icon = "assets/icon.ico"
include_modules = ["winrt.windows.foundation.collections"]
'''


@pytest.mark.parametrize("text,error", [
    ('id = "x"\n[python]\nentry = "a.py"', "`name`"),
    ('name = "A"\nid = "x"', "exactly one"),
    ('name = "A"\nid = "x"\n[python]\nentry = "a.py"\n[swift]\nproduct = "A"', "exactly one"),
    ('name = "A"\nid = "x"\n[python]', "`entry`"),
    ('name = "A"\nid = "x"\n[swift]', "`product`"),
    ('name = "A"\nid = "x"\n[python]\nentry = "a.py"\n[windows]\nicon = "a.png"', ".ico"),
    ('name = "A"\nid = "x"\n[python]\nentry = "a.py"\n[macos]\nicon = "a.jpg"', ".png or an .icns"),
])
def test_config_errors(tmp_path, text, error):
    with pytest.raises(build.ConfigError, match=error):
        config(tmp_path, text)


def test_nuitka_windows_args(tmp_path):
    c = config(tmp_path, PY)
    args = build.nuitka_args(c, "1.4.0", "windows", tmp_path / "out")
    assert args[:4] == [sys.executable, "-m", "nuitka", str(tmp_path / "main.py")]
    for flag in ["--mode=onefile", "--onefile-cache-mode=cached", "--enable-plugins=pyside6",
                 "--onefile-tempdir-spec={CACHE_DIR}/{COMPANY}/{PRODUCT}/{VERSION}",
                 "--company-name=ugurcandede", "--product-name=Lumea", "--file-version=1.4.0",
                 "--include-module=json", "--include-module=winrt.windows.foundation.collections",
                 "--nofollow-import-to=PySide6.QtWebEngineCore",
                 f"--include-data-files={tmp_path / 'assets/icon.png'}=assets/icon.png",
                 f"--include-data-files={tmp_path / 'version.txt'}=version.txt",
                 f"--windows-icon-from-ico={tmp_path / 'assets/icon.ico'}", "--output-filename=Lumea.exe"]:
        assert flag in args
    assert not any(a.startswith("--macos") for a in args)


def test_nuitka_macos_args(tmp_path):
    args = build.nuitka_args(config(tmp_path, PY), "1.4.0", "macos", tmp_path / "out")
    for flag in ["--mode=app", "--macos-app-name=Lumea", "--macos-signed-app-name=com.ugurcandede.lumea",
                 "--macos-app-version=1.4.0", "--output-filename=Lumea"]:
        assert flag in args
    assert not any(a.startswith(("--onefile", "--windows", "--msvc")) for a in args)
    assert "--include-module=json" in args  # [python]: every OS
    assert "--include-module=winrt.windows.foundation.collections" not in args  # [windows] only


def test_plist_values_merge_the_config(tmp_path):
    values = build.plist_values(config(tmp_path, PY), "1.4.0")
    assert values == {"CFBundleIdentifier": "com.ugurcandede.lumea", "CFBundleShortVersionString": "1.4.0",
                      "CFBundleVersion": "1.4.0", "NSBluetoothAlwaysUsageDescription": "Lumea uses Bluetooth."}


def test_asset_names_follow_the_platform_keys(tmp_path):
    c = config(tmp_path, PY)
    assert build.asset_name(c, "windows-x64") == "Lumea-windows-x64.exe"
    assert build.asset_name(c, "macos-arm64") == "Lumea-macos-arm64.zip"


def test_universal_swift_build():
    assert build.swift_build_args() == ["swift", "build", "-c", "release", "--arch", "arm64", "--arch", "x86_64"]


def test_nuitka_pin_matches_the_build_extra():
    assert f'"{build.NUITKA}"' in (ROOT / "python/pyproject.toml").read_text()


# ---- resource() -------------------------------------------------------------------


def test_resource_in_a_source_run(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "main.py")])
    monkeypatch.setattr(system, "frozen", lambda: False)
    assert system.resource("assets/icon.png") == tmp_path.resolve() / "assets/icon.png"


@pytest.mark.parametrize("module_dir,expected", [
    ("Lumea.app/Contents/MacOS/beacon", "Lumea.app/Contents/Resources"),  # Nuitka .app
    ("cache/ugurcandede/Lumea/1.4.0/beacon", "cache/ugurcandede/Lumea/1.4.0"),  # Nuitka onefile unpack
])
def test_resource_when_compiled(monkeypatch, tmp_path, module_dir, expected):
    monkeypatch.setattr(system, "frozen", lambda: True)
    monkeypatch.setattr(system, "__file__", str(tmp_path / module_dir / "system.py"))
    assert system.resource("x.json") == (tmp_path / expected / "x.json").resolve()


# ---- a real build (macOS, Swift demo) ----------------------------------------------


@pytest.mark.skipif(platform.system() != "Darwin" or not shutil.which("swift"), reason="needs macOS and Swift")
def test_builds_the_swift_demo(tmp_path):
    demo = tmp_path / "demo-swift"
    shutil.copytree(ROOT / "examples/demo-swift", demo, ignore=shutil.ignore_patterns(".build", ".swiftpm"))
    # The demo's Package.swift points at the repository root, ../..; keep that path valid.
    (demo / "Package.swift").write_text((demo / "Package.swift").read_text().replace(
        '"../.."', f'"{ROOT}"'))
    (demo / "demo_config.json").write_text('{"version": "2.0.0"}')
    lines = build.build(build.load(demo / "beacon.toml"), "2.0.0", tmp_path / "dist")

    zip_path = tmp_path / "dist/BeaconDemoSwift-macos.zip"
    assert lines == [f"macos-arm64={zip_path}", f"macos-x64={zip_path}"]
    subprocess.run(["ditto", "-x", "-k", str(zip_path), str(tmp_path / "x")], check=True)
    app = tmp_path / "x/BeaconDemoSwift.app"
    plist = plistlib.loads((app / "Contents/Info.plist").read_bytes())
    assert plist["CFBundleShortVersionString"] == "2.0.0" and plist["CFBundleIdentifier"] == "com.ugurcandede.beacon-demo-swift"
    assert plist["CFBundleExecutable"] == "BeaconDemo"
    assert (app / "Contents/Resources/demo_config.json").is_file()
    archs = subprocess.run(["lipo", "-archs", str(app / "Contents/MacOS/BeaconDemo")], capture_output=True, text=True).stdout.split()
    assert sorted(archs) == ["arm64", "x86_64"]
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)


def test_move_resources_leaves_code_and_links_the_rest(tmp_path):
    app = tmp_path / "A.app"
    macos = app / "Contents/MacOS"
    (macos / "certifi").mkdir(parents=True)
    (app / "Contents/Resources").mkdir()
    (macos / "A").write_bytes(b"\xcf\xfa\xed\xfe" + b"\0" * 16)  # a Mach-O binary
    (macos / "lib.so").write_bytes(b"\xca\xfe\xba\xbe" + b"\0" * 16)  # a universal one
    (macos / "certifi/cacert.pem").write_text("certs")
    (macos / "demo_config.json").write_text("{}")
    build.move_resources(app)
    assert not (macos / "A").is_symlink() and not (macos / "lib.so").is_symlink()
    for rel in ("certifi/cacert.pem", "demo_config.json"):
        assert (macos / rel).is_symlink()
        assert (app / "Contents/Resources" / rel).is_file() and not (app / "Contents/Resources" / rel).is_symlink()
        assert (macos / rel).read_text() == (app / "Contents/Resources" / rel).read_text()
        assert not os.path.isabs(os.readlink(macos / rel))  # survives moving the .app


def test_signs_ad_hoc_unless_an_identity_is_set(tmp_path, monkeypatch):
    c = config(tmp_path, PY)
    monkeypatch.delenv("BEACON_CODESIGN_IDENTITY", raising=False)
    assert build.codesign_args(c, tmp_path / "A.app")[4] == "-"
    monkeypatch.setenv("BEACON_CODESIGN_IDENTITY", "ABCDEF0123")
    args = build.codesign_args(c, tmp_path / "A.app")
    assert args[4] == "ABCDEF0123" and args[6] == "com.ugurcandede.lumea"
