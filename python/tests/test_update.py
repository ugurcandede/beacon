import base64
import hashlib
import subprocess
import sys
import json

import pytest

from beacon import _ed25519, release, system, update

# RFC 8032 section 7.1, test 1.
SECRET = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
PUBLIC = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
SIGNATURE = bytes.fromhex(
    "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")


def test_ed25519_matches_rfc_vector():
    assert _ed25519.public_key(SECRET) == PUBLIC
    assert _ed25519.sign(SECRET, b"") == SIGNATURE
    assert _ed25519.verify(PUBLIC, b"", SIGNATURE)
    assert not _ed25519.verify(PUBLIC, b"x", SIGNATURE)


def signed_manifest(tmp_path, version):
    asset = tmp_path / "App.exe"
    asset.write_bytes(b"MZ new build")
    data = json.dumps(release.manifest("me/app", f"v{version}", {system.platform_key(): asset})).encode()
    return data, _ed25519.sign(SECRET, data)


def test_parse_manifest_offers_newer_release(tmp_path):
    data, sig = signed_manifest(tmp_path, "1.2.0")
    found = update.parse_manifest(data, sig, PUBLIC, "1.1.9")
    assert found.version == "1.2.0"
    assert found.url == "https://github.com/me/app/releases/download/v1.2.0/App.exe"
    assert found.size == 12


def test_parse_manifest_ignores_same_version(tmp_path):
    data, sig = signed_manifest(tmp_path, "1.2.0")
    assert update.parse_manifest(data, sig, PUBLIC, "1.2.0") is None


def test_parse_manifest_rejects_tampering(tmp_path):
    data, sig = signed_manifest(tmp_path, "1.2.0")
    with pytest.raises(update.UpdateError) as e:
        update.parse_manifest(data.replace(b"1.2.0", b"9.9.9"), sig, PUBLIC, "1.0.0")
    assert e.value.step == "verify"


def test_download_rejects_hash_mismatch(tmp_path, monkeypatch):
    asset = tmp_path / "asset"
    asset.write_bytes(b"payload")
    updater = update.Updater("me/app", "1.0.0", base64.b64encode(PUBLIC), tmp_path)
    bad = update.Release("2.0.0", asset.as_uri(), 7, "0" * 64, "")
    monkeypatch.setattr(update.system, "ssl_context", lambda: None)
    with pytest.raises(update.UpdateError) as e:
        updater._download(bad, tmp_path / "out", None)
    assert e.value.step == "verify"
    assert not (tmp_path / "out").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="ditto and .app bundles")
def test_bundle_install_swaps_running_app(tmp_path, monkeypatch):
    def make_app(root, version):
        exe = root / "Demo.app/Contents/MacOS/Demo"
        exe.parent.mkdir(parents=True)
        exe.write_text(version)
        return root / "Demo.app"

    installed = make_app(tmp_path / "Applications", "1.0.0")
    new = make_app(tmp_path / "build", "2.0.0")
    archive = tmp_path / "Demo.zip"
    subprocess.run(["ditto", "-c", "-k", "--keepParent", str(new), str(archive)], check=True)
    data = archive.read_bytes()
    found = update.Release("2.0.0", archive.as_uri(), len(data), hashlib.sha256(data).hexdigest(), "")

    relaunched = []
    monkeypatch.setattr(update, "app_path", lambda: installed / "Contents/MacOS/Demo")
    monkeypatch.setattr(update, "frozen", lambda: True)
    monkeypatch.setattr(update.system, "ssl_context", lambda: None)
    updater = update.Updater("me/app", "1.0.0", base64.b64encode(PUBLIC), tmp_path / "data")
    monkeypatch.setattr(updater, "_relaunch_mac", relaunched.append)
    monkeypatch.setattr(updater, "check", lambda manual=False: found)  # the server still offers it

    assert updater.install_kind() == "bundle"
    updater.install(found)
    assert (installed / "Contents/MacOS/Demo").read_text() == "2.0.0"
    assert relaunched == [installed]
    updater.cleanup()
    assert not list((tmp_path / "data").glob("beacon-update-*"))


def test_release_refuses_key_not_matching_app(tmp_path, monkeypatch):
    asset = tmp_path / "App.exe"
    asset.write_bytes(b"MZ")
    monkeypatch.setenv("BEACON_SIGNING_KEY", base64.b64encode(b"\x01" * 32).decode())
    with pytest.raises(SystemExit) as e:
        release.main(["manifest", "--repo", "me/app", "--tag", "v1.0.0", "--out", str(tmp_path / "out"),
                      "--public-key", base64.b64encode(PUBLIC).decode(), f"windows-x64={asset}"])
    assert "does not belong" in str(e.value)
    assert not (tmp_path / "out" / "update.json.sig").exists()


def test_updater_takes_one_key_or_several(tmp_path):
    other = base64.b64encode(b"\x02" * 32).decode()
    assert update.Updater("me/app", "1.0", base64.b64encode(PUBLIC).decode(), tmp_path)._keys == [PUBLIC]
    assert update.Updater("me/app", "1.0", base64.b64encode(PUBLIC), tmp_path)._keys == [PUBLIC]
    keys = update.Updater("me/app", "1.0", [other, base64.b64encode(PUBLIC).decode()], tmp_path)._keys
    assert keys == [b"\x02" * 32, PUBLIC]
