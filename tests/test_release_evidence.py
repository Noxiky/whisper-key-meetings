from importlib.metadata import PackagePath

import pytest

from scripts import build_release_evidence


def test_capture_critical_runtime_files_are_required(tmp_path):
    distribution = tmp_path / "WhisperKey"
    distribution.mkdir()

    with pytest.raises(ValueError, match="silero_vad_v6[.]onnx"):
        build_release_evidence.validate_required_runtime_files(distribution)

    for relative in build_release_evidence.REQUIRED_RUNTIME_FILES:
        path = distribution / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"runtime")

    build_release_evidence.validate_required_runtime_files(distribution)


def test_license_collector_is_portable_hashed_and_excludes_code(monkeypatch, tmp_path):
    relative_files = (
        PackagePath("first.dist-info/licenses/LICENSE.txt"),
        PackagePath("vendor/licenses/LICENSE.txt"),
        PackagePath("first.dist-info/licenses/helper.pyc"),
    )
    for relative, content in zip(relative_files, (b"license one", b"license two", b"code"), strict=True):
        source = tmp_path / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(content)

    class FakeDistribution:
        files = relative_files

        @staticmethod
        def locate_file(relative):
            return tmp_path / relative

    monkeypatch.setattr(
        build_release_evidence.importlib.metadata,
        "distribution",
        lambda _name: FakeDistribution(),
    )
    destination = tmp_path / "evidence"

    records = build_release_evidence.collect_license_files("Example Runtime", destination)

    assert len(records) == 2
    assert len({record["path"] for record in records}) == 2
    assert all(".." not in record["path"] for record in records)
    assert all(len(record["sha256"]) == 64 for record in records)
    assert all((destination / record["path"]).is_file() for record in records)


def test_cyclonedx_sbom_has_stable_unique_components_and_release_binding():
    manifest = {
        "product": "WhisperKey",
        "version": "0.9.0",
        "platform": "windows-x64",
        "created_utc": "2026-07-17T12:00:00+00:00",
        "file_count": 778,
        "total_bytes": 123,
    }
    records = [
        {"name": "PySide6", "version": "6.10.2", "license": "LGPL-3.0-only", "homepage": "Qt"},
        {"name": "faster-whisper", "version": "1.2.1", "license": "MIT", "homepage": "GitHub"},
    ]

    first = build_release_evidence.build_cyclonedx_sbom(manifest, records, "a" * 64)
    second = build_release_evidence.build_cyclonedx_sbom(manifest, list(reversed(records)), "a" * 64)

    assert first == second
    assert first["bomFormat"] == "CycloneDX"
    assert first["$schema"] == "https://cyclonedx.org/schema/bom-1.7.schema.json"
    assert first["specVersion"] == "1.7"
    assert first["serialNumber"].startswith("urn:uuid:")
    refs = [item["bom-ref"] for item in first["components"]]
    assert len(refs) == len(set(refs)) == 2
    root = first["metadata"]["component"]
    properties = {item["name"]: item["value"] for item in root["properties"]}
    assert properties["whisperkey:release-manifest-sha256"] == "a" * 64
    assert first["dependencies"][0]["dependsOn"] == refs
