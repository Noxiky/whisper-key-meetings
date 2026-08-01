from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import re
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

RUNTIME_DISTRIBUTIONS = (
    "av",
    "ctranslate2",
    "faster-whisper",
    "global-hotkeys",
    "huggingface-hub",
    "numpy",
    "onnxruntime",
    "Pillow",
    "playsound3",
    "pyperclip",
    "pystray",
    "PySide6",
    "PySide6-Addons",
    "PySide6-Essentials",
    "pywin32",
    "ruamel.yaml",
    "sherpa-onnx",
    "shiboken6",
    "soundcard",
    "sounddevice",
    "soxr",
    "ten-vad",
    "tokenizers",
    "nvidia-cublas-cu12",
    "nvidia-cuda-runtime-cu12",
    "nvidia-cuda-nvrtc-cu12",
    "nvidia-cudnn-cu12",
)

REQUIRED_RUNTIME_FILES = (
    "_internal/faster_whisper/assets/silero_vad_v6.onnx",
    "_internal/ten_vad/lib/Windows/x64/ten_vad.dll",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def metadata_record(name: str) -> dict[str, str]:
    metadata = importlib.metadata.metadata(name)
    license_value = metadata.get("License-Expression") or metadata.get("License") or "Not declared"
    return {
        "name": metadata.get("Name", name),
        "version": importlib.metadata.version(name),
        "license": " ".join(license_value.split()),
        "homepage": metadata.get("Home-page") or metadata.get("Project-URL") or "Not declared",
    }


def validate_required_runtime_files(distribution: Path) -> None:
    """Reject a package that can start but cannot execute its capture-critical path."""
    missing = []
    for relative in REQUIRED_RUNTIME_FILES:
        path = distribution.joinpath(*Path(relative).parts)
        if not path.is_file() or path.stat().st_size <= 0:
            missing.append(relative)
    if missing:
        raise ValueError("Missing capture-critical runtime files: " + ", ".join(missing))


def collect_license_files(name: str, destination: Path) -> list[dict[str, str | int]]:
    distribution = importlib.metadata.distribution(name)
    package_folder = destination / re.sub(r"[^a-z0-9._-]+", "-", name.casefold())
    records = []
    for relative in distribution.files or ():
        basename = relative.name.casefold()
        parts = {part.casefold() for part in relative.parts}
        legal_name = basename.startswith(("license", "copying", "notice", "authors"))
        if not (legal_name or "licenses" in parts) or basename.endswith((".py", ".pyc", ".pyo")):
            continue
        source = Path(distribution.locate_file(relative))
        if not source.is_file():
            continue
        digest = sha256(source)
        filename = source.name
        target = package_folder / filename
        if target.exists() and sha256(target) != digest:
            target = package_folder / f"{source.stem}-{digest[:12]}{source.suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copyfile(source, target)
        records.append(
            {
                "source": relative.as_posix(),
                "path": target.relative_to(destination).as_posix(),
                "bytes": target.stat().st_size,
                "sha256": digest,
            }
        )
    return records


def build_cyclonedx_sbom(manifest: dict, records: list[dict[str, str]], manifest_sha256: str) -> dict:
    """Return a minimal CycloneDX 1.7 inventory without claiming unavailable hashes."""
    version = str(manifest["version"])
    root_ref = f"pkg:generic/whisperkey@{version}?arch=x86_64&os=windows"
    components = []
    component_refs = []
    for record in sorted(records, key=lambda item: item["name"].casefold()):
        name = str(record["name"])
        package_version = str(record["version"])
        normalized = re.sub(r"[-_.]+", "-", name).casefold()
        component_ref = f"pkg:pypi/{normalized}@{package_version}"
        component_refs.append(component_ref)
        components.append(
            {
                "type": "library",
                "bom-ref": component_ref,
                "name": name,
                "version": package_version,
                "purl": component_ref,
                "licenses": [{"license": {"name": str(record["license"])}}],
                "properties": [
                    {"name": "whisperkey:metadata-source", "value": "python-distribution"},
                    {"name": "whisperkey:homepage-metadata", "value": str(record["homepage"])},
                ],
            }
        )
    serial = uuid.uuid5(
        uuid.NAMESPACE_URL,
        f"https://whisperkey.local/sbom/{version}/{manifest_sha256}",
    )
    return {
        "$schema": "https://cyclonedx.org/schema/bom-1.7.schema.json",
        "bomFormat": "CycloneDX",
        "specVersion": "1.7",
        "serialNumber": f"urn:uuid:{serial}",
        "version": 1,
        "metadata": {
            "timestamp": manifest["created_utc"],
            "component": {
                "type": "application",
                "bom-ref": root_ref,
                "name": "WhisperKey",
                "version": version,
                "properties": [
                    {"name": "whisperkey:platform", "value": str(manifest["platform"])},
                    {"name": "whisperkey:file-count", "value": str(manifest["file_count"])},
                    {"name": "whisperkey:total-bytes", "value": str(manifest["total_bytes"])},
                    {"name": "whisperkey:release-manifest-sha256", "value": manifest_sha256},
                ],
            },
        },
        "components": components,
        "dependencies": [{"ref": root_ref, "dependsOn": component_refs}],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Create reproducible release evidence for a WhisperKey folder.")
    parser.add_argument("distribution", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--version", default="0.9.0")
    args = parser.parse_args()

    distribution = args.distribution.resolve()
    output = args.output.resolve()
    executable = distribution / "WhisperKey.exe"
    if not executable.is_file():
        raise SystemExit(f"Missing packaged executable: {executable}")
    try:
        validate_required_runtime_files(distribution)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    output.mkdir(parents=True, exist_ok=True)
    files = []
    for path in sorted(item for item in distribution.rglob("*") if item.is_file()):
        files.append(
            {
                "path": path.relative_to(distribution).as_posix(),
                "bytes": path.stat().st_size,
                "sha256": sha256(path),
            }
        )

    manifest = {
        "product": "WhisperKey",
        "version": args.version,
        "platform": "windows-x64",
        "created_utc": datetime.now(UTC).isoformat(),
        "entrypoint": "WhisperKey.exe",
        "file_count": len(files),
        "total_bytes": sum(item["bytes"] for item in files),
        "files": files,
    }
    manifest_path = output / "release-manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (output / "SHA256SUMS.txt").write_text(
        "".join(f"{item['sha256']}  {item['path']}\n" for item in files),
        encoding="utf-8",
    )

    records = []
    licenses_root = output / "THIRD_PARTY_LICENSES"
    if licenses_root.exists():
        shutil.rmtree(licenses_root)
    license_index = []
    for name in RUNTIME_DISTRIBUTIONS:
        try:
            record = metadata_record(name)
            records.append(record)
            license_index.append(
                {
                    "distribution": record["name"],
                    "version": record["version"],
                    "declared_license": record["license"],
                    "files": collect_license_files(name, licenses_root),
                }
            )
        except importlib.metadata.PackageNotFoundError:
            records.append({"name": name, "version": "not found", "license": "unknown", "homepage": "unknown"})
            license_index.append(
                {
                    "distribution": name,
                    "version": "not found",
                    "declared_license": "unknown",
                    "files": [],
                }
            )
    licenses_root.mkdir(parents=True, exist_ok=True)
    (licenses_root / "INDEX.json").write_text(
        json.dumps(
            {
                "generated_utc": datetime.now(UTC).isoformat(),
                "distributions": license_index,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    collected_count = sum(len(item["files"]) for item in license_index)
    missing_texts = [item["distribution"] for item in license_index if not item["files"]]
    lines = [
        "WhisperKey 0.9.0 - Third-party runtime inventory\n",
        "Generated from the isolated build environment. This is an inventory, not legal advice.\n\n",
        f"Collected legal/attribution files: {collected_count}.\n",
        "See THIRD_PARTY_LICENSES/INDEX.json for source paths, sizes, and SHA-256 values.\n",
        "No bundled legal text was discovered for: "
        + (", ".join(missing_texts) if missing_texts else "none")
        + ". This absence requires review; it is not a license conclusion.\n\n",
    ]
    for record in sorted(records, key=lambda item: item["name"].lower()):
        lines.append(f"{record['name']} {record['version']}\n")
        lines.append(f"  License metadata: {record['license']}\n")
        lines.append(f"  Project metadata: {record['homepage']}\n")
    (output / "THIRD_PARTY_NOTICES.txt").write_text("".join(lines), encoding="utf-8")
    sbom = build_cyclonedx_sbom(manifest, records, sha256(manifest_path))
    (output / "sbom.cdx.json").write_text(
        json.dumps(sbom, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
