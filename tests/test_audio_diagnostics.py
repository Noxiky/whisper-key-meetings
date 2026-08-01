from types import SimpleNamespace

import numpy as np

from whisper_key.infrastructure import AudioDiagnosticsService


class FakeSoundDevice:
    default = SimpleNamespace(device=(0, 1))

    @staticmethod
    def query_devices(device=None, kind=None):
        devices = [
            {
                "index": 0,
                "name": "Studio MIC",
                "hostapi": 0,
                "max_input_channels": 1,
                "max_output_channels": 0,
                "default_samplerate": 16_000,
            },
            {
                "index": 1,
                "name": "Speakers",
                "hostapi": 0,
                "max_input_channels": 0,
                "max_output_channels": 2,
                "default_samplerate": 48_000,
            },
        ]
        if kind == "input":
            return devices[0]
        return devices

    @staticmethod
    def query_hostapis(_index):
        return {"name": "Windows WASAPI"}

    @staticmethod
    def rec(frames, **_kwargs):
        return np.full((frames, 1), 0.1, dtype=np.float32)


class FakeRecorder:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    @staticmethod
    def record(numframes):
        return np.full((numframes, 2), 0.05, dtype=np.float32)


class FakeLoopback:
    @staticmethod
    def recorder(**_kwargs):
        return FakeRecorder()


class FakeSoundCard:
    speaker = SimpleNamespace(name="Speakers")

    @classmethod
    def all_speakers(cls):
        return [cls.speaker]

    @classmethod
    def default_speaker(cls):
        return cls.speaker

    @staticmethod
    def get_microphone(**_kwargs):
        return FakeLoopback()


def test_diagnostics_reports_live_mic_and_system_without_audio_payload():
    service = AudioDiagnosticsService(
        duration_seconds=0.25,
        sounddevice_module=FakeSoundDevice(),
        soundcard_module=FakeSoundCard(),
    )

    result = service.run()

    assert result["summary"]["status"] == "pass"
    assert result["mic"]["status"] == "active"
    assert result["system"]["status"] == "active"
    assert result["mic"]["peak_dbfs"] == -20.0
    assert result["privacy"] == {
        "audio_persisted": False,
        "transcribed": False,
        "uploaded": False,
    }
    assert len(result["devices"]) == 2
    assert result["system_outputs"] == [{"name": "Speakers", "default": True}]
    assert "audio" not in result["mic"]
    assert "audio" not in result["system"]


def test_diagnostics_distinguishes_silence_from_unavailable_route():
    class SilentSoundDevice(FakeSoundDevice):
        @staticmethod
        def rec(frames, **_kwargs):
            return np.zeros((frames, 1), dtype=np.float32)

    class BrokenSoundCard:
        @staticmethod
        def all_speakers():
            raise RuntimeError("loopback unavailable")

    result = AudioDiagnosticsService(
        duration_seconds=0.25,
        sounddevice_module=SilentSoundDevice(),
        soundcard_module=BrokenSoundCard(),
    ).run()

    assert result["summary"]["status"] == "attention"
    assert result["mic"]["status"] == "silent"
    assert result["system"]["status"] == "unavailable"
    assert result["system"]["error"] == "loopback unavailable"
    assert result["system_outputs"] == []
