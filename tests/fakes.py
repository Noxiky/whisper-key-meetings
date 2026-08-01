from dataclasses import dataclass

import numpy as np


@dataclass
class FakeSegment:
    text: str


@dataclass
class FakeInfo:
    language: str = "es"


class FakeWhisperModel:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def transcribe(self, audio, **kwargs):
        self.calls.append((np.asarray(audio).copy(), kwargs))
        text = self.responses.pop(0) if self.responses else ""
        return [FakeSegment(text)] if text else [], FakeInfo()


class FakeWhisperEngine:
    def __init__(self, responses=None):
        self.model = FakeWhisperModel(responses)
        self.beam_size = 5
        self.language = None
        self.initial_prompt = ""
        self.hotwords = []


class FakeAudioSource:
    def __init__(self, source_id, label, chunks=None):
        self.source_id = source_id
        self.label = label
        self.chunks = list(chunks or [])
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def read(self):
        if not self.started or not self.chunks:
            return None
        return self.chunks.pop(0)
