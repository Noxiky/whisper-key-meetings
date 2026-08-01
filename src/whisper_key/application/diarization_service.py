from __future__ import annotations

from whisper_key.domain.diarization import DiarizationSegment, SpeakerAssignment


class DiarizationService:
    """Map optional anonymous speaker turns onto immutable raw transcript segments."""

    def assign(
        self,
        events: list[dict],
        diarization: list[DiarizationSegment] | None = None,
    ) -> list[SpeakerAssignment]:
        turns = diarization or []
        assignments = []
        for event in events:
            if event.get("type") != "transcript_final":
                continue
            payload = event["payload"]
            source = payload["source"]
            if source == "SYS":
                assignments.append(SpeakerAssignment(payload["segment_id"], "SYS", 1.0, "source"))
                continue
            match = self._best_overlap(payload["started_at_ms"], payload["ended_at_ms"], turns)
            if match:
                assignments.append(
                    SpeakerAssignment(payload["segment_id"], match.speaker_id, match.confidence, "diarization")
                )
            else:
                fallback = "MIC" if source == "MIC" else source
                assignments.append(SpeakerAssignment(payload["segment_id"], fallback, 1.0, "source_fallback"))
        return assignments

    @staticmethod
    def build_revision(
        assignments: list[SpeakerAssignment],
        speaker_names: dict[str, str] | None = None,
        revision: int = 1,
    ) -> dict:
        names = speaker_names or {}
        speaker_ids = sorted({assignment.speaker_id for assignment in assignments})
        return {
            "schema_version": 1,
            "revision": revision,
            "speakers": [
                {"speaker_id": speaker_id, "display_name": names.get(speaker_id, _default_name(speaker_id))}
                for speaker_id in speaker_ids
            ],
            "assignments": [assignment.to_dict() for assignment in assignments],
        }

    @staticmethod
    def revise_names(current: dict, speaker_names: dict[str, str]) -> dict:
        existing_ids = {item["speaker_id"] for item in current.get("speakers", [])}
        if set(speaker_names) - existing_ids:
            raise ValueError("Cannot rename an unknown speaker")
        normalized = {}
        for speaker_id, display_name in speaker_names.items():
            value = " ".join(display_name.split()).strip()
            if not value:
                raise ValueError("Speaker names cannot be empty")
            if len(value) > 100:
                raise ValueError("Speaker names cannot exceed 100 characters")
            normalized[speaker_id] = value
        return {
            "schema_version": 1,
            "revision": int(current.get("revision", 0)) + 1,
            "speakers": [
                {
                    "speaker_id": item["speaker_id"],
                    "display_name": normalized.get(item["speaker_id"], item["display_name"]),
                }
                for item in current.get("speakers", [])
            ],
            "assignments": list(current.get("assignments", [])),
        }

    @staticmethod
    def _best_overlap(started_at_ms: int, ended_at_ms: int, turns: list[DiarizationSegment]):
        best = None
        best_overlap = 0
        for turn in turns:
            overlap = max(0, min(ended_at_ms, turn.ended_at_ms) - max(started_at_ms, turn.started_at_ms))
            if overlap > best_overlap:
                best = turn
                best_overlap = overlap
        return best


def _default_name(speaker_id: str) -> str:
    if speaker_id.startswith("speaker_"):
        return f"Speaker {speaker_id.rsplit('_', 1)[-1]}"
    return speaker_id
