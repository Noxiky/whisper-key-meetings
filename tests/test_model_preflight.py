import hashlib
import json
from types import SimpleNamespace

from whisper_key.infrastructure import ModelPreflightService
from whisper_key.model_registry import ModelRegistry


def build_registry(tmp_path, monkeypatch):
    registry = ModelRegistry(
        {
            "test-model": {
                "source": "owner/test-model",
                "label": "Test Model (100MB)",
                "enabled": True,
            }
        }
    )
    monkeypatch.setattr(registry, "get_hf_cache_path", lambda: str(tmp_path))
    return registry


def build_snapshot(tmp_path, registry, *, model=b"model bytes", tokenizer=True, config=b"{}"):
    revision = "a" * 40
    cache = tmp_path / registry.get_cache_folder("test-model")
    snapshot = cache / "snapshots" / revision
    snapshot.mkdir(parents=True)
    (snapshot / "model.bin").write_bytes(model)
    (snapshot / "config.json").write_bytes(config)
    if tokenizer:
        (snapshot / "tokenizer.json").write_text("{}", encoding="utf-8")
    tree = {
        "format_version": 1,
        "files": {
            "model.bin": {
                "size": len(model),
                "lfs_size": len(model),
                "lfs_sha256": hashlib.sha256(model).hexdigest(),
            },
            "config.json": {"size": len(config)},
            "tokenizer.json": {"size": 2},
        },
    }
    (cache / "trees").mkdir()
    (cache / "trees" / f"{revision}.json").write_text(json.dumps(tree), encoding="utf-8")
    return snapshot


def test_complete_cache_and_published_lfs_hash_are_verified(tmp_path, monkeypatch):
    registry = build_registry(tmp_path, monkeypatch)
    snapshot = build_snapshot(tmp_path, registry)
    progress = []
    service = ModelPreflightService(registry)

    inspection = service.inspect_cache("test-model")
    result = service.verify_cache("test-model", progress.append)

    assert registry.is_model_cached("test-model")
    assert inspection.state == "ready"
    assert result["status"] == "verified"
    assert result["verified_bytes"] == len(b"model bytes")
    assert progress[-1] == 1.0
    marker = json.loads((snapshot / ".whisperkey-verification.json").read_text(encoding="utf-8"))
    assert marker["status"] == "verified"


def test_hash_failure_quarantines_same_model_bytes_until_repaired(tmp_path, monkeypatch):
    registry = build_registry(tmp_path, monkeypatch)
    snapshot = build_snapshot(tmp_path, registry, model=b"original")
    (snapshot / "model.bin").write_bytes(b"modified")
    service = ModelPreflightService(registry)

    result = service.verify_cache("test-model")
    inspection = service.inspect_cache("test-model")

    assert result["status"] == "corrupt"
    assert inspection.state == "corrupt"
    assert not service.preflight("test-model", device="cuda", compute_type="float16").allowed


def test_incomplete_download_is_reanudable_but_disk_and_vram_are_preflighted(tmp_path, monkeypatch):
    registry = build_registry(tmp_path, monkeypatch)
    build_snapshot(tmp_path, registry, tokenizer=False)

    def enough_disk(_path):
        return SimpleNamespace(free=5 * 1024**3)

    service = ModelPreflightService(
        registry,
        disk_usage=enough_disk,
        gpu_memory_probe=lambda: 4 * 1024**3,
    )

    resumable = service.preflight("test-model", device="cuda", compute_type="float16")

    assert resumable.cache.state == "incomplete"
    assert resumable.download_bytes > 0
    assert resumable.allowed
    assert "reanudará" in resumable.detail

    disk_blocked = ModelPreflightService(
        registry,
        disk_usage=lambda _path: SimpleNamespace(free=1),
        gpu_memory_probe=lambda: 4 * 1024**3,
    ).preflight("test-model", device="cuda", compute_type="float16")
    memory_blocked = ModelPreflightService(
        registry,
        disk_usage=enough_disk,
        gpu_memory_probe=lambda: 1,
    ).preflight("test-model", device="cuda", compute_type="float16")

    assert not disk_blocked.allowed
    assert "disco insuficiente" in disk_blocked.detail
    assert not memory_blocked.allowed
    assert "VRAM libre insuficiente" in memory_blocked.detail


def test_missing_cache_uses_label_size_and_allows_unknown_memory_probe(tmp_path, monkeypatch):
    registry = build_registry(tmp_path, monkeypatch)
    service = ModelPreflightService(
        registry,
        disk_usage=lambda _path: SimpleNamespace(free=5 * 1024**3),
        gpu_memory_probe=lambda: None,
    )

    result = service.preflight("test-model", device="cuda", compute_type="float16")

    assert result.cache.state == "missing"
    assert result.download_bytes == 100_000_000
    assert result.allowed
    assert "VRAM libre no detectable" in result.detail
