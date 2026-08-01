import logging

import whisper_key.whisper_engine as whisper_module
from whisper_key.whisper_engine import WhisperEngine


class Registry:
    @staticmethod
    def is_model_cached(_key):
        return True

    @staticmethod
    def get_source(key):
        return key


def engine_without_loading():
    engine = WhisperEngine.__new__(WhisperEngine)
    engine.model_key = "old"
    engine.model = object()
    engine.device = "cuda"
    engine.compute_type = "float16"
    engine.registry = Registry()
    engine.logger = logging.getLogger("test-model-switch")
    engine._loading_thread = None
    engine._progress_callback = None
    engine.last_load_error = None
    return engine


def test_failed_async_model_switch_keeps_previous_model_and_records_error(monkeypatch):
    engine = engine_without_loading()
    previous = engine.model
    updates = []

    def fail_model(*_args, **_kwargs):
        raise RuntimeError("CUDA allocation failed")

    monkeypatch.setattr(whisper_module, "WhisperModel", fail_model)
    thread = engine._load_model_async("new", updates.append)
    thread.join(timeout=2)

    assert engine.model_key == "old"
    assert engine.model is previous
    assert engine.last_load_error == "CUDA allocation failed"
    assert updates[-1] == "Failed to load model: CUDA allocation failed"


def test_successful_switch_is_not_broken_by_progress_callback(monkeypatch):
    engine = engine_without_loading()
    replacement = object()
    monkeypatch.setattr(whisper_module, "WhisperModel", lambda *_args, **_kwargs: replacement)

    def broken_progress(_message):
        raise RuntimeError("UI disappeared")

    thread = engine._load_model_async("new", broken_progress)
    thread.join(timeout=2)

    assert engine.model_key == "new"
    assert engine.model is replacement
    assert engine.last_load_error is None


def test_initial_load_uses_registry_local_runtime_source(monkeypatch):
    captured = {}

    class LocalRegistry:
        @staticmethod
        def is_model_cached(_key):
            return True

        @staticmethod
        def get_runtime_source(_key):
            return r"C:\cache\snapshot"

    def fake_model(source, **kwargs):
        captured.update({"source": source, **kwargs})
        return object()

    monkeypatch.setattr(whisper_module, "WhisperModel", fake_model)
    engine = WhisperEngine(
        model_key="large-v3-turbo",
        device="cuda",
        compute_type="float16",
        model_registry=LocalRegistry(),
    )

    assert engine.model is not None
    assert captured == {
        "source": r"C:\cache\snapshot",
        "device": "cuda",
        "compute_type": "float16",
    }
