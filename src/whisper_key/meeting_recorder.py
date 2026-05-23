import logging
import threading
import time
import warnings
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import numpy as np
import sounddevice as sd


warnings.filterwarnings("ignore", message="data discontinuity in recording")

from .meeting_live_transcriber import MeetingLiveTranscriber


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

    def __init__(
        self,
        config: dict,
        audio_config: dict,
        live_transcriber: Optional[MeetingLiveTranscriber] = None,
    ):
        self.config = config or {}
        self.audio_config = audio_config or {}
        self.live_transcriber = live_transcriber
        self.logger = logging.getLogger(__name__)
        self.is_recording = False
        self.started_at: Optional[datetime] = None
        self.session_id: Optional[str] = None
        self._lock = threading.Lock()
        self._mic_thread: Optional[threading.Thread] = None
        self._system_thread: Optional[threading.Thread] = None
        self._mic_sample_rate: int = self.WHISPER_SAMPLE_RATE
        self._system_sample_rate: int = self.SYSTEM_SAMPLE_RATE
        self._mic_samples_captured: int = 0
        self._system_samples_captured: int = 0

        if self.live_transcriber:
            if self.config.get("capture_microphone", True):
                self.live_transcriber.register_source("mic", "MIC")
            if self.config.get("capture_system_audio", True):
                self.live_transcriber.register_source("system", "SYS")

    def start_recording(self) -> bool:
        with self._lock:
            if self.is_recording:
                return False
            self.is_recording = True
            self.started_at = datetime.now()
            self.session_id = self.started_at.strftime("meeting-%Y%m%d-%H%M%S")
            self._mic_samples_captured = 0
            self._system_samples_captured = 0

        if self.config.get("capture_microphone", True):
            self._mic_thread = threading.Thread(target=self._record_microphone, daemon=True)
            self._mic_thread.start()

        if self.config.get("capture_system_audio", True):
            self._system_thread = threading.Thread(target=self._record_system_audio, daemon=True)
            self._system_thread.start()

        return True

    def stop_recording(self) -> Optional[MeetingSession]:
        with self._lock:
            if not self.is_recording:
                return None
            self.is_recording = False

        for thread in (self._mic_thread, self._system_thread):
            if thread:
                thread.join(timeout=self.THREAD_JOIN_TIMEOUT)
                if thread.is_alive():
                    self.logger.warning("Meeting recorder thread did not exit within timeout")

        return self._build_session()

    def cancel_recording(self) -> None:
        with self._lock:
            self.is_recording = False
        for thread in (self._mic_thread, self._system_thread):
            if thread:
                thread.join(timeout=self.THREAD_JOIN_TIMEOUT)

    def get_recording_status(self) -> bool:
        return self.is_recording

    def _record_microphone(self) -> None:
        try:
            device = self.audio_config.get("input_device", "default")
            if device == "default":
                device = None
            self._mic_sample_rate = self._get_microphone_sample_rate(device)

            def callback(audio_data, _frames, _time, status):
                if status:
                    self.logger.debug(f"Meeting microphone callback status: {status}")
                if not self.is_recording:
                    return
                chunk = audio_data.copy()
                self._mic_samples_captured += len(chunk)
                if self.live_transcriber:
                    self.live_transcriber.push_audio("mic", chunk, self._mic_sample_rate)

            with sd.InputStream(
                samplerate=self._mic_sample_rate,
                channels=1,
                callback=callback,
                dtype=np.float32,
                device=device,
            ):
                while self.is_recording:
                    sd.sleep(int(self.RECORDING_SLEEP_SECONDS * 1000))
        except Exception as exc:
            self.logger.error(f"Meeting microphone capture failed: {exc}")
            print(f"❌ Meeting microphone capture failed: {exc}")

    def _get_microphone_sample_rate(self, device) -> int:
        try:
            info = (
                sd.query_devices(device, kind="input")
                if device is not None
                else sd.query_devices(kind="input")
            )
            return int(info.get("default_samplerate") or self.WHISPER_SAMPLE_RATE)
        except Exception:
            return self.WHISPER_SAMPLE_RATE

    def _record_system_audio(self) -> None:
        backend = str(self.config.get("system_audio_backend", "auto")).lower()
        if backend not in {"auto", "soundcard"}:
            self.logger.warning("Unsupported meeting system audio backend: %s", backend)
            return

        try:
            import soundcard as sc
        except Exception as exc:
            self.logger.warning("SoundCard not installed; system audio capture disabled: %s", exc)
            return

        try:
            speaker = self._select_speaker(sc)
            if speaker is None:
                print("   ⚠ No output device available for loopback capture.", flush=True)
                return
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
                                f"   ⚠ Loopback is returning silence after 5s. Is audio actually playing through '{speaker.name}'?",
                                flush=True,
                            )
                        else:
                            print(f"   ✓ Loopback active (peak so far: {max_peak:.3f})", flush=True)
                        diagnosed = True
                    self._system_samples_captured += len(arr)
                    if self.live_transcriber:
                        self.live_transcriber.push_audio("system", arr, self._system_sample_rate)
        except Exception as exc:
            self.logger.error(f"Meeting system audio capture failed: {exc}")
            print(f"⚠️ Meeting system audio capture unavailable: {exc}")

    def _select_speaker(self, sc):
        all_speakers = sc.all_speakers()
        default = sc.default_speaker()
        configured = str(self.config.get("system_audio_device") or "default").strip()

        print(f"   ℹ Available output devices ({len(all_speakers)}):", flush=True)
        for s in all_speakers:
            marker = " (Windows default)" if s.name == default.name else ""
            print(f"       - {s.name}{marker}", flush=True)

        if configured.lower() in {"", "default"}:
            print(f"   ℹ Capturing loopback from: {default.name}", flush=True)
            return default

        needle = configured.lower()
        for candidate in all_speakers:
            if needle in candidate.name.lower():
                print(f"   ℹ Capturing loopback from: {candidate.name} (matched '{configured}')", flush=True)
                return candidate

        print(
            f"   ⚠ No speaker matched '{configured}'. Falling back to default: {default.name}",
            flush=True,
        )
        return default

    def _build_session(self) -> Optional[MeetingSession]:
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
