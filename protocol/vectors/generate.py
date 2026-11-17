"""Writes the signed manifest vectors. Run it after changing what they cover:

    PYTHONPATH=../../python python3 generate.py

Signed with the RFC 8032 section 7.1 test 1 key; `other` is a second key the
manifests are NOT signed with, for the key-rotation cases. Every platform key
points at App-<platform>, so each client finds its own asset."""

import base64
import hashlib
import json
from pathlib import Path

from beacon import _ed25519

SECRET = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
OTHER = bytes.fromhex("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb")  # RFC 8032 test 2
HERE = Path(__file__).parent / "manifest"
KEY = base64.b64encode(_ed25519.public_key(SECRET)).decode()
OTHER_KEY = base64.b64encode(_ed25519.public_key(OTHER)).decode()


def manifest(**extra):
    asset = {"size": 12, "sha256": "0" * 64}
    return {"version": "1.2.0", "notes_url": "https://github.com/me/app/releases/tag/v1.2.0",
            "assets": {k: {"url": f"https://github.com/me/app/releases/download/v1.2.0/App-{k}", **asset}
                       for k in ["macos-arm64", "macos-x64", "windows-x64", "windows-arm64"]}, **extra}


def write(name, body):
    data = json.dumps(body, indent=2).encode() + b"\n"
    (HERE / name).write_bytes(data)
    (HERE / f"{name}.sig").write_text(base64.b64encode(_ed25519.sign(SECRET, data)).decode())
    return data


def bucket(machine_id, version):
    return int(hashlib.sha256(f"{machine_id}:{version}".encode()).hexdigest()[:8], 16) % 100


def machine_in(lo, hi):
    """A machine id whose bucket for 1.2.0 is in [lo, hi)."""
    i = 0
    while not lo <= bucket(f"machine-{i}", "1.2.0") < hi:
        i += 1
    return f"machine-{i}"


data = write("update.json", manifest())
(HERE / "update.json.tampered").write_bytes(data.replace(b'"version": "1.2.0"', b'"version": "9.9.9"'))
(HERE / "update.json.tampered.sig").unlink(missing_ok=True)
write("paused.json", manifest(paused=True))
write("rollout-30.json", manifest(rollout=30))
write("required.json", manifest(min_version="1.1.0"))
inside, outside = machine_in(0, 30), machine_in(30, 100)

cases = [
    {"file": "update.json", "keys": [KEY], "current": "1.1.9", "offered": "1.2.0"},
    {"file": "update.json", "keys": [KEY], "current": "1.2.0", "offered": None},
    {"file": "update.json", "keys": [KEY], "current": "2.0.0", "offered": None},
    {"file": "update.json", "keys": [KEY], "current": "dev", "offered": "1.2.0"},
    {"file": "update.json", "keys": [OTHER_KEY, KEY], "current": "1.1.9", "offered": "1.2.0",
     "why": "rotation: the app has the old and the new key"},
    {"file": "update.json", "keys": [OTHER_KEY], "current": "1.1.9", "error": "verify", "why": "no matching key"},
    {"file": "update.json.tampered", "signature": "update.json.sig", "keys": [KEY], "current": "1.0.0", "error": "verify"},
    {"file": "paused.json", "keys": [KEY], "current": "1.1.9", "offered": None, "why": "paused"},
    {"file": "rollout-30.json", "keys": [KEY], "current": "1.1.9", "machine_id": inside, "offered": "1.2.0",
     "why": f"bucket {bucket(inside, '1.2.0')} < 30"},
    {"file": "rollout-30.json", "keys": [KEY], "current": "1.1.9", "machine_id": outside, "offered": None,
     "why": f"bucket {bucket(outside, '1.2.0')} >= 30"},
    {"file": "required.json", "keys": [KEY], "current": "1.0.5", "offered": "1.2.0", "required": True,
     "why": "older than min_version 1.1.0: a forced update"},
    {"file": "required.json", "keys": [KEY], "current": "1.1.0", "offered": "1.2.0", "required": False,
     "why": "at min_version: an ordinary update"},
]
(HERE / "cases.json").write_text(json.dumps({
    "note": "Each case: verify `file` with `signature` (default <file>.sig) against any of `keys`, as a client "
            "running `current` on `machine_id` (default \"\"); expect the release `offered` (and `required`, "
            "default false), or an UpdateError `error`.",
    "cases": cases}, indent=2) + "\n")

buckets = [{"machine_id": m, "version": v, "bucket": bucket(m, v)}
           for m, v in [("", "1.2.0"), ("acec38d633a3395ac980fd58d39e86ed", "1.2.0"),
                        ("acec38d633a3395ac980fd58d39e86ed", "1.3.0"), (inside, "1.2.0")]]
(Path(__file__).parent / "rollout.json").write_text(json.dumps({
    "note": "bucket = int(first 8 hex chars of sha256('<machine_id>:<version>'), 16) % 100; "
            "a release with rollout N is offered when bucket < N.", "cases": buckets}, indent=2) + "\n")
print("written")
