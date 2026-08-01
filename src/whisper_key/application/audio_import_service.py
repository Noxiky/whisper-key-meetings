from __future__ import annotations

import hashlib
import os
import time
import wave
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from whisper_key.application.session_service import SessionService
from whisper_key.domain.session import SessionMode
from whisper_key.meeting_live_transcriber import MeetingLiveTranscriber

SUPPORTED_AUDIO_EXTENSIONS = {
    ".aac",
    ".flac",
    ".m4a",
    ".mka",
    ".mov",
    ".mp3",
    ".mp4",
    ".ogg",
    ".opus",
    ".wav",
    ".webm",
    ".wma",
}
IMPORT_SAMPLE_RATE = 16_000


class AudioImportError(RuntimeError):
    pass


@dataclass(frozen=True)
class AudioImportProgress:
    state: str
    detail: str
    processed_ms: int
    total_ms: int | None

    @property
    def percent(self) -> int:
        if not self.total_ms or self.total_ms <= 0:
            return 0
        return min(100, max(0, round(self.processed_ms / self.total_ms * 100)))


@dataclass(frozen=True)
class AudioImportResult:
    session_id: str
    folder: Path
    duration_ms: int
    transcript_segments: int
    failed_segments: int


class PyAvStreamingDecoder:
    """Decode arbitrary local media into bounded PCM16 mono chunks."""

    def probe_duration_ms(self, source: Path) -> int | None:
        import av

        with av.open(str(source)) as container:
            stream = next((item for item in container.streams if item.type == "audio"), None)
            if stream is None:
                raise AudioImportError("El archivo no contiene una pista de audio")
            if stream.duration is not None and stream.time_base is not None:
                return max(1, round(float(stream.duration * stream.time_base) * 1000))
            if container.duration is not None:
                return max(1, round(container.duration / av.time_base * 1000))
        return None

    def decode(self, source: Path) -> Iterable[np.ndarray]:
        import av

        with av.open(str(source)) as container:
            stream = next((item for item in container.streams if item.type == "audio"), None)
            if stream is None:
                raise AudioImportError("El archivo no contiene una pista de audio")
            resampler = av.AudioResampler(format="s16", layout="mono", rate=IMPORT_SAMPLE_RATE)
            for frame in container.decode(stream):
                for converted in resampler.resample(frame):
                    values = np.asarray(converted.to_ndarray()).reshape(-1)
                    if values.size:
                        yield values.astype("<i2", copy=False)
            for converted in resampler.resample(None):
                values = np.asarray(converted.to_ndarray()).reshape(-1)
                if values.size:
                    yield values.astype("<i2", copy=False)


class AudioImportService:
    MAX_TRANSCRIPTION_BACKLOG = 6

    def __init__(
        self,
        session_service: SessionService,
        whisper_engine,
        *,
        decoder=None,
        transcriber_factory=MeetingLiveTranscriber,
        on_progress: Callable[[AudioImportProgress], None] | None = None,
    ):
        self.session_service = session_service
        self.whisper_engine = whisper_engine
        self.decoder = decoder or PyAvStreamingDecoder()
        self.transcriber_factory = transcriber_factory
        self.on_progress = on_progress

    def import_file(
        self,
        source: Path,
        *,
        mode: SessionMode | str = SessionMode.LEARNING,
        title: str | None = None,
        retention: dict | None = None,
    ) -> AudioImportResult:
        source = Path(source).expanduser().resolve()
        if not source.is_file():
            raise AudioImportError("El archivo de audio no existe")
        if source.suffix.casefold() not in SUPPORTED_AUDIO_EXTENSIONS:
            raise AudioImportError(f"Formato no compatible: {source.suffix or 'sin extensión'}")

        total_ms = self.decoder.probe_duration_ms(source)
        self._progress("preparing", "Preparando una copia WAV durable…", 0, total_ms)
        session = self.session_service.create(mode, retention=retention)
        stage = self.session_service.start_stage()
        folder = self.session_service.folder
        if folder is None:
            raise AudioImportError("No se pudo crear la sesión de importación")
        imported_dir = folder / "audio" / "imported"
        imported_dir.mkdir(parents=True, exist_ok=True)
        final_audio = imported_dir / f"{stage.stage_id}-0001.wav"
        temporary_audio = final_audio.with_suffix(".wav.part")
        failures = []
        transcript_count = 0

        def on_transcript(result) -> None:
            nonlocal transcript_count
            self.session_service.add_transcript(
                "IMPORTED",
                result.text,
                result.started_at_ms,
                result.ended_at_ms,
                result.language,
                audio_path=final_audio.relative_to(folder).as_posix(),
            )
            transcript_count += 1

        transcriber = self.transcriber_factory(
            self.whisper_engine,
            enable_provisional=False,
            on_transcript=on_transcript,
            on_backpressure=failures.append,
        )
        transcriber.register_source("imported", "IMPORTED")
        processed_samples = 0
        try:
            transcriber.start(active_sources=["imported"])
            with wave.open(str(temporary_audio), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(IMPORT_SAMPLE_RATE)
                for samples in self.decoder.decode(source):
                    while transcriber.backlog >= self.MAX_TRANSCRIPTION_BACKLOG:
                        time.sleep(0.05)
                    output.writeframesraw(samples.tobytes())
                    processed_samples += len(samples)
                    accepted = transcriber.push_audio(
                        "imported",
                        samples.astype(np.float32) / 32768.0,
                        IMPORT_SAMPLE_RATE,
                    )
                    if not accepted:
                        raise AudioImportError("La cola de transcripción no pudo aceptar un fragmento")
                    processed_ms = round(processed_samples / IMPORT_SAMPLE_RATE * 1000)
                    self._progress(
                        "transcribing",
                        f"Transcribiendo {self._format_duration(processed_ms)}",
                        processed_ms,
                        total_ms,
                    )
            transcriber.stop()
            os.replace(temporary_audio, final_audio)
            duration_ms = max(1, round(processed_samples / IMPORT_SAMPLE_RATE * 1000))
            digest = self._sha256(final_audio)
            self.session_service.add_audio_chunk(
                source="IMPORTED",
                relative_path=final_audio.relative_to(folder).as_posix(),
                started_at_ms=0,
                ended_at_ms=duration_ms,
                sample_rate=IMPORT_SAMPLE_RATE,
                channels=1,
                frames=processed_samples,
                sha256=digest,
            )
            if failures:
                self.session_service.interrupt()
                raise AudioImportError(
                    f"El audio quedó guardado, pero {len(failures)} fragmento(s) necesitan reprocesamiento"
                )
            self.session_service.finish_stage()
            normalized_title = " ".join((title or source.stem).split()).strip()[:200] or "Audio importado"
            self.session_service.name(normalized_title)
            archived = self.session_service.finalize()
            self._progress("complete", "Audio importado y transcrito", duration_ms, duration_ms)
            return AudioImportResult(
                session_id=session.session_id,
                folder=archived,
                duration_ms=duration_ms,
                transcript_segments=transcript_count,
                failed_segments=0,
            )
        except Exception:
            try:
                transcriber.stop()
            except Exception:
                pass
            if temporary_audio.is_file() and processed_samples > 0:
                os.replace(temporary_audio, final_audio)
                if self.session_service.session and self.session_service.session.status.value in {
                    "recording",
                    "paused",
                }:
                    duration_ms = max(1, round(processed_samples / IMPORT_SAMPLE_RATE * 1000))
                    self.session_service.add_audio_chunk(
                        source="IMPORTED",
                        relative_path=final_audio.relative_to(folder).as_posix(),
                        started_at_ms=0,
                        ended_at_ms=duration_ms,
                        sample_rate=IMPORT_SAMPLE_RATE,
                        channels=1,
                        frames=processed_samples,
                        sha256=self._sha256(final_audio),
                    )
            if self.session_service.session and self.session_service.session.status.value in {"recording", "paused"}:
                self.session_service.interrupt()
            raise

    def _progress(self, state: str, detail: str, processed_ms: int, total_ms: int | None) -> None:
        if self.on_progress:
            self.on_progress(AudioImportProgress(state, detail, processed_ms, total_ms))

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _format_duration(milliseconds: int) -> str:
        seconds = max(0, milliseconds // 1000)
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
