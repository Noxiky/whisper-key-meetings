from whisper_key.application import DiarizationService
from whisper_key.domain import DiarizationSegment
from whisper_key.infrastructure import SherpaDiarizationAdapter


def transcript(segment_id, source, start, end):
    return {
        "type": "transcript_final",
        "payload": {
            "segment_id": segment_id,
            "source": source,
            "started_at_ms": start,
            "ended_at_ms": end,
        },
    }


def test_missing_optional_models_are_visible(tmp_path):
    adapter = SherpaDiarizationAdapter(tmp_path)

    assert not adapter.available
    assert adapter.missing_models == ["speaker segmentation", "speaker embedding"]


def test_runtime_validation_loads_models_and_reports_sample_rate(tmp_path, monkeypatch):
    adapter = SherpaDiarizationAdapter(tmp_path)
    adapter.segmentation_model.write_bytes(b"segmentation")
    adapter.embedding_model.write_bytes(b"embedding")

    class Runtime:
        sample_rate = 16_000

    monkeypatch.setattr(adapter, "_create_diarizer", lambda speakers, threshold: Runtime())

    assert adapter.validate_runtime() == 16_000


def test_diarization_assigns_anonymous_speakers_and_preserves_source_fallback():
    events = [
        transcript("11111111-1111-4111-8111-111111111111", "MIC", 0, 1000),
        transcript("22222222-2222-4222-8222-222222222222", "MIC", 1100, 2000),
        transcript("33333333-3333-4333-8333-333333333333", "SYS", 0, 2000),
    ]
    turns = [DiarizationSegment("speaker_1", 0, 900), DiarizationSegment("speaker_2", 1000, 2100)]
    service = DiarizationService()

    assignments = service.assign(events, turns)
    revision = service.build_revision(assignments, {"speaker_2": "Facilitadora"})

    assert [item.speaker_id for item in assignments] == ["speaker_1", "speaker_2", "SYS"]
    assert assignments[2].method == "source"
    assert assignments[0].confidence is None
    names = {speaker["speaker_id"]: speaker["display_name"] for speaker in revision["speakers"]}
    assert names["speaker_2"] == "Facilitadora"


def test_empty_or_failed_diarization_never_erases_mic_sys_labels():
    events = [
        transcript("11111111-1111-4111-8111-111111111111", "MIC", 0, 1000),
        transcript("22222222-2222-4222-8222-222222222222", "SYS", 0, 1000),
    ]

    assignments = DiarizationService().assign(events, diarization=[])

    assert [(item.speaker_id, item.method) for item in assignments] == [
        ("MIC", "source_fallback"),
        ("SYS", "source"),
    ]


def test_compressed_audio_turns_map_across_pause_gaps():
    turns = [DiarizationSegment("speaker_1", 500, 1_500)]
    spans = [
        (0, 1_000, 0, 1_000),
        (1_000, 2_000, 61_000, 62_000),
    ]

    mapped = SherpaDiarizationAdapter.map_to_session_timeline(turns, spans)

    assert [(item.started_at_ms, item.ended_at_ms) for item in mapped] == [
        (500, 1_000),
        (61_000, 61_500),
    ]


def test_speaker_name_revision_preserves_assignments_and_history_number():
    service = DiarizationService()
    current = service.build_revision(
        service.assign(
            [transcript("11111111-1111-4111-8111-111111111111", "MIC", 0, 1000)],
            [DiarizationSegment("speaker_1", 0, 1000)],
        )
    )

    revised = service.revise_names(current, {"speaker_1": "  Ana   Pérez  "})

    assert revised["revision"] == 2
    assert revised["speakers"][0]["display_name"] == "Ana Pérez"
    assert revised["assignments"] == current["assignments"]
