import hashlib
import json
import os
import wave
from uuid import uuid4

import numpy as np

from whisper_key.application import ProcessingService, RetentionService, SessionService
from whisper_key.domain.session import rebuild_session


def completed_audio_session(tmp_path, policy: str = "none", *, marker: bool = False):
    service = SessionService(tmp_path)
    service.create(
        "learning",
        retention={
            "audio": policy,
            "marker_context_before_ms": 30000,
            "marker_context_after_ms": 30000,
        },
    )
    service.start_stage()
    relative = f"audio/mic/{service.session.active_stage.stage_id}-0001.wav"
    path = service.folder / relative
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes((np.ones(16_000, dtype="<i2") * 900).tobytes())
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    service.add_audio_chunk(
        source="MIC",
        relative_path=relative,
        started_at_ms=0,
        ended_at_ms=1000,
        sample_rate=16_000,
        channels=1,
        frames=16_000,
        sha256=digest,
    )
    service.add_transcript("MIC", "Evidencia durable.", 0, 900, "es")
    if marker:
        service.add_marker("important", "Conservar contexto", at_ms=500)
    service.finish_stage()
    service.name("Retención segura")
    service.finalize()
    return service, service.folder / relative, digest


def test_session_created_retention_survives_journal_rebuild(tmp_path):
    service, _path, _digest = completed_audio_session(tmp_path, "marker_context")

    rebuilt = rebuild_session(service.repository.read_events(service.folder))

    assert rebuilt.retention == {
        "audio": "marker_context",
        "marker_context_before_ms": 30000,
        "marker_context_after_ms": 30000,
    }


def test_default_all_retention_never_selects_audio(tmp_path):
    service, path, _digest = completed_audio_session(tmp_path, "all")

    preview = RetentionService(tmp_path).preview(service)

    assert preview.candidates == ()
    assert preview.blocked_reason is None
    assert path.is_file()


def test_retention_preview_apply_and_restore_are_hash_checked_and_durable(tmp_path, schema_validator):
    service, path, digest = completed_audio_session(tmp_path, "none")
    retention = RetentionService(tmp_path)

    preview = retention.preview(service)
    relative = path.relative_to(service.folder).as_posix()
    assert [item.relative_path for item in preview.candidates] == [relative]
    assert preview.candidates[0].sha256 == digest
    result = retention.apply(service, preview.preview_id)

    assert result["moved"] == 1
    assert not path.exists()
    events = service.repository.read_events(service.folder)
    applied = next(event for event in events if event["type"] == "retention_applied")
    schema_validator("timeline-event.schema.json").validate(applied)
    trash_file = tmp_path / applied["payload"]["trash_key"] / relative
    assert trash_file.is_file()
    assert retention.preview(service).restorable

    restored = retention.restore_latest(service)

    assert restored["restored"] == 1
    assert path.is_file()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    restored_event = service.repository.read_events(service.folder)[-1]
    assert restored_event["type"] == "retention_restored"
    schema_validator("timeline-event.schema.json").validate(restored_event)
    assert not retention.preview(service).restorable


def test_retention_refuses_audio_modified_after_journal_commit(tmp_path):
    service, path, _digest = completed_audio_session(tmp_path, "none")
    path.write_bytes(path.read_bytes() + b"changed")

    preview = RetentionService(tmp_path).preview(service)

    assert preview.candidates == ()
    assert "cambió" in preview.blocked_reason
    assert path.is_file()


def test_until_verified_and_marker_context_require_their_safety_gates(tmp_path):
    until_verified, _path, _digest = completed_audio_session(tmp_path / "verified", "until_verified")
    verified_retention = RetentionService(tmp_path / "verified")
    blocked = verified_retention.preview(until_verified)
    assert blocked.requires_verification
    assert blocked.blocked_reason
    assert verified_retention.preview(until_verified, verified=True).candidates

    marker_session, _path, _digest = completed_audio_session(
        tmp_path / "marker",
        "marker_context",
        marker=True,
    )
    marker_retention = RetentionService(tmp_path / "marker")
    assert marker_retention.preview(marker_session).blocked_reason
    ProcessingService(tmp_path / "marker" / "models").run_job(marker_session, "marker_context")
    assert marker_retention.preview(marker_session).candidates
    excerpt = next((marker_session.folder / "audio" / "excerpts").glob("*.wav"))
    excerpt.write_bytes(excerpt.read_bytes() + b"changed")
    assert marker_retention.preview(marker_session).blocked_reason


def test_retention_treats_journal_commit_as_authoritative_if_snapshot_write_fails(tmp_path, monkeypatch):
    service, path, _digest = completed_audio_session(tmp_path, "none")
    retention = RetentionService(tmp_path)
    preview = retention.preview(service)
    original_save = service.repository.save_session

    monkeypatch.setattr(
        service.repository,
        "save_session",
        lambda _folder, _session: (_ for _ in ()).throw(OSError("snapshot failed")),
    )
    result = retention.apply(service, preview.preview_id)

    assert result["moved"] == 1
    assert not path.exists()
    assert any(event["type"] == "retention_applied" for event in service.repository.read_events(service.folder))
    monkeypatch.setattr(service.repository, "save_session", original_save)
    assert retention.restore_latest(service)["restored"] == 1


def test_startup_recovery_rolls_back_interrupted_precommit_move(tmp_path):
    service, path, digest = completed_audio_session(tmp_path, "none")
    retention = RetentionService(tmp_path)
    application_id = str(uuid4())
    relative = path.relative_to(service.folder).as_posix()
    trash_folder = retention.trash_root / service.session.session_id / application_id
    trash_file = trash_folder / relative
    trash_file.parent.mkdir(parents=True)
    os.replace(path, trash_file)
    manifest = {
        "schema_version": 1,
        "state": "moving",
        "application_id": application_id,
        "session_id": service.session.session_id,
        "session_folder": service.folder.relative_to(tmp_path).as_posix(),
        "policy": "none",
        "preview_id": "0" * 64,
        "items": [
            {
                "relative_path": relative,
                "bytes": trash_file.stat().st_size,
                "sha256": digest,
            }
        ],
    }
    (trash_folder / "retention.json").write_text(json.dumps(manifest), encoding="utf-8")

    recovered = retention.recover_incomplete()

    assert recovered == [f"rolled_back:{application_id}"]
    assert path.is_file()
    saved = json.loads((trash_folder / "retention.json").read_text(encoding="utf-8"))
    assert saved["state"] == "rolled_back"
