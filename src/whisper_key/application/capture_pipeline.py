import hashlib
import wave
from collections.abc import Callable
from pathlib import Path

import numpy as np

from whisper_key.application.session_service import SessionService
from whisper_key.domain.audio import SourceHealth, TranscriptionJob, TranscriptResult
from whisper_key.infrastructure.audio_store import DurableAudioStore
from whisper_key.meeting_live_transcriber import MeetingLiveTranscriber


class DurableCapturePipeline:
    def __init__(
        self,
        session_service: SessionService,
        transcriber: MeetingLiveTranscriber,
        audio_store: DurableAudioStore | None = None,
        on_health: Callable[[SourceHealth], None] | None = None,
    ):
        self.session_service = session_service
        self.transcriber = transcriber
        self.audio_store = audio_store or DurableAudioStore()
        self.on_health = on_health
        self.transcriber.on_transcript = self._on_transcript
        if hasattr(self.transcriber, "on_provisional"):
            self.transcriber.on_provisional = self._on_provisional
        self.transcriber.on_backpressure = self._on_transcription_backpressure
        self.running = False
        self._last_health: dict[str, tuple[str, str, bool]] = {}
        self._transcript_offset_ms = 0
        self._audio_cursors_ms: dict[str, int] = {}
        existing_finalizer = getattr(self.audio_store, "on_file_finalized", None)
        if hasattr(self.audio_store, "on_file_finalized"):

            def finalize_audio(source_id: str, path: Path) -> None:
                if existing_finalizer:
                    existing_finalizer(source_id, path)
                self._on_audio_file_finalized(source_id, path)

            self.audio_store.on_file_finalized = finalize_audio

    @property
    def transcription_backlog(self) -> int:
        return self.transcriber.backlog

    @property
    def persistence_backlog(self) -> int:
        return self.audio_store.backlog

    def start(self, active_sources: list[str], auto_stop_seconds: float = 0.0) -> None:
        session = self.session_service.session
        stage = session.active_stage if session else None
        if not stage or not self.session_service.folder:
            raise RuntimeError("A recording stage is required")
        self._transcript_offset_ms = self.session_service.current_offset_ms()
        self._audio_cursors_ms = {source_id: self._transcript_offset_ms for source_id in active_sources}
        self.audio_store.start(self.session_service.folder, stage.stage_id)
        try:
            self.transcriber.start(auto_stop_seconds=auto_stop_seconds, active_sources=active_sources)
        except Exception:
            self.audio_store.stop()
            raise
        self._last_health = {}
        self.running = True

    def ingest(self, source_id: str, label: str, audio: np.ndarray, sample_rate: int) -> bool:
        if not self.running:
            return False
        if not self.audio_store.submit(source_id, audio, sample_rate):
            self._health(source_id, "persistence_backpressure", "Audio could not be persisted fast enough", True)
            return False
        if not self.transcriber.push_audio(source_id, audio, sample_rate):
            self._health(source_id, "transcription_backpressure", "Audio is safe; transcription requires replay", False)
        else:
            self._health(source_id, "active", "Audio persisted and queued", False)
        return True

    def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        self.transcriber.stop()
        self.audio_store.stop()

    def report_source_error(self, source_id: str, detail: str) -> None:
        self._health(source_id, "unavailable", detail, False)

    def _on_transcript(self, result: TranscriptResult) -> None:
        self.session_service.add_transcript(
            result.source,
            result.text,
            self._transcript_offset_ms + result.started_at_ms,
            self._transcript_offset_ms + result.ended_at_ms,
            result.language,
        )

    def _on_provisional(self, result: TranscriptResult) -> None:
        self.session_service.add_provisional_transcript(
            result.source,
            result.text,
            self._transcript_offset_ms + result.started_at_ms,
            self._transcript_offset_ms + result.ended_at_ms,
        )

    def _on_audio_file_finalized(self, source_id: str, path: Path) -> None:
        folder = self.session_service.folder
        if not folder:
            return
        with wave.open(str(path), "rb") as reader:
            sample_rate = reader.getframerate()
            channels = reader.getnchannels()
            frames = reader.getnframes()
        if not frames or not sample_rate:
            return
        started_at_ms = self._audio_cursors_ms.get(source_id, self._transcript_offset_ms)
        duration_ms = max(1, round(frames / sample_rate * 1000))
        ended_at_ms = started_at_ms + duration_ms
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        self.session_service.add_audio_chunk(
            source="SYS" if source_id == "system" else "MIC",
            relative_path=path.relative_to(folder).as_posix(),
            started_at_ms=started_at_ms,
            ended_at_ms=ended_at_ms,
            sample_rate=sample_rate,
            channels=channels,
            frames=frames,
            sha256=digest.hexdigest(),
        )
        self._audio_cursors_ms[source_id] = ended_at_ms

    def _on_transcription_backpressure(self, job: TranscriptionJob) -> None:
        self._health(job.source_id, "transcription_backpressure", "Segment remains available in retained audio", False)

    def _health(self, source_id: str, status: str, detail: str, fatal: bool) -> None:
        current = (status, detail, fatal)
        if self._last_health.get(source_id) == current:
            return
        self._last_health[source_id] = current
        self.session_service.add_source_health(source_id, status, detail, fatal)
        if self.on_health:
            self.on_health(SourceHealth(source_id, status, detail, fatal))
