import wave
from datetime import UTC, datetime, timedelta

import numpy as np

from whisper_key.application import SessionService
from whisper_key.infrastructure import DurableAudioStore, MarkerContextService


class FakeClock:
    def __init__(self):
        self.current = datetime(2026, 7, 17, 10, 0, tzinfo=UTC)
        self.elapsed = 0.0

    def now(self):
        return self.current

    def monotonic(self):
        return self.elapsed

    def advance(self, seconds):
        self.current += timedelta(seconds=seconds)
        self.elapsed += seconds


def persist_audio(folder, stage_id, value, seconds=4):
    store = DurableAudioStore()
    store.start(folder, stage_id)
    assert store.submit("mic", np.full(16000 * seconds, value, dtype=np.float32), 16000)
    store.stop()


def test_marker_excerpt_stays_aligned_across_long_pause_and_is_idempotent(tmp_path):
    clock = FakeClock()
    service = SessionService(tmp_path, clock=clock)
    session = service.create("learning")
    stage = service.start_stage()
    session.retention["marker_context_before_ms"] = 1000
    session.retention["marker_context_after_ms"] = 1000
    persist_audio(service.folder, stage.stage_id, 0.1)
    clock.advance(4)
    service.pause()
    clock.advance(100)
    service.resume()
    persist_audio(service.folder, stage.stage_id, 0.5)
    clock.advance(2)
    marker_id = service.add_marker("important")
    clock.advance(2)
    service.finish_stage()
    builder = MarkerContextService()

    created = builder.build(service)
    repeated = builder.build(service)

    assert repeated == []
    assert len(created) == 1
    assert created[0].name == f"{marker_id}-mic.wav"
    with wave.open(str(created[0]), "rb") as reader:
        audio = np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2").astype(np.float32) / 32767
        assert 1900 <= reader.getnframes() / reader.getframerate() * 1000 <= 2100
    assert float(np.mean(audio)) > 0.4
    attachments = [
        event for event in service.repository.read_events(service.folder) if event["type"] == "snapshot_created"
    ]
    assert attachments[0]["payload"]["source"] == "MIC"
    assert attachments[0]["payload"]["duration_ms"] == 2000
