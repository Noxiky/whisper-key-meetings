from pathlib import Path

from whisper_key.application.capture_pipeline import DurableCapturePipeline
from whisper_key.application.session_service import SessionService
from whisper_key.domain.session import InvalidSessionTransition, SessionMode, SessionStatus
from whisper_key.meeting_recorder import MeetingRecorder


class MeetingCaptureCoordinator:
    def __init__(
        self,
        session_service: SessionService,
        pipeline: DurableCapturePipeline,
        recorder: MeetingRecorder,
    ):
        self.session_service = session_service
        self.pipeline = pipeline
        self.recorder = recorder
        self.recorder.audio_consumer = self.pipeline.ingest
        self.recorder.on_source_error = self.pipeline.report_source_error
        self.active_sources: list[str] = []

    def start(
        self,
        mode: SessionMode | str,
        capture_microphone: bool = True,
        capture_system_audio: bool = True,
        auto_stop_seconds: float = 0.0,
        retention: dict | None = None,
    ) -> None:
        if not capture_microphone and not capture_system_audio:
            raise ValueError("At least one audio source is required")
        self.session_service.create(mode, retention=retention)
        self.session_service.start_stage()
        self.active_sources = []
        if capture_microphone:
            self.pipeline.transcriber.register_source("mic", "MIC")
            self.active_sources.append("mic")
        if capture_system_audio:
            self.pipeline.transcriber.register_source("system", "SYS")
            self.active_sources.append("system")
        self.pipeline.start(self.active_sources, auto_stop_seconds)
        if not self.recorder.start_recording(capture_microphone, capture_system_audio):
            self.pipeline.stop()
            self.session_service.interrupt()
            raise RuntimeError("Audio recorder did not start")

    def pause(self) -> None:
        self._require_status(SessionStatus.RECORDING)
        self.recorder.stop_recording()
        self.pipeline.stop()
        self.session_service.pause()

    def continue_session(
        self,
        session_id: str,
        capture_microphone: bool = True,
        capture_system_audio: bool = True,
    ) -> None:
        if not capture_microphone and not capture_system_audio:
            raise ValueError("At least one audio source is required")
        session = self.session_service.load(session_id)
        if session.status in {
            SessionStatus.RECORDING,
            SessionStatus.PAUSED,
            SessionStatus.PREPARING,
            SessionStatus.PROCESSING,
        }:
            self.session_service.recover()
        if self.session_service.session.status not in {SessionStatus.DRAFT, SessionStatus.RECOVERABLE}:
            raise InvalidSessionTransition(f"Cannot continue from {self.session_service.session.status}")
        self.session_service.start_stage()
        self.active_sources = []
        if capture_microphone:
            self.pipeline.transcriber.register_source("mic", "MIC")
            self.active_sources.append("mic")
        if capture_system_audio:
            self.pipeline.transcriber.register_source("system", "SYS")
            self.active_sources.append("system")
        self.pipeline.start(self.active_sources)
        if not self.recorder.start_recording(capture_microphone, capture_system_audio):
            self.pipeline.stop()
            self.session_service.interrupt()
            raise RuntimeError("Audio recorder did not resume recovered session")

    def resume(self) -> None:
        self._require_status(SessionStatus.PAUSED)
        self.session_service.resume()
        self.pipeline.start(self.active_sources)
        capture_microphone = "mic" in self.active_sources
        capture_system_audio = "system" in self.active_sources
        if not self.recorder.start_recording(capture_microphone, capture_system_audio):
            self.pipeline.stop()
            self.session_service.pause()
            raise RuntimeError("Audio recorder did not resume")

    def interrupt(self) -> None:
        session = self.session_service.session
        if not session or session.status not in {SessionStatus.RECORDING, SessionStatus.PAUSED}:
            return
        if session.status == SessionStatus.RECORDING:
            self.recorder.stop_recording()
            self.pipeline.stop()
        self.session_service.interrupt()

    def finish_stage(self) -> None:
        session = self.session_service.session
        if not session or session.status not in {SessionStatus.RECORDING, SessionStatus.PAUSED}:
            raise InvalidSessionTransition("No active capture stage")
        if session.status == SessionStatus.RECORDING:
            self.recorder.stop_recording()
            self.pipeline.stop()
        self.session_service.finish_stage()

    def finalize(self, title: str) -> Path:
        session = self.session_service.session
        if not session:
            raise InvalidSessionTransition("No active session")
        if session.status == SessionStatus.RECORDING:
            self.recorder.stop_recording()
            self.pipeline.stop()
            self.session_service.finish_stage()
        elif session.status == SessionStatus.PAUSED:
            self.session_service.finish_stage()
        self.session_service.name(title)
        return self.session_service.finalize()

    def _require_status(self, status: SessionStatus) -> None:
        session = self.session_service.session
        if not session or session.status != status:
            actual = session.status if session else "none"
            raise InvalidSessionTransition(f"Expected {status}, found {actual}")
