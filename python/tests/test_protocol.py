"""The shared vectors in protocol/vectors: every client checks the same cases."""

import base64
import json
from pathlib import Path

import pytest

from beacon import system, update

VECTORS = Path(__file__).resolve().parents[2] / "protocol" / "vectors"
MANIFEST = json.loads((VECTORS / "manifest" / "cases.json").read_text())

@pytest.mark.parametrize("case", json.loads((VECTORS / "versions.json").read_text()))
def test_versions(case):
    assert update.is_newer(case["candidate"], case["current"]) is case["newer"]


@pytest.mark.parametrize("case", MANIFEST["cases"], ids=lambda c: c.get("why") or f"{c['file']} {c['current']}")
def test_manifest(case):
    folder = VECTORS / "manifest"
    data = (folder / case["file"]).read_bytes()
    signature = base64.b64decode((folder / case.get("signature", case["file"] + ".sig")).read_text())
    keys = [base64.b64decode(k) for k in case["keys"]]
    if "error" in case:
        with pytest.raises(update.UpdateError) as e:
            update.parse_manifest(data, signature, keys, case["current"], case.get("machine_id", ""))
        assert e.value.step == case["error"]
        return
    found = update.parse_manifest(data, signature, keys, case["current"], case.get("machine_id", ""))
    if case["offered"] is None:
        assert found is None
    else:
        assert found.version == case["offered"]
        assert found.url.endswith(f"/App-{system.platform_key()}")
        assert found.required is case.get("required", False)


@pytest.mark.parametrize("case", json.loads((VECTORS / "rollout.json").read_text())["cases"])
def test_rollout_bucket(case):
    assert update.rollout_bucket(case["machine_id"], case["version"]) == case["bucket"]


@pytest.mark.parametrize("case", json.loads((VECTORS / "machine_id.json").read_text())["cases"])
def test_machine_id(case):
    assert system.hashed_machine_id(case["raw"]) == case["machine_id"]


def test_this_machine_has_an_id():
    # macOS and Windows both say; an empty id would fall back to the install id.
    assert len(system.machine_id()) == 32
