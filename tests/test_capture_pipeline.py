import time
from datetime import UTC, datetime, timedelta

import numpy as np
from fakes import FakeWhisperEngine

from whisper_key.application import DurableCapturePipeline, SessionService
from whisper_key.domain.audio import TranscriptResult
from whisper_key.meeting_live_transcriber import MeetingLiveTranscriber


class RejectingTranscriber:
    def __init__(self):
        self.on_transcript = None
        self.on_backpressure = None

    def start(self, **_kwargs):
        return None

    def push_audio(self, *_args):
        return False

    def stop(self):
        return None

    @property
    def backlog(self):
        return 0


class RejectingAudioStore:
    backlog = 1

    def start(self, *_args):
        return None

    def submit(self, *_args):
        return False

    def stop(self):
        return None


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


def test_durable_pipeline_persists_before_transcript_and_emits_health(tmp_path):
    service = SessionService(tmp_path)
    service.create("meeting")
    service.start_stage()
    health = []
    transcriber = MeetingLiveTranscriber(FakeWhisperEngine(["hola desde el micrófono"]))
    transcriber.register_source("mic", "MIC")
    pipeline = DurableCapturePipeline(service, transcriber, on_health=health.append)
    pipeline.start(["mic"])
    assert pipeline.ingest("mic", "MIC", np.full(8000, 0.05, dtype=np.float32), 16000)
    assert pipeline.ingest("mic", "MIC", np.zeros(12800, dtype=np.float32), 16000)
    time.sleep(0.05)
    pipeline.stop()
    events = service.repository.read_events(service.folder)
    transcript = next(event for event in events if event["type"] == "transcript_final")
    assert transcript["payload"]["raw_text"] == "hola desde el micrófono"
    assert transcript["payload"]["source"] == "MIC"
    wave_path = next((service.folder / "audio" / "mic").glob("*.wav"))
    assert wave_path.exists()
    audio_event = next(event for event in events if event["type"] == "audio_chunk_finalized")
    assert audio_event["payload"]["relative_path"] == wave_path.relative_to(service.folder).as_posix()
    assert audio_event["payload"]["sha256"]
    assert health[-1].status == "active"
    source_health = [event for event in events if event["type"] == "source_health"]
    assert len(source_health) == 1
    assert source_health[0]["payload"]["status"] == "active"


def test_system_failure_does_not_prevent_microphone_capture(tmp_path):
    service = SessionService(tmp_path)
    service.create("meeting")
    service.start_stage()
    transcriber = MeetingLiveTranscriber(FakeWhisperEngine(["micrófono intacto"]))
    transcriber.register_source("mic", "MIC")
    transcriber.register_source("system", "SYS")
    pipeline = DurableCapturePipeline(service, transcriber)
    pipeline.start(["mic", "system"])
    pipeline.report_source_error("system", "Loopback unavailable")
    pipeline.ingest("mic", "MIC", np.full(8000, 0.05, dtype=np.float32), 16000)
    pipeline.stop()
    events = service.repository.read_events(service.folder)
    assert any(event["type"] == "transcript_final" for event in events)
    system_health = [
        event for event in events if event["type"] == "source_health" and event["payload"]["source_id"] == "system"
    ]
    assert system_health[-1]["payload"]["status"] == "unavailable"


def test_reconnected_source_returns_to_active_health(tmp_path):
    service = SessionService(tmp_path)
    service.create("meeting")
    service.start_stage()
    transcriber = MeetingLiveTranscriber(FakeWhisperEngine(["audio recuperado"]))
    transcriber.register_source("system", "SYS")
    pipeline = DurableCapturePipeline(service, transcriber)
    pipeline.start(["system"])

    pipeline.report_source_error("system", "Salida desconectada · reintentando automáticamente")
    assert pipeline.ingest(
        "system",
        "SYS",
        np.full(8000, 0.05, dtype=np.float32),
        16000,
    )
    pipeline.stop()

    statuses = [
        event["payload"]["status"]
        for event in service.repository.read_events(service.folder)
        if event["type"] == "source_health" and event["payload"]["source_id"] == "system"
    ]
    assert statuses == ["unavailable", "active"]


def test_transcription_backpressure_is_visible_while_audio_remains_safe(tmp_path):
    service = SessionService(tmp_path)
    service.create("meeting")
    service.start_stage()
    health = []
    pipeline = DurableCapturePipeline(service, RejectingTranscriber(), on_health=health.append)
    pipeline.start(["mic"])
    assert pipeline.ingest("mic", "MIC", np.ones(1600, dtype=np.float32), 16000)
    pipeline.stop()
    assert any(item.status == "transcription_backpressure" and not item.fatal for item in health)
    assert next((service.folder / "audio" / "mic").glob("*.wav")).exists()


def test_persistence_backpressure_is_fatal_and_visible(tmp_path):
    service = SessionService(tmp_path)
    service.create("meeting")
    service.start_stage()
    health = []
    pipeline = DurableCapturePipeline(
        service,
        RejectingTranscriber(),
        audio_store=RejectingAudioStore(),
        on_health=health.append,
    )
    pipeline.start(["mic"])
    assert not pipeline.ingest("mic", "MIC", np.ones(100, dtype=np.float32), 16000)
    pipeline.stop()
    assert health[-1].status == "persistence_backpressure"
    assert health[-1].fatal


def test_transcript_offsets_continue_after_a_long_pause(tmp_path):
    clock = FakeClock()
    service = SessionService(tmp_path, clock=clock)
    service.create("learning")
    service.start_stage()
    pipeline = DurableCapturePipeline(service, RejectingTranscriber())
    pipeline.start(["mic"])
    pipeline.stop()
    service.pause()
    clock.advance(90)
    service.resume()
    pipeline.start(["mic"])
    pipeline._on_transcript(TranscriptResult("mic", "MIC", "despuÃ©s de la pausa", 250, 1_250, "es"))
    pipeline.stop()

    transcript = next(
        event for event in service.repository.read_events(service.folder) if event["type"] == "transcript_final"
    )
    assert transcript["payload"]["started_at_ms"] == 90_250
    assert transcript["payload"]["ended_at_ms"] == 91_250
