import logging
import queue
import threading
import time
from collections.abc import Callable

import numpy as np
import soxr

from .domain.audio import TranscriptionJob, TranscriptResult

WHISPER_SAMPLE_RATE = 16000

HALLUCINATION_PHRASES = {
    "transcription by castingwords",
    "transcription by eso",
    "translation by",
    "subtitles by",
    "subtitles by the amara.org community",
    "thanks for watching",
    "thanks for watching!",
    "thank you for watching",
    "thank you.",
    "thank you",
    "you",
    "bye.",
    "bye",
    ".",
    "..",
    "...",
    "♪",
    "♪♪",
    "♪♪♪",
    "[music]",
    "[applause]",
    "(music)",
    "(applause)",
}


def _is_hallucination(text: str) -> bool:
    stripped = text.strip().lower()
    if not stripped:
        return True
    if stripped in HALLUCINATION_PHRASES:
        return True
    if stripped.startswith("transcription by ") or stripped.startswith("subtitles by "):
        return True
    if all(ch in ".,;:!?-—–…·•" for ch in stripped):
        return True
    return False


class MeetingLiveTranscriber:
    SILENCE_RMS_THRESHOLD = 0.004
    SILENCE_TRIGGER_SECONDS = 0.8
    MIN_SEGMENT_SECONDS = 1.0
    MAX_SEGMENT_SECONDS = 15.0
    FINAL_FLUSH_MIN_SECONDS = 0.4
    PROVISIONAL_INTERVAL_SECONDS = 5.0
    QUEUE_POLL_SECONDS = 0.2
    SENTINEL = object()

    def __init__(
        self,
        whisper_engine,
        auto_stop_silence_seconds: float = 0.0,
        on_transcript: Callable[[TranscriptResult], None] | None = None,
        on_provisional: Callable[[TranscriptResult], None] | None = None,
        on_backpressure: Callable[[TranscriptionJob], None] | None = None,
        max_queue_segments: int = 128,
        enable_provisional: bool = True,
    ):
        self.whisper_engine = whisper_engine
        self.auto_stop_silence_seconds = float(auto_stop_silence_seconds or 0.0)
        self.logger = logging.getLogger(__name__)
        self._sources: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue_segments)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._silence_callback: Callable[[float], None] | None = None
        self._silence_timeout_fired = False
        self.on_transcript = on_transcript
        self.on_provisional = on_provisional
        self.on_backpressure = on_backpressure
        self.enable_provisional = enable_provisional

    @property
    def backlog(self) -> int:
        return self._queue.qsize()

    def set_silence_timeout_callback(self, callback: Callable[[float], None] | None) -> None:
        self._silence_callback = callback

    def register_source(self, source_id: str, label: str) -> None:
        with self._lock:
            self._sources[source_id] = self._fresh_source_state(label)

    def reset_sources(self) -> None:
        now = time.monotonic()
        with self._lock:
            for source in self._sources.values():
                source["buffer"] = np.array([], dtype=np.float32)
                source["silence_samples"] = 0
                source["last_audio_at"] = now
                source["total_samples"] = 0
                source["buffer_started_at_ms"] = 0

    def push_audio(self, source_id: str, audio: np.ndarray, sample_rate: int) -> bool:
        if audio is None or len(audio) == 0:
            return True
        normalized = self._normalize_audio(audio, sample_rate)
        chunk_rms = float(np.sqrt(np.mean(normalized**2))) if normalized.size else 0.0
        chunk_is_silent = chunk_rms < self.SILENCE_RMS_THRESHOLD

        segment_to_flush: TranscriptionJob | None = None
        provisional_to_queue: TranscriptionJob | None = None

        with self._lock:
            source = self._sources.get(source_id)
            if source is None:
                return False

            if len(source["buffer"]) == 0:
                source["buffer_started_at_ms"] = round(source["total_samples"] / WHISPER_SAMPLE_RATE * 1000)
            source["buffer"] = np.concatenate([source["buffer"], normalized])
            source["total_samples"] += len(normalized)
            if chunk_is_silent:
                source["silence_samples"] += len(normalized)
            else:
                source["silence_samples"] = 0
                source["last_audio_at"] = time.monotonic()

            buffer_seconds = len(source["buffer"]) / WHISPER_SAMPLE_RATE
            silence_seconds = source["silence_samples"] / WHISPER_SAMPLE_RATE

            pause_flush = silence_seconds >= self.SILENCE_TRIGGER_SECONDS and buffer_seconds >= self.MIN_SEGMENT_SECONDS
            overflow_flush = buffer_seconds >= self.MAX_SEGMENT_SECONDS

            if pause_flush or overflow_flush:
                segment_to_flush = TranscriptionJob(
                    source_id=source_id,
                    label=source["label"],
                    audio=source["buffer"],
                    started_at_ms=source["buffer_started_at_ms"],
                    ended_at_ms=round(source["total_samples"] / WHISPER_SAMPLE_RATE * 1000),
                )
                source["buffer"] = np.array([], dtype=np.float32)
                source["silence_samples"] = 0
                source["next_provisional_samples"] = source["total_samples"] + round(
                    self.PROVISIONAL_INTERVAL_SECONDS * WHISPER_SAMPLE_RATE
                )
            elif self.enable_provisional and source["total_samples"] >= source["next_provisional_samples"]:
                provisional_to_queue = TranscriptionJob(
                    source_id=source_id,
                    label=source["label"],
                    audio=source["buffer"].copy(),
                    started_at_ms=source["buffer_started_at_ms"],
                    ended_at_ms=round(source["total_samples"] / WHISPER_SAMPLE_RATE * 1000),
                    provisional=True,
                )
                source["next_provisional_samples"] = source["total_samples"] + round(
                    self.PROVISIONAL_INTERVAL_SECONDS * WHISPER_SAMPLE_RATE
                )

        if segment_to_flush is not None:
            try:
                self._queue.put_nowait(segment_to_flush)
            except queue.Full:
                if self.on_backpressure:
                    self.on_backpressure(segment_to_flush)
                return False
        if provisional_to_queue is not None:
            try:
                self._queue.put_nowait(provisional_to_queue)
            except queue.Full:
                # Provisional text is expendable. Durable audio and final jobs
                # keep priority and remain visible through normal backpressure.
                pass
        return True

    def start(self, auto_stop_seconds: float | None = None, active_sources: list | None = None) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.reset_sources()
        with self._queue.mutex:
            self._queue.queue.clear()
        self._stop_event.clear()
        self._silence_timeout_fired = False
        self._current_auto_stop_seconds = (
            float(auto_stop_seconds) if auto_stop_seconds is not None else self.auto_stop_silence_seconds
        )
        with self._lock:
            self._active_source_ids = set(active_sources) if active_sources else set(self._sources.keys())
        self._thread = threading.Thread(target=self._run, name="MeetingLiveTranscriber", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

        with self._lock:
            for source_id, source in self._sources.items():
                buffer = source["buffer"]
                if len(buffer) >= int(WHISPER_SAMPLE_RATE * self.FINAL_FLUSH_MIN_SECONDS):
                    self._queue.put(
                        TranscriptionJob(
                            source_id=source_id,
                            label=source["label"],
                            audio=buffer,
                            started_at_ms=source["buffer_started_at_ms"],
                            ended_at_ms=round(source["total_samples"] / WHISPER_SAMPLE_RATE * 1000),
                        )
                    )
                source["buffer"] = np.array([], dtype=np.float32)
                source["silence_samples"] = 0

        self._queue.put(self.SENTINEL)

        if self._thread:
            self._thread.join(timeout=60.0)
            self._thread = None

    def _run(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=self.QUEUE_POLL_SECONDS)
            except queue.Empty:
                self._check_silence_timeout()
                if self._stop_event.is_set() and self._queue.empty():
                    return
                continue

            if item is self.SENTINEL:
                return

            self._transcribe_segment(item)
            self._check_silence_timeout()

    def _check_silence_timeout(self) -> None:
        if self._silence_timeout_fired:
            return
        threshold = getattr(self, "_current_auto_stop_seconds", self.auto_stop_silence_seconds)
        if threshold <= 0:
            return
        callback = self._silence_callback
        if callback is None:
            return

        now = time.monotonic()
        with self._lock:
            if not self._sources:
                return
            active_ids = getattr(self, "_active_source_ids", None) or set(self._sources.keys())
            all_silent = all(
                (now - source.get("last_audio_at", now)) >= threshold
                for source_id, source in self._sources.items()
                if source_id in active_ids
            )

        if not all_silent:
            return

        self._silence_timeout_fired = True
        try:
            callback(threshold)
        except Exception as exc:
            self.logger.warning(f"Silence timeout callback failed: {exc}")

    def _transcribe_segment(self, job: TranscriptionJob) -> None:
        rms = float(np.sqrt(np.mean(job.audio**2))) if job.audio.size else 0.0
        if rms < self.SILENCE_RMS_THRESHOLD:
            return

        try:
            text, language = self._transcribe(job.audio)
        except Exception as exc:
            self.logger.warning("Live transcription failed; retained audio requires replay: %s", exc)
            if self.on_backpressure:
                self.on_backpressure(job)
            return
        if text and not _is_hallucination(text):
            result = TranscriptResult(
                source_id=job.source_id,
                source=job.label,
                text=text,
                started_at_ms=job.started_at_ms,
                ended_at_ms=job.ended_at_ms,
                language=language,
            )
            if job.provisional:
                if self.on_provisional:
                    try:
                        self.on_provisional(result)
                    except Exception as exc:
                        self.logger.debug("Provisional transcript callback failed: %s", exc)
                return
            print(f"[{job.label}] {text}", flush=True)
            if self.on_transcript:
                try:
                    self.on_transcript(result)
                except Exception as exc:
                    self.logger.error("Transcript persistence callback failed: %s", exc)
                    if self.on_backpressure:
                        self.on_backpressure(job)

    def _transcribe(self, audio: np.ndarray) -> tuple[str | None, str | None]:
        model = getattr(self.whisper_engine, "model", None)
        if model is None:
            return None, None
        kwargs = dict(
            beam_size=getattr(self.whisper_engine, "beam_size", 5),
            language=getattr(self.whisper_engine, "language", None),
            task="transcribe",
            condition_on_previous_text=False,
            multilingual=getattr(self.whisper_engine, "language", None) is None,
            language_detection_segments=3,
            # Audio has already been segmented on silence by this class. Running
            # Faster-Whisper's nested Silero VAD here adds latency and creates an
            # unnecessary capture-critical asset dependency.
            vad_filter=False,
            no_speech_threshold=0.6,
        )
        initial_prompt = getattr(self.whisper_engine, "initial_prompt", None)
        hotwords = getattr(self.whisper_engine, "hotwords", None)
        if initial_prompt:
            kwargs["initial_prompt"] = initial_prompt
        if hotwords:
            kwargs["hotwords"] = hotwords
        segments, info = model.transcribe(audio, **kwargs)
        parts = []
        for segment in segments:
            text = str(getattr(segment, "text", "") or "").strip()
            if text:
                parts.append(text)
        text = " ".join(parts) if parts else None
        preservation = getattr(self.whisper_engine, "ensure_detected_language_script", None)
        if text and callable(preservation):
            text, info = preservation(audio, text, info, kwargs)
        return text, getattr(info, "language", None)

    def _normalize_audio(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        if audio.ndim > 1:
            audio = np.mean(audio, axis=1)
        audio = audio.astype(np.float32).flatten()
        if sample_rate != WHISPER_SAMPLE_RATE:
            audio = soxr.resample(audio, sample_rate, WHISPER_SAMPLE_RATE).astype(np.float32)
        return audio

    def _fresh_source_state(self, label: str) -> dict:
        return {
            "label": label,
            "buffer": np.array([], dtype=np.float32),
            "silence_samples": 0,
            "last_audio_at": time.monotonic(),
            "total_samples": 0,
            "buffer_started_at_ms": 0,
            "next_provisional_samples": round(self.PROVISIONAL_INTERVAL_SECONDS * WHISPER_SAMPLE_RATE),
        }
