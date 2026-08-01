from __future__ import annotations

import ctypes
import os
import subprocess
import threading
import time
from collections.abc import Callable


class PerformanceSampler:
    """Sample local process/GPU health during an explicit acceptance benchmark."""

    SCHEMA_VERSION = 1

    def __init__(
        self,
        *,
        interval_seconds: float = 0.5,
        process_probe: Callable[[], int | None] | None = None,
        gpu_probe: Callable[[], dict | None] | None = None,
        gpu_enabled: bool = True,
    ):
        self.interval_seconds = max(0.1, min(5.0, float(interval_seconds)))
        self.process_probe = process_probe or probe_process_working_set
        self.gpu_probe = gpu_probe or probe_nvidia_gpu
        self.gpu_enabled = gpu_enabled
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at = 0.0
        self._ended_at = 0.0
        self._samples = 0
        self._process_values: list[int] = []
        self._gpu_values: list[int] = []
        self._temperatures: list[int] = []
        self._gpu_scope: str | None = None
        self._process_error: str | None = None
        self._gpu_error: str | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            raise RuntimeError("Performance sampling is already running")
        self._stop_event.clear()
        self._started_at = time.monotonic()
        self._ended_at = 0.0
        self._samples = 0
        self._process_values = []
        self._gpu_values = []
        self._temperatures = []
        self._gpu_scope = None
        self._process_error = None
        self._gpu_error = None
        self._thread = threading.Thread(
            target=self._run,
            name="wk-acceptance-performance",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> dict:
        self._stop_event.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            # Never let a slow vendor CLI make Dictation cancel/exit feel hung. A benchmark phrase
            # normally yields several completed samples; an in-flight final sample may be omitted.
            thread.join(timeout=0.75)
        self._ended_at = self._ended_at or time.monotonic()
        return self.result()

    def result(self) -> dict:
        with self._lock:
            process_values = list(self._process_values)
            gpu_values = list(self._gpu_values)
            temperatures = list(self._temperatures)
            samples = self._samples
            process_error = self._process_error
            gpu_error = self._gpu_error
            gpu_scope = self._gpu_scope
        ended_at = self._ended_at or time.monotonic()
        return {
            "schema_version": self.SCHEMA_VERSION,
            "duration_ms": max(0, round((ended_at - self._started_at) * 1000)),
            "sample_interval_ms": round(self.interval_seconds * 1000),
            "samples": samples,
            "process": _series_result(
                process_values,
                unit="bytes",
                scope="whisperkey_process_working_set",
                error=process_error,
            ),
            "gpu": (
                _gpu_result(gpu_values, temperatures, gpu_scope, gpu_error)
                if self.gpu_enabled
                else {
                    "status": "not_applicable",
                    "scope": "none",
                    "unit": "bytes",
                    "baseline_vram_used_bytes": None,
                    "peak_vram_used_bytes": None,
                    "delta_vram_used_bytes": None,
                    "peak_temperature_c": None,
                    "error": None,
                }
            ),
            "privacy": {
                "audio_sampled": False,
                "transcript_sampled": False,
                "uploaded": False,
            },
        }

    def _run(self) -> None:
        self._sample_once()
        while not self._stop_event.wait(self.interval_seconds):
            self._sample_once()
        self._ended_at = time.monotonic()

    def _sample_once(self) -> None:
        process_value = None
        gpu_value = None
        temperature = None
        gpu_scope = None
        process_error = None
        gpu_error = None
        try:
            process_value = self.process_probe()
        except Exception as exc:
            process_error = str(exc)
        if self.gpu_enabled:
            try:
                gpu = self.gpu_probe() or {}
                gpu_value = gpu.get("vram_used_bytes")
                temperature = gpu.get("temperature_c")
                gpu_scope = gpu.get("scope")
                gpu_error = gpu.get("error")
            except Exception as exc:
                gpu_error = str(exc)
        with self._lock:
            self._samples += 1
            if isinstance(process_value, int) and process_value >= 0:
                self._process_values.append(process_value)
            elif process_error and not self._process_error:
                self._process_error = process_error
            if isinstance(gpu_value, int) and gpu_value >= 0:
                self._gpu_values.append(gpu_value)
            if isinstance(temperature, int):
                self._temperatures.append(temperature)
            if gpu_scope:
                self._gpu_scope = str(gpu_scope)
            if gpu_error and not self._gpu_error:
                self._gpu_error = str(gpu_error)


def probe_process_working_set() -> int | None:
    if os.name != "nt":
        return None

    class ProcessMemoryCountersEx(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("page_fault_count", ctypes.c_ulong),
            ("peak_working_set_size", ctypes.c_size_t),
            ("working_set_size", ctypes.c_size_t),
            ("quota_peak_paged_pool_usage", ctypes.c_size_t),
            ("quota_paged_pool_usage", ctypes.c_size_t),
            ("quota_peak_non_paged_pool_usage", ctypes.c_size_t),
            ("quota_non_paged_pool_usage", ctypes.c_size_t),
            ("pagefile_usage", ctypes.c_size_t),
            ("peak_pagefile_usage", ctypes.c_size_t),
            ("private_usage", ctypes.c_size_t),
        ]

    counters = ProcessMemoryCountersEx()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    get_memory_info = getattr(kernel32, "K32GetProcessMemoryInfo", psapi.GetProcessMemoryInfo)
    get_memory_info.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ProcessMemoryCountersEx),
        ctypes.c_ulong,
    ]
    get_memory_info.restype = ctypes.c_int
    process = kernel32.GetCurrentProcess()
    if not get_memory_info(
        process,
        ctypes.byref(counters),
        counters.cb,
    ):
        return None
    return int(counters.working_set_size)


def probe_nvidia_gpu(runner=subprocess.run) -> dict:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = runner(
            [
                "nvidia-smi",
                "--query-gpu=index,memory.used,temperature.gpu",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
            creationflags=flags,
        )
        first = next((line for line in result.stdout.splitlines() if line.strip()), "")
        fields = [field.strip() for field in first.split(",")]
        if len(fields) < 3:
            raise ValueError("nvidia-smi no devolvió índice, VRAM y temperatura")
        return {
            "gpu_index": int(fields[0]),
            "vram_used_bytes": int(fields[1]) * 1024 * 1024,
            "temperature_c": int(fields[2]),
            "scope": "gpu_total_used",
            "error": None,
        }
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {
            "gpu_index": None,
            "vram_used_bytes": None,
            "temperature_c": None,
            "scope": "gpu_total_used",
            "error": str(exc),
        }


def _series_result(values: list[int], *, unit: str, scope: str, error: str | None) -> dict:
    if not values:
        return {
            "status": "unavailable",
            "scope": scope,
            "unit": unit,
            "baseline_bytes": None,
            "peak_bytes": None,
            "delta_bytes": None,
            "error": error or "Windows no devolvió la métrica",
        }
    baseline = values[0]
    peak = max(values)
    return {
        "status": "measured",
        "scope": scope,
        "unit": unit,
        "baseline_bytes": baseline,
        "peak_bytes": peak,
        "delta_bytes": max(0, peak - baseline),
        "error": error,
    }


def _gpu_result(
    values: list[int],
    temperatures: list[int],
    scope: str | None,
    error: str | None,
) -> dict:
    if not values:
        return {
            "status": "unavailable",
            "scope": scope or "gpu_total_used",
            "unit": "bytes",
            "baseline_vram_used_bytes": None,
            "peak_vram_used_bytes": None,
            "delta_vram_used_bytes": None,
            "peak_temperature_c": max(temperatures) if temperatures else None,
            "error": error or "NVIDIA no devolvió la métrica",
        }
    baseline = values[0]
    peak = max(values)
    return {
        "status": "measured",
        "scope": scope or "gpu_total_used",
        "unit": "bytes",
        "baseline_vram_used_bytes": baseline,
        "peak_vram_used_bytes": peak,
        "delta_vram_used_bytes": max(0, peak - baseline),
        "peak_temperature_c": max(temperatures) if temperatures else None,
        "error": error,
    }
