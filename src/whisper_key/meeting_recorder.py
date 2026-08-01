import logging
import threading
import time
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import numpy as np
import sounddevice as sd

from .infrastructure.audio_routes import resolve_input_device
from .meeting_live_transcriber import MeetingLiveTranscriber

warnings.filterwarnings("ignore", message="data discontinuity in recording")


@dataclass
class MeetingSession:
    session_id: str
    started_at: datetime
    duration_seconds: float = 0.0


class MeetingRecorder:
    WHISPER_SAMPLE_RATE = 16000
    SYSTEM_SAMPLE_RATE = 48000
    THREAD_JOIN_TIMEOUT = 3.0
    RECORDING_SLEEP_SECONDS = 0.1
    SYSTEM_CHUNK_SECONDS = 0.5
    DEVICE_RECONNECT_INITIAL_SECONDS = 1.0
    DEVICE_RECONNECT_MAX_SECONDS = 8.0
    DEVICE_STABLE_SECONDS = 10.0

    def __init__(
        self,
        config: dict,
        audio_config: dict,
        live_transcriber: MeetingLiveTranscriber | None = None,
        audio_consumer: Callable[[str, str, np.ndarray, int], bool] | None = None,
        on_source_error: Callable[[str, str], None] | None = None,
    ):
        self.config = config or {}
        self.audio_config = audio_config or {}
        self.live_transcriber = live_transcriber
        self.audio_consumer = audio_consumer
        self.on_source_error = on_source_error
        self.logger = logging.getLogger(__name__)
        self.is_recording = False
        self.started_at: datetime | None = None
        self.session_id: str | None = None
        self._lock = threading.Lock()
        self._mic_thread: threading.Thread | None = None
        self._system_thread: threading.Thread | None = None
        self._mic_sample_rate: int = self.WHISPER_SAMPLE_RATE
        self._system_sample_rate: int = self.SYSTEM_SAMPLE_RATE
        self._mic_samples_captured: int = 0
        self._system_samples_captured: int = 0
        self._stop_event = threading.Event()
        self._mic_device_name: str | None = str(self.audio_config.get("input_device_name") or "").strip() or None

        if self.live_transcriber:
            self.live_transcriber.register_source("mic", "MIC")
            self.live_transcriber.register_source("system", "SYS")

    def start_recording(self, capture_microphone: bool | None = None, capture_system_audio: bool | None = None) -> bool:
        with self._lock:
            if self.is_recording:
                return False
            self.is_recording = True
            self.started_at = datetime.now()
            self.session_id = self.started_at.strftime("meeting-%Y%m%d-%H%M%S")
            self._mic_samples_captured = 0
            self._system_samples_captured = 0
            self._mic_device_name = str(self.audio_config.get("input_device_name") or "").strip() or None
            self._stop_event.clear()

        use_mic = capture_microphone if capture_microphone is not None else self.config.get("capture_microphone", True)
        use_sys = (
            capture_system_audio if capture_system_audio is not None else self.config.get("capture_system_audio", True)
        )

        if use_mic:
            self._mic_thread = threading.Thread(target=self._record_microphone, daemon=True)
            self._mic_thread.start()

        if use_sys:
            self._system_thread = threading.Thread(target=self._record_system_audio, daemon=True)
            self._system_thread.start()

        return True

    def stop_recording(self) -> MeetingSession | None:
        with self._lock:
            if not self.is_recording:
                return None
            self.is_recording = False
            self._stop_event.set()

        for thread in (self._mic_thread, self._system_thread):
            if thread:
                thread.join(timeout=self.THREAD_JOIN_TIMEOUT)
                if thread.is_alive():
                    self.logger.warning("Meeting recorder thread did not exit within timeout")

        return self._build_session()

    def cancel_recording(self) -> None:
        with self._lock:
            self.is_recording = False
            self._stop_event.set()
        for thread in (self._mic_thread, self._system_thread):
            if thread:
                thread.join(timeout=self.THREAD_JOIN_TIMEOUT)

    def get_recording_status(self) -> bool:
        return self.is_recording

    def _record_microphone(self) -> None:
        self._run_source_with_reconnect("mic", "micrófono", self._record_microphone_once)

    def _record_microphone_once(self) -> None:
        device = self._resolve_microphone_device()
        self._mic_sample_rate = self._get_microphone_sample_rate(device)

        def callback(audio_data, _frames, _time, status):
            if status:
                self.logger.debug(f"Meeting microphone callback status: {status}")
            if not self.is_recording:
                return
            chunk = audio_data.copy()
            self._mic_samples_captured += len(chunk)
            self._deliver_audio("mic", "MIC", chunk, self._mic_sample_rate)

        with sd.InputStream(
            samplerate=self._mic_sample_rate,
            channels=1,
            callback=callback,
            dtype=np.float32,
            device=device,
        ):
            while self.is_recording:
                sd.sleep(int(self.RECORDING_SLEEP_SECONDS * 1000))

    def _resolve_microphone_device(self):
        configured = self.audio_config.get("input_device", "default")
        device, detected_name = resolve_input_device(sd, configured, self._mic_device_name)
        self._mic_device_name = detected_name
        return device

    def _run_source_with_reconnect(
        self,
        source_id: str,
        source_label: str,
        capture_once: Callable[[], None],
    ) -> None:
        initial_delay = max(
            0.05,
            float(
                self.config.get(
                    "device_reconnect_initial_seconds",
                    self.DEVICE_RECONNECT_INITIAL_SECONDS,
                )
            ),
        )
        max_delay = max(
            initial_delay,
            float(
                self.config.get(
                    "device_reconnect_max_seconds",
                    self.DEVICE_RECONNECT_MAX_SECONDS,
                )
            ),
        )
        delay = initial_delay
        while self.is_recording:
            opened_at = time.monotonic()
            try:
                capture_once()
                if self.is_recording:
                    raise RuntimeError(f"La captura de {source_label} terminó inesperadamente")
            except Exception as exc:
                if not self.is_recording:
                    break
                self.logger.warning(
                    "Meeting %s capture unavailable; retrying in %.2fs: %s",
                    source_id,
                    delay,
                    exc,
                )
                self._source_error(
                    source_id,
                    f"{exc} · reintentando automáticamente",
                )
                if time.monotonic() - opened_at >= self.DEVICE_STABLE_SECONDS:
                    delay = initial_delay
                if self._stop_event.wait(delay):
                    break
                delay = min(max_delay, delay * 2)

    def _get_microphone_sample_rate(self, device) -> int:
        try:
            info = sd.query_devices(device, kind="input") if device is not None else sd.query_devices(kind="input")
            return int(info.get("default_samplerate") or self.WHISPER_SAMPLE_RATE)
        except Exception:
            return self.WHISPER_SAMPLE_RATE

    def _record_system_audio(self) -> None:
        backend = str(self.config.get("system_audio_backend", "auto")).lower()
        if backend not in {"auto", "soundcard"}:
            detail = f"Backend de audio del sistema no compatible: {backend}"
            self.logger.warning(detail)
            self._source_error("system", detail)
            return

        self._run_source_with_reconnect("system", "audio del sistema", self._record_system_audio_once)

    def _record_system_audio_once(self) -> None:
        try:
            import soundcard as sc
        except Exception as exc:
            raise RuntimeError(f"SoundCard no está disponible: {exc}") from exc

        speaker = self._select_speaker(sc)
        if speaker is None:
            raise RuntimeError("Windows no informó una salida disponible para loopback")
        loopback = sc.get_microphone(id=str(speaker.name), include_loopback=True)
        chunk_frames = int(self._system_sample_rate * self.SYSTEM_CHUNK_SECONDS)
        max_peak = 0.0
        diagnostic_at = time.monotonic() + 5.0
        diagnosed = False
        with loopback.recorder(samplerate=self._system_sample_rate, channels=2) as recorder:
            while self.is_recording:
                chunk = recorder.record(numframes=chunk_frames)
                if chunk is None or len(chunk) == 0:
                    continue
                arr = np.asarray(chunk, dtype=np.float32)
                if arr.size:
                    max_peak = max(max_peak, float(np.max(np.abs(arr))))
                if not diagnosed and time.monotonic() >= diagnostic_at:
                    if max_peak < 0.001:
                        print(
                            "   ⚠ Loopback is returning silence after 5s. "
                            f"Is audio actually playing through '{speaker.name}'?",
                            flush=True,
                        )
                    else:
                        print(f"   ✓ Loopback active (peak so far: {max_peak:.3f})", flush=True)
                    diagnosed = True
                self._system_samples_captured += len(arr)
                self._deliver_audio("system", "SYS", arr, self._system_sample_rate)

    def _deliver_audio(self, source_id: str, label: str, audio: np.ndarray, sample_rate: int) -> bool:
        if self.audio_consumer:
            accepted = self.audio_consumer(source_id, label, audio, sample_rate)
        elif self.live_transcriber:
            accepted = self.live_transcriber.push_audio(source_id, audio, sample_rate)
        else:
            accepted = True
        if not accepted:
            self.logger.error("Meeting audio pipeline rejected %s", source_id)
            self.is_recording = False
            self._stop_event.set()
        return accepted

    def _source_error(self, source_id: str, detail: str) -> None:
        if self.on_source_error:
            self.on_source_error(source_id, detail)

    def _select_speaker(self, sc):
        all_speakers = list(sc.all_speakers())
        default = sc.default_speaker()
        configured = str(self.config.get("system_audio_device") or "default").strip()

        print(f"   ℹ Available output devices ({len(all_speakers)}):", flush=True)
        for s in all_speakers:
            marker = " (Windows default)" if default and s.name == default.name else ""
            print(f"       - {s.name}{marker}", flush=True)

        if configured.lower() in {"", "default"}:
            if default is None:
                return None
            print(f"   ℹ Capturing loopback from: {default.name}", flush=True)
            return default

        needle = configured.casefold()
        for candidate in all_speakers:
            if candidate.name.casefold() == needle:
                print(f"   ℹ Capturing loopback from: {candidate.name}", flush=True)
                return candidate
        for candidate in all_speakers:
            if needle in candidate.name.casefold():
                print(f"   ℹ Capturing loopback from: {candidate.name} (matched '{configured}')", flush=True)
                return candidate

        raise RuntimeError(
            f"La salida configurada '{configured}' no está conectada; no se cambiará silenciosamente a otra"
        )

    def _build_session(self) -> MeetingSession | None:
        if not self.started_at or not self.session_id:
            return None
        mic_seconds = self._mic_samples_captured / max(1, self._mic_sample_rate)
        system_seconds = self._system_samples_captured / max(1, self._system_sample_rate)
        duration = max(mic_seconds, system_seconds)
        if duration <= 0:
            return None
        return MeetingSession(
            session_id=self.session_id,
            started_at=self.started_at,
            duration_seconds=duration,
        )
