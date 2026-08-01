from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import sys
import tempfile
import zipfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

LOG_LINE = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:,\d+)?)\s+"
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+"
    r"(?P<logger>[^ ]+)\s+(?P<message>.*)$"
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _device_reference(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.casefold() == "default":
        return "default"
    return f"device-{_sha256_bytes(text.encode('utf-8'))[:12]}"


class DiagnosticsBundleService:
    """Create a support bundle that never reads captured user content.

    The service only inspects top-level application log metadata. Raw log messages,
    user settings, sessions, dictation history, audio, screenshots, and transcripts
    are deliberately excluded. Warning/error messages are represented by a stable
    fingerprint so repeated faults can be correlated without exposing their text.
    """

    def __init__(self, app_data_root: Path, output_root: Path | None = None):
        self.app_data_root = Path(app_data_root).resolve()
        self.output_root = Path(output_root or self.app_data_root / "diagnostics").resolve()

    def create(
        self,
        *,
        version: str,
        application: dict[str, Any] | None = None,
        safe_settings: dict[str, Any] | None = None,
        audio_diagnostics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        created = datetime.now(UTC)
        report = {
            "schema_version": 1,
            "product": "WhisperKey",
            "version": version,
            "created_utc": created.isoformat(),
            "system": {
                "platform": platform.system(),
                "release": platform.release(),
                "version": platform.version(),
                "machine": platform.machine(),
                "python": platform.python_version(),
                "frozen": bool(getattr(sys, "frozen", False)),
            },
            "application": self._safe_application(application or {}),
            "settings": self._safe_settings(safe_settings or {}),
            "audio_diagnostics": self._safe_audio_diagnostics(audio_diagnostics or {}),
            "logs": self._summarize_logs(),
            "privacy": {
                "raw_log_messages_included": False,
                "user_settings_file_included": False,
                "transcripts_included": False,
                "audio_included": False,
                "screenshots_included": False,
                "session_files_included": False,
                "dictation_history_included": False,
                "uploaded": False,
            },
        }
        readme = (
            "WhisperKey diagnostics bundle\n"
            "=============================\n\n"
            "Created locally. Nothing was uploaded. This ZIP intentionally excludes raw log messages, "
            "user settings files, transcripts, audio, screenshots, session folders, and dictation history.\n"
            "Device names are replaced by one-way short references. Warning/error messages are represented "
            "only by fingerprints. Review diagnostics.json before sharing this file.\n"
        )
        self.output_root.mkdir(parents=True, exist_ok=True)
        filename = f"WhisperKey-diagnostics-{created.strftime('%Y%m%d-%H%M%S')}.zip"
        destination = self.output_root / filename
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".WhisperKey-diagnostics-",
            suffix=".tmp",
            dir=self.output_root,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(
                    "diagnostics.json",
                    json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                )
                archive.writestr("README.txt", readme)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return {
            "path": str(destination),
            "bytes": destination.stat().st_size,
            "sha256": _sha256_file(destination),
            "privacy": report["privacy"],
        }

    @staticmethod
    def _safe_application(value: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "ready",
            "busy",
            "model",
            "device",
            "compute_type",
            "model_loaded",
            "model_load_ms",
            "dictation_active",
            "session_status",
        }
        return {key: value[key] for key in sorted(allowed & value.keys())}

    @staticmethod
    def _safe_settings(value: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key in ("model", "device", "compute_type", "language", "theme"):
            if key in value:
                result[key] = value[key]
        hotkeys = value.get("hotkeys")
        if isinstance(hotkeys, dict):
            result["hotkeys"] = {
                str(key): str(item)
                for key, item in sorted(hotkeys.items())
                if isinstance(item, (str, int, float, bool))
            }
        routes = value.get("audio_routes")
        if isinstance(routes, dict):
            result["audio_routes"] = {
                "input_route": "default" if routes.get("input_device") in {None, "default"} else "explicit",
                "input_device_ref": _device_reference(routes.get("input_device_name")),
                "system_route": "default" if routes.get("system_audio_device") in {None, "default", ""} else "explicit",
                "system_device_ref": _device_reference(routes.get("system_audio_device")),
            }
        return result

    @staticmethod
    def _safe_audio_diagnostics(value: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        summary = value.get("summary")
        if isinstance(summary, dict):
            result["summary"] = {
                key: summary[key]
                for key in ("status", "title")
                if isinstance(summary.get(key), (str, int, float, bool))
            }
        for source in ("mic", "system"):
            item = value.get(source)
            if not isinstance(item, dict):
                continue
            safe = {
                key: item[key]
                for key in (
                    "status",
                    "available",
                    "peak_dbfs",
                    "rms_dbfs",
                    "sample_rate",
                    "channels",
                    "frames",
                    "duration_seconds",
                )
                if isinstance(item.get(key), (str, int, float, bool))
            }
            safe["device_ref"] = _device_reference(item.get("device"))
            result[source] = safe
        return result

    def _summarize_logs(self) -> dict[str, Any]:
        files = []
        level_counts: Counter[str] = Counter()
        logger_counts: Counter[str] = Counter()
        incidents: list[dict[str, str]] = []
        try:
            candidates = sorted(self.app_data_root.glob("*.log"), key=lambda path: path.name.casefold())
        except OSError:
            candidates = []
        for path in candidates:
            try:
                stat = path.stat()
                content = self._read_tail(path)
            except OSError:
                continue
            parsed = 0
            for line in content.splitlines():
                match = LOG_LINE.match(line)
                if not match:
                    continue
                parsed += 1
                level = match.group("level")
                logger = match.group("logger")
                level_counts[level] += 1
                logger_counts[logger] += 1
                if level in {"WARNING", "ERROR", "CRITICAL"}:
                    incidents.append(
                        {
                            "timestamp": match.group("timestamp"),
                            "level": level,
                            "logger": logger,
                            "message_fingerprint": _sha256_bytes(
                                match.group("message").encode("utf-8", errors="replace")
                            )[:16],
                        }
                    )
            files.append(
                {
                    "name": path.name,
                    "bytes": stat.st_size,
                    "modified_utc": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
                    "tail_lines_parsed": parsed,
                }
            )
        return {
            "files": files,
            "tail_limit_bytes_per_file": 1024 * 1024,
            "levels": dict(sorted(level_counts.items())),
            "loggers": dict(logger_counts.most_common(30)),
            "recent_incidents": incidents[-50:],
            "messages_included": False,
        }

    @staticmethod
    def _read_tail(path: Path, limit: int = 1024 * 1024) -> str:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - limit), os.SEEK_SET)
            value = handle.read(limit)
        return value.decode("utf-8", errors="replace")
