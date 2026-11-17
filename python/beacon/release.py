"""Release-side tool: signing key, signed update manifest, next version, Homebrew cask.

    python -m beacon.release keygen
        Prints a new key pair. The private key goes into the repo's
        BEACON_SIGNING_KEY secret, the public key into the app.

    BEACON_SIGNING_KEY=... python -m beacon.release manifest \\
            --repo ugurcandede/Lumea --tag v1.4.0 --out release \\
            windows-x64=release/Lumea-windows.exe macos-arm64=release/Lumea-macos-arm64.zip
        Writes update.json and update.json.sig into --out; upload both with the
        release assets. With --public-key (the app's), a signing key that does
        not match it is refused. --rollout N offers it to N% of machines first;
        --min-version V makes it a required update for anything older than V;
        --channel beta writes update-beta.json (upload it to the latest stable
        release, so releases/latest/download/ serves it).

    BEACON_SIGNING_KEY=... python -m beacon.release control DIR [--rollout N] [--pause | --resume] [--min-version V]
        Changes a published manifest (DIR/update.json, downloaded from the
        release) and signs it again; then upload both files back:
        gh release upload <tag> DIR/update.json DIR/update.json.sig --clobber
        --pause stops offering a bad release at once; --rollout widens a staged one.

    python -m beacon.release next-version [--since SHA]
        Prints tag=v1.5.0 and version=1.5.0 (GITHUB_OUTPUT lines): the highest
        v* tag bumped by the commit subjects since SHA, or the last one --
        BREAKING:/bump: major, feat: minor, anything else patch.

    python -m beacon.release cask FILE --version 1.5.0 --sha256 HEX
        Sets version and sha256 in a Homebrew cask file.

    python -m beacon.release codesign-cert --name "ugurcandede apps" --out DIR
        Makes a self-signed code signing certificate (10 years) as DIR/codesign.p12
        and prints it as base64 with its password: the BEACON_CODESIGN_P12 and
        BEACON_CODESIGN_PASSWORD secrets the codesign action takes. One per
        owner, kept: a new one makes macOS forget the apps' permissions once more.

    POSTHOG_ANNOTATION_KEY=phx_... python -m beacon.release annotate --app lumea --version 1.4.0
        Marks the release on PostHog's charts ("lumea 1.4.0"), so a jump in errors
        or users lines up with the version that caused it. The key needs only
        the annotation:write scope.

    python -m beacon.release check-version v1.4.0 [--root .]
        For beacon's own release: the tag, the Python and Swift versions and a
        CHANGELOG section must agree; prints that section (the release notes).

    python -m beacon.release config KEY [--config beacon.toml]
        Prints a top-level value from the app's beacon.toml (public_key, cask,
        name, ...), or nothing; the actions use it for inputs left out.
"""

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from . import _ed25519


def manifest(repo, tag, assets, base_url=None):
    version = tag[1:] if tag.startswith("v") else tag
    entries = {}
    for key, path in assets.items():
        data = Path(path).read_bytes()
        entries[key] = {
            "url": f"{base_url or f'https://github.com/{repo}/releases/download/{tag}/'}{Path(path).name}",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
    return {"version": version, "notes_url": f"https://github.com/{repo}/releases/tag/{tag}", "assets": entries}


def next_version(tags, subjects):
    """The highest vX.Y.Z tag -- not the nearest, so an out-of-order tag cannot
    send the version back to one that exists -- bumped by the commit subjects."""
    found = [tuple(map(int, m.groups())) for t in tags if (m := re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", t))]
    major, minor, patch = max(found, default=(0, 0, 0))
    # Conventional commits: `type(scope)!: subject`. A `!`, `BREAKING:` or `bump:` is a major.
    if any(re.match(r"(BREAKING( CHANGE)?|bump)(\(.+?\))?:|\w+(\(.+?\))?!:", s) for s in subjects):
        return f"{major + 1}.0.0"
    if any(re.match(r"feat(\(.+?\))?:", s, re.I) for s in subjects):
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def cask(text, version, sha256):
    """The cask with its version and sha256 replaced; fails rather than leave
    a cask that still points at the previous release."""
    for key, value in (("version", version), ("sha256", sha256)):
        text, n = re.subn(rf'^(\s*{key}\s+)"[^"]*"', rf'\g<1>"{value}"', text, flags=re.M)
        if n != 1:
            raise ValueError(f"expected one {key} line in the cask, found {n}")
    return text


def config_value(path, key):
    """A top-level string from beacon.toml; "" when the file or key is missing."""
    import tomllib

    try:
        value = tomllib.loads(Path(path).read_text(encoding="utf-8")).get(key, "")
    except FileNotFoundError:
        return ""
    return value if isinstance(value, str) else ""


def codesign_cert(name, out):
    """A self-signed certificate for code signing, and its key, as a .p12."""
    out.mkdir(parents=True, exist_ok=True)
    password = base64.urlsafe_b64encode(os.urandom(18)).decode()
    key, cert, p12 = out / "codesign.key", out / "codesign.crt", out / "codesign.p12"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-days", "3650", "-nodes",
                    "-keyout", key, "-out", cert, "-subj", f"/CN={name}",
                    "-addext", "basicConstraints=critical,CA:false",
                    "-addext", "keyUsage=critical,digitalSignature",
                    "-addext", "extendedKeyUsage=critical,codeSigning"], check=True, capture_output=True)
    try:
        subprocess.run(["openssl", "pkcs12", "-export", *_pkcs12_legacy(), "-inkey", key, "-in", cert, "-out", p12,
                        "-name", name, "-passout", f"pass:{password}"], check=True, capture_output=True)
    finally:
        key.unlink(missing_ok=True)  # the unencrypted key never outlives this call
    return p12, password


def annotate(api, project, key, content):
    """A project-wide annotation now, the marker PostHog draws on every chart."""
    import datetime
    import urllib.request

    body = {"content": content, "scope": "project",
            "date_marker": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    request = urllib.request.Request(f"{api}/api/projects/{project}/annotations/", method="POST",
                                     data=json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def check_version(tag, root):
    """(problems, release notes) for releasing beacon itself at `tag`."""
    version = tag[1:] if tag.startswith("v") else tag
    problems = []
    python = re.search(r'__version__ = "([^"]+)"', (root / "python/beacon/__init__.py").read_text())
    swift = re.search(r'static let libVersion = "([^"]+)"', (root / "swift/Sources/Beacon/System.swift").read_text())
    for name, found in (("python/beacon/__init__.py", python), ("swift/Sources/Beacon/System.swift", swift)):
        if not found or found.group(1) != version:
            problems.append(f"{name} says {found.group(1) if found else 'nothing'}, the tag {version}")
    changelog = (root / "CHANGELOG.md").read_text()
    section = re.search(rf"^## {re.escape(version)}[ \t]*\n(.*?)(?=^## |\Z)", changelog, re.M | re.S)
    if not section or not section.group(1).strip():
        problems.append(f"CHANGELOG.md has no '## {version}' section")
    return problems, section.group(1).strip() if section else ""


def _pkcs12_legacy():
    """["-legacy"] where openssl has it (OpenSSL 3 and later): macOS's `security
    import` reads only the old .p12 ciphers. LibreSSL writes those already."""
    help_text = subprocess.run(["openssl", "pkcs12", "-help"], capture_output=True, text=True)
    return ["-legacy"] if "-legacy" in help_text.stdout + help_text.stderr else []


def _git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout.splitlines()


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m beacon.release")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("keygen")
    make = commands.add_parser("manifest")
    make.add_argument("--repo", required=True)
    make.add_argument("--tag", required=True)
    make.add_argument("--out", required=True)
    make.add_argument("--base-url", help="where the assets are served instead of the GitHub release (local tests)")
    make.add_argument("--public-key", help="the key built into the app; refuse to sign with a key that does not match it")
    make.add_argument("--rollout", type=int, choices=range(0, 101), metavar="0-100",
                      help="offer it to this percent of machines")
    make.add_argument("--min-version", help="older versions must update (no skipping)")
    make.add_argument("--channel", choices=["stable", "beta"], default="stable")
    make.add_argument("assets", nargs="+", metavar="PLATFORM=PATH")
    control = commands.add_parser("control")
    control.add_argument("dir")
    control.add_argument("--rollout", type=int, choices=range(0, 101), metavar="0-100")
    control.add_argument("--public-key")
    control.add_argument("--min-version")
    control.add_argument("--channel", choices=["stable", "beta"], default="stable")
    pause = control.add_mutually_exclusive_group()
    pause.add_argument("--pause", action="store_true")
    pause.add_argument("--resume", action="store_true")
    bump = commands.add_parser("next-version")
    bump.add_argument("--since", help="the commit before this push (github.event.before)")
    brew = commands.add_parser("cask")
    brew.add_argument("file")
    brew.add_argument("--version", required=True)
    brew.add_argument("--sha256", required=True)
    cert = commands.add_parser("codesign-cert")
    cert.add_argument("--name", required=True)
    cert.add_argument("--out", required=True)
    note = commands.add_parser("annotate")
    note.add_argument("--app", required=True)
    note.add_argument("--version", required=True)
    note.add_argument("--project", default="323268")
    note.add_argument("--api", default="https://eu.posthog.com")
    check = commands.add_parser("check-version")
    check.add_argument("tag")
    check.add_argument("--root", default=".")
    get = commands.add_parser("config")
    get.add_argument("key")
    get.add_argument("--config", default="beacon.toml")
    args = parser.parse_args(argv)

    if args.command == "codesign-cert":
        p12, password = codesign_cert(args.name, Path(args.out))
        print(f"BEACON_CODESIGN_P12:      {base64.b64encode(p12.read_bytes()).decode()}")
        print(f"BEACON_CODESIGN_PASSWORD: {password}")
        return

    if args.command == "annotate":
        annotate(args.api, args.project, os.environ["POSTHOG_ANNOTATION_KEY"], f"{args.app} {args.version}")
        print(f"annotated {args.app} {args.version}")
        return

    if args.command == "check-version":
        problems, notes = check_version(args.tag, Path(args.root))
        if problems:
            sys.exit("\n".join(problems))
        print(notes)
        return

    if args.command == "config":
        print(config_value(args.config, args.key))
        return

    if args.command == "next-version":
        number = re.compile(r"v(\d+)\.(\d+)\.(\d+)")
        key = lambda tag: tuple(map(int, number.fullmatch(tag).groups()))
        tags = [t for t in _git("tag", "--list", "v[0-9]*") if number.fullmatch(t)]
        here = [t for t in _git("tag", "--points-at", "HEAD") if number.fullmatch(t)]
        if here:
            # This commit is a release already (a re-run): the same version, not the next one.
            version = max(here, key=key)[1:]
        else:
            try:
                # Every commit since the last release, not only this push's: a
                # feat: whose own run failed is still unreleased when a fix: follows.
                since = max(tags, key=key) if tags else args.since
                subjects = _git("log", "--pretty=%s", f"{since}..HEAD") if since else []
            except subprocess.CalledProcessError:
                subjects = []  # first push, or history rewritten: no such commit
            version = next_version(tags, subjects or _git("log", "-1", "--pretty=%s"))
        print(f"tag=v{version}\nversion={version}")
        return

    if args.command == "cask":
        path = Path(args.file)
        path.write_text(cask(path.read_text(), args.version, args.sha256))
        return

    if args.command == "keygen":
        secret = os.urandom(32)
        print("private (BEACON_SIGNING_KEY secret):", base64.b64encode(secret).decode())
        print("public (in the app):                ", base64.b64encode(_ed25519.public_key(secret)).decode())
        return

    if args.command == "control":
        out = Path(args.dir)
        name = manifest_name(args.channel)
        data = (out / name).read_bytes()
        # Sign again only what was signed before: a manifest someone changed on the
        # release must not get a valid signature from a pause or a rollout change.
        trusted = [_ed25519.public_key(_signing_key())] + ([base64.b64decode(args.public_key)] if args.public_key else [])
        try:
            signature = base64.b64decode((out / f"{name}.sig").read_text())
        except (OSError, ValueError):
            sys.exit(f"{name}.sig is missing or unreadable; not signing {name}")
        if not any(_ed25519.verify(key, data, signature) for key in trusted):
            sys.exit(f"the signature of {name} does not match this app's key: it was changed after signing; not signing")
        body = json.loads(data)
        if args.rollout is not None:
            body["rollout"] = args.rollout
        if args.pause:
            body["paused"] = True
        if args.resume:
            body.pop("paused", None)
        if args.min_version:
            body["min_version"] = args.min_version
        _sign_into(out, body, args.public_key, args.channel)
        return

    body = manifest(args.repo, args.tag, dict(a.split("=", 1) for a in args.assets), args.base_url)
    if args.rollout is not None:
        body["rollout"] = args.rollout
    if args.min_version:
        body["min_version"] = args.min_version
    _sign_into(Path(args.out), body, args.public_key, args.channel)


def manifest_name(channel):
    return "update-beta.json" if channel == "beta" else "update.json"


def _signing_key():
    """BEACON_SIGNING_KEY as bytes; an unset secret (empty in CI) or a mistyped one stops here."""
    try:
        secret = base64.b64decode(os.environ.get("BEACON_SIGNING_KEY", ""), validate=True)
    except ValueError:
        secret = b""
    if len(secret) != 32:
        sys.exit("BEACON_SIGNING_KEY is not a signing key (32 bytes, base64, from `beacon.release keygen`)")
    return secret


def _sign_into(out, body, public_key, channel="stable"):
    """Writes update(-beta).json and its .sig into `out`, signed with BEACON_SIGNING_KEY."""
    secret = _signing_key()
    # A manifest signed with the wrong key is one no installed app will accept,
    # and nothing would say so until users stopped getting updates.
    if public_key and base64.b64decode(public_key) != _ed25519.public_key(secret):
        sys.exit("BEACON_SIGNING_KEY does not belong to --public-key; not signing")
    data = json.dumps(body, indent=2).encode()
    out.mkdir(parents=True, exist_ok=True)
    name = manifest_name(channel)
    (out / name).write_bytes(data)
    (out / f"{name}.sig").write_text(base64.b64encode(_ed25519.sign(secret, data)).decode())
    print(data.decode(), file=sys.stderr)


if __name__ == "__main__":
    main()
