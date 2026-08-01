import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from whisper_key.domain.projections import render_clean_markdown, render_literal_markdown
from whisper_key.domain.session import (
    InvalidSessionTransition,
    Session,
    SessionMode,
    SessionStage,
    SessionStatus,
    StageStatus,
    isoformat_utc,
    rebuild_session,
)
from whisper_key.infrastructure.session_repository import SessionRepository


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


class SessionService:
    def __init__(self, root: Path, producer_version: str = "0.9.0", clock=None):
        self.repository = SessionRepository(root)
        self.producer_version = producer_version
        self.clock = clock or SystemClock()
        self.session: Session | None = None
        self.folder: Path | None = None
        self._anchor_monotonic = self.clock.monotonic()
        self._anchor_offset_ms = 0
        self._status_started_monotonic = self._anchor_monotonic
        self._next_sequence = 1
        self._commit_lock = threading.RLock()
        self._pending_spoken_note_marker_id: str | None = None

    def create(self, mode: SessionMode | str, retention: dict | None = None) -> Session:
        if self.session and self.session.status != SessionStatus.COMPLETED:
            raise InvalidSessionTransition("An unfinished session is already active")
        now = isoformat_utc(self.clock.now())
        session = Session(
            session_id=str(uuid4()),
            mode=SessionMode(mode),
            status=SessionStatus.DRAFT,
            created_at=now,
            updated_at=now,
            retention=self._normalize_retention(retention),
        )
        self.session = session
        self.folder = self.repository.create_folder(session.session_id)
        self._reset_time_anchor(0)
        self._next_sequence = 1
        self._pending_spoken_note_marker_id = None
        self._commit(
            "session_created",
            {"mode": session.mode.value, "retention": dict(session.retention)},
        )
        return session

    def load(self, session_id: str) -> Session:
        self.folder = self.repository.find_folder(session_id)
        events = self.repository.read_events(self.folder)
        self.session = rebuild_session(events)
        self.repository.save_session(self.folder, self.session)
        offset = events[-1]["session_offset_ms"] if events else 0
        self._reset_time_anchor(offset)
        self._next_sequence = (events[-1]["sequence"] + 1) if events else 1
        self._pending_spoken_note_marker_id = None
        return self.session

    def start_stage(self) -> SessionStage:
        session = self._require_session()
        if session.status not in {SessionStatus.DRAFT, SessionStatus.RECOVERABLE}:
            raise InvalidSessionTransition(f"Cannot start a stage from {session.status}")
        stage = SessionStage(
            stage_id=str(uuid4()),
            sequence=len(session.stages) + 1,
            started_at=isoformat_utc(self.clock.now()),
        )
        session.stages.append(stage)
        self._change_status(SessionStatus.RECORDING)
        self._commit("stage_started", {"stage_id": stage.stage_id, "stage_sequence": stage.sequence})
        return stage

    def pause(self) -> None:
        session = self._require_status(SessionStatus.RECORDING)
        self._accrue_status_time(session)
        self._reconcile_capture_timing_from_audio("pause")
        session.status = SessionStatus.PAUSED
        session.active_stage.status = StageStatus.PAUSED
        self._status_started_monotonic = self.clock.monotonic()
        self._commit("pause_started", {"stage_id": session.active_stage.stage_id})

    def resume(self) -> None:
        session = self._require_status(SessionStatus.PAUSED)
        self._accrue_status_time(session)
        session.status = SessionStatus.RECORDING
        session.active_stage.status = StageStatus.RECORDING
        self._status_started_monotonic = self.clock.monotonic()
        self._commit("pause_ended", {"stage_id": session.active_stage.stage_id})

    def interrupt(self) -> None:
        session = self._require_session()
        if session.status not in {SessionStatus.RECORDING, SessionStatus.PAUSED}:
            raise InvalidSessionTransition(f"Cannot interrupt from {session.status}")
        self._accrue_status_time(session)
        self._reconcile_capture_timing_from_audio("interruption")
        stage = session.active_stage
        stage.status = StageStatus.INTERRUPTED
        stage.ended_at = isoformat_utc(self.clock.now())
        session.status = SessionStatus.RECOVERABLE
        self._commit("stage_ended", {"stage_id": stage.stage_id, "reason": "interrupted"})

    def recover(self) -> bool:
        session = self._require_session()
        recoverable_statuses = {
            SessionStatus.RECORDING,
            SessionStatus.PAUSED,
            SessionStatus.PREPARING,
            SessionStatus.PROCESSING,
        }
        if session.status not in recoverable_statuses:
            return False
        previous_status = session.status.value
        stage = session.active_stage
        if stage:
            stage.status = StageStatus.INTERRUPTED
            stage.ended_at = isoformat_utc(self.clock.now())
        session.status = SessionStatus.RECOVERABLE
        self._commit("recovery_detected", {"previous_status": previous_status})
        return True

    def name(self, title: str) -> None:
        session = self._require_session()
        normalized = " ".join(title.split()).strip()
        if not normalized:
            raise ValueError("Session title is required")
        if len(normalized) > 200:
            raise ValueError("Session title is too long")
        session.title = normalized
        self._commit("session_named", {"title": normalized})

    def add_transcript(
        self,
        source: str,
        raw_text: str,
        started_at_ms: int,
        ended_at_ms: int,
        language: str | None = None,
        confidence: float | None = None,
        audio_path: str | None = None,
    ) -> str:
        self._require_status(SessionStatus.RECORDING)
        if source not in {"MIC", "SYS", "MIXED", "IMPORTED"}:
            raise ValueError("Unknown transcript source")
        if started_at_ms < 0 or ended_at_ms < started_at_ms:
            raise ValueError("Invalid transcript time range")
        if confidence is not None and not 0 <= confidence <= 1:
            raise ValueError("Confidence must be between zero and one")
        normalized = raw_text.strip()
        if not normalized:
            raise ValueError("Transcript text is required")
        segment_id = str(uuid4())
        self._commit(
            "transcript_final",
            {
                "segment_id": segment_id,
                "source": source,
                "started_at_ms": started_at_ms,
                "ended_at_ms": ended_at_ms,
                "language": language,
                "raw_text": normalized,
                "confidence": confidence,
                "audio_path": audio_path,
            },
        )
        if source == "MIC":
            self._consume_spoken_note(segment_id, started_at_ms, ended_at_ms, normalized)
        return segment_id

    def add_provisional_transcript(
        self,
        source: str,
        raw_text: str,
        started_at_ms: int,
        ended_at_ms: int,
    ) -> str:
        self._require_status(SessionStatus.RECORDING)
        if source not in {"MIC", "SYS", "MIXED", "IMPORTED"}:
            raise ValueError("Unknown transcript source")
        if started_at_ms < 0 or ended_at_ms < started_at_ms:
            raise ValueError("Invalid transcript time range")
        normalized = raw_text.strip()
        if not normalized:
            raise ValueError("Provisional transcript text is required")
        provisional_id = str(uuid4())
        self._commit(
            "transcript_provisional",
            {
                "provisional_id": provisional_id,
                "source": source,
                "started_at_ms": started_at_ms,
                "ended_at_ms": ended_at_ms,
                "raw_text": normalized,
            },
        )
        return provisional_id

    def add_marker(self, kind: str, note: str | None = None, at_ms: int | None = None) -> str:
        session = self._require_session()
        if session.status not in {SessionStatus.RECORDING, SessionStatus.PAUSED}:
            raise InvalidSessionTransition(f"Cannot add marker from {session.status}")
        if kind not in {"question", "not_understood", "important", "investigate", "quote", "disagreement", "action"}:
            raise ValueError("Unknown marker kind")
        marker_offset = self._offset_ms() if at_ms is None else at_ms
        if marker_offset < 0:
            raise ValueError("Marker time cannot be negative")
        marker_id = str(uuid4())
        self._commit(
            "marker_created",
            {
                "marker_id": marker_id,
                "kind": kind,
                "at_ms": marker_offset,
                "context_before_ms": session.retention["marker_context_before_ms"],
                "context_after_ms": session.retention["marker_context_after_ms"],
                "note": note,
            },
        )
        return marker_id

    def arm_spoken_note(self, kind: str, note: str | None = None) -> str:
        with self._commit_lock:
            self._require_status(SessionStatus.RECORDING)
            if self._pending_spoken_note_marker_id:
                raise InvalidSessionTransition("A spoken note is already waiting for microphone speech")
            marker_id = self.add_marker(kind, note)
            self._pending_spoken_note_marker_id = marker_id
            return marker_id

    def _consume_spoken_note(self, segment_id: str, started_at_ms: int, ended_at_ms: int, text: str) -> None:
        with self._commit_lock:
            marker_id = self._pending_spoken_note_marker_id
            if not marker_id:
                return
            self._commit(
                "spoken_note",
                {
                    "spoken_note_id": str(uuid4()),
                    "marker_id": marker_id,
                    "segment_id": segment_id,
                    "started_at_ms": started_at_ms,
                    "ended_at_ms": ended_at_ms,
                    "raw_text": text,
                },
            )
            self._pending_spoken_note_marker_id = None

    def add_attachment(
        self,
        *,
        kind: str,
        relative_path: str,
        media_type: str,
        sha256: str,
        at_ms: int | None = None,
        width: int | None = None,
        height: int | None = None,
        duration_ms: int | None = None,
        source: str | None = None,
    ) -> str:
        session = self._require_session()
        if session.status not in {
            SessionStatus.RECORDING,
            SessionStatus.PAUSED,
            SessionStatus.RECOVERABLE,
            SessionStatus.PROCESSING,
            SessionStatus.COMPLETED,
        }:
            raise InvalidSessionTransition(f"Cannot attach media from {session.status}")
        if kind not in {"screenshot", "photo", "audio_excerpt", "imported_file"}:
            raise ValueError("Unknown attachment kind")
        normalized_path = relative_path.replace("\\", "/")
        if not normalized_path or Path(normalized_path).is_absolute() or ".." in Path(normalized_path).parts:
            raise ValueError("Attachment path must stay inside the session")
        if not re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", media_type):
            raise ValueError("Invalid attachment media type")
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("Invalid attachment digest")
        attachment_offset = self._offset_ms() if at_ms is None else at_ms
        if attachment_offset < 0:
            raise ValueError("Attachment time cannot be negative")
        if width is not None and width < 1 or height is not None and height < 1:
            raise ValueError("Attachment dimensions must be positive")
        if duration_ms is not None and duration_ms < 1:
            raise ValueError("Attachment duration must be positive")
        if source is not None and source not in {"MIC", "SYS"}:
            raise ValueError("Unknown attachment source")
        attachment_id = str(uuid4())
        payload = {
            "attachment_id": attachment_id,
            "kind": kind,
            "at_ms": attachment_offset,
            "relative_path": normalized_path,
            "media_type": media_type,
            "sha256": sha256,
        }
        optional = {
            "width": width,
            "height": height,
            "duration_ms": duration_ms,
            "source": source,
        }
        payload.update({key: value for key, value in optional.items() if value is not None})
        self._commit("snapshot_created", payload)
        return attachment_id

    def add_source_health(self, source_id: str, status: str, detail: str, fatal: bool = False) -> None:
        session = self._require_session()
        if session.status not in {SessionStatus.RECORDING, SessionStatus.PAUSED}:
            return
        self._commit(
            "source_health",
            {"source_id": source_id, "status": status, "detail": detail, "fatal": fatal},
        )

    def add_audio_chunk(
        self,
        *,
        source: str,
        relative_path: str,
        started_at_ms: int,
        ended_at_ms: int,
        sample_rate: int,
        channels: int,
        frames: int,
        sha256: str,
    ) -> None:
        session = self._require_session()
        if session.status not in {SessionStatus.RECORDING, SessionStatus.PAUSED}:
            raise InvalidSessionTransition(f"Cannot finalize audio from {session.status}")
        if source not in {"MIC", "SYS", "IMPORTED"}:
            raise ValueError("Unknown audio source")
        normalized_path = relative_path.replace("\\", "/")
        if not normalized_path or Path(normalized_path).is_absolute() or ".." in Path(normalized_path).parts:
            raise ValueError("Audio path must stay inside the session")
        if started_at_ms < 0 or ended_at_ms <= started_at_ms:
            raise ValueError("Invalid audio time range")
        if sample_rate < 1 or channels < 1 or frames < 1:
            raise ValueError("Invalid audio metadata")
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ValueError("Invalid audio digest")
        self._commit(
            "audio_chunk_finalized",
            {
                "source": source,
                "relative_path": normalized_path,
                "started_at_ms": started_at_ms,
                "ended_at_ms": ended_at_ms,
                # Timeline offsets can jump when Windows resumes from sleep. WAV frames are the
                # durable source of truth for how much audio was actually captured.
                "duration_ms": max(1, round(frames / sample_rate * 1000)),
                "sample_rate": sample_rate,
                "channels": channels,
                "frames": frames,
                "sha256": sha256,
            },
        )

    def live_captured_duration_ms(self) -> int:
        """Return captured time including the currently active recording interval."""
        with self._commit_lock:
            session = self._require_session()
            captured = session.captured_duration_ms
            if session.status == SessionStatus.RECORDING:
                captured += max(0, round((self.clock.monotonic() - self._status_started_monotonic) * 1000))
            return captured

    def current_offset_ms(self) -> int:
        with self._commit_lock:
            self._require_session()
            return self._offset_ms()

    def record_processing_job(
        self,
        job: str,
        status: str,
        attempt: int,
        output: str | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        if status not in {"queued", "processing", "complete", "failed", "skipped"}:
            raise ValueError("Unknown processing status")
        if duration_ms is not None and duration_ms < 0:
            raise ValueError("Processing duration cannot be negative")
        if duration_ms is not None:
            self._require_session().processing_duration_ms += duration_ms
        payload = {
            "job": job,
            "status": status,
            "attempt": attempt,
            "output": output,
            "error": error,
            "duration_ms": duration_ms,
        }
        self._commit("processing_job", payload)

    def record_retention_applied(self, payload: dict) -> dict:
        self._require_status(SessionStatus.COMPLETED)
        return self._commit("retention_applied", payload)

    def record_retention_restored(self, payload: dict) -> dict:
        self._require_status(SessionStatus.COMPLETED)
        return self._commit("retention_restored", payload)

    def finalize(self) -> Path:
        session = self._require_session()
        if session.status == SessionStatus.COMPLETED:
            if self.folder.parent == self.repository.inbox:
                self.folder = self.repository.promote(self.folder, session)
            return self.folder
        if not session.title:
            raise ValueError("Name the session before finalizing")
        if session.status in {SessionStatus.RECORDING, SessionStatus.PAUSED}:
            self._finish_active_stage()
        if session.status not in {SessionStatus.DRAFT, SessionStatus.RECOVERABLE}:
            raise InvalidSessionTransition(f"Cannot finalize from {session.status}")
        session.status = SessionStatus.PROCESSING
        self._commit("processing_started", {"jobs": ["literal_markdown"]})
        self._commit("session_finalized", {})
        events = self.repository.read_events(self.folder)
        literal = render_literal_markdown(session.to_dict(), events)
        self.repository.write_projection(self.folder, "transcript.raw.md", literal)
        self._commit("projection_written", {"kind": "literal_markdown", "relative_path": "transcript.raw.md"})
        events = self.repository.read_events(self.folder)
        clean = render_clean_markdown(session.to_dict(), events)
        self.repository.write_projection(self.folder, "transcript.clean.md", clean)
        self._commit("projection_written", {"kind": "clean_markdown", "relative_path": "transcript.clean.md"})
        session.status = SessionStatus.COMPLETED
        self._commit("session_completed", {})
        self.folder = self.repository.promote(self.folder, session)
        return self.folder

    def finish_stage(self) -> None:
        session = self._require_session()
        if session.status not in {SessionStatus.RECORDING, SessionStatus.PAUSED}:
            raise InvalidSessionTransition(f"Cannot finish a stage from {session.status}")
        self._finish_active_stage()

    def _finish_active_stage(self) -> None:
        session = self._require_session()
        self._accrue_status_time(session)
        self._reconcile_capture_timing_from_audio("stage_end")
        stage = session.active_stage
        stage.status = StageStatus.COMPLETED
        stage.ended_at = isoformat_utc(self.clock.now())
        session.status = SessionStatus.RECOVERABLE
        self._commit("stage_ended", {"stage_id": stage.stage_id, "reason": "completed"})

    def _commit(self, event_type: str, payload: dict) -> dict:
        with self._commit_lock:
            session = self._require_session()
            folder = self._require_folder()
            event = {
                "schema_version": 1,
                "event_id": str(uuid4()),
                "session_id": session.session_id,
                "sequence": self._next_sequence,
                "occurred_at": isoformat_utc(self.clock.now()),
                "session_offset_ms": self._offset_ms(),
                "producer_version": self.producer_version,
                "type": event_type,
                "payload": payload,
            }
            session.updated_at = event["occurred_at"]
            self.repository.append_event(folder, event)
            self._next_sequence += 1
            self.repository.save_session(folder, session)
            return event

    def _accrue_status_time(self, session: Session) -> None:
        elapsed = max(0, round((self.clock.monotonic() - self._status_started_monotonic) * 1000))
        session.wall_duration_ms += elapsed
        if session.status == SessionStatus.RECORDING:
            session.captured_duration_ms += elapsed
        elif session.status == SessionStatus.PAUSED:
            session.paused_duration_ms += elapsed
        self._status_started_monotonic = self.clock.monotonic()

    def _reconcile_capture_timing_from_audio(self, reason: str) -> None:
        """Replace suspend-inflated capture time with durable WAV frame durations."""
        session = self._require_session()
        folder = self._require_folder()
        totals: dict[str, int] = {}
        for event in self.repository.read_events(folder):
            if event.get("type") != "audio_chunk_finalized":
                continue
            payload = event.get("payload", {})
            source = str(payload.get("source", ""))
            frames = max(0, int(payload.get("frames", 0)))
            sample_rate = max(1, int(payload.get("sample_rate", 1)))
            duration_ms = max(0, round(frames / sample_rate * 1000))
            totals[source] = totals.get(source, 0) + duration_ms
        if not totals:
            return
        captured_ms = max(totals.values())
        session.captured_duration_ms = captured_ms
        session.paused_duration_ms = max(0, session.wall_duration_ms - captured_ms)
        self._commit(
            "capture_timing_reconciled",
            {
                "reason": reason,
                "captured_duration_ms": session.captured_duration_ms,
                "paused_duration_ms": session.paused_duration_ms,
                "wall_duration_ms": session.wall_duration_ms,
                "source_duration_ms": totals,
            },
        )

    def _change_status(self, status: SessionStatus) -> None:
        session = self._require_session()
        session.status = status
        self._status_started_monotonic = self.clock.monotonic()

    def _offset_ms(self) -> int:
        elapsed = max(0, round((self.clock.monotonic() - self._anchor_monotonic) * 1000))
        return self._anchor_offset_ms + elapsed

    def _reset_time_anchor(self, offset_ms: int) -> None:
        now = self.clock.monotonic()
        self._anchor_monotonic = now
        self._status_started_monotonic = now
        self._anchor_offset_ms = offset_ms

    def _require_status(self, status: SessionStatus) -> Session:
        session = self._require_session()
        if session.status != status:
            raise InvalidSessionTransition(f"Expected {status}, found {session.status}")
        return session

    def _require_session(self) -> Session:
        if self.session is None:
            raise InvalidSessionTransition("No active session")
        return self.session

    def _require_folder(self) -> Path:
        if self.folder is None:
            raise InvalidSessionTransition("No session folder")
        return self.folder

    @staticmethod
    def _normalize_retention(value: dict | None) -> dict:
        policy = {
            "audio": "all",
            "marker_context_before_ms": 30000,
            "marker_context_after_ms": 30000,
        }
        if value:
            policy.update(value)
        if policy["audio"] not in {"all", "until_verified", "marker_context", "none"}:
            raise ValueError("Unknown audio retention policy")
        for key in ("marker_context_before_ms", "marker_context_after_ms"):
            amount = policy[key]
            if not isinstance(amount, int) or isinstance(amount, bool) or not 0 <= amount <= 600000:
                raise ValueError("Marker context retention must be between 0 and 600000 ms")
        return policy
