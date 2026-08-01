import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from whisper_key.application import SessionService
from whisper_key.domain.projections import render_literal_markdown, render_markers_markdown
from whisper_key.domain.session import InvalidSessionTransition, SessionMode, SessionStatus, rebuild_session
from whisper_key.infrastructure import SessionJournalError, SessionRepository


class FakeClock:
    def __init__(self):
        self.current = datetime(2026, 7, 17, 14, 0, tzinfo=UTC)
        self.elapsed = 100.0

    def now(self):
        return self.current

    def monotonic(self):
        return self.elapsed

    def advance(self, seconds):
        self.current += timedelta(seconds=seconds)
        self.elapsed += seconds


def create_recording(tmp_path):
    clock = FakeClock()
    service = SessionService(tmp_path, clock=clock)
    session = service.create("learning")
    service.start_stage()
    return service, session, clock


def test_create_builds_portable_folder_and_default_retention(tmp_path):
    service = SessionService(tmp_path, clock=FakeClock())
    session = service.create("learning")
    assert service.folder == tmp_path / "inbox" / session.session_id
    assert session.retention == {
        "audio": "all",
        "marker_context_before_ms": 30000,
        "marker_context_after_ms": 30000,
    }
    for relative in ("attachments", "audio/mic", "audio/sys", "audio/excerpts", "exports"):
        assert (service.folder / relative).is_dir()


def test_dictation_is_a_durable_session_mode(tmp_path):
    service = SessionService(tmp_path, clock=FakeClock())

    session = service.create("dictation")

    assert session.mode is SessionMode.DICTATION


def test_provisional_transcript_is_separate_from_final_evidence(tmp_path):
    service, _session, _clock = create_recording(tmp_path)

    provisional_id = service.add_provisional_transcript("MIC", "texto prov", 0, 500)
    final_id = service.add_transcript("MIC", "texto final", 0, 700)
    events = service.repository.read_events(service.folder)

    provisional = next(event for event in events if event["type"] == "transcript_provisional")
    final = next(event for event in events if event["type"] == "transcript_final")
    assert provisional["payload"]["provisional_id"] == provisional_id
    assert final["payload"]["segment_id"] == final_id
    assert provisional_id != final_id


def test_spoken_note_links_only_the_next_final_microphone_segment(tmp_path, schema_validator):
    service, session, _clock = create_recording(tmp_path)

    marker_id = service.arm_spoken_note("important", "Explicar con mi voz")
    service.add_transcript("SYS", "Contenido del sistema.", 100, 400, "es")
    segment_id = service.add_transcript("MIC", "Esta es la nota que quería guardar.", 500, 1200, "es")
    service.add_transcript("MIC", "Esto ya es transcripción normal.", 1300, 1800, "es")

    events = service.repository.read_events(service.folder)
    notes = [event for event in events if event["type"] == "spoken_note"]
    assert len(notes) == 1
    assert notes[0]["payload"]["marker_id"] == marker_id
    assert notes[0]["payload"]["segment_id"] == segment_id
    assert notes[0]["payload"]["raw_text"] == "Esta es la nota que quería guardar."
    schema_validator("timeline-event.schema.json").validate(notes[0])
    assert "**Spoken note:** Esta es la nota que quería guardar." in render_literal_markdown(session.to_dict(), events)
    assert "**Spoken note:** Esta es la nota que quería guardar." in render_markers_markdown(session.to_dict(), events)


def test_finish_stage_safety_saves_without_forcing_a_title(tmp_path):
    service, session, _clock = create_recording(tmp_path)

    service.finish_stage()

    assert session.status == SessionStatus.RECOVERABLE
    assert service.folder.parent == tmp_path / "inbox"
    assert session.title is None


def test_pause_resume_interrupt_and_continue_preserve_timers(tmp_path):
    service, session, clock = create_recording(tmp_path)
    first_stage = session.active_stage
    clock.advance(5)
    service.pause()
    clock.advance(2)
    service.resume()
    clock.advance(3)
    service.interrupt()
    assert session.status == SessionStatus.RECOVERABLE
    assert session.captured_duration_ms == 8000
    assert session.paused_duration_ms == 2000
    assert session.wall_duration_ms == 10000
    second_stage = service.start_stage()
    assert second_stage.sequence == 2
    assert second_stage.stage_id != first_stage.stage_id


def test_sleep_gap_is_not_counted_as_captured_audio(tmp_path, schema_validator):
    service, session, clock = create_recording(tmp_path)
    clock.advance(120)
    clock.advance(11 * 60 * 60)
    service.add_audio_chunk(
        source="MIC",
        relative_path="audio/mic/chunk.wav",
        started_at_ms=0,
        ended_at_ms=(11 * 60 * 60 + 120) * 1000,
        sample_rate=16_000,
        channels=1,
        frames=1_920_000,
        sha256="a" * 64,
    )
    service.finish_stage()

    assert session.captured_duration_ms == 120_000
    assert session.paused_duration_ms == 11 * 60 * 60 * 1000
    chunk = next(
        event
        for event in service.repository.read_events(service.folder)
        if event["type"] == "audio_chunk_finalized"
    )
    assert chunk["payload"]["duration_ms"] == 120_000
    reconciled = next(
        event
        for event in service.repository.read_events(service.folder)
        if event["type"] == "capture_timing_reconciled"
    )
    schema_validator("timeline-event.schema.json").validate(reconciled)
    restarted = SessionService(tmp_path, clock=clock)
    restored = restarted.load(session.session_id)
    assert restored.captured_duration_ms == 120_000
    assert restored.paused_duration_ms == 11 * 60 * 60 * 1000


def test_invalid_transitions_and_payloads_fail_fast(tmp_path):
    service = SessionService(tmp_path, clock=FakeClock())
    service.create("meeting")
    with pytest.raises(InvalidSessionTransition):
        service.pause()
    service.start_stage()
    with pytest.raises(InvalidSessionTransition):
        service.start_stage()
    with pytest.raises(ValueError):
        service.add_transcript("UNKNOWN", "text", 0, 1)
    with pytest.raises(ValueError):
        service.add_transcript("MIC", "text", 2, 1)
    with pytest.raises(ValueError):
        service.add_transcript("MIC", "text", 0, 1, confidence=1.1)
    with pytest.raises(ValueError):
        service.add_transcript("MIC", "   ", 0, 1)
    with pytest.raises(ValueError):
        service.add_marker("unknown")
    with pytest.raises(ValueError):
        service.add_marker("question", at_ms=-1)


def test_name_is_normalized_and_bounded(tmp_path):
    service = SessionService(tmp_path, clock=FakeClock())
    service.create("idea")
    with pytest.raises(ValueError):
        service.name("   ")
    with pytest.raises(ValueError):
        service.name("x" * 201)
    service.name("  Una   idea   útil  ")
    assert service.session.title == "Una idea útil"


def test_finalize_requires_name_promotes_and_is_idempotent(tmp_path):
    service, session, clock = create_recording(tmp_path)
    service.add_transcript("SYS", "Bounded audio chunks preserve order.", 1000, 2500, "en", 0.9)
    service.add_marker("not_understood", "How is overlap reconciled?", 3000)
    with pytest.raises(ValueError):
        service.finalize()
    service.name("Streaming lesson")
    clock.advance(5)
    final_folder = service.finalize()
    assert final_folder.parent.parent.parent == tmp_path / "sessions"
    assert service.finalize() == final_folder
    assert session.status == SessionStatus.COMPLETED
    literal = (final_folder / "transcript.raw.md").read_text(encoding="utf-8")
    clean = (final_folder / "transcript.clean.md").read_text(encoding="utf-8")
    assert "# Streaming lesson" in literal
    assert "Bounded audio chunks preserve order." in literal
    assert "How is overlap reconciled?" in literal
    assert "Bounded audio chunks preserve order." in clean


def test_load_rebuilds_session_from_journal_not_stale_snapshot(tmp_path):
    service, session, clock = create_recording(tmp_path)
    stale = json.loads((service.folder / "session.json").read_text(encoding="utf-8"))
    clock.advance(4)
    service.pause()
    (service.folder / "session.json").write_text(json.dumps(stale), encoding="utf-8")
    loaded = SessionService(tmp_path, clock=clock)
    restored = loaded.load(session.session_id)
    assert restored.status == SessionStatus.PAUSED
    assert restored.captured_duration_ms == 4000


def test_recover_marks_interrupted_stage_and_can_continue(tmp_path):
    service, session, clock = create_recording(tmp_path)
    clock.advance(2)
    loaded = SessionService(tmp_path, clock=clock)
    loaded.load(session.session_id)
    assert loaded.recover()
    assert loaded.session.status == SessionStatus.RECOVERABLE
    assert loaded.session.stages[-1].status.value == "interrupted"
    assert loaded.start_stage().sequence == 2
    events = loaded.repository.read_events(loaded.folder)
    recovery = next(event for event in events if event["type"] == "recovery_detected")
    assert recovery["payload"]["previous_status"] == "recording"


def test_truncated_last_journal_record_is_ignored_but_middle_corruption_fails(tmp_path):
    service, _session, _clock = create_recording(tmp_path)
    timeline = service.folder / "timeline.jsonl"
    with timeline.open("a", encoding="utf-8") as handle:
        handle.write('{"schema_version":1')
    assert len(service.repository.read_events(service.folder)) == 2
    timeline.write_text("{broken}\n" + timeline.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(SessionJournalError):
        service.repository.read_events(service.folder)


def test_projection_rejects_path_escape_and_leaves_no_temp_files(tmp_path):
    repository = SessionRepository(tmp_path)
    folder = repository.create_folder("11111111-1111-4111-8111-111111111111")
    with pytest.raises(ValueError):
        repository.write_projection(folder, "../escape.md", "bad")
    output = repository.write_projection(folder, "exports/good.md", "good\n")
    assert output.read_text(encoding="utf-8") == "good\n"
    assert not list(folder.rglob("*.tmp"))


def test_repository_rejects_session_identifier_and_folder_escape(tmp_path):
    repository = SessionRepository(tmp_path)
    outside = tmp_path.with_name(f"{tmp_path.name}-outside-session")
    outside.mkdir(exist_ok=True)

    with pytest.raises(ValueError, match="identifier"):
        repository.create_folder("../outside")
    with pytest.raises(FileNotFoundError):
        repository.find_folder("../outside")
    with pytest.raises(ValueError, match="escapes"):
        repository.write_projection(outside, "projection.md", "bad")


def test_reducer_rejects_timeline_without_creation_event():
    with pytest.raises(ValueError):
        rebuild_session([])


def test_completed_session_left_in_inbox_is_promoted_on_retry(tmp_path):
    service, session, _clock = create_recording(tmp_path)
    service.name("Reunión de economía")
    service._finish_active_stage()
    session.status = SessionStatus.COMPLETED
    service._commit("session_completed", {})
    assert service.folder.parent == service.repository.inbox
    final_folder = service.finalize()
    assert final_folder.parent.parent.parent == tmp_path / "sessions"
    assert "reunion-de-economia" in final_folder.name


def test_generated_session_and_events_conform_to_contracts(tmp_path, schema_validator):
    service, _session, _clock = create_recording(tmp_path)
    service.add_transcript("MIC", "Una prueba contractual.", 100, 900, "es", 0.95, "audio/mic/segment.wav")
    service.add_marker("important", "Conservar este punto", 950)
    session_validator = schema_validator("session.schema.json")
    event_validator = schema_validator("timeline-event.schema.json")
    session_validator.validate(service.session.to_dict())
    for event in service.repository.read_events(service.folder):
        event_validator.validate(event)


def test_concurrent_event_commits_keep_unique_monotonic_sequences(tmp_path):
    service, _session, _clock = create_recording(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda index: service.add_source_health("mic", "active", f"packet {index}"), range(40)))
    events = service.repository.read_events(service.folder)
    sequences = [event["sequence"] for event in events]
    assert sequences == list(range(1, len(events) + 1))
