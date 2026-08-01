from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from whisper_key.model_registry import ModelRegistry


@dataclass(frozen=True)
class ModelCacheInspection:
    state: str
    snapshot_path: str | None
    cached_bytes: int
    expected_bytes: int | None
    missing_files: tuple[str, ...]
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class ModelPreflight:
    model_key: str
    cache: ModelCacheInspection
    download_bytes: int
    disk_required_bytes: int
    disk_free_bytes: int
    memory_required_bytes: int
    memory_free_bytes: int | None
    memory_kind: str
    allowed: bool
    detail: str

    def to_dict(self) -> dict:
        value = asdict(self)
        value["cache"] = self.cache.to_dict()
        return value


class ModelPreflightService:
    REQUIRED_BASE = ("model.bin", "config.json")
    TOKENIZER_OPTIONS = ("tokenizer.json", "vocabulary.json", "vocabulary.txt")
    DOWNLOAD_HEADROOM = 1.20
    DOWNLOAD_FIXED_HEADROOM = 256 * 1024 * 1024
    MEMORY_FIXED_HEADROOM = 512 * 1024 * 1024

    def __init__(
        self,
        registry: ModelRegistry,
        *,
        disk_usage=shutil.disk_usage,
        gpu_memory_probe=None,
        system_memory_probe=None,
    ):
        self.registry = registry
        self.cache_root = Path(registry.get_hf_cache_path())
        self.disk_usage = disk_usage
        self.gpu_memory_probe = gpu_memory_probe or self._probe_nvidia_free_memory
        self.system_memory_probe = system_memory_probe or self._probe_system_free_memory

    def inspect_cache(self, model_key: str) -> ModelCacheInspection:
        definition = self.registry.get_model(model_key)
        if not definition:
            return ModelCacheInspection("missing", None, 0, None, (), "Modelo desconocido")
        expected_hint = self._estimated_bytes(definition.label)
        if definition.is_local_path:
            return self._inspect_snapshot(Path(definition.source), expected_hint, None)

        cache_folder = self.cache_root / str(definition.cache_folder)
        snapshots = cache_folder / "snapshots"
        if not snapshots.is_dir():
            state = "incomplete" if cache_folder.exists() else "missing"
            return ModelCacheInspection(
                state,
                None,
                self._folder_bytes(cache_folder),
                expected_hint,
                self.REQUIRED_BASE,
                "Descarga incompleta; puede reanudarse" if state == "incomplete" else "No instalado",
            )
        candidates = sorted(
            (path for path in snapshots.iterdir() if path.is_dir()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        best: ModelCacheInspection | None = None
        for snapshot in candidates:
            tree = self._load_tree(cache_folder, snapshot.name)
            inspection = self._inspect_snapshot(snapshot, expected_hint, tree)
            if inspection.state == "ready":
                return inspection
            if best is None or inspection.cached_bytes > best.cached_bytes:
                best = inspection
        return best or ModelCacheInspection(
            "incomplete",
            None,
            self._folder_bytes(cache_folder),
            expected_hint,
            self.REQUIRED_BASE,
            "La caché no contiene un snapshot utilizable",
        )

    def preflight(self, model_key: str, *, device: str, compute_type: str) -> ModelPreflight:
        cache = self.inspect_cache(model_key)
        estimate = (
            cache.expected_bytes
            or self._estimated_bytes(self.registry.get_model(model_key).label)
            or 1024 * 1024 * 1024
        )
        download_bytes = 0 if cache.state == "ready" else max(0, estimate - cache.cached_bytes)
        disk_required = (
            round(download_bytes * self.DOWNLOAD_HEADROOM) + self.DOWNLOAD_FIXED_HEADROOM if download_bytes else 0
        )
        disk_free = self.disk_usage(self._existing_cache_parent()).free
        memory_required = self._memory_requirement(estimate, compute_type)
        memory_kind = "VRAM" if device == "cuda" else "RAM"
        memory_free = self.gpu_memory_probe() if device == "cuda" else self.system_memory_probe()
        disk_ok = disk_free >= disk_required
        memory_ok = memory_free is None or memory_free >= memory_required
        cache_ok = cache.state != "corrupt"
        allowed = disk_ok and memory_ok and cache_ok
        details = []
        if cache.state == "ready":
            details.append("Caché estructuralmente completa")
        elif cache.state == "incomplete":
            details.append("La descarga se reanudará")
        elif cache.state == "corrupt":
            details.append("La caché está dañada; abre la caché para repararla")
        else:
            details.append("Se descargará localmente")
        if not disk_ok:
            details.append("espacio en disco insuficiente")
        if not memory_ok:
            details.append(f"{memory_kind} libre insuficiente para cargar sin expulsar el modelo actual")
        if memory_free is None:
            details.append(f"{memory_kind} libre no detectable; se verificará al cargar")
        return ModelPreflight(
            model_key=model_key,
            cache=cache,
            download_bytes=download_bytes,
            disk_required_bytes=disk_required,
            disk_free_bytes=disk_free,
            memory_required_bytes=memory_required,
            memory_free_bytes=memory_free,
            memory_kind=memory_kind,
            allowed=allowed,
            detail=" · ".join(details),
        )

    def verify_cache(self, model_key: str, progress=None) -> dict:
        inspection = self.inspect_cache(model_key)
        if inspection.state != "ready" or not inspection.snapshot_path:
            return {
                "model_key": model_key,
                "status": inspection.state,
                "detail": inspection.detail,
                "verified_bytes": 0,
            }
        snapshot = Path(inspection.snapshot_path)
        definition = self.registry.get_model(model_key)
        tree = (
            None
            if definition.is_local_path
            else self._load_tree(
                self.cache_root / str(definition.cache_folder),
                snapshot.name,
            )
        )
        model_path = snapshot / "model.bin"
        expected_hash = ((tree or {}).get("files", {}).get("model.bin", {})).get("lfs_sha256")
        verified_bytes = 0
        if expected_hash:
            actual = self._sha256(model_path, progress)
            verified_bytes = model_path.stat().st_size
            if actual != expected_hash:
                result = {
                    "model_key": model_key,
                    "status": "corrupt",
                    "detail": "model.bin no coincide con el SHA-256 publicado por el repositorio",
                    "verified_bytes": verified_bytes,
                    "expected_sha256": expected_hash,
                    "actual_sha256": actual,
                }
                self._record_verification(snapshot, result)
                return result
        try:
            json.loads((snapshot / "config.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            result = {
                "model_key": model_key,
                "status": "corrupt",
                "detail": f"config.json no es válido: {exc}",
                "verified_bytes": verified_bytes,
            }
            self._record_verification(snapshot, result)
            return result
        status = "verified" if expected_hash else "structural"
        detail = (
            "SHA-256 de model.bin y estructura verificados"
            if expected_hash
            else "Estructura verificada; el repositorio local no publica un SHA-256 contrastable"
        )
        if progress:
            progress(1.0)
        result = {
            "model_key": model_key,
            "status": status,
            "detail": detail,
            "verified_bytes": verified_bytes,
            "expected_sha256": expected_hash,
        }
        self._record_verification(snapshot, result)
        return result

    def _inspect_snapshot(
        self,
        snapshot: Path,
        expected_hint: int | None,
        tree: dict | None,
    ) -> ModelCacheInspection:
        expected_files = (tree or {}).get("files", {})
        expected_bytes = (
            sum(int(metadata.get("lfs_size") or metadata.get("size") or 0) for metadata in expected_files.values())
            or expected_hint
        )
        missing = [name for name in self.REQUIRED_BASE if not (snapshot / name).is_file()]
        if not any((snapshot / name).is_file() for name in self.TOKENIZER_OPTIONS):
            missing.append("tokenizer/vocabulary")
        cached_bytes = self._folder_bytes(snapshot)
        if missing:
            return ModelCacheInspection(
                "incomplete",
                str(snapshot),
                cached_bytes,
                expected_bytes,
                tuple(missing),
                f"Faltan archivos: {', '.join(missing)}; la descarga puede reanudarse",
            )
        for name in self.REQUIRED_BASE:
            path = snapshot / name
            expected = expected_files.get(name, {})
            expected_size = int(expected.get("lfs_size") or expected.get("size") or 0)
            if path.stat().st_size < 1 or (expected_size and path.stat().st_size != expected_size):
                return ModelCacheInspection(
                    "corrupt",
                    str(snapshot),
                    cached_bytes,
                    expected_bytes,
                    (),
                    f"{name} tiene un tamaño inesperado",
                )
        try:
            json.loads((snapshot / "config.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ModelCacheInspection(
                "corrupt",
                str(snapshot),
                cached_bytes,
                expected_bytes,
                (),
                "config.json está dañado",
            )
        verification = self._read_verification(snapshot)
        model_stat = (snapshot / "model.bin").stat()
        if (
            verification
            and verification.get("status") == "corrupt"
            and verification.get("model_size") == model_stat.st_size
            and verification.get("model_mtime_ns") == model_stat.st_mtime_ns
        ):
            return ModelCacheInspection(
                "corrupt",
                str(snapshot),
                cached_bytes,
                expected_bytes,
                (),
                verification.get("detail", "La verificación SHA-256 falló"),
            )
        return ModelCacheInspection(
            "ready",
            str(snapshot),
            cached_bytes,
            expected_bytes,
            (),
            "Archivos requeridos presentes y tamaños coherentes",
        )

    @staticmethod
    def _load_tree(cache_folder: Path, revision: str) -> dict | None:
        tree_root = cache_folder / "trees" / revision
        candidates = []
        if tree_root.is_file():
            candidates.append(tree_root)
        sibling_json = tree_root.with_suffix(".json")
        if sibling_json.is_file():
            candidates.append(sibling_json)
        if tree_root.is_dir():
            candidates.extend(tree_root.glob("*.json"))
        for path in candidates:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(value, dict) and isinstance(value.get("files"), dict):
                return value
        return None

    @staticmethod
    def _estimated_bytes(label: str) -> int | None:
        match = re.search(r"([0-9]+(?:[.][0-9]+)?)\s*(MB|GB)", label, re.IGNORECASE)
        if not match:
            return None
        multiplier = 1000**2 if match.group(2).upper() == "MB" else 1000**3
        return round(float(match.group(1)) * multiplier)

    @classmethod
    def _memory_requirement(cls, model_bytes: int, compute_type: str) -> int:
        multiplier = {
            "int8": 1.25,
            "float16": 2.0,
            "float32": 3.5,
        }.get(compute_type, 2.0)
        return round(model_bytes * multiplier) + cls.MEMORY_FIXED_HEADROOM

    def _existing_cache_parent(self) -> Path:
        path = self.cache_root
        while not path.exists() and path != path.parent:
            path = path.parent
        return path

    @staticmethod
    def _read_verification(snapshot: Path) -> dict | None:
        path = snapshot / ".whisperkey-verification.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _record_verification(snapshot: Path, result: dict) -> None:
        model_path = snapshot / "model.bin"
        try:
            model_stat = model_path.stat()
            record = {
                "schema_version": 1,
                "verified_at": datetime.now(UTC).isoformat(),
                "status": result["status"],
                "detail": result["detail"],
                "model_size": model_stat.st_size,
                "model_mtime_ns": model_stat.st_mtime_ns,
                "expected_sha256": result.get("expected_sha256"),
                "actual_sha256": result.get("actual_sha256"),
            }
            destination = snapshot / ".whisperkey-verification.json"
            temporary = snapshot / ".whisperkey-verification.json.tmp"
            temporary.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
            os.replace(temporary, destination)
        except OSError:
            return

    @staticmethod
    def _folder_bytes(folder: Path) -> int:
        if not folder.exists():
            return 0
        total = 0
        for path in folder.rglob("*"):
            try:
                if path.is_file():
                    total += path.stat().st_size
            except OSError:
                continue
        return total

    @staticmethod
    def _probe_nvidia_free_memory() -> int | None:
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=memory.free",
                    "--format=csv,noheader,nounits",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=4,
                creationflags=flags,
            )
            values = [int(line.strip()) for line in result.stdout.splitlines() if line.strip()]
            return values[0] * 1024 * 1024 if values else None
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    @staticmethod
    def _probe_system_free_memory() -> int | None:
        if os.name != "nt":
            return None

        class MemoryStatus(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ulong),
                ("memory_load", ctypes.c_ulong),
                ("total_physical", ctypes.c_ulonglong),
                ("available_physical", ctypes.c_ulonglong),
                ("total_page_file", ctypes.c_ulonglong),
                ("available_page_file", ctypes.c_ulonglong),
                ("total_virtual", ctypes.c_ulonglong),
                ("available_virtual", ctypes.c_ulonglong),
                ("available_extended_virtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(MemoryStatus)
        try:
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return None
        except (AttributeError, OSError):
            return None
        return int(status.available_physical)

    @staticmethod
    def _sha256(path: Path, progress=None) -> str:
        digest = hashlib.sha256()
        total = max(1, path.stat().st_size)
        read = 0
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
                digest.update(block)
                read += len(block)
                if progress:
                    progress(min(0.99, read / total))
        return digest.hexdigest()
