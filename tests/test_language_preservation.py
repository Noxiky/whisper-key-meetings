import logging
from types import SimpleNamespace

import numpy as np

from whisper_key.whisper_engine import WhisperEngine


class Segment:
    def __init__(self, text):
        self.text = text


class RetryModel:
    def __init__(self):
        self.calls = []

    def transcribe(self, audio, **kwargs):
        self.calls.append((audio, kwargs))
        return [Segment("Привет, это русская идея.")], SimpleNamespace(
            language="ru",
            language_probability=1.0,
        )


def test_confident_russian_detection_retries_non_cyrillic_translation():
    engine = WhisperEngine.__new__(WhisperEngine)
    engine.beam_size = 5
    engine.model = RetryModel()
    engine.logger = logging.getLogger("test")
    info = SimpleNamespace(language="ru", language_probability=0.99)
    audio = np.ones(1600, dtype=np.float32)

    text, result_info = engine.ensure_detected_language_script(
        audio,
        "Hello, this is a Russian idea.",
        info,
        {"task": "transcribe", "language": None, "multilingual": True},
    )

    assert text == "Привет, это русская идея."
    assert result_info.language == "ru"
    assert engine.model.calls[0][1]["language"] == "ru"
    assert engine.model.calls[0][1]["task"] == "transcribe"
    assert "не переводи" in engine.model.calls[0][1]["initial_prompt"]


def test_existing_cyrillic_text_does_not_pay_for_a_retry():
    engine = WhisperEngine.__new__(WhisperEngine)
    engine.beam_size = 5
    engine.model = RetryModel()
    engine.logger = logging.getLogger("test")
    info = SimpleNamespace(language="ru", language_probability=1.0)

    text, _ = engine.ensure_detected_language_script(
        np.ones(1600, dtype=np.float32),
        "Уже записано по-русски.",
        info,
        {},
    )

    assert text == "Уже записано по-русски."
    assert engine.model.calls == []


def test_transcription_records_language_latency_and_real_time_factor():
    engine = WhisperEngine.__new__(WhisperEngine)
    engine.beam_size = 5
    engine.model = RetryModel()
    engine.logger = logging.getLogger("test")
    engine.vad_manager = None
    engine.language = None
    engine.initial_prompt = None
    engine.hotwords = None
    engine.strip_trailing_period = False
    engine.model_key = "large-v3-turbo"
    engine.device = "cuda"
    engine.compute_type = "float16"
    engine.log_transcriptions = False

    text = engine.transcribe_audio(np.ones(16_000, dtype=np.float32))

    assert text == "Привет, это русская идея."
    assert engine.last_transcription_metrics["status"] == "complete"
    assert engine.last_transcription_metrics["detected_language"] == "ru"
    assert engine.last_transcription_metrics["audio_duration_ms"] == 1000
    assert engine.last_transcription_metrics["processing_ms"] >= 0
    assert engine.last_transcription_metrics["real_time_factor"] >= 0
    assert engine.last_transcription_metrics["model"] == "large-v3-turbo"
    assert engine.last_transcription_metrics["inference_index"] == 1
    assert engine.last_transcription_metrics["cold_inference"] is True
