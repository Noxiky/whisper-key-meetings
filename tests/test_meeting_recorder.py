import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from whisper_key.meeting_recorder import MeetingRecorder


def test_meeting_recorder_routes_audio_to_durable_consumer():
    received = []

    def consume(source_id, label, audio, sample_rate):
        received.append((source_id, label, audio.copy(), sample_rate))
        return True

    recorder = MeetingRecorder({}, {}, audio_consumer=consume)
    chunk = np.ones(16, dtype=np.float32)
    assert recorder._deliver_audio("mic", "MIC", chunk, 16000)
    assert received[0][0:2] == ("mic", "MIC")
    assert received[0][3] == 16000


def test_meeting_recorder_reports_consumer_rejection_and_source_error():
    errors = []
    recorder = MeetingRecorder(
        {},
        {},
        audio_consumer=lambda *_args: False,
        on_source_error=lambda *args: errors.append(args),
    )
    assert not recorder._deliver_audio("system", "SYS", np.ones(4, dtype=np.float32), 48000)
    assert not recorder.is_recording
    recorder._source_error("system", "unavailable")
    assert errors == [("system", "unavailable")]


def test_source_retries_after_disconnect_and_keeps_session_alive():
    errors = []
    attempts = []
    recorder = MeetingRecorder(
        {
            "device_reconnect_initial_seconds": 0.01,
            "device_reconnect_max_seconds": 0.02,
        },
        {},
        on_source_error=lambda *args: errors.append(args),
    )
    recorder.is_recording = True

    def capture_once():
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise OSError("USB device removed")
        recorder.is_recording = False

    recorder._run_source_with_reconnect("mic", "micrófono", capture_once)

    assert attempts == [1, 2]
    assert errors == [("mic", "USB device removed · reintentando automáticamente")]


def test_stop_interrupts_a_long_reconnect_wait_immediately():
    failed = threading.Event()
    recorder = MeetingRecorder(
        {
            "device_reconnect_initial_seconds": 30,
            "device_reconnect_max_seconds": 30,
        },
        {},
    )
    recorder.is_recording = True

    def fail_once():
        failed.set()
        raise OSError("Bluetooth unavailable")

    worker = threading.Thread(
        target=recorder._run_source_with_reconnect,
        args=("mic", "micrófono", fail_once),
    )
    worker.start()
    assert failed.wait(1)
    started = time.perf_counter()
    recorder.is_recording = False
    recorder._stop_event.set()
    worker.join(1)

    assert not worker.is_alive()
    assert time.perf_counter() - started < 0.5


def test_explicit_microphone_reconnects_by_stable_name_when_index_changes(monkeypatch):
    class FakeSoundDevice:
        devices = [
            {
                "index": 4,
                "name": "USB Podcast MIC",
                "max_input_channels": 1,
                "default_samplerate": 48000,
            }
        ]

        @classmethod
        def query_devices(cls, device=None, kind=None):
            if device is None and kind is None:
                return cls.devices
            return next(item for item in cls.devices if item["index"] == device)

    monkeypatch.setattr("whisper_key.meeting_recorder.sd", FakeSoundDevice)
    recorder = MeetingRecorder({}, {"input_device": 4})

    assert recorder._resolve_microphone_device() == 4
    FakeSoundDevice.devices = [
        {
            "index": 9,
            "name": "USB Podcast MIC",
            "max_input_channels": 1,
            "default_samplerate": 48000,
        }
    ]

    assert recorder._resolve_microphone_device() == 9


def test_explicit_system_route_never_silently_falls_back_to_default():
    default = SimpleNamespace(name="Laptop Speakers")

    class FakeSoundCard:
        @staticmethod
        def all_speakers():
            return [default]

        @staticmethod
        def default_speaker():
            return default

    recorder = MeetingRecorder({"system_audio_device": "Conference Headset"}, {})

    with pytest.raises(RuntimeError, match="no está conectada"):
        recorder._select_speaker(FakeSoundCard)
