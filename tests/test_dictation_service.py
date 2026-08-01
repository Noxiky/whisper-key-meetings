import numpy as np

from whisper_key.application import DictationService
from whisper_key.voice_activity_detection import VadEvent


class FakeEngine:
    def __init__(self):
        self.audio = None

    def transcribe_audio(self, audio):
        self.audio = audio
        return "  dictado listo  "


class FakeStream:
    def __init__(self, **kwargs):
        self.callback = kwargs["callback"]
        self.started = False
        self.closed = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def close(self):
        self.closed = True


def test_explicit_safety_cap_bounds_dictation():
    engine = FakeEngine()
    streams = []

    def factory(**kwargs):
        stream = FakeStream(**kwargs)
        streams.append(stream)
        return stream

    service = DictationService(engine, max_seconds=1, stream_factory=factory)
    service._device_sample_rate = lambda _device=None: 16000
    service.start()
    streams[0].callback(np.ones((20_000, 1), dtype=np.float32), 20_000, None, None)

    text = service.stop_and_transcribe()

    assert text == "dictado listo"
    assert len(engine.audio) == 16000
    assert streams[0].closed
    assert not service.is_recording


def test_unlimited_dictation_keeps_audio_beyond_old_five_minute_cap():
    engine = FakeEngine()
    streams = []

    def factory(**kwargs):
        stream = FakeStream(**kwargs)
        streams.append(stream)
        return stream

    service = DictationService(engine, max_seconds=0, stream_factory=factory)
    service._device_sample_rate = lambda _device=None: 16000
    service.start()
    six_minutes = 6 * 60 * 16000
    streams[0].callback(np.ones((six_minutes, 1), dtype=np.float32), six_minutes, None, None)

    service.stop_and_transcribe()

    assert len(engine.audio) == six_minutes


class FakeContinuousVad:
    def __init__(self, callback):
        self.callback = callback
        self.chunks = 0

    def reset(self):
        self.chunks = 0

    def process_chunk(self, _chunk):
        self.chunks += 1
        if self.chunks == 2:
            self.callback(VadEvent.SILENCE_TIMEOUT)


class FakeVadManager:
    def __init__(self):
        self.detector = None

    def create_continuous_detector(self, event_callback=None):
        self.detector = FakeContinuousVad(event_callback)
        return self.detector


def test_vad_silence_requests_automatic_stop_without_truncating_audio():
    engine = FakeEngine()
    streams = []
    stopped = []
    vad = FakeVadManager()

    def factory(**kwargs):
        stream = FakeStream(**kwargs)
        streams.append(stream)
        return stream

    service = DictationService(
        engine,
        max_seconds=0,
        vad_manager=vad,
        on_silence_timeout=lambda: stopped.append(True),
        stream_factory=factory,
    )
    service._device_sample_rate = lambda _device=None: 16000
    service.start()
    streams[0].callback(np.zeros((512, 1), dtype=np.float32), 512, None, None)

    assert stopped == [True]
    assert service.is_recording
    service.stop_and_transcribe()
    assert len(engine.audio) == 512
