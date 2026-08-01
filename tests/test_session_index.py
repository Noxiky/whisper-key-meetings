import json

from whisper_key.infrastructure import SessionIndex

SESSION_ONE = "11111111-1111-4111-8111-111111111111"
SESSION_VALID = "22222222-2222-4222-8222-222222222222"
SESSION_PERCENT = "33333333-3333-4333-8333-333333333333"
SESSION_PLAIN = "44444444-4444-4444-8444-444444444444"


def write_session(root, folder_name, *, session_id, title, mode="learning", status="completed", transcript=""):
    folder = root / "sessions" / "2026" / "07" / folder_name
    folder.mkdir(parents=True)
    value = {
        "session_id": session_id,
        "title": title,
        "mode": mode,
        "status": status,
        "created_at": "2026-07-17T10:00:00Z",
        "updated_at": "2026-07-17T11:00:00Z",
        "captured_duration_ms": 90_000,
    }
    (folder / "session.json").write_text(json.dumps(value), encoding="utf-8")
    if transcript:
        (folder / "transcript.raw.md").write_text(transcript, encoding="utf-8")
    return folder


def test_index_rebuilds_from_folders_and_searches_transcript(tmp_path):
    folder = write_session(
        tmp_path,
        "distributed-systems",
        session_id=SESSION_ONE,
        title="Clase de arquitectura",
        transcript="La consistencia eventual converge.",
    )
    index = SessionIndex(tmp_path)

    assert index.rebuild() == 1
    result = index.search("eventual")

    assert result[0]["session_id"] == SESSION_ONE
    assert result[0]["folder"] == str(folder)


def test_index_is_rebuildable_and_ignores_corrupt_metadata(tmp_path):
    write_session(tmp_path, "valid", session_id=SESSION_VALID, title="Válida")
    corrupt = tmp_path / "inbox" / "broken"
    corrupt.mkdir(parents=True)
    (corrupt / "session.json").write_text("{", encoding="utf-8")
    index = SessionIndex(tmp_path)
    index.rebuild()
    index.path.unlink()

    assert [item["session_id"] for item in index.search()] == [SESSION_VALID]


def test_search_escapes_sql_wildcards(tmp_path):
    write_session(tmp_path, "percent", session_id=SESSION_PERCENT, title="100% local")
    write_session(tmp_path, "plain", session_id=SESSION_PLAIN, title="1000 local")
    index = SessionIndex(tmp_path)
    index.rebuild()

    assert [item["session_id"] for item in index.search("100%")] == [SESSION_PERCENT]


def test_index_ignores_unsafe_or_oversized_imported_metadata(tmp_path):
    write_session(tmp_path, "unsafe-id", session_id="../outside", title="No importar")
    oversized = tmp_path / "sessions" / "2026" / "07" / "oversized"
    oversized.mkdir(parents=True)
    (oversized / "session.json").write_text(
        '{"session_id":"55555555-5555-4555-8555-555555555555","mode":"learning",'
        '"status":"completed","title":"' + ("x" * (2 * 1024 * 1024)) + '"}',
        encoding="utf-8",
    )

    index = SessionIndex(tmp_path)

    assert index.rebuild() == 0
