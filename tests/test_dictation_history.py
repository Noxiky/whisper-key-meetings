import wave

import numpy as np

from whisper_key.infrastructure import DictationHistoryStore


def test_dictation_history_preserves_text_delivery_and_audio_across_reloads(tmp_path):
    store = DictationHistoryStore(tmp_path / "dictations")
    audio = np.linspace(-0.25, 0.25, 1600, dtype=np.float32)

    first = store.append(
        text="primera idea",
        audio=audio,
        sample_rate=16_000,
        delivery="clipboard",
        transcription={"detected_language": "es", "real_time_factor": 0.4},
    )
    second = store.append(
        text="segunda idea",
        audio=audio[:800],
        sample_rate=16_000,
        delivery="pasted",
    )

    reloaded = DictationHistoryStore(tmp_path / "dictations").list_entries()

    assert [entry["dictation_id"] for entry in reloaded] == [second["dictation_id"], first["dictation_id"]]
    assert reloaded[0]["text"] == "segunda idea"
    assert first["schema_version"] == 2
    assert reloaded[1]["transcription"]["detected_language"] == "es"
    audio_path = tmp_path / "dictations" / first["audio_path"]
    with wave.open(str(audio_path), "rb") as reader:
        assert reader.getframerate() == 16_000
        assert reader.getnframes() == 1600


def test_dictation_history_tolerates_a_truncated_last_record(tmp_path):
    store = DictationHistoryStore(tmp_path / "dictations")
    store.append(text="seguro", audio=None, sample_rate=16_000, delivery="clipboard")
    with store.timeline.open("a", encoding="utf-8") as handle:
        handle.write('{"dictation_id":')

    assert [entry["text"] for entry in store.list_entries()] == ["seguro"]
