from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path

from whisper_key.application.diarization_service import DiarizationService
from whisper_key.application.handoff_service import HandoffService
from whisper_key.application.session_service import SessionService
from whisper_key.domain.projections import (
    render_clean_markdown,
    render_handoff_markdown,
    render_markers_markdown,
    render_mode_markdown,
    render_self_contained_html,
)
from whisper_key.infrastructure.marker_context import MarkerContextService
from whisper_key.infrastructure.sherpa_diarization import DiarizationAudioChunk, SherpaDiarizationAdapter


class ProcessingService:
    # The native diarizer currently combines every MIC chunk into one inference
    # buffer. Keep automatic processing below a conservative limit until the
    # adapter is truly streaming. Raw audio and transcript remain complete.
    AUTO_DIARIZATION_MAX_MS = 10 * 60 * 1000
    JOBS = (
        "clean",
        "markers",
        "mode",
        "html",
        "marker_context",
        "diarization",
        "integrity",
        "handoff",
        "handoff_verify",
    )

    def __init__(self, diarization_model_root: Path, *, enable_automatic_diarization: bool = False):
        self.marker_context = MarkerContextService()
        self.diarization_adapter = SherpaDiarizationAdapter(diarization_model_root)
        self.diarization = DiarizationService()
        self.enable_automatic_diarization = enable_automatic_diarization
        self.handoff = HandoffService()
        self._attempts: dict[str, int] = {}

    def run_all(self, service: SessionService, callback: Callable[[str, str, str], None] | None = None) -> None:
        for job in self.JOBS:
            self.run_job(service, job, callback)

    def queue_jobs(self, service: SessionService, jobs: tuple[str, ...] | None = None) -> tuple[str, ...]:
        selected = jobs or self.JOBS
        latest: dict[str, dict] = {}
        for event in service.repository.read_events(service.folder):
            if event.get("type") == "processing_job":
                payload = event.get("payload", {})
                latest[str(payload.get("job", ""))] = payload
        queued: list[str] = []
        for job in selected:
            previous = latest.get(job, {})
            if previous.get("status") in {"queued", "processing", "complete", "skipped"}:
                continue
            attempt = max(1, int(previous.get("attempt", 0)) + 1)
            service.record_processing_job(job, "queued", attempt)
            queued.append(job)
        return tuple(queued)

    def pending_jobs(self, service: SessionService, jobs: tuple[str, ...] | None = None) -> tuple[str, ...]:
        selected = jobs or self.JOBS
        latest: dict[str, str] = {}
        for event in service.repository.read_events(service.folder):
            if event.get("type") != "processing_job":
                continue
            payload = event.get("payload", {})
            latest[str(payload.get("job", ""))] = str(payload.get("status", ""))
        return tuple(job for job in selected if latest.get(job) not in {"complete", "skipped"})

    def run_pending(
        self,
        service: SessionService,
        callback: Callable[[str, str, str], None] | None = None,
        jobs: tuple[str, ...] | None = None,
    ) -> tuple[str, ...]:
        pending = self.pending_jobs(service, jobs)
        for job in pending:
            self.run_job(service, job, callback)
        return pending

    def run_job(
        self,
        service: SessionService,
        job: str,
        callback: Callable[[str, str, str], None] | None = None,
    ) -> tuple[str, str]:
        if job not in self.JOBS:
            raise ValueError(f"Unknown processing job: {job}")
        durable_events = [
            event["payload"]
            for event in service.repository.read_events(service.folder)
            if event.get("type") == "processing_job" and event["payload"].get("job") == job
        ]
        latest = durable_events[-1] if durable_events else {}
        if latest.get("status") == "queued":
            attempt = int(latest["attempt"])
        else:
            durable_attempts = [int(payload["attempt"]) for payload in durable_events]
            attempt = max(self._attempts.get(job, 0), max(durable_attempts, default=0)) + 1
        self._attempts[job] = attempt
        service.record_processing_job(job, "processing", attempt)
        if callback:
            callback(job, "processing", "")
        started = time.monotonic()
        try:
            output, status = getattr(self, f"_run_{job}")(service)
            duration_ms = max(0, round((time.monotonic() - started) * 1000))
            service.record_processing_job(job, status, attempt, output=output, duration_ms=duration_ms)
            if callback:
                callback(job, status, output or "")
            return status, output or ""
        except Exception as exc:
            duration_ms = max(0, round((time.monotonic() - started) * 1000))
            service.record_processing_job(job, "failed", attempt, error=str(exc), duration_ms=duration_ms)
            if callback:
                callback(job, "failed", str(exc))
            return "failed", str(exc)

    def _run_integrity(self, service: SessionService) -> tuple[str, str]:
        folder = service.folder
        records = []
        for path in sorted(folder.rglob("*")):
            relative_path = path.relative_to(folder)
            if (
                not path.is_file()
                or ".index" in path.parts
                or path.name in {"integrity.json", "timeline.jsonl", "session.json"}
                or relative_path.parts[:1] == ("handoff",)
                or relative_path.as_posix() == "handoff.md"
                or relative_path.parts[:2] == ("exports", "downstream")
            ):
                continue
            records.append(
                {
                    "path": relative_path.as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": self._sha256_file(path),
                }
            )
        relative = "integrity.json"
        service.repository.write_projection(
            folder,
            relative,
            json.dumps({"schema_version": 1, "files": records}, indent=2),
        )
        return relative, "complete"

    def _run_marker_context(self, service: SessionService) -> tuple[str, str]:
        created = self.marker_context.build(service)
        return f"{len(created)} excerpts", "complete"

    def _run_diarization(self, service: SessionService) -> tuple[str, str]:
        events = service.repository.read_events(service.folder)
        relative = "speakers.json"
        fallback = self.diarization.build_revision(self.diarization.assign(events, []))
        service.repository.write_projection(service.folder, relative, json.dumps(fallback, indent=2))
        if not self.enable_automatic_diarization:
            return f"{relative} · diarización automática desactivada por estabilidad", "skipped"
        if not self.diarization_adapter.available:
            return relative, "skipped"
        mic_duration_ms = sum(
            max(0, int(event.get("payload", {}).get("duration_ms", 0)))
            for event in events
            if event.get("type") == "audio_chunk_finalized"
            and event.get("payload", {}).get("source") == "MIC"
        )
        if mic_duration_ms > self.AUTO_DIARIZATION_MAX_MS:
            minutes = self.AUTO_DIARIZATION_MAX_MS // 60_000
            return f"{relative} · omitida automáticamente (audio MIC > {minutes} min)", "skipped"
        chunks = []
        folder = service.folder.resolve()
        for event in events:
            if event.get("type") != "audio_chunk_finalized" or event["payload"]["source"] != "MIC":
                continue
            payload = event["payload"]
            path = (folder / payload["relative_path"]).resolve()
            if folder not in path.parents or not path.is_file():
                raise ValueError(f"Missing or unsafe diarization audio: {payload['relative_path']}")
            chunks.append(DiarizationAudioChunk(path, payload["started_at_ms"], payload["ended_at_ms"]))
        if not chunks:
            return relative, "skipped"
        turns = self.diarization_adapter.process_files(chunks)
        revision = self.diarization.build_revision(self.diarization.assign(events, turns))
        service.repository.write_projection(service.folder, relative, json.dumps(revision, indent=2))
        return relative, "complete"

    def _run_clean(self, service: SessionService) -> tuple[str, str]:
        if self._active_clean_is_manual(service):
            return "transcript.clean.md · revisión manual protegida", "skipped"
        events = service.repository.read_events(service.folder)
        content = render_clean_markdown(
            service.session.to_dict(),
            events,
            speaker_revision=self._speaker_revision(service),
        )
        relative = "transcript.clean.md"
        service.repository.write_projection(service.folder, relative, content)
        return relative, "complete"

    def _run_markers(self, service: SessionService) -> tuple[str, str]:
        events = service.repository.read_events(service.folder)
        relative = "markers.md"
        service.repository.write_projection(
            service.folder,
            relative,
            render_markers_markdown(service.session.to_dict(), events),
        )
        return relative, "complete"

    def _run_mode(self, service: SessionService) -> tuple[str, str]:
        events = service.repository.read_events(service.folder)
        relative = f"exports/{service.session.mode.value}.md"
        clean_path = service.folder / "transcript.clean.md"
        markers_path = service.folder / "markers.md"
        service.repository.write_projection(
            service.folder,
            relative,
            render_mode_markdown(
                service.session.to_dict(),
                events,
                speaker_revision=self._speaker_revision(service),
                clean_markdown=clean_path.read_text(encoding="utf-8") if clean_path.is_file() else None,
                markers_markdown=markers_path.read_text(encoding="utf-8") if markers_path.is_file() else None,
            ),
        )
        return relative, "complete"

    def _run_handoff(self, service: SessionService) -> tuple[str, str]:
        manifest = self.handoff.prepare(service)
        relative = "handoff.md"
        service.repository.write_projection(
            service.folder,
            relative,
            render_handoff_markdown(service.session.to_dict(), manifest),
        )
        return relative, "complete"

    def _run_handoff_verify(self, service: SessionService) -> tuple[str, str]:
        report = self.handoff.verify(service)
        return f"{report['manifest']} · {report['files']} archivos · {report['bytes']} bytes", "complete"

    def _run_html(self, service: SessionService) -> tuple[str, str]:
        events = service.repository.read_events(service.folder)
        embedded = {}
        total = 0
        for event in events:
            if event["type"] != "snapshot_created" or not event["payload"]["media_type"].startswith("image/"):
                continue
            relative_path = event["payload"]["relative_path"]
            path = service.folder / relative_path
            data = path.read_bytes()
            total += len(data)
            if total > 25 * 1024 * 1024:
                raise ValueError("Embedded images exceed the 25 MB offline HTML limit")
            encoded = base64.b64encode(data).decode("ascii")
            embedded[relative_path] = f"data:{event['payload']['media_type']};base64,{encoded}"
        relative = "exports/session.html"
        content = render_self_contained_html(
            service.session.to_dict(),
            events,
            embedded,
            speaker_revision=self._speaker_revision(service),
        )
        service.repository.write_projection(service.folder, relative, content)
        return relative, "complete"

    @staticmethod
    def _speaker_revision(service: SessionService) -> dict | None:
        path = service.folder / "speakers.json"
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _active_clean_is_manual(service: SessionService) -> bool:
        path = service.folder / "clean-revisions.json"
        current = service.folder / "transcript.clean.md"
        if not path.is_file() or not current.is_file():
            return False
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            revisions = manifest.get("revisions", [])
            active_revision = manifest.get("active_revision")
            active = next(
                (item for item in revisions if item.get("revision") == active_revision),
                revisions[-1] if revisions else None,
            )
            if not active or active.get("kind") != "manual":
                return False
            digest = ProcessingService._sha256_file(current)
            return digest.casefold() == str(active.get("sha256", "")).casefold()
        except (OSError, ValueError, json.JSONDecodeError):
            return False

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
