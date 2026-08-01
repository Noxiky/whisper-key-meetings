from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DiarizationSegment:
    speaker_id: str
    started_at_ms: int
    ended_at_ms: int
    confidence: float | None = None


@dataclass(frozen=True)
class SpeakerAssignment:
    segment_id: str
    speaker_id: str
    confidence: float | None
    method: str

    def to_dict(self) -> dict:
        return {
            "segment_id": self.segment_id,
            "speaker_id": self.speaker_id,
            "confidence": self.confidence,
            "method": self.method,
        }
