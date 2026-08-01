from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

DEFAULT_RETENTION = {
    "audio": "all",
    "marker_context_before_ms": 30000,
    "marker_context_after_ms": 30000,
}


class SessionMode(StrEnum):
    DICTATION = "dictation"
    MEETING = "meeting"
    LEARNING = "learning"
    READING = "reading"
    IDEA = "idea"


class SessionStatus(StrEnum):
    DRAFT = "draft"
    PREPARING = "preparing"
    RECORDING = "recording"
    PAUSED = "paused"
    PROCESSING = "processing"
    RECOVERABLE = "recoverable"
    COMPLETED = "completed"
    ERROR = "error"


class StageStatus(StrEnum):
    RECORDING = "recording"
    PAUSED = "paused"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"


class InvalidSessionTransition(RuntimeError):
    pass


@dataclass
class SessionStage:
    stage_id: str
    sequence: int
    started_at: str
    ended_at: str | None = None
    status: StageStatus = StageStatus.RECORDING

    def to_dict(self) -> dict:
        return {
            "stage_id": self.stage_id,
            "sequence": self.sequence,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "status": self.status.value,
        }

    @classmethod
    def from_dict(cls, value: dict):
        return cls(
            stage_id=value["stage_id"],
            sequence=value["sequence"],
            started_at=value["started_at"],
            ended_at=value.get("ended_at"),
            status=StageStatus(value["status"]),
        )


@dataclass
class Session:
    session_id: str
    mode: SessionMode
    status: SessionStatus
    created_at: str
    updated_at: str
    title: str | None = None
    retention: dict = field(default_factory=lambda: dict(DEFAULT_RETENTION))
    stages: list[SessionStage] = field(default_factory=list)
    schema_version: int = 1
    wall_duration_ms: int = 0
    captured_duration_ms: int = 0
    paused_duration_ms: int = 0
    processing_duration_ms: int = 0

    @property
    def active_stage(self) -> SessionStage | None:
        if not self.stages:
            return None
        stage = self.stages[-1]
        if stage.status in {StageStatus.RECORDING, StageStatus.PAUSED}:
            return stage
        return None

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "mode": self.mode.value,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "title": self.title,
            "retention": dict(self.retention),
            "stages": [stage.to_dict() for stage in self.stages],
            "wall_duration_ms": self.wall_duration_ms,
            "captured_duration_ms": self.captured_duration_ms,
            "paused_duration_ms": self.paused_duration_ms,
            "processing_duration_ms": self.processing_duration_ms,
        }

    @classmethod
    def from_dict(cls, value: dict):
        return cls(
            schema_version=value["schema_version"],
            session_id=value["session_id"],
            mode=SessionMode(value["mode"]),
            status=SessionStatus(value["status"]),
            created_at=value["created_at"],
            updated_at=value.get("updated_at", value["created_at"]),
            title=value.get("title"),
            retention=dict(value["retention"]),
            stages=[SessionStage.from_dict(stage) for stage in value["stages"]],
            wall_duration_ms=value.get("wall_duration_ms", 0),
            captured_duration_ms=value.get("captured_duration_ms", 0),
            paused_duration_ms=value.get("paused_duration_ms", 0),
            processing_duration_ms=value.get("processing_duration_ms", 0),
        )


def isoformat_utc(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def rebuild_session(events: list[dict]) -> Session:
    if not events or events[0]["type"] != "session_created":
        raise ValueError("Timeline must begin with session_created")
    first = events[0]
    session = Session(
        session_id=first["session_id"],
        mode=SessionMode(first["payload"]["mode"]),
        status=SessionStatus.DRAFT,
        created_at=first["occurred_at"],
        updated_at=first["occurred_at"],
        retention=dict(first["payload"].get("retention") or DEFAULT_RETENTION),
    )
    previous_offset = first["session_offset_ms"]
    for event in events[1:]:
        offset = event["session_offset_ms"]
        elapsed = max(0, offset - previous_offset)
        if session.status == SessionStatus.RECORDING:
            session.captured_duration_ms += elapsed
        elif session.status == SessionStatus.PAUSED:
            session.paused_duration_ms += elapsed
        session.wall_duration_ms = session.captured_duration_ms + session.paused_duration_ms
        previous_offset = offset
        session.updated_at = event["occurred_at"]
        payload = event["payload"]
        event_type = event["type"]
        if event_type == "stage_started":
            session.stages.append(
                SessionStage(
                    stage_id=payload["stage_id"],
                    sequence=payload["stage_sequence"],
                    started_at=event["occurred_at"],
                )
            )
            session.status = SessionStatus.RECORDING
        elif event_type == "pause_started":
            session.status = SessionStatus.PAUSED
            session.active_stage.status = StageStatus.PAUSED
        elif event_type == "pause_ended":
            session.status = SessionStatus.RECORDING
            session.active_stage.status = StageStatus.RECORDING
        elif event_type == "stage_ended":
            stage = session.active_stage
            if stage:
                stage.status = (
                    StageStatus.COMPLETED if payload.get("reason") == "completed" else StageStatus.INTERRUPTED
                )
                stage.ended_at = event["occurred_at"]
            session.status = SessionStatus.RECOVERABLE
        elif event_type == "recovery_detected":
            stage = session.active_stage
            if stage:
                stage.status = StageStatus.INTERRUPTED
                stage.ended_at = event["occurred_at"]
            session.status = SessionStatus.RECOVERABLE
        elif event_type == "session_named":
            session.title = payload["title"]
        elif event_type in {"processing_started", "session_finalized"} and session.status != SessionStatus.COMPLETED:
            session.status = SessionStatus.PROCESSING
        elif event_type == "processing_failed":
            session.status = SessionStatus.RECOVERABLE
        elif event_type == "session_completed":
            session.status = SessionStatus.COMPLETED
        elif event_type == "processing_job" and payload.get("duration_ms") is not None:
            session.processing_duration_ms += payload["duration_ms"]
        elif event_type == "capture_timing_reconciled":
            session.captured_duration_ms = max(0, int(payload["captured_duration_ms"]))
            session.paused_duration_ms = max(0, int(payload["paused_duration_ms"]))
            session.wall_duration_ms = max(0, int(payload["wall_duration_ms"]))
    return session
