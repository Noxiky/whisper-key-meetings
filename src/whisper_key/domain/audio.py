from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class TranscriptionJob:
    source_id: str
    label: str
    audio: np.ndarray
    started_at_ms: int
    ended_at_ms: int
    provisional: bool = False


@dataclass(frozen=True)
class TranscriptResult:
    source_id: str
    source: str
    text: str
    started_at_ms: int
    ended_at_ms: int
    language: str | None


@dataclass(frozen=True)
class SourceHealth:
    source_id: str
    status: str
    detail: str
    fatal: bool = False
