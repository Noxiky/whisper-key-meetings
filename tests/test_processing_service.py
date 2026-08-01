import hashlib
import json
import wave

import numpy as np
import pytest

from whisper_key.application import HandoffVerificationError, ProcessingService, SessionService
from whisper_key.domain import DiarizationSegment
from whisper_key.domain.session import rebuild_session


def completed_session(tmp_path):
    service = SessionService(tmp_path)
    service.create("learning")
    service.start_stage()
    first = service.add_transcript("SYS", "  La   consistencia eventual converge.  ", 0, 1000, "es")
    second = service.add_transcript("MIC", "¿Qué observa el usuario?", 1100, 1800, "es")
    service.add_marker("question", "Revisar garantía", at_ms=1200)
    service.finish_stage()
    service.name("Clase portable")
    service.finalize()
    return service, first, second


def test_processing_jobs_are_offline_traceable_and_independently_visible(tmp_path):
    service, first, second = completed_session(tmp_path)
    updates = []
    processing = ProcessingService(tmp_path / "models")

    processing.run_all(service, lambda job, status, detail: updates.append((job, status, detail)))

    clean = (service.folder / "transcript.clean.md").read_text(encoding="utf-8")
    assert f"raw-segment:{first}" in clean
    assert f"raw-segment:{second}" in clean
    assert "La consistencia eventual converge." in clean
    assert (service.folder / "markers.md").exists()
    assert (service.folder / "exports" / "learning.md").exists()
    assert (service.folder / "exports" / "session.html").exists()
    assert (service.folder / "handoff.md").exists()
    assert (service.folder / "speakers.json").exists()
    assert all(str(tmp_path) not in path.read_text(encoding="utf-8") for path in service.folder.rglob("*.md"))
    states = {(job, status) for job, status, _detail in updates}
    assert ("clean", "complete") in states
    assert ("diarization", "skipped") in states


def test_post_processing_events_do_not_reopen_completed_lifecycle(tmp_path):
    service, _first, _second = completed_session(tmp_path)
    ProcessingService(tmp_path / "models").run_job(service, "clean")

    events = service.repository.read_events(service.folder)
    rebuilt = rebuild_session(events)

    assert rebuilt.status.value == "completed"
    job_events = [event for event in events if event["type"] == "processing_job"]
    assert [event["payload"]["status"] for event in job_events] == ["processing", "complete"]
    assert job_events[-1]["payload"]["duration_ms"] >= 0
    assert service.session.processing_duration_ms >= 0


def test_processing_attempts_continue_across_service_restart(tmp_path):
    service, _first, _second = completed_session(tmp_path)
    ProcessingService(tmp_path / "models").run_job(service, "clean")
    ProcessingService(tmp_path / "models").run_job(service, "clean")

    attempts = [
        event["payload"]["attempt"]
        for event in service.repository.read_events(service.folder)
        if event["type"] == "processing_job" and event["payload"]["job"] == "clean"
    ]
    assert attempts == [1, 1, 2, 2]


def test_pending_jobs_resume_after_interrupted_attempt(tmp_path):
    service, _first, _second = completed_session(tmp_path)
    first = ProcessingService(tmp_path / "models")
    first.run_job(service, "clean")
    service.record_processing_job("markers", "processing", 1)

    restarted = ProcessingService(tmp_path / "models")
    pending = restarted.pending_jobs(service, jobs=("clean", "markers", "mode"))
    resumed = restarted.run_pending(service, jobs=("clean", "markers", "mode"))

    assert pending == ("markers", "mode")
    assert resumed == pending
    assert (service.folder / "markers.md").is_file()
    assert (service.folder / "exports" / "learning.md").is_file()


def test_queued_jobs_keep_their_attempt_and_completed_jobs_are_not_repeated(tmp_path):
    service, _first, _second = completed_session(tmp_path)
    processing = ProcessingService(tmp_path / "models")

    assert processing.queue_jobs(service, jobs=("clean", "markers")) == ("clean", "markers")
    processing.run_job(service, "clean")
    assert processing.queue_jobs(service, jobs=("clean", "markers")) == ()

    events = [
        event["payload"]
        for event in service.repository.read_events(service.folder)
        if event.get("type") == "processing_job" and event["payload"].get("job") == "clean"
    ]
    assert [(event["status"], event["attempt"]) for event in events] == [
        ("queued", 1),
        ("processing", 1),
        ("complete", 1),
    ]


def test_integrity_manifest_contains_only_relative_evidence_paths(tmp_path):
    service, _first, _second = completed_session(tmp_path)
    ProcessingService(tmp_path / "models").run_job(service, "integrity")

    manifest = json.loads((service.folder / "integrity.json").read_text(encoding="utf-8"))

    assert manifest["files"]
    assert all(not item["path"].startswith(("/", "C:")) for item in manifest["files"])
    assert all(item["path"] not in {"session.json", "timeline.jsonl"} for item in manifest["files"])


def test_installed_diarization_processes_aligned_mic_audio(tmp_path):
    service = SessionService(tmp_path)
    service.create("meeting")
    service.start_stage()
    wave_path = service.folder / "audio" / "mic" / "chunk.wav"
    with wave.open(str(wave_path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes((np.ones(16_000, dtype="<i2") * 1000).tobytes())
    digest = hashlib.sha256(wave_path.read_bytes()).hexdigest()
    service.add_audio_chunk(
        source="MIC",
        relative_path="audio/mic/chunk.wav",
        started_at_ms=4_000,
        ended_at_ms=5_000,
        sample_rate=16_000,
        channels=1,
        frames=16_000,
        sha256=digest,
    )
    segment_id = service.add_transcript("MIC", "Una voz", 4_100, 4_900, "es")
    service.finish_stage()
    service.name("Prueba de voces")
    service.finalize()

    class FakeAdapter:
        available = True

        def process_files(self, chunks):
            assert chunks[0].started_at_ms == 4_000
            return [DiarizationSegment("speaker_1", 4_000, 5_000)]

    processing = ProcessingService(tmp_path / "models", enable_automatic_diarization=True)
    processing.diarization_adapter = FakeAdapter()
    updates = []
    processing.run_job(service, "diarization", lambda job, status, detail: updates.append((job, status)))

    revision = json.loads((service.folder / "speakers.json").read_text(encoding="utf-8"))
    assert updates[-1] == ("diarization", "complete")
    assert revision["assignments"] == [
        {
            "segment_id": segment_id,
            "speaker_id": "speaker_1",
            "confidence": None,
            "method": "diarization",
        }
    ]


def test_automatic_diarization_is_disabled_by_default_for_stability(tmp_path):
    service, _first, _second = completed_session(tmp_path)

    class MustNotRunAdapter:
        available = True

        def process_files(self, _chunks):
            raise AssertionError("native diarization must remain opt-in")

    processing = ProcessingService(tmp_path / "models")
    processing.diarization_adapter = MustNotRunAdapter()

    status, detail = processing.run_job(service, "diarization")

    assert status == "skipped"
    assert "desactivada por estabilidad" in detail
    assert (service.folder / "speakers.json").is_file()


def test_diarization_failure_leaves_source_fallback_file(tmp_path):
    service, _first, _second = completed_session(tmp_path)

    class FailingAdapter:
        available = True

        def process_files(self, _chunks):
            raise RuntimeError("modelo incompatible")

    # An aligned chunk is enough to enter the real adapter path.
    events = service.repository.read_events(service.folder)
    audio_event = {
        "schema_version": 1,
        "event_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "session_id": service.session.session_id,
        "sequence": len(events) + 1,
        "occurred_at": events[-1]["occurred_at"],
        "session_offset_ms": events[-1]["session_offset_ms"],
        "producer_version": "0.9.0",
        "type": "audio_chunk_finalized",
        "payload": {
            "source": "MIC",
            "relative_path": "audio/mic/failure.wav",
            "started_at_ms": 0,
            "ended_at_ms": 1,
            "duration_ms": 1,
            "sample_rate": 16_000,
            "channels": 1,
            "frames": 1,
            "sha256": "0" * 64,
        },
    }
    (service.folder / "audio" / "mic" / "failure.wav").write_bytes(b"not read by fake")
    service.repository.append_event(service.folder, audio_event)
    processing = ProcessingService(tmp_path / "models", enable_automatic_diarization=True)
    processing.diarization_adapter = FailingAdapter()
    updates = []
    processing.run_job(service, "diarization", lambda job, status, detail: updates.append((job, status)))

    revision = json.loads((service.folder / "speakers.json").read_text(encoding="utf-8"))
    assert updates[-1] == ("diarization", "failed")
    assert any(item["speaker_id"] == "MIC" for item in revision["assignments"])


def test_long_session_skips_memory_hungry_automatic_diarization(tmp_path):
    service, _first, _second = completed_session(tmp_path)
    events = service.repository.read_events(service.folder)
    audio_event = {
        "schema_version": 1,
        "event_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "session_id": service.session.session_id,
        "sequence": len(events) + 1,
        "occurred_at": events[-1]["occurred_at"],
        "session_offset_ms": events[-1]["session_offset_ms"],
        "producer_version": "0.9.0",
        "type": "audio_chunk_finalized",
        "payload": {
            "source": "MIC",
            "relative_path": "audio/mic/long.wav",
            "started_at_ms": 0,
            "ended_at_ms": ProcessingService.AUTO_DIARIZATION_MAX_MS + 1,
            "duration_ms": ProcessingService.AUTO_DIARIZATION_MAX_MS + 1,
            "sample_rate": 16_000,
            "channels": 1,
            "frames": 1,
            "sha256": "0" * 64,
        },
    }
    service.repository.append_event(service.folder, audio_event)

    class MustNotRunAdapter:
        available = True

        def process_files(self, _chunks):
            raise AssertionError("long automatic diarization must be skipped")

    processing = ProcessingService(tmp_path / "models", enable_automatic_diarization=True)
    processing.diarization_adapter = MustNotRunAdapter()
    status, detail = processing.run_job(service, "diarization")

    assert status == "skipped"
    assert "omitida automáticamente" in detail
    assert (service.folder / "speakers.json").is_file()


def test_clean_and_html_use_editable_speaker_names(tmp_path):
    service, _first, second = completed_session(tmp_path)
    revision = {
        "schema_version": 1,
        "revision": 2,
        "speakers": [{"speaker_id": "speaker_1", "display_name": "Profesora Ana"}],
        "assignments": [
            {
                "segment_id": second,
                "speaker_id": "speaker_1",
                "confidence": 0.9,
                "method": "diarization",
            }
        ],
    }
    service.repository.write_projection(service.folder, "speakers.json", json.dumps(revision))
    processing = ProcessingService(tmp_path / "models")

    processing.run_job(service, "clean")
    processing.run_job(service, "html")

    assert "Profesora Ana" in (service.folder / "transcript.clean.md").read_text(encoding="utf-8")
    assert "Profesora Ana" in (service.folder / "exports" / "session.html").read_text(encoding="utf-8")


def test_handoff_freezes_verified_relative_inputs_and_preferred_workflow(tmp_path, schema_validator):
    service, _first, _second = completed_session(tmp_path)
    processing = ProcessingService(tmp_path / "models")

    processing.run_all(service)

    manifest = json.loads((service.folder / "handoff" / "handoff.json").read_text(encoding="utf-8"))
    schema_validator("handoff.schema.json").validate(manifest)
    assert [step["processor"] for step in manifest["workflow"]] == [
        "nox-learn-anything",
        "nox-html-learning",
    ]
    assert manifest["privacy"] == {
        "uploaded_automatically": False,
        "requires_explicit_user_action": True,
        "paid_api_required": False,
    }
    assert {item["role"] for item in manifest["inputs"]} >= {
        "literal",
        "clean",
        "session_snapshot",
        "timeline_snapshot",
    }
    assert all(not item["path"].startswith(("/", "C:")) for item in manifest["inputs"])
    assert processing.handoff.verify(service)["files"] == len(manifest["inputs"])
    instructions = (service.folder / "handoff.md").read_text(encoding="utf-8")
    assert "no sube ni envía automáticamente" in instructions
    assert "exports/downstream/learning.md" in instructions


def test_handoff_verification_detects_changed_source_without_reopening_session(tmp_path):
    service, _first, _second = completed_session(tmp_path)
    processing = ProcessingService(tmp_path / "models")
    processing.run_all(service)
    raw_path = service.folder / "transcript.raw.md"
    raw_path.write_text(raw_path.read_text(encoding="utf-8") + "alterado\n", encoding="utf-8")
    updates = []

    processing.run_job(
        service,
        "handoff_verify",
        lambda job, status, detail: updates.append((job, status, detail)),
    )

    assert updates[-1][0:2] == ("handoff_verify", "failed")
    assert "SHA-256 changed" in updates[-1][2] or "Size changed" in updates[-1][2]
    assert service.session.status.value == "completed"


def test_handoff_rejects_absolute_or_traversing_manifest_paths(tmp_path):
    service, _first, _second = completed_session(tmp_path)
    processing = ProcessingService(tmp_path / "models")
    processing.run_all(service)
    manifest_path = service.folder / "handoff" / "handoff.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["inputs"][0]["path"] = "C:/private/source.md"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(HandoffVerificationError, match="Unsafe"):
        processing.handoff.verify(service)


def test_manual_clean_revision_is_not_overwritten_and_feeds_mode_export(tmp_path):
    service, _first, _second = completed_session(tmp_path)
    processing = ProcessingService(tmp_path / "models")
    processing.run_all(service)
    manual = "# Clase portable · Clean\n\n## Transcript\n\nMi revisión manual intacta.\n"
    service.repository.write_projection(service.folder, "transcript.clean.md", manual)
    service.repository.write_projection(
        service.folder,
        "clean-revisions.json",
        json.dumps(
            {
                "schema_version": 1,
                "active_revision": 1,
                "revisions": [
                    {
                        "revision": 1,
                        "kind": "manual",
                        "relative_path": "revisions/clean-v1.md",
                        "sha256": hashlib.sha256(manual.encode("utf-8")).hexdigest(),
                    }
                ],
            }
        ),
    )
    updates = []

    processing.run_job(service, "clean", lambda job, status, detail: updates.append((job, status, detail)))
    processing.run_job(service, "mode")

    assert updates[-1][0:2] == ("clean", "skipped")
    assert (service.folder / "transcript.clean.md").read_text(encoding="utf-8") == manual
    assert "Mi revisión manual intacta." in (service.folder / "exports" / "learning.md").read_text(encoding="utf-8")


def test_downstream_outputs_do_not_change_protected_session_evidence(tmp_path):
    service, _first, _second = completed_session(tmp_path)
    processing = ProcessingService(tmp_path / "models")
    processing.run_all(service)
    raw_path = service.folder / "transcript.raw.md"
    before = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    downstream = service.folder / "exports" / "downstream"
    downstream.mkdir(parents=True)
    (downstream / "learning.md").write_text("derived", encoding="utf-8")

    report = processing.handoff.verify(service)

    assert report["files"] > 0
    assert hashlib.sha256(raw_path.read_bytes()).hexdigest() == before
