from __future__ import annotations

import hashlib
import os
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import numpy as np
import soxr

if TYPE_CHECKING:
    from whisper_key.application.session_service import SessionService


@dataclass(frozen=True)
class AudioFileSpan:
    path: Path
    start_ms: int
    end_ms: int
    sample_rate: int
    channels: int
    frames: int


class MarkerContextService:
    OUTPUT_RATE = 16000

    def build(self, service: SessionService) -> list[Path]:
        if not service.folder:
            raise RuntimeError("Session folder is required")
        events = service.repository.read_events(service.folder)
        intervals = self._recording_intervals(events)
        existing = {
            event["payload"]["relative_path"]
            for event in events
            if event.get("type") == "snapshot_created" and event["payload"].get("kind") == "audio_excerpt"
        }
        stage_order = {
            event["payload"]["stage_id"]: event["payload"]["stage_sequence"]
            for event in events
            if event.get("type") == "stage_started"
        }
        spans = {
            "MIC": self._audio_spans(service.folder / "audio" / "mic", stage_order),
            "SYS": self._audio_spans(service.folder / "audio" / "sys", stage_order),
        }
        created: list[Path] = []
        for event in events:
            if event.get("type") != "marker_created":
                continue
            marker = event["payload"]
            center_ms = self._wall_to_captured(marker["at_ms"], intervals)
            start_ms = max(0, center_ms - marker["context_before_ms"])
            end_ms = center_ms + marker["context_after_ms"]
            for source, source_spans in spans.items():
                if not source_spans:
                    continue
                relative = f"audio/excerpts/{marker['marker_id']}-{source.lower()}.wav"
                if relative in existing:
                    continue
                audio = self._read_range(source_spans, start_ms, end_ms)
                if not audio.size:
                    continue
                destination = service.folder / Path(relative)
                self._write_wave_atomic(destination, audio)
                digest = hashlib.sha256(destination.read_bytes()).hexdigest()
                duration_ms = round(len(audio) / self.OUTPUT_RATE * 1000)
                service.add_attachment(
                    kind="audio_excerpt",
                    relative_path=relative,
                    media_type="audio/wav",
                    sha256=digest,
                    at_ms=marker["at_ms"],
                    duration_ms=duration_ms,
                    source=source,
                )
                created.append(destination)
        return created

    @staticmethod
    def _recording_intervals(events: list[dict]) -> list[tuple[int, int]]:
        intervals: list[tuple[int, int]] = []
        active_start: int | None = None
        for event in events:
            offset = event["session_offset_ms"]
            if event["type"] in {"stage_started", "pause_ended"}:
                active_start = offset
            elif event["type"] in {"pause_started", "stage_ended"} and active_start is not None:
                intervals.append((active_start, max(active_start, offset)))
                active_start = None
        if active_start is not None and events:
            intervals.append((active_start, max(active_start, events[-1]["session_offset_ms"])))
        return intervals

    @staticmethod
    def _wall_to_captured(at_ms: int, intervals: list[tuple[int, int]]) -> int:
        captured = 0
        for start, end in intervals:
            if at_ms >= end:
                captured += end - start
            elif at_ms > start:
                return captured + at_ms - start
            else:
                return captured
        return captured

    @staticmethod
    def _audio_spans(folder: Path, stage_order: dict[str, int]) -> list[AudioFileSpan]:
        paths = []
        for path in folder.glob("*.wav") if folder.exists() else []:
            stage_id, _, index_text = path.stem.rpartition("-")
            try:
                index = int(index_text)
            except ValueError:
                continue
            paths.append((stage_order.get(stage_id, 1_000_000), index, path))
        spans: list[AudioFileSpan] = []
        cursor = 0
        for _stage, _index, path in sorted(paths):
            try:
                with wave.open(str(path), "rb") as reader:
                    frames = reader.getnframes()
                    rate = reader.getframerate()
                    channels = reader.getnchannels()
            except (OSError, EOFError, wave.Error):
                continue
            duration = round(frames / rate * 1000)
            spans.append(AudioFileSpan(path, cursor, cursor + duration, rate, channels, frames))
            cursor += duration
        return spans

    def _read_range(self, spans: list[AudioFileSpan], start_ms: int, end_ms: int) -> np.ndarray:
        pieces = []
        for span in spans:
            overlap_start = max(start_ms, span.start_ms)
            overlap_end = min(end_ms, span.end_ms)
            if overlap_end <= overlap_start:
                continue
            first_frame = round((overlap_start - span.start_ms) / 1000 * span.sample_rate)
            frame_count = round((overlap_end - overlap_start) / 1000 * span.sample_rate)
            with wave.open(str(span.path), "rb") as reader:
                reader.setpos(min(first_frame, span.frames))
                raw = reader.readframes(min(frame_count, span.frames - first_frame))
            audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32767
            if span.channels > 1:
                audio = audio.reshape(-1, span.channels).mean(axis=1)
            if span.sample_rate != self.OUTPUT_RATE and audio.size:
                audio = soxr.resample(audio, span.sample_rate, self.OUTPUT_RATE).astype(np.float32)
            pieces.append(audio)
        return np.concatenate(pieces) if pieces else np.array([], dtype=np.float32)

    def _write_wave_atomic(self, destination: Path, audio: np.ndarray) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        try:
            with wave.open(str(temporary), "wb") as writer:
                writer.setnchannels(1)
                writer.setsampwidth(2)
                writer.setframerate(self.OUTPUT_RATE)
                writer.writeframes((np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes())
            with temporary.open("r+b") as handle:
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
