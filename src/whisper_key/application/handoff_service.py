from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from whisper_key.application.session_service import SessionService


class HandoffVerificationError(RuntimeError):
    pass


class HandoffService:
    SCHEMA_VERSION = 1
    MANIFEST_PATH = "handoff/handoff.json"

    def prepare(self, service: SessionService) -> dict:
        if not service.folder or not service.session:
            raise HandoffVerificationError("No session is loaded")
        folder = service.folder.resolve()
        events = service.repository.read_events(folder)
        service.repository.write_projection(
            folder,
            "handoff/session.snapshot.json",
            json.dumps(service.session.to_dict(), ensure_ascii=False, indent=2) + "\n",
        )
        service.repository.write_projection(
            folder,
            "handoff/timeline.snapshot.jsonl",
            "".join(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n" for event in events),
        )

        mode = service.session.mode.value
        required = [
            ("literal", "transcript.raw.md", True),
            ("clean", "transcript.clean.md", False),
            ("markers", "markers.md", False),
            ("mode_document", f"exports/{mode}.md", False),
            ("integrity", "integrity.json", True),
            ("session_snapshot", "handoff/session.snapshot.json", True),
            ("timeline_snapshot", "handoff/timeline.snapshot.jsonl", True),
        ]
        if (folder / "speakers.json").is_file():
            required.append(("speaker_revision", "speakers.json", False))
        inputs = [self._file_record(folder, relative, role, protected) for role, relative, protected in required]

        attachment_paths: dict[str, dict] = {}
        for event in events:
            if event.get("type") != "snapshot_created":
                continue
            payload = event.get("payload", {})
            relative = self._normalize_relative(payload.get("relative_path"))
            attachment_paths.setdefault(
                relative,
                self._file_record(
                    folder,
                    relative,
                    "marker_context" if payload.get("kind") == "audio_excerpt" else "attachment",
                    True,
                ),
            )

        manifest = {
            "schema_version": self.SCHEMA_VERSION,
            "session_id": service.session.session_id,
            "title": service.session.title,
            "mode": mode,
            "generated_from_sequence": max((event.get("sequence", 0) for event in events), default=0),
            "generated_at": events[-1].get("occurred_at") if events else service.session.updated_at,
            "privacy": {
                "uploaded_automatically": False,
                "requires_explicit_user_action": True,
                "paid_api_required": False,
            },
            "workflow": [
                {
                    "step": 1,
                    "processor": "nox-learn-anything",
                    "input": self.MANIFEST_PATH,
                    "output": "exports/downstream/learning.md",
                },
                {
                    "step": 2,
                    "processor": "nox-html-learning",
                    "input": "exports/downstream/learning.md",
                    "output": "exports/downstream/learning.html",
                },
            ],
            "inputs": inputs,
            "attachments": list(attachment_paths.values()),
            "protected_paths": [record["path"] for record in inputs if record["protected"]]
            + [record["path"] for record in attachment_paths.values()],
            "output_directory": "exports/downstream",
            "constraints": [
                "Do not edit transcript.raw.md or the handoff snapshots.",
                "Trace claims to raw-segment IDs when evidence is available.",
                "Write every generated artifact under exports/downstream/.",
                "Flag uncertainty instead of inventing missing context.",
            ],
        }
        service.repository.write_projection(
            folder,
            self.MANIFEST_PATH,
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        )
        self.verify(service)
        return manifest

    def verify(self, service: SessionService) -> dict:
        if not service.folder:
            raise HandoffVerificationError("No session is loaded")
        folder = service.folder.resolve()
        manifest_path = self._safe_target(folder, self.MANIFEST_PATH)
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise HandoffVerificationError("Prepare the handoff package first") from exc
        except json.JSONDecodeError as exc:
            raise HandoffVerificationError("handoff/handoff.json is not valid JSON") from exc
        if manifest.get("schema_version") != self.SCHEMA_VERSION:
            raise HandoffVerificationError("Unsupported handoff schema version")

        records = manifest.get("inputs", []) + manifest.get("attachments", [])
        if not records:
            raise HandoffVerificationError("The handoff manifest contains no inputs")
        seen: set[str] = set()
        total_bytes = 0
        roles: set[str] = set()
        for record in records:
            if not isinstance(record, dict):
                raise HandoffVerificationError("The handoff manifest contains an invalid file record")
            relative = self._normalize_relative(record.get("path"))
            key = relative.casefold()
            if key in seen:
                raise HandoffVerificationError(f"Duplicate handoff path: {relative}")
            seen.add(key)
            target = self._safe_target(folder, relative)
            if not target.is_file():
                raise HandoffVerificationError(f"Missing handoff input: {relative}")
            actual_bytes = target.stat().st_size
            if record.get("bytes") != actual_bytes:
                raise HandoffVerificationError(f"Size changed for handoff input: {relative}")
            actual_hash = self._sha256(target)
            if record.get("sha256", "").casefold() != actual_hash:
                raise HandoffVerificationError(f"SHA-256 changed for handoff input: {relative}")
            total_bytes += actual_bytes
            roles.add(str(record.get("role", "")))
        required_roles = {"literal", "clean", "session_snapshot", "timeline_snapshot"}
        missing_roles = sorted(required_roles - roles)
        if missing_roles:
            raise HandoffVerificationError(f"Missing required handoff roles: {', '.join(missing_roles)}")
        return {
            "manifest": self.MANIFEST_PATH,
            "files": len(records),
            "bytes": total_bytes,
            "generated_from_sequence": manifest.get("generated_from_sequence", 0),
        }

    def _file_record(self, folder: Path, relative: str, role: str, protected: bool) -> dict:
        normalized = self._normalize_relative(relative)
        target = self._safe_target(folder, normalized)
        if not target.is_file():
            raise HandoffVerificationError(f"Required handoff input is missing: {normalized}")
        return {
            "role": role,
            "path": normalized,
            "bytes": target.stat().st_size,
            "sha256": self._sha256(target),
            "protected": protected,
        }

    @staticmethod
    def _normalize_relative(value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise HandoffVerificationError("Empty or non-string handoff path")
        normalized = value.replace("\\", "/")
        relative = PurePosixPath(normalized)
        if relative.is_absolute() or ".." in relative.parts or "." in relative.parts:
            raise HandoffVerificationError(f"Unsafe handoff path: {value}")
        if not relative.parts or ":" in relative.parts[0]:
            raise HandoffVerificationError(f"Unsafe handoff path: {value}")
        return relative.as_posix()

    @classmethod
    def _safe_target(cls, folder: Path, relative: str) -> Path:
        normalized = cls._normalize_relative(relative)
        target = (folder / PurePosixPath(normalized)).resolve()
        if target != folder and folder not in target.parents:
            raise HandoffVerificationError(f"Handoff path escapes the session: {relative}")
        return target

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
