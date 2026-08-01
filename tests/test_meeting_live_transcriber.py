import time

import numpy as np
from fakes import FakeWhisperEngine

from whisper_key.meeting_live_transcriber import MeetingLiveTranscriber, _is_hallucination


def test_hallucination_filter_keeps_real_speech():
    assert _is_hallucination("Thanks for watching")
    assert _is_hallucination("...")
    assert not _is_hallucination("Thanks for reviewing the proposal")


def test_stereo_resampling_produces_16khz_mono():
    transcriber = MeetingLiveTranscriber(FakeWhisperEngine())
    stereo = np.full((4800, 2), 0.1, dtype=np.float32)
    normalized = transcriber._normalize_audio(stereo, 48000)
    assert normalized.ndim == 1
    assert 1590 <= len(normalized) <= 1610


def test_pause_flushes_source_as_one_segment():
    transcriber = MeetingLiveTranscriber(FakeWhisperEngine())
    transcriber.register_source("mic", "MIC")
    speech = np.full(8000, 0.05, dtype=np.float32)
    silence = np.zeros(12800, dtype=np.float32)
    transcriber.push_audio("mic", speech, 16000)
    transcriber.push_audio("mic", silence, 16000)
    job = transcriber._queue.get_nowait()
    assert job.label == "MIC"
    assert job.source_id == "mic"
    assert len(job.audio) == 20800
    assert job.started_at_ms == 0
    assert job.ended_at_ms == 1300


def test_stop_flushes_short_final_audio_and_transcribes():
    engine = FakeWhisperEngine(["final words"])
    transcriber = MeetingLiveTranscriber(engine)
    transcriber.register_source("mic", "MIC")
    transcriber.start(active_sources=["mic"])
    transcriber.push_audio("mic", np.full(8000, 0.05, dtype=np.float32), 16000)
    transcriber.stop()
    assert len(engine.model.calls) == 1


def test_transcript_callback_includes_source_time_and_language():
    results = []
    engine = FakeWhisperEngine(["texto final"])
    transcriber = MeetingLiveTranscriber(engine, on_transcript=results.append)
    transcriber.register_source("mic", "MIC")
    transcriber.start(active_sources=["mic"])
    transcriber.push_audio("mic", np.full(8000, 0.05, dtype=np.float32), 16000)
    transcriber.stop()
    assert results[0].source == "MIC"
    assert results[0].text == "texto final"
    assert results[0].started_at_ms == 0
    assert results[0].ended_at_ms == 500
    assert results[0].language == "es"


def test_bounded_queue_reports_backpressure_without_blocking_capture():
    rejected = []
    transcriber = MeetingLiveTranscriber(FakeWhisperEngine(), max_queue_segments=1, on_backpressure=rejected.append)
    transcriber.register_source("mic", "MIC")
    speech = np.full(8000, 0.05, dtype=np.float32)
    silence = np.zeros(12800, dtype=np.float32)
    assert transcriber.push_audio("mic", speech, 16000)
    assert transcriber.push_audio("mic", silence, 16000)
    assert transcriber.push_audio("mic", speech, 16000)
    assert not transcriber.push_audio("mic", silence, 16000)
    assert rejected[0].source_id == "mic"


def test_stop_drains_segments_in_capture_order():
    results = []
    engine = FakeWhisperEngine(["uno", "dos", "tres"])
    transcriber = MeetingLiveTranscriber(engine, on_transcript=results.append)
    transcriber.register_source("mic", "MIC")
    transcriber.start(active_sources=["mic"])
    speech = np.full(8000, 0.05, dtype=np.float32)
    silence = np.zeros(12800, dtype=np.float32)
    for _index in range(3):
        transcriber.push_audio("mic", speech, 16000)
        transcriber.push_audio("mic", silence, 16000)
    transcriber.stop()
    assert [result.text for result in results] == ["uno", "dos", "tres"]
    assert [result.started_at_ms for result in results] == sorted(result.started_at_ms for result in results)


def test_callback_failure_marks_segment_for_replay_and_worker_survives():
    replay = []

    def fail(_result):
        raise OSError("journal unavailable")

    transcriber = MeetingLiveTranscriber(
        FakeWhisperEngine(["uno", "dos"]),
        on_transcript=fail,
        on_backpressure=replay.append,
    )
    transcriber.register_source("mic", "MIC")
    transcriber.start(active_sources=["mic"])
    speech = np.full(8000, 0.05, dtype=np.float32)
    silence = np.zeros(12800, dtype=np.float32)
    for _index in range(2):
        transcriber.push_audio("mic", speech, 16000)
        transcriber.push_audio("mic", silence, 16000)
    transcriber.stop()
    assert len(replay) == 2


def test_silence_timeout_fires_once_for_active_sources():
    fired = []
    transcriber = MeetingLiveTranscriber(FakeWhisperEngine(), auto_stop_silence_seconds=0.01)
    transcriber.register_source("mic", "MIC")
    transcriber.set_silence_timeout_callback(fired.append)
    transcriber.start(active_sources=["mic"])
    time.sleep(0.03)
    transcriber._check_silence_timeout()
    transcriber._check_silence_timeout()
    transcriber.stop()
    assert fired == [0.01]


def test_live_transcriber_forces_language_preserving_transcribe_task():
    engine = FakeWhisperEngine(["привет mundo hello"])
    transcriber = MeetingLiveTranscriber(engine)

    text, language = transcriber._transcribe(np.ones(16000, dtype=np.float32))

    assert text == "привет mundo hello"
    assert language == "es"
    assert engine.model.calls[0][1]["task"] == "transcribe"
    assert engine.model.calls[0][1]["multilingual"] is True
    assert engine.model.calls[0][1]["language_detection_segments"] == 3
    assert engine.model.calls[0][1]["vad_filter"] is False


def test_inference_failure_marks_retained_segment_for_replay_without_killing_worker():
    engine = FakeWhisperEngine()

    def fail(_audio, **_kwargs):
        raise FileNotFoundError("silero_vad_v6.onnx")

    engine.model.transcribe = fail
    replay = []
    transcriber = MeetingLiveTranscriber(engine, on_backpressure=replay.append)
    transcriber.register_source("mic", "MIC")
    transcriber.start(active_sources=["mic"])
    transcriber.push_audio("mic", np.full(8000, 0.05, dtype=np.float32), 16000)
    transcriber.stop()

    assert len(replay) == 1
    assert replay[0].source_id == "mic"


def test_long_utterance_emits_provisional_then_final_without_replacing_evidence():
    engine = FakeWhisperEngine(["borrador visible", "texto final"])
    provisional = []
    final = []
    transcriber = MeetingLiveTranscriber(
        engine,
        on_provisional=provisional.append,
        on_transcript=final.append,
    )
    transcriber.register_source("mic", "MIC")
    transcriber.start(active_sources=["mic"])
    transcriber.push_audio("mic", np.full(16_000 * 5, 0.05, dtype=np.float32), 16_000)
    transcriber.stop()

    assert [item.text for item in provisional] == ["borrador visible"]
    assert [item.text for item in final] == ["texto final"]
    assert provisional[0].started_at_ms == final[0].started_at_ms == 0
