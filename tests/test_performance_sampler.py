import os
import time
from types import SimpleNamespace

import pytest

from whisper_key.infrastructure.performance_sampler import (
    PerformanceSampler,
    probe_nvidia_gpu,
    probe_process_working_set,
)


def wait_for_sample(sampler):
    deadline = time.monotonic() + 1
    while sampler.result()["samples"] < 1 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert sampler.result()["samples"] >= 1


def test_sampler_records_interval_peaks_deltas_and_privacy():
    process_values = iter([100, 160, 130])
    gpu_values = iter(
        [
            {"vram_used_bytes": 1_000, "temperature_c": 60, "scope": "gpu_total_used"},
            {"vram_used_bytes": 1_800, "temperature_c": 72, "scope": "gpu_total_used"},
            {"vram_used_bytes": 1_500, "temperature_c": 68, "scope": "gpu_total_used"},
        ]
    )
    sampler = PerformanceSampler(
        interval_seconds=5,
        process_probe=lambda: next(process_values),
        gpu_probe=lambda: next(gpu_values),
    )

    sampler.start()
    wait_for_sample(sampler)
    sampler._sample_once()
    sampler._sample_once()
    result = sampler.stop()

    assert result["samples"] == 3
    assert result["process"]["baseline_bytes"] == 100
    assert result["process"]["peak_bytes"] == 160
    assert result["process"]["delta_bytes"] == 60
    assert result["gpu"]["baseline_vram_used_bytes"] == 1_000
    assert result["gpu"]["peak_vram_used_bytes"] == 1_800
    assert result["gpu"]["delta_vram_used_bytes"] == 800
    assert result["gpu"]["peak_temperature_c"] == 72
    assert result["gpu"]["scope"] == "gpu_total_used"
    assert result["privacy"] == {
        "audio_sampled": False,
        "transcript_sampled": False,
        "uploaded": False,
    }


def test_sampler_reports_unavailable_or_not_applicable_without_inventing_values():
    unavailable = PerformanceSampler(
        interval_seconds=5,
        process_probe=lambda: None,
        gpu_probe=lambda: {"error": "nvidia-smi unavailable", "scope": "gpu_total_used"},
    )
    unavailable.start()
    wait_for_sample(unavailable)
    unavailable_result = unavailable.stop()

    assert unavailable_result["process"]["status"] == "unavailable"
    assert unavailable_result["process"]["peak_bytes"] is None
    assert unavailable_result["gpu"]["status"] == "unavailable"
    assert unavailable_result["gpu"]["peak_vram_used_bytes"] is None
    assert unavailable_result["gpu"]["error"] == "nvidia-smi unavailable"

    cpu = PerformanceSampler(interval_seconds=5, process_probe=lambda: 42, gpu_enabled=False)
    cpu.start()
    wait_for_sample(cpu)
    cpu_result = cpu.stop()
    assert cpu_result["gpu"]["status"] == "not_applicable"


def test_nvidia_probe_labels_total_gpu_scope_and_converts_mib_to_bytes():
    def runner(*_args, **_kwargs):
        return SimpleNamespace(stdout="0, 5120, 67\n")

    result = probe_nvidia_gpu(runner=runner)

    assert result == {
        "gpu_index": 0,
        "vram_used_bytes": 5120 * 1024 * 1024,
        "temperature_c": 67,
        "scope": "gpu_total_used",
        "error": None,
    }


@pytest.mark.skipif(os.name != "nt", reason="Windows process-memory contract")
def test_windows_process_working_set_probe_returns_real_bytes():
    assert probe_process_working_set() > 0
