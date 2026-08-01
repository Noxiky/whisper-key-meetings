import json

import pytest
from conftest import GOLDEN
from jsonschema import Draft202012Validator, ValidationError

from whisper_key.domain import render_literal_markdown
from whisper_key.domain.projections import render_clean_markdown, render_markers_markdown
from whisper_key.domain.session import SessionStatus, rebuild_session


def read_events():
    return [json.loads(line) for line in (GOLDEN / "timeline.jsonl").read_text(encoding="utf-8").splitlines()]


def test_golden_session_and_events_are_valid(schema_validator):
    schema_validator("session.schema.json").validate(json.loads((GOLDEN / "session.json").read_text(encoding="utf-8")))
    validator = schema_validator("timeline-event.schema.json")
    for event in read_events():
        validator.validate(event)


def test_every_schema_is_meta_schema_valid():
    schema_dir = GOLDEN.parents[2] / "schemas"
    for path in schema_dir.glob("*.schema.json"):
        Draft202012Validator.check_schema(json.loads(path.read_text(encoding="utf-8")))


def test_sequences_are_strictly_monotonic_and_session_ids_match():
    events = read_events()
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
    assert {event["session_id"] for event in events} == {"11111111-1111-4111-8111-111111111111"}


def test_transcript_event_rejects_unknown_source(schema_validator):
    event = next(item for item in read_events() if item["type"] == "transcript_final")
    event["payload"]["source"] = "UNKNOWN"
    with pytest.raises(ValidationError):
        schema_validator("timeline-event.schema.json").validate(event)


def test_attachment_rejects_absolute_and_parent_paths(schema_validator):
    event = next(item for item in read_events() if item["type"] == "snapshot_created")
    validator = schema_validator("timeline-event.schema.json")
    for path in ("C:/private/image.png", "../private/image.png", "/private/image.png"):
        event["payload"]["relative_path"] = path
        with pytest.raises(ValidationError):
            validator.validate(event)


def test_date_time_format_is_enforced(schema_validator):
    session = json.loads((GOLDEN / "session.json").read_text(encoding="utf-8"))
    session["created_at"] = "yesterday"
    with pytest.raises(ValidationError):
        schema_validator("session.schema.json").validate(session)


def test_literal_projection_matches_golden_and_is_deterministic():
    session = json.loads((GOLDEN / "session.json").read_text(encoding="utf-8"))
    events = read_events()
    expected = (GOLDEN / "transcript.raw.md").read_text(encoding="utf-8")
    assert render_literal_markdown(session, events) == expected
    assert render_literal_markdown(session, list(reversed(events))) == expected


def test_golden_timeline_rebuilds_completed_session():
    rebuilt = rebuild_session(read_events())
    assert rebuilt.status == SessionStatus.COMPLETED
    assert rebuilt.title == "How streaming transcription works"
    assert len(rebuilt.stages) == 1


def test_markdown_projections_neutralize_imported_active_content():
    session = {"title": "Imported <img src=x>", "mode": "learning"}
    malicious = "Normal [click](file:///C:/private) ![image](data:text/html,bad) <script>alert(1)</script>"
    events = [
        {
            "sequence": 1,
            "type": "transcript_final",
            "payload": {
                "segment_id": "11111111-1111-4111-8111-111111111111",
                "source": "IMPORTED",
                "started_at_ms": 0,
                "ended_at_ms": 1000,
                "language": "en",
                "raw_text": malicious,
            },
        },
        {
            "sequence": 2,
            "type": "marker_created",
            "payload": {
                "marker_id": "22222222-2222-4222-8222-222222222222",
                "kind": "question",
                "at_ms": 500,
                "note": malicious,
            },
        },
        {
            "sequence": 3,
            "type": "snapshot_created",
            "payload": {
                "kind": "screenshot",
                "at_ms": 500,
                "relative_path": "attachments/board image.png",
            },
        },
    ]

    outputs = (
        render_literal_markdown(session, events),
        render_clean_markdown(session, events),
        render_markers_markdown(session, events),
    )

    for output in outputs:
        assert "<script>" not in output
        assert "<img" not in output
        assert "[click](" not in output
        assert "![image](" not in output
        assert "&lt;script&gt;" in output
    assert "attachments/board%20image.png" in outputs[0]
    assert "attachments/board%20image.png" in outputs[2]
