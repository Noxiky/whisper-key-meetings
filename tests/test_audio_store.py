import time
import wave

import numpy as np

from whisper_key.infrastructure import DurableAudioStore


def read_wave(path):
    with wave.open(str(path), "rb") as reader:
        return reader.getnchannels(), reader.getframerate(), reader.getnframes()


def test_audio_store_writes_and_rotates_durable_wave_files(tmp_path):
    finalized = []
    store = DurableAudioStore(
        rotate_seconds=1,
        sync_every_packets=1,
        on_file_finalized=lambda source, path: finalized.append((source, path)),
    )
    store.start(tmp_path, "stage-1")
    packet = np.full(8000, 0.25, dtype=np.float32)
    assert store.submit("mic", packet, 16000)
    assert store.submit("mic", packet, 16000)
    assert store.submit("mic", packet, 16000)
    store.stop()
    waves = sorted((tmp_path / "audio" / "mic").glob("*.wav"))
    assert len(waves) == 2
    assert read_wave(waves[0]) == (1, 16000, 16000)
    assert read_wave(waves[1]) == (1, 16000, 8000)
    assert [source for source, _path in finalized] == ["mic", "mic"]
    assert not list(tmp_path.rglob("*.part"))


def test_audio_store_preserves_stereo_shape(tmp_path):
    store = DurableAudioStore(sync_every_packets=1)
    store.start(tmp_path, "stage-2")
    assert store.submit("system", np.full((4800, 2), 0.1, dtype=np.float32), 48000)
    store.stop()
    wave_path = next((tmp_path / "audio" / "sys").glob("*.wav"))
    assert read_wave(wave_path) == (2, 48000, 4800)


def test_audio_store_rejects_submit_when_not_running(tmp_path):
    store = DurableAudioStore()
    assert not store.submit("mic", np.ones(10, dtype=np.float32), 16000)


def test_audio_store_rejects_unsafe_source_path(tmp_path):
    store = DurableAudioStore()
    store.start(tmp_path, "stage-safe")
    assert not store.submit("../escape", np.ones(10, dtype=np.float32), 16000)
    store.stop()


def test_valid_partial_wave_is_recovered(tmp_path):
    store = DurableAudioStore(sync_every_packets=1)
    store.start(tmp_path, "stage-3")
    assert store.submit("mic", np.ones(1600, dtype=np.float32), 16000)
    store.stop()
    wave_path = next((tmp_path / "audio" / "mic").glob("*.wav"))
    part_path = wave_path.with_suffix(".wav.part")
    wave_path.replace(part_path)
    assert store.recover_partials(tmp_path) == [wave_path]
    assert wave_path.exists()


def test_restart_continues_file_index_without_overwrite(tmp_path):
    packet = np.ones(1600, dtype=np.float32)
    first = DurableAudioStore(sync_every_packets=1)
    first.start(tmp_path, "same-stage")
    assert first.submit("mic", packet, 16000)
    first.stop()
    second = DurableAudioStore(sync_every_packets=1)
    second.start(tmp_path, "same-stage")
    assert second.submit("mic", packet, 16000)
    second.stop()
    paths = sorted((tmp_path / "audio" / "mic").glob("*.wav"))
    assert [path.name for path in paths] == ["same-stage-0001.wav", "same-stage-0002.wav"]


def test_two_hour_synthetic_stream_keeps_bounded_backlog(tmp_path):
    store = DurableAudioStore(max_queue_packets=32, rotate_seconds=1800, sync_every_packets=1000)
    store.start(tmp_path, "long-stage")
    packet = np.ones(1, dtype=np.float32)
    max_backlog = 0
    for _second in range(7200):
        while not store.submit("mic", packet, 1):
            time.sleep(0.001)
        max_backlog = max(max_backlog, store.backlog)
    store.stop()
    assert max_backlog <= 32
    assert len(list((tmp_path / "audio" / "mic").glob("*.wav"))) == 4
