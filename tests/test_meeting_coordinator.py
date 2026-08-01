import numpy as np
from fakes import FakeWhisperEngine

from whisper_key.application import (
    DurableCapturePipeline,
    MeetingCaptureCoordinator,
    SessionService,
)
from whisper_key.domain.session import SessionStatus
from whisper_key.meeting_live_transcriber import MeetingLiveTranscriber


class FakeRecorder:
    def __init__(self):
        self.audio_consumer = None
        self.on_source_error = None
        self.started = False

    def start_recording(self, capture_microphone, capture_system_audio):
        self.started = True
        return True

    def stop_recording(self):
        self.started = False
        return object()


def build_coordinator(tmp_path):
    service = SessionService(tmp_path)
    transcriber = MeetingLiveTranscriber(FakeWhisperEngine(["texto guardado"]))
    pipeline = DurableCapturePipeline(service, transcriber)
    recorder = FakeRecorder()
    coordinator = MeetingCaptureCoordinator(service, pipeline, recorder)
    return coordinator, service, recorder


def test_coordinator_pause_resume_and_finalize(tmp_path):
    coordinator, service, recorder = build_coordinator(tmp_path)
    coordinator.start("learning", capture_microphone=True, capture_system_audio=False)
    assert recorder.started
    recorder.audio_consumer("mic", "MIC", np.full(8000, 0.05, dtype=np.float32), 16000)
    coordinator.pause()
    assert service.session.status == SessionStatus.PAUSED
    coordinator.resume()
    assert service.session.status == SessionStatus.RECORDING
    recorder.audio_consumer("mic", "MIC", np.full(8000, 0.05, dtype=np.float32), 16000)
    final_folder = coordinator.finalize("Sesión continua")
    assert final_folder.exists()
    assert service.session.status == SessionStatus.COMPLETED
    assert len(list((final_folder / "audio" / "mic").glob("*.wav"))) == 2


def test_coordinator_requires_at_least_one_source(tmp_path):
    coordinator, _service, _recorder = build_coordinator(tmp_path)
    try:
        coordinator.start("meeting", False, False)
    except ValueError as exc:
        assert "At least one" in str(exc)
    else:
        raise AssertionError("Expected source validation")


def test_coordinator_continues_recoverable_session_as_new_stage(tmp_path):
    coordinator, service, recorder = build_coordinator(tmp_path)
    coordinator.start("learning", capture_microphone=True, capture_system_audio=False)
    session_id = service.session.session_id
    coordinator.interrupt()

    coordinator.continue_session(session_id, capture_microphone=True, capture_system_audio=False)

    assert service.session.status == SessionStatus.RECORDING
    assert len(service.session.stages) == 2
    assert recorder.started


def test_coordinator_applies_new_session_retention_policy(tmp_path):
    coordinator, service, _recorder = build_coordinator(tmp_path)

    coordinator.start(
        "learning",
        capture_microphone=True,
        capture_system_audio=False,
        retention={
            "audio": "marker_context",
            "marker_context_before_ms": 12000,
            "marker_context_after_ms": 18000,
        },
    )

    assert service.session.retention == {
        "audio": "marker_context",
        "marker_context_before_ms": 12000,
        "marker_context_after_ms": 18000,
    }
    coordinator.interrupt()
