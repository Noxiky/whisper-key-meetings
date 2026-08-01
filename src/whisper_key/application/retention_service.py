from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4

from whisper_key.application.session_service import SessionService
from whisper_key.domain.session import SessionStatus


@dataclass(frozen=True)
class RetentionCandidate:
    relative_path: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class RetentionPreview:
    preview_id: str
    policy: str
    candidates: tuple[RetentionCandidate, ...]
    total_bytes: int
    blocked_reason: str | None
    requires_verification: bool
    verified: bool
    restorable: bool
    message: str

    def to_dict(self) -> dict:
        return {
            "preview_id": self.preview_id,
            "policy": self.policy,
            "candidates": [asdict(item) for item in self.candidates],
            "total_bytes": self.total_bytes,
            "blocked_reason": self.blocked_reason,
            "requires_verification": self.requires_verification,
            "verified": self.verified,
            "restorable": self.restorable,
            "message": self.message,
        }


class RetentionService:
    def __init__(self, library_root: Path):
        self.library_root = Path(library_root).resolve()
        self.trash_root = self.library_root / ".trash" / "retention"

    def recover_incomplete(self) -> list[str]:
        recovered: list[str] = []
        if not self.trash_root.is_dir():
            return recovered
        for manifest_path in self.trash_root.rglob("retention.json"):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if manifest.get("state") not in {"moving", "moved"}:
                continue
            application_id = manifest.get("application_id")
            try:
                session_folder = self._safe_library_folder(manifest["session_folder"])
            except (KeyError, ValueError):
                continue
            if self._timeline_has_application(session_folder, "retention_applied", application_id):
                manifest["state"] = "committed"
                self._write_manifest(manifest_path.parent, manifest)
                recovered.append(f"committed:{application_id}")
                continue
            restored = []
            conflict = False
            for item in manifest.get("items", []):
                try:
                    destination = self._safe_session_audio(session_folder, item["relative_path"])
                except (KeyError, ValueError):
                    conflict = True
                    break
                source = manifest_path.parent / PurePosixPath(item["relative_path"])
                if not source.exists():
                    continue
                if destination.exists():
                    conflict = True
                    break
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, destination)
                restored.append(item["relative_path"])
            manifest["state"] = "needs_attention" if conflict else "rolled_back"
            manifest["recovered_items"] = restored
            self._write_manifest(manifest_path.parent, manifest)
            recovered.append(f"{manifest['state']}:{application_id}")
        return recovered

    def preview(self, service: SessionService, *, verified: bool = False) -> RetentionPreview:
        session = service.session
        folder = service.folder
        if not session or not folder:
            raise RuntimeError("No hay una sesión abierta")
        events = service.repository.read_events(folder)
        policy = session.retention["audio"]
        restorable = self._active_application(events) is not None
        if session.status != SessionStatus.COMPLETED:
            return self._blocked(
                session.session_id,
                policy,
                "La retención solo puede aplicarse después de finalizar la sesión.",
                verified,
                restorable,
            )
        if policy == "all":
            return RetentionPreview(
                preview_id=self._preview_id(session.session_id, policy, verified, ()),
                policy=policy,
                candidates=(),
                total_bytes=0,
                blocked_reason=None,
                requires_verification=False,
                verified=verified,
                restorable=restorable,
                message="Esta sesión conserva todo el audio; no hay nada que retirar.",
            )
        if policy == "until_verified" and not verified:
            return self._blocked(
                session.session_id,
                policy,
                "Confirma primero que revisaste el documento literal.",
                verified,
                restorable,
                requires_verification=True,
            )
        if policy == "marker_context" and not self._marker_context_ready(
            folder.resolve(),
            events,
            session.retention,
        ):
            return self._blocked(
                session.session_id,
                policy,
                "Los extractos de contexto de los marcadores todavía no terminaron correctamente.",
                verified,
                restorable,
            )

        candidates, unsafe_reason = self._audio_candidates(folder, events)
        if unsafe_reason:
            return self._blocked(
                session.session_id,
                policy,
                unsafe_reason,
                verified,
                restorable,
            )
        total = sum(item.bytes for item in candidates)
        message = (
            f"{len(candidates)} archivo(s) se moverán a la papelera recuperable de WhisperKey."
            if candidates
            else "La política ya está aplicada; no quedan archivos de audio candidatos."
        )
        return RetentionPreview(
            preview_id=self._preview_id(session.session_id, policy, verified, candidates),
            policy=policy,
            candidates=candidates,
            total_bytes=total,
            blocked_reason=None,
            requires_verification=policy == "until_verified",
            verified=verified,
            restorable=restorable,
            message=message,
        )

    def apply(self, service: SessionService, preview_id: str, *, verified: bool = False) -> dict:
        preview = self.preview(service, verified=verified)
        if preview.blocked_reason:
            raise RuntimeError(preview.blocked_reason)
        if preview.preview_id != preview_id:
            raise RuntimeError("La sesión cambió; revisa nuevamente la lista antes de aplicar la retención")
        if not preview.candidates:
            return {"moved": 0, "total_bytes": 0, "restorable": preview.restorable}

        session = service.session
        folder = service.folder.resolve()
        application_id = str(uuid4())
        trash_folder = self.trash_root / session.session_id / application_id
        trash_key = trash_folder.relative_to(self.library_root).as_posix()
        manifest = {
            "schema_version": 1,
            "state": "moving",
            "application_id": application_id,
            "session_id": session.session_id,
            "session_folder": folder.relative_to(self.library_root).as_posix(),
            "policy": preview.policy,
            "preview_id": preview.preview_id,
            "items": [asdict(item) for item in preview.candidates],
        }
        trash_folder.mkdir(parents=True, exist_ok=False)
        self._write_manifest(trash_folder, manifest)
        moved: list[tuple[Path, Path]] = []
        try:
            for item in preview.candidates:
                source = self._safe_session_audio(folder, item.relative_path)
                destination = trash_folder / PurePosixPath(item.relative_path)
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, destination)
                moved.append((source, destination))
            manifest["state"] = "moved"
            self._write_manifest(trash_folder, manifest)
        except Exception:
            self._restore_moves(moved)
            manifest["state"] = "rolled_back"
            self._write_manifest(trash_folder, manifest)
            raise

        try:
            event = service.record_retention_applied(
                {
                    "application_id": application_id,
                    "policy": preview.policy,
                    "preview_id": preview.preview_id,
                    "trash_key": trash_key,
                    "items": [asdict(item) for item in preview.candidates],
                    "total_bytes": preview.total_bytes,
                }
            )
        except Exception:
            event = self._find_retention_event(service, "retention_applied", application_id)
            if not event:
                self._restore_moves(moved)
                manifest["state"] = "rolled_back"
                self._write_manifest(trash_folder, manifest)
                raise
        try:
            manifest["state"] = "committed"
            manifest["event_id"] = event["event_id"]
            self._write_manifest(trash_folder, manifest)
        except OSError:
            pass
        return {
            "moved": len(preview.candidates),
            "total_bytes": preview.total_bytes,
            "restorable": True,
        }

    def restore_latest(self, service: SessionService) -> dict:
        session = service.session
        folder = service.folder
        if not session or not folder or session.status != SessionStatus.COMPLETED:
            raise RuntimeError("Solo puede restaurarse audio de una sesión finalizada")
        events = service.repository.read_events(folder)
        application = self._active_application(events)
        if not application:
            return {"restored": 0, "total_bytes": 0, "restorable": False}
        payload = application["payload"]
        trash_folder = self._safe_trash_folder(payload["trash_key"])
        manifest_path = trash_folder / "retention.json"
        if not manifest_path.is_file():
            raise FileNotFoundError("La papelera recuperable ya no contiene el manifiesto de esta sesión")

        moves: list[tuple[Path, Path]] = []
        for item in payload["items"]:
            destination = self._safe_session_audio(folder.resolve(), item["relative_path"])
            source = trash_folder / PurePosixPath(item["relative_path"])
            if destination.exists():
                raise FileExistsError(f"No se sobrescribirá el archivo existente: {item['relative_path']}")
            if not source.is_file() or self._sha256(source) != item["sha256"]:
                raise RuntimeError(f"El audio recuperable falta o cambió: {item['relative_path']}")
            moves.append((source, destination))

        restored: list[tuple[Path, Path]] = []
        try:
            for source, destination in moves:
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, destination)
                restored.append((source, destination))
        except Exception:
            self._undo_restores(restored)
            raise

        try:
            service.record_retention_restored(
                {
                    "application_id": payload["application_id"],
                    "items": [item["relative_path"] for item in payload["items"]],
                    "total_bytes": payload["total_bytes"],
                }
            )
        except Exception:
            event = self._find_retention_event(
                service,
                "retention_restored",
                payload["application_id"],
            )
            if not event:
                self._undo_restores(restored)
                raise
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["state"] = "restored"
            self._write_manifest(trash_folder, manifest)
        except (OSError, json.JSONDecodeError):
            pass
        return {
            "restored": len(restored),
            "total_bytes": payload["total_bytes"],
            "restorable": False,
        }

    def _audio_candidates(
        self,
        folder: Path,
        events: list[dict],
    ) -> tuple[tuple[RetentionCandidate, ...], str | None]:
        chunks: dict[str, dict] = {}
        for event in events:
            if event.get("type") == "audio_chunk_finalized":
                chunks[event["payload"]["relative_path"]] = event["payload"]
        candidates = []
        for relative, payload in sorted(chunks.items()):
            try:
                path = self._safe_session_audio(folder.resolve(), relative)
            except ValueError:
                return (), f"La ruta de audio no es segura: {relative}"
            if not path.exists():
                continue
            if not path.is_file():
                return (), f"La ruta de audio no es un archivo: {relative}"
            size = path.stat().st_size
            digest = self._sha256(path)
            if size < 1 or digest != payload["sha256"]:
                return (), f"El audio cambió desde su registro y no se tocará: {relative}"
            candidates.append(RetentionCandidate(relative, size, digest))
        return tuple(candidates), None

    def _marker_context_ready(self, folder: Path, events: list[dict], policy: dict) -> bool:
        markers = [event["payload"] for event in events if event.get("type") == "marker_created"]
        if not markers:
            return True
        states = [
            event["payload"]["status"]
            for event in events
            if event.get("type") == "processing_job" and event["payload"].get("job") == "marker_context"
        ]
        if not states or states[-1] != "complete":
            return False
        if not policy["marker_context_before_ms"] and not policy["marker_context_after_ms"]:
            return True
        sources = {
            event["payload"]["source"].lower() for event in events if event.get("type") == "audio_chunk_finalized"
        }
        excerpts = {
            event["payload"]["relative_path"]: event["payload"]
            for event in events
            if event.get("type") == "snapshot_created" and event["payload"].get("kind") == "audio_excerpt"
        }
        for marker in markers:
            for source in sources:
                relative = f"audio/excerpts/{marker['marker_id']}-{source}.wav"
                attachment = excerpts.get(relative)
                path = folder / PurePosixPath(relative)
                if not attachment or not path.is_file() or self._sha256(path) != attachment["sha256"]:
                    return False
        return True

    @staticmethod
    def _active_application(events: list[dict]) -> dict | None:
        restored = {event["payload"]["application_id"] for event in events if event.get("type") == "retention_restored"}
        for event in reversed(events):
            if event.get("type") == "retention_applied":
                return None if event["payload"]["application_id"] in restored else event
        return None

    def _safe_session_audio(self, folder: Path, relative: str) -> Path:
        normalized = PurePosixPath(relative)
        if (
            normalized.is_absolute()
            or len(normalized.parts) != 3
            or normalized.parts[0] != "audio"
            or normalized.parts[1] not in {"mic", "sys"}
            or normalized.suffix.lower() != ".wav"
            or ".." in normalized.parts
        ):
            raise ValueError(relative)
        path = (folder / normalized).resolve()
        if folder not in path.parents:
            raise ValueError(relative)
        return path

    def _safe_trash_folder(self, trash_key: str) -> Path:
        relative = PurePosixPath(trash_key)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Ruta de papelera no segura")
        path = (self.library_root / relative).resolve()
        if self.trash_root not in path.parents:
            raise ValueError("Ruta de papelera fuera de WhisperKey")
        return path

    def _safe_library_folder(self, relative_path: str) -> Path:
        relative = PurePosixPath(relative_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Ruta de sesión no segura")
        path = (self.library_root / relative).resolve()
        if self.library_root not in path.parents:
            raise ValueError("Ruta fuera de la biblioteca")
        return path

    @staticmethod
    def _timeline_has_application(folder: Path, event_type: str, application_id: str) -> bool:
        timeline = folder / "timeline.jsonl"
        if not timeline.is_file():
            return False
        try:
            lines = timeline.read_text(encoding="utf-8").splitlines()
        except OSError:
            return False
        for line in reversed(lines):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == event_type and event.get("payload", {}).get("application_id") == application_id:
                return True
        return False

    @staticmethod
    def _find_retention_event(
        service: SessionService,
        event_type: str,
        application_id: str,
    ) -> dict | None:
        for event in reversed(service.repository.read_events(service.folder)):
            if event.get("type") == event_type and event.get("payload", {}).get("application_id") == application_id:
                return event
        return None

    @staticmethod
    def _restore_moves(moved: list[tuple[Path, Path]]) -> None:
        for source, destination in reversed(moved):
            if destination.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                os.replace(destination, source)

    @staticmethod
    def _undo_restores(restored: list[tuple[Path, Path]]) -> None:
        for source, destination in reversed(restored):
            if destination.exists() and not source.exists():
                source.parent.mkdir(parents=True, exist_ok=True)
                os.replace(destination, source)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _preview_id(
        session_id: str,
        policy: str,
        verified: bool,
        candidates: tuple[RetentionCandidate, ...],
    ) -> str:
        canonical = json.dumps(
            {
                "session_id": session_id,
                "policy": policy,
                "verified": verified,
                "items": [asdict(item) for item in candidates],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def _blocked(
        self,
        session_id: str,
        policy: str,
        reason: str,
        verified: bool,
        restorable: bool,
        *,
        requires_verification: bool = False,
    ) -> RetentionPreview:
        return RetentionPreview(
            preview_id=self._preview_id(session_id, policy, verified, ()),
            policy=policy,
            candidates=(),
            total_bytes=0,
            blocked_reason=reason,
            requires_verification=requires_verification,
            verified=verified,
            restorable=restorable,
            message=reason,
        )

    @staticmethod
    def _write_manifest(folder: Path, manifest: dict) -> None:
        destination = folder / "retention.json"
        temporary = folder / ".retention.json.tmp"
        temporary.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        with temporary.open("r+b") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
