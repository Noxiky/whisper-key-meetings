from __future__ import annotations

import threading
from collections.abc import Callable

import numpy as np
import sounddevice as sd
import soxr

from whisper_key.infrastructure.audio_routes import resolve_input_device
from whisper_key.voice_activity_detection import VAD_CHUNK_SIZE, VadEvent


class DictationService:
    TARGET_RATE = 16000

    def __init__(
        self,
        whisper_engine,
        input_device=None,
        input_device_name: str | None = None,
        max_seconds: float = 0.0,
        vad_manager=None,
        on_silence_timeout: Callable[[], None] | None = None,
        stream_factory: Callable | None = None,
    ):
        self.whisper_engine = whisper_engine
        self.input_device = None if input_device in {None, "default"} else input_device
        self.input_device_name = str(input_device_name or "").strip() or None
        self.max_seconds = max_seconds
        self.vad_manager = vad_manager
        self.on_silence_timeout = on_silence_timeout
        self.stream_factory = stream_factory or sd.InputStream
        self._stream = None
        self._chunks: list[np.ndarray] = []
        self._sample_rate = self.TARGET_RATE
        self._frames = 0
        self._lock = threading.Lock()
        self.last_audio = np.array([], dtype=np.float32)
        self.last_sample_rate = self.TARGET_RATE
        self._vad_buffer = np.array([], dtype=np.float32)
        self._continuous_vad = (
            vad_manager.create_continuous_detector(event_callback=self._handle_vad_event)
            if vad_manager is not None
            else None
        )

    @property
    def is_recording(self) -> bool:
        return self._stream is not None

    def start(self) -> None:
        with self._lock:
            if self._stream is not None:
                raise RuntimeError("Dictation is already recording")
            self._chunks = []
            self._frames = 0
            self.last_audio = np.array([], dtype=np.float32)
            self.last_sample_rate = self.TARGET_RATE
            self._vad_buffer = np.array([], dtype=np.float32)
            if self._continuous_vad:
                self._continuous_vad.reset()
            device, detected_name = resolve_input_device(
                sd,
                self.input_device,
                self.input_device_name,
            )
            self.input_device_name = detected_name
            self._sample_rate = self._device_sample_rate(device)
            stream = self.stream_factory(
                samplerate=self._sample_rate,
                channels=1,
                dtype=np.float32,
                device=device,
                callback=self._callback,
            )
            stream.start()
            self._stream = stream

    def stop_and_transcribe(self) -> str:
        with self._lock:
            stream = self._stream
            self._stream = None
        if stream is None:
            raise RuntimeError("Dictation is not recording")
        stream.stop()
        stream.close()
        with self._lock:
            audio = np.concatenate(self._chunks) if self._chunks else np.array([], dtype=np.float32)
            self._chunks = []
            self._vad_buffer = np.array([], dtype=np.float32)
        if not audio.size:
            self.last_audio = audio
            return ""
        if self._sample_rate != self.TARGET_RATE:
            audio = soxr.resample(audio, self._sample_rate, self.TARGET_RATE).astype(np.float32)
        self.last_audio = audio.copy()
        self.last_sample_rate = self.TARGET_RATE
        return (self.whisper_engine.transcribe_audio(audio) or "").strip()

    def cancel(self) -> None:
        with self._lock:
            stream = self._stream
            self._stream = None
            self._chunks = []
            self.last_audio = np.array([], dtype=np.float32)
            self._vad_buffer = np.array([], dtype=np.float32)
        if stream:
            stream.stop()
            stream.close()
        if self._continuous_vad:
            self._continuous_vad.reset()

    def _callback(self, audio, _frames, _time, _status) -> None:
        array = np.asarray(audio, dtype=np.float32).reshape(-1)
        accepted = np.array([], dtype=np.float32)
        with self._lock:
            if self.max_seconds and self.max_seconds > 0:
                maximum = round(self.max_seconds * self._sample_rate)
                remaining = max(0, maximum - self._frames)
                accepted = array[:remaining].copy()
            else:
                accepted = array.copy()
            if accepted.size:
                self._chunks.append(accepted)
                self._frames += len(accepted)
        if accepted.size and self._continuous_vad:
            self._process_vad_audio(accepted)

    def _process_vad_audio(self, audio: np.ndarray) -> None:
        if self._sample_rate != self.TARGET_RATE:
            audio = soxr.resample(audio, self._sample_rate, self.TARGET_RATE).astype(np.float32)
        self._vad_buffer = np.concatenate((self._vad_buffer, audio))
        while len(self._vad_buffer) >= VAD_CHUNK_SIZE:
            chunk = self._vad_buffer[:VAD_CHUNK_SIZE]
            self._vad_buffer = self._vad_buffer[VAD_CHUNK_SIZE:]
            self._continuous_vad.process_chunk(chunk)

    def _handle_vad_event(self, event: VadEvent) -> None:
        if event == VadEvent.SILENCE_TIMEOUT and self.is_recording and self.on_silence_timeout:
            self.on_silence_timeout()

    def _device_sample_rate(self, device=None) -> int:
        try:
            info = sd.query_devices(device, kind="input")
            return int(info.get("default_samplerate") or self.TARGET_RATE)
        except Exception:
            return self.TARGET_RATE
