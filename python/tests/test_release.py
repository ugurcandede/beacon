import base64
import json
import subprocess
import sys
from pathlib import Path

import pytest

from beacon import _ed25519, release

# RFC 8032 section 7.1, test 1 (as in test_update).
SECRET = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
PUBLIC = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")


@pytest.mark.parametrize("tags,subjects,expected", [
    ([], ["first"], "0.0.1"),
    (["v1.2.3"], ["fix: x"], "1.2.4"),
    (["v1.2.3"], ["fix: x", "feat: y"], "1.3.0"),           # any commit in the push counts
    (["v1.2.3"], ["Feat: y"], "1.3.0"),
    (["v1.2.3"], ["feat: y", "BREAKING: z"], "2.0.0"),
    (["v1.2.3"], ["bump: z"], "2.0.0"),
    (["v2.5.0", "v2.4.1", "v10.0.0-rc", "latest"], ["fix"], "2.5.1"),  # highest, not nearest
    (["v1.9.0", "v1.10.0"], ["fix"], "1.10.1"),             # numeric, not text order
])
def test_next_version(tags, subjects, expected):
    assert release.next_version(tags, subjects) == expected


def test_next_version_from_git(tmp_path, monkeypatch, capsys):
    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "fix: one")
    git("tag", "v1.0.0")
    before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True).stdout.strip()
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "feat: two")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "fix: three")
    monkeypatch.chdir(tmp_path)

    release.main(["next-version", "--since", before])
    assert capsys.readouterr().out == "tag=v1.1.0\nversion=1.1.0\n"
    # Whatever the push held: every commit since the last release counts, so the feat: is not lost.
    release.main(["next-version", "--since", "0" * 40])
    assert capsys.readouterr().out == "tag=v1.1.0\nversion=1.1.0\n"
    release.main(["next-version"])
    assert capsys.readouterr().out == "tag=v1.1.0\nversion=1.1.0\n"


CASK = '''cask "lumea" do
  version "2.1.0"
  sha256 "8692f404"

  url "https://github.com/ugurcandede/lumea/releases/download/v#{version}/Lumea-macos-arm64.zip"
end
'''


def test_cask_sets_version_and_sha(tmp_path):
    path = tmp_path / "lumea.rb"
    path.write_text(CASK)
    release.main(["cask", str(path), "--version", "2.2.0", "--sha256", "abc"])
    assert path.read_text() == CASK.replace('"2.1.0"', '"2.2.0"').replace('"8692f404"', '"abc"')


def test_cask_without_sha_line_fails():
    with pytest.raises(ValueError, match="sha256"):
        release.cask('cask "x" do\n  version "1"\nend\n', "2", "abc")


def test_config_value_reads_beacon_toml(tmp_path, capsys):
    toml = tmp_path / "beacon.toml"
    toml.write_text('name = "Lumea"\npublic_key = "abc="\ncask = "lumea"\n[python]\nentry = "main.py"\n')
    assert release.config_value(toml, "public_key") == "abc="
    assert release.config_value(toml, "cask") == "lumea"
    assert release.config_value(toml, "missing") == ""
    assert release.config_value(toml, "python") == ""  # a table, not a value
    assert release.config_value(tmp_path / "nope.toml", "cask") == ""
    release.main(["config", "cask", "--config", str(toml)])
    assert capsys.readouterr().out == "lumea\n"


def signed(tmp_path, monkeypatch, *extra):
    asset = tmp_path / "App.exe"
    asset.write_bytes(b"MZ")
    monkeypatch.setenv("BEACON_SIGNING_KEY", base64.b64encode(SECRET).decode())
    release.main(["manifest", "--repo", "me/app", "--tag", "v1.0.0", "--out", str(tmp_path / "out"),
                  *extra, f"windows-x64={asset}"])
    return tmp_path / "out"


def body_and_check(out):
    data = (out / "update.json").read_bytes()
    assert _ed25519.verify(PUBLIC, data, base64.b64decode((out / "update.json.sig").read_text()))
    return json.loads(data)


def test_manifest_with_rollout(tmp_path, monkeypatch):
    out = signed(tmp_path, monkeypatch, "--rollout", "10")
    assert body_and_check(out)["rollout"] == 10


def test_control_pauses_widens_and_resumes_with_a_fresh_signature(tmp_path, monkeypatch):
    out = signed(tmp_path, monkeypatch, "--rollout", "10")
    release.main(["control", str(out), "--pause"])
    assert body_and_check(out)["paused"] is True
    release.main(["control", str(out), "--resume", "--rollout", "50"])
    body = body_and_check(out)
    assert "paused" not in body and body["rollout"] == 50
    assert body["version"] == "1.0.0" and "windows-x64" in body["assets"]


def test_control_refuses_a_key_not_the_apps(tmp_path, monkeypatch):
    out = signed(tmp_path, monkeypatch)
    with pytest.raises(SystemExit, match="does not belong"):
        release.main(["control", str(out), "--pause", "--public-key", base64.b64encode(b"\x02" * 32).decode()])
    assert "paused" not in body_and_check(out)


@pytest.mark.skipif(sys.platform == "win32", reason="made on the owner's Mac; Windows runners' openssl lacks -legacy")
def test_codesign_cert_is_a_code_signing_p12(tmp_path, capsys):
    release.main(["codesign-cert", "--name", "test apps", "--out", str(tmp_path)])
    out = capsys.readouterr().out
    password = out.split("BEACON_CODESIGN_PASSWORD: ")[1].strip()
    p12 = tmp_path / "codesign.p12"
    assert base64.b64decode(out.split("BEACON_CODESIGN_P12:")[1].split()[0]) == p12.read_bytes()
    assert not (tmp_path / "codesign.key").exists()  # the key lives only in the p12
    legacy = release._pkcs12_legacy()
    text = subprocess.run(["openssl", "pkcs12", *legacy, "-in", str(p12), "-nokeys", "-passin", f"pass:{password}"],
                          capture_output=True, check=True).stdout
    details = subprocess.run(["openssl", "x509", "-noout", "-text"], input=text, capture_output=True, check=True).stdout
    assert b"Code Signing" in details and b"CN=test apps" in details.replace(b" = ", b"=")


def beacon_tree(tmp_path, python="1.4.0", swift="1.4.0", changelog="## Unreleased\n\n## 1.4.0\n\n- New thing.\n\n## 1.3.0\n\n- Old.\n"):
    (tmp_path / "python/beacon").mkdir(parents=True)
    (tmp_path / "swift/Sources/Beacon").mkdir(parents=True)
    (tmp_path / "python/beacon/__init__.py").write_text(f'__version__ = "{python}"\n')
    (tmp_path / "swift/Sources/Beacon/System.swift").write_text(f'    static let libVersion = "{swift}"\n')
    (tmp_path / "CHANGELOG.md").write_text(changelog)
    return tmp_path


def test_check_version_passes_and_gives_the_notes(tmp_path):
    assert release.check_version("v1.4.0", beacon_tree(tmp_path)) == ([], "- New thing.")


@pytest.mark.parametrize("kwargs,problem", [
    ({"python": "1.3.0"}, "python/beacon/__init__.py says 1.3.0"),
    ({"swift": "1.3.9"}, "System.swift says 1.3.9"),
    ({"changelog": "## Unreleased\n\n- New thing.\n"}, "no '## 1.4.0' section"),
    ({"changelog": "## 1.4.0\n\n## 1.3.0\n- Old.\n"}, "no '## 1.4.0' section"),  # empty
    ({"changelog": "## 1.4.01\n\n- Other.\n"}, "no '## 1.4.0' section"),
])
def test_check_version_problems(tmp_path, kwargs, problem):
    problems, _ = release.check_version("v1.4.0", beacon_tree(tmp_path, **kwargs))
    assert any(problem in p for p in problems), problems


def test_this_repo_versions_agree_with_each_other():
    import beacon

    problems, _ = release.check_version(f"v{beacon.__version__}", Path(__file__).resolve().parents[2])
    assert not [p for p in problems if "CHANGELOG" not in p]  # the section is written when releasing


def test_manifest_for_the_beta_channel_and_a_minimum_version(tmp_path, monkeypatch):
    out = signed(tmp_path, monkeypatch, "--channel", "beta", "--min-version", "0.9.0")
    assert not (out / "update.json").exists()
    data = (out / "update-beta.json").read_bytes()
    assert _ed25519.verify(PUBLIC, data, base64.b64decode((out / "update-beta.json.sig").read_text()))
    assert json.loads(data)["min_version"] == "0.9.0"


def test_control_sets_a_minimum_version(tmp_path, monkeypatch):
    out = signed(tmp_path, monkeypatch)
    release.main(["control", str(out), "--min-version", "1.0.0"])
    assert body_and_check(out)["min_version"] == "1.0.0"


def test_annotate_marks_the_release_on_posthog(monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path, self.headers["Authorization"],
                             json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(201)
            self.end_headers()
            self.wfile.write(b'{"id": 1}')

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("POSTHOG_ANNOTATION_KEY", "phx_test")
        release.main(["annotate", "--app", "lumea", "--version", "1.4.0", "--project", "42",
                      "--api", f"http://127.0.0.1:{server.server_port}"])
    finally:
        server.shutdown()
    [(path, auth, body)] = received
    assert path == "/api/projects/42/annotations/" and auth == "Bearer phx_test"
    assert body["content"] == "lumea 1.4.0" and body["scope"] == "project" and body["date_marker"]
