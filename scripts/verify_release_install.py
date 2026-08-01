from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True)
class VerificationResult:
    file_count: int
    total_bytes: int
    extra_files: tuple[str, ...]


class VerificationError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_relative_path(value: object) -> PurePosixPath:
    if not isinstance(value, str) or not value:
        raise VerificationError("Manifest contains an empty or non-string path")
    relative = PurePosixPath(value)
    if relative.is_absolute() or ".." in relative.parts or "." in relative.parts:
        raise VerificationError(f"Unsafe manifest path: {value}")
    return relative


def verify_installation(
    installation: Path,
    manifest_path: Path,
    *,
    allow_extra: bool = False,
) -> VerificationResult:
    installation = installation.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = manifest.get("files")
    if not isinstance(records, list) or not records:
        raise VerificationError("Manifest does not contain a non-empty files list")

    expected: set[str] = set()
    total_bytes = 0
    errors: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            raise VerificationError("Manifest file record is not an object")
        relative = _safe_relative_path(record.get("path"))
        normalized = relative.as_posix()
        key = normalized.casefold()
        if key in expected:
            raise VerificationError(f"Duplicate manifest path: {normalized}")
        expected.add(key)

        target = installation.joinpath(*relative.parts)
        if not target.is_file():
            errors.append(f"missing: {normalized}")
            continue
        expected_bytes = record.get("bytes")
        if not isinstance(expected_bytes, int) or expected_bytes < 0:
            raise VerificationError(f"Invalid byte count for {normalized}")
        actual_bytes = target.stat().st_size
        if actual_bytes != expected_bytes:
            errors.append(f"size: {normalized} expected={expected_bytes} actual={actual_bytes}")
            continue
        expected_hash = record.get("sha256")
        if not isinstance(expected_hash, str) or len(expected_hash) != 64:
            raise VerificationError(f"Invalid SHA-256 for {normalized}")
        actual_hash = sha256(target)
        if actual_hash.casefold() != expected_hash.casefold():
            errors.append(f"sha256: {normalized} expected={expected_hash} actual={actual_hash}")
            continue
        total_bytes += actual_bytes

    actual = {
        path.relative_to(installation).as_posix().casefold() for path in installation.rglob("*") if path.is_file()
    }
    extras = tuple(sorted(actual - expected))
    if extras and not allow_extra:
        errors.extend(f"extra: {item}" for item in extras)
    if errors:
        preview = "\n".join(errors[:25])
        suffix = f"\n... and {len(errors) - 25} more" if len(errors) > 25 else ""
        raise VerificationError(f"Release verification failed:\n{preview}{suffix}")

    declared_count = manifest.get("file_count")
    if declared_count != len(records):
        raise VerificationError(f"Manifest file_count is {declared_count}, but contains {len(records)} records")
    declared_bytes = manifest.get("total_bytes")
    if declared_bytes != total_bytes:
        raise VerificationError(f"Manifest total_bytes is {declared_bytes}, verified {total_bytes}")
    return VerificationResult(len(records), total_bytes, extras)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify an installed WhisperKey tree against release evidence.")
    parser.add_argument("installation", type=Path)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--allow-extra", action="store_true", help="Allow installer metadata and uninstaller files")
    args = parser.parse_args()
    try:
        result = verify_installation(args.installation, args.manifest, allow_extra=args.allow_extra)
    except (OSError, ValueError, VerificationError) as exc:
        parser.exit(1, f"{exc}\n")
    print(
        f"Verified {result.file_count} files / {result.total_bytes} bytes"
        f"; extra files allowed={len(result.extra_files)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
