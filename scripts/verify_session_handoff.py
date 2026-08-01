from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from whisper_key.application import ProcessingService, SessionService
from whisper_key.domain.session import SessionStatus


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def protected_evidence(folder: Path) -> dict[str, str]:
    paths = [folder / "transcript.raw.md"]
    for relative_root in ("audio", "attachments"):
        root = folder / relative_root
        if root.exists():
            paths.extend(path for path in root.rglob("*") if path.is_file())
    return {path.relative_to(folder).as_posix(): sha256(path) for path in sorted(paths) if path.is_file()}


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify P6 handoff on a copied completed session.")
    parser.add_argument("library", type=Path)
    parser.add_argument("session_id")
    parser.add_argument("--models", type=Path)
    args = parser.parse_args()

    service = SessionService(args.library.resolve())
    session = service.load(args.session_id)
    if session.status != SessionStatus.COMPLETED:
        parser.error(f"Session must be completed, got {session.status.value}")
    before = protected_evidence(service.folder)
    updates: list[dict[str, str]] = []
    processing = ProcessingService((args.models or args.library / "models").resolve())
    processing.run_all(
        service,
        lambda job, status, detail: updates.append({"job": job, "status": status, "detail": detail}),
    )
    after = protected_evidence(service.folder)
    changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
    report = processing.handoff.verify(service)
    final_states = {}
    for update in updates:
        final_states[update["job"]] = update["status"]
    result = {
        "session_id": session.session_id,
        "session_folder": str(service.folder),
        "protected_evidence_files": len(before),
        "protected_evidence_changed": changed,
        "handoff": report,
        "jobs": final_states,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if changed or any(status == "failed" for status in final_states.values()):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
