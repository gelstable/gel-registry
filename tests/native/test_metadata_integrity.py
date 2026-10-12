"""Unsigned RPM checks reject auxiliary payloads before they can be signed."""

import gzip
import hashlib
from typing import Any
from xml.etree import ElementTree as ET

import pytest

from .test_render import render
from .test_sign_validate import sign

pytestmark = pytest.mark.native_tools


@pytest.mark.parametrize("damage", ["xml", "open-checksum", "inventory"])
def test_signing_rejects_invalid_auxiliary_metadata(
    native_repo: Any, monkeypatch: Any, damage: str
) -> None:
    repo, _, _ = native_repo
    assert render(native_repo)[2] == 0
    repodata = repo / "public/rpm/stable/x86_64/repodata"
    for path in repodata.iterdir():
        path.unlink()
    repomd = ET.Element("repomd")
    bodies = {
        "primary": b'<metadata xmlns="http://linux.duke.edu/metadata/common" '
        b'packages="0"/>',
        "filelists": b'<filelists xmlns="http://linux.duke.edu/metadata/filelists" '
        b'packages="0"/>',
        "other": b'<otherdata xmlns="http://linux.duke.edu/metadata/other" '
        b'packages="0"/>',
    }
    if damage == "xml":
        bodies["filelists"] = b"not XML"
    elif damage == "inventory":
        bodies["filelists"] = (
            b'<filelists xmlns="http://linux.duke.edu/metadata/filelists" packages="1">'
            b'<package pkgid="bad" name="extra" arch="x86_64">'
            b'<version epoch="0" ver="1" rel="1"/></package></filelists>'
        )
    for kind, raw in bodies.items():
        compressed = gzip.compress(raw, mtime=0)
        filename = f"{kind}.xml.gz"
        (repodata / filename).write_bytes(compressed)
        data = ET.SubElement(repomd, "data", type=kind)
        ET.SubElement(data, "location", href="repodata/" + filename)
        ET.SubElement(data, "checksum", type="sha256").text = hashlib.sha256(
            compressed
        ).hexdigest()
        ET.SubElement(data, "size").text = str(len(compressed))
        ET.SubElement(data, "open-size").text = str(len(raw))
        ET.SubElement(data, "open-checksum", type="sha256").text = (
            "0" * 64
            if damage == "open-checksum" and kind == "other"
            else hashlib.sha256(raw).hexdigest()
        )
    (repodata / "repomd.xml").write_bytes(ET.tostring(repomd))
    assert sign(repo, repo.parent / "gnupg", monkeypatch) == 1
    assert not (repodata / "repomd.xml.asc").exists()
