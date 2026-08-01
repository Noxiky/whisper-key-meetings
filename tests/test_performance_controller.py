import time
from types import SimpleNamespace

import numpy as np

import whisper_key.ui.controller as controller_module
from whisper_key.ui.controller import AppController


class FakeSampler:
    def __init__(self, *, gpu_enabled):
        self.gpu_enabled = gpu_enabled
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True
        return {"samples": 3, "gpu": {"status": "measured"}}


def test_controller_starts_gpu_sampler_and_finishes_it_once(monkeypatch, tmp_path):
    created = []

    def factory(**kwargs):
        sampler = FakeSampler(**kwargs)
        created.append(sampler)
        return sampler

    monkeypatch.setattr(controller_module, "PerformanceSampler", factory)
    controller = AppController(tmp_path / "library", tmp_path / "models")
    controller.engine = SimpleNamespace(device="cuda")

    controller._start_acceptance_performance()
    result = controller._finish_acceptance_performance()

    assert created[0].gpu_enabled is True
    assert created[0].started is True
    assert created[0].stopped is True
    assert result["samples"] == 3
    assert controller._finish_acceptance_performance() is None


def test_benchmark_history_and_acceptance_receive_same_performance_evidence(tmp_path):
    controller = AppController(tmp_path / "library", tmp_path / "models")
    performance = {
        "samples": 4,
        "process": {"status": "measured", "peak_bytes": 123},
        "gpu": {"status": "measured", "peak_vram_used_bytes": 456},
    }
    sampler = FakeSampler(gpu_enabled=True)
    sampler.stop = lambda: performance
    controller._acceptance_performance = sampler
    controller._pending_acceptance_scenario = "p7_spanish"
    engine = SimpleNamespace(
        last_transcription_metrics={"real_time_factor": 0.4},
    )
    controller.dictation = SimpleNamespace(
        stop_and_transcribe=lambda: "texto medido",
        last_audio=np.ones(1600, dtype=np.float32),
        last_sample_rate=16000,
        whisper_engine=engine,
    )
    appended = []

    class History:
        def append(self, **values):
            appended.append(values)
            return {
                **values,
                "dictation_id": "benchmark-one",
                "audio_path": "audio/benchmark-one.wav",
                "duration_ms": 100,
            }

        @staticmethod
        def list_entries():
            return []

    evaluated = []

    class Acceptance:
        def evaluate_dictation(self, scenario_id, entry):
            evaluated.append((scenario_id, entry))
            return {"scenarios": []}

    controller.dictation_history = History()
    controller.acceptance = Acceptance()
    controller._stop_dictation()
    deadline = time.monotonic() + 2
    while controller.busy and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not controller.busy

    assert appended[0]["transcription"]["performance"] == performance
    assert evaluated[0][0] == "p7_spanish"
    assert evaluated[0][1]["transcription"]["performance"] == performance
