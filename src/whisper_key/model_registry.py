import logging
import os
from pathlib import Path

from faster_whisper.utils import _MODELS


class ModelRegistry:
    DEFAULT_CACHE_PREFIX = "models--Systran--faster-whisper-"

    def __init__(self, whisper_models_config: dict = None, streaming_models_config: dict = None):
        self.whisper_models = {}
        self.streaming_models = {}
        self.logger = logging.getLogger(__name__)

        if whisper_models_config:
            for key, config in whisper_models_config.items():
                if isinstance(config, dict):
                    self.whisper_models[key] = ModelDefinition(key, config, model_type="whisper")

        if streaming_models_config:
            for key, config in streaming_models_config.items():
                if isinstance(config, dict):
                    self.streaming_models[key] = ModelDefinition(key, config, model_type="streaming")

    def get_model(self, key: str):
        return self.whisper_models.get(key)

    def get_source(self, key: str) -> str:
        model = self.get_model(key)
        return model.source if model else key

    def get_runtime_source(self, key: str) -> str:
        """Prefer a complete local snapshot so normal startup never needs the network."""
        snapshot = self.get_cached_snapshot_path(key)
        return snapshot or self.get_source(key)

    def get_cached_snapshot_path(self, key: str) -> str | None:
        model = self.get_model(key)
        if model and model.is_local_path:
            return model.source if self._snapshot_is_complete(model.source) else None
        cache_folder = self.get_cache_folder(key)
        if not cache_folder:
            return None
        snapshots = Path(self.get_hf_cache_path()) / cache_folder / "snapshots"
        if not snapshots.is_dir():
            return None
        candidates = sorted(
            (path for path in snapshots.iterdir() if path.is_dir()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for snapshot in candidates:
            if self._snapshot_is_complete(str(snapshot)):
                return str(snapshot)
        return None

    def get_cache_folder(self, key: str) -> str:
        model = self.get_model(key)
        if not model:
            return f"{self.DEFAULT_CACHE_PREFIX}{key}"
        return model.cache_folder

    def get_models_by_group(self, group: str) -> list:
        return [m for m in self.whisper_models.values() if m.group == group and m.enabled]

    def get_groups_ordered(self) -> list:
        return ["official", "custom"]

    def get_hf_cache_path(self) -> str:
        userprofile = os.environ.get("USERPROFILE")
        if userprofile:
            return os.path.join(userprofile, ".cache", "huggingface", "hub")
        return os.path.join(os.path.expanduser("~"), ".cache", "huggingface", "hub")

    def is_model_cached(self, key: str) -> bool:
        return self.get_cached_snapshot_path(key) is not None

    @staticmethod
    def _snapshot_is_complete(folder: str) -> bool:
        required = ("model.bin", "config.json")
        tokenizer_options = ("tokenizer.json", "vocabulary.json", "vocabulary.txt")
        return (
            os.path.isdir(folder)
            and all(os.path.isfile(os.path.join(folder, name)) for name in required)
            and any(os.path.isfile(os.path.join(folder, name)) for name in tokenizer_options)
        )

    def _is_streaming_model_cached(self, key: str) -> bool:
        model = self.streaming_models.get(key)
        if not model or not model.files:
            return False

        snapshot_path = self._get_streaming_snapshot_path(key)
        if not snapshot_path:
            return False

        for file_path in model.files.values():
            if not os.path.exists(os.path.join(snapshot_path, file_path)):
                return False
        return True

    def _get_streaming_snapshot_path(self, key: str) -> str | None:
        model = self.streaming_models.get(key)
        if not model:
            return None

        model_dir = os.path.join(self.get_hf_cache_path(), model.cache_folder)
        snapshots_dir = os.path.join(model_dir, "snapshots")

        if not os.path.exists(snapshots_dir):
            return None

        snapshots = os.listdir(snapshots_dir)
        if not snapshots:
            return None

        return os.path.join(snapshots_dir, snapshots[0])

    def get_streaming_model_path(self, key: str) -> tuple | None:
        model = self.streaming_models.get(key)
        if not model:
            return None

        if not self._is_streaming_model_cached(key):
            if not self.download_streaming_model(key):
                return None

        snapshot_path = self._get_streaming_snapshot_path(key)
        if not snapshot_path:
            return None

        return snapshot_path, model.files

    def download_streaming_model(self, key: str) -> bool:
        model = self.streaming_models.get(key)
        if not model:
            return False

        try:
            from huggingface_hub import snapshot_download

            self.logger.info(f"Downloading streaming model: {model.source}")
            print(f"   Downloading streaming model: {model.label}...")
            snapshot_download(repo_id=model.source)
            self.logger.info(f"Streaming model downloaded: {model.source}")
            return True
        except Exception as e:
            self.logger.error(f"Failed to download streaming model {model.source}: {e}")
            return False


class ModelDefinition:
    def __init__(self, key: str, config: dict, model_type: str = "whisper"):
        self.key = key
        self.model_type = model_type
        self.source = config.get("source", key)
        self.label = config.get("label", key.title())
        self.group = config.get("group", "custom")
        self.enabled = config.get("enabled", True)
        self.files = config.get("files", {})
        self.is_local_path = self._check_is_local_path()
        self.cache_folder = self._derive_cache_folder()

    def _check_is_local_path(self) -> bool:
        if self.source.startswith("\\\\") or (len(self.source) > 2 and self.source[1] == ":"):
            return True
        if "/" in self.source:
            return os.path.exists(self.source)
        return False

    def _derive_cache_folder(self) -> str:
        if self.is_local_path:
            return None

        if "/" in self.source:
            return "models--" + self.source.replace("/", "--")

        if self.source in _MODELS:
            repo = _MODELS[self.source]
            return "models--" + repo.replace("/", "--")

        return f"{ModelRegistry.DEFAULT_CACHE_PREFIX}{self.source}"
