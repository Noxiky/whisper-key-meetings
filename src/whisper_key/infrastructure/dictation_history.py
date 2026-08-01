from __future__ import annotations

import json
import os
import wave
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import numpy as np


class DictationHistoryStore:
    """Append-only local history for completed quick dictations and their audio."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.audio_root = self.root / "audio"
        self.timeline = self.root / "history.jsonl"
        self.audio_root.mkdir(parents=True, exist_ok=True)

    def append(
        self,
        *,
        text: str,
        audio: np.ndarray | None,
        sample_rate: int,
        delivery: str,
        error: str | None = None,
        transcription: dict | None = None,
    ) -> dict:
        created = datetime.now(UTC)
        entry_id = str(uuid4())
        relative_audio = None
        duration_ms = 0
        if audio is not None and np.asarray(audio).size:
            normalized = np.asarray(audio, dtype=np.float32).reshape(-1)
            duration_ms = round(len(normalized) / max(1, sample_rate) * 1000)
            relative_audio = Path("audio") / f"{created.year:04d}" / f"{created.month:02d}" / f"{entry_id}.wav"
            self._write_wav(relative_audio, normalized, sample_rate)
        entry = {
            "schema_version": 2,
            "dictation_id": entry_id,
            "created_at": created.isoformat().replace("+00:00", "Z"),
            "text": text.strip(),
            "delivery": delivery,
            "duration_ms": duration_ms,
            "audio_path": relative_audio.as_posix() if relative_audio else None,
            "error": error,
            "transcription": dict(transcription or {}),
        }
        self.timeline.parent.mkdir(parents=True, exist_ok=True)
        with self.timeline.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return entry

    def list_entries(self) -> list[dict]:
        if not self.timeline.exists():
            return []
        entries = []
        with self.timeline.open("r", encoding="utf-8") as handle:
            lines = handle.readlines()
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                if index == len(lines) - 1:
                    break
                continue
            if isinstance(value, dict) and value.get("dictation_id"):
                entries.append(value)
        return list(reversed(entries))

    def _write_wav(self, relative: Path, audio: np.ndarray, sample_rate: int) -> None:
        destination = (self.root / relative).resolve()
        root = self.root.resolve()
        if root not in destination.parents:
            raise ValueError("Dictation audio path escapes history root")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".tmp")
        pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2")
        try:
            with wave.open(str(temporary), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(sample_rate)
                handle.writeframes(pcm.tobytes())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
