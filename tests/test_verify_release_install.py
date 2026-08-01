from __future__ import annotations

import hashlib
import json

import pytest

from scripts.verify_release_install import VerificationError, verify_installation


def _manifest(root, *names):
    records = []
    for name in names:
        payload = (root / name).read_bytes()
        records.append(
            {
                "path": name,
                "bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    path = root.parent / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "file_count": len(records),
                "total_bytes": sum(record["bytes"] for record in records),
                "files": records,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_verifies_exact_release_tree(tmp_path):
    install = tmp_path / "install"
    install.mkdir()
    (install / "WhisperKey.exe").write_bytes(b"binary")
    manifest = _manifest(install, "WhisperKey.exe")

    result = verify_installation(install, manifest)

    assert result.file_count == 1
    assert result.total_bytes == 6
    assert result.extra_files == ()


def test_detects_corrupt_file(tmp_path):
    install = tmp_path / "install"
    install.mkdir()
    (install / "WhisperKey.exe").write_bytes(b"before")
    manifest = _manifest(install, "WhisperKey.exe")
    (install / "WhisperKey.exe").write_bytes(b"afters")

    with pytest.raises(VerificationError, match="sha256"):
        verify_installation(install, manifest)


def test_rejects_path_traversal(tmp_path):
    install = tmp_path / "install"
    install.mkdir()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "file_count": 1,
                "total_bytes": 1,
                "files": [{"path": "../outside", "bytes": 1, "sha256": "0" * 64}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(VerificationError, match="Unsafe"):
        verify_installation(install, manifest)


def test_allows_installer_metadata_only_when_requested(tmp_path):
    install = tmp_path / "install"
    install.mkdir()
    (install / "WhisperKey.exe").write_bytes(b"binary")
    manifest = _manifest(install, "WhisperKey.exe")
    (install / "unins000.exe").write_bytes(b"uninstaller")

    with pytest.raises(VerificationError, match="extra"):
        verify_installation(install, manifest)

    result = verify_installation(install, manifest, allow_extra=True)
    assert result.extra_files == ("unins000.exe",)
