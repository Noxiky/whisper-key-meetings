from pathlib import Path

from whisper_key.model_registry import ModelRegistry


def _complete_snapshot(folder: Path) -> None:
    folder.mkdir(parents=True)
    (folder / "model.bin").write_bytes(b"model")
    (folder / "config.json").write_text("{}", encoding="utf-8")
    (folder / "tokenizer.json").write_text("{}", encoding="utf-8")


def test_runtime_source_prefers_complete_local_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    registry = ModelRegistry(
        whisper_models_config={
            "large-v3-turbo": {
                "source": "example/faster-whisper-large-v3-turbo",
                "label": "Large-V3-Turbo (1.5GB)",
            }
        }
    )
    snapshot = (
        tmp_path
        / ".cache"
        / "huggingface"
        / "hub"
        / "models--example--faster-whisper-large-v3-turbo"
        / "snapshots"
        / "revision-one"
    )
    _complete_snapshot(snapshot)

    assert registry.is_model_cached("large-v3-turbo") is True
    assert registry.get_runtime_source("large-v3-turbo") == str(snapshot)


def test_runtime_source_uses_repository_only_when_cache_is_incomplete(monkeypatch, tmp_path):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    registry = ModelRegistry(whisper_models_config={"medium": {"source": "example/medium", "label": "Medium (1.4GB)"}})
    incomplete = tmp_path / ".cache" / "huggingface" / "hub" / "models--example--medium" / "snapshots" / "partial"
    incomplete.mkdir(parents=True)
    (incomplete / "config.json").write_text("{}", encoding="utf-8")

    assert registry.is_model_cached("medium") is False
    assert registry.get_runtime_source("medium") == "example/medium"
