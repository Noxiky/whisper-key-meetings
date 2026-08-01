from __future__ import annotations

import math
from datetime import UTC, datetime

import numpy as np

from whisper_key.infrastructure.audio_routes import resolve_input_device


class AudioDiagnosticsService:
    """Probe MIC/SYS briefly without persisting or transcribing captured samples."""

    SCHEMA_VERSION = 1

    def __init__(
        self,
        *,
        input_device=None,
        input_device_name: str | None = None,
        system_device: str = "default",
        duration_seconds: float = 1.5,
        sounddevice_module=None,
        soundcard_module=None,
    ):
        self.input_device = None if input_device in {None, "default"} else input_device
        self.input_device_name = str(input_device_name or "").strip() or None
        self.system_device = str(system_device or "default")
        self.duration_seconds = max(0.25, min(5.0, float(duration_seconds)))
        self._sounddevice = sounddevice_module
        self._soundcard = soundcard_module

    def run(self) -> dict:
        sd = self._sounddevice or self._load_sounddevice()
        devices, defaults = self._enumerate_devices(sd)
        mic = self._probe_microphone(sd)
        system, system_outputs = self._probe_system_audio()
        summary = self._summarize(mic, system)
        return {
            "schema_version": self.SCHEMA_VERSION,
            "created_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "duration_ms": round(self.duration_seconds * 1000),
            "privacy": {
                "audio_persisted": False,
                "transcribed": False,
                "uploaded": False,
            },
            "summary": summary,
            "defaults": defaults,
            "mic": mic,
            "system": system,
            "system_outputs": system_outputs,
            "devices": devices,
        }

    @staticmethod
    def _load_sounddevice():
        import sounddevice as sd

        return sd

    def _load_soundcard(self):
        if self._soundcard is not None:
            return self._soundcard
        import soundcard as sc

        return sc

    def _enumerate_devices(self, sd) -> tuple[list[dict], dict]:
        raw_devices = sd.query_devices()
        default_input, default_output = sd.default.device
        devices = []
        for index, raw in enumerate(raw_devices):
            input_channels = int(raw.get("max_input_channels", 0) or 0)
            output_channels = int(raw.get("max_output_channels", 0) or 0)
            if not input_channels and not output_channels:
                continue
            hostapi_index = raw.get("hostapi")
            try:
                hostapi = sd.query_hostapis(hostapi_index).get("name", "")
            except Exception:
                hostapi = ""
            devices.append(
                {
                    "device_id": int(raw.get("index", index)),
                    "name": str(raw.get("name", f"Dispositivo {index}")),
                    "hostapi": str(hostapi),
                    "input_channels": input_channels,
                    "output_channels": output_channels,
                    "sample_rate": round(float(raw.get("default_samplerate", 0) or 0)),
                    "default_input": int(raw.get("index", index)) == default_input,
                    "default_output": int(raw.get("index", index)) == default_output,
                }
            )
        return devices, {"input_device_id": default_input, "output_device_id": default_output}

    def _probe_microphone(self, sd) -> dict:
        try:
            device, detected_name = resolve_input_device(
                sd,
                self.input_device,
                self.input_device_name,
            )
            self.input_device_name = detected_name
            info = sd.query_devices(device, kind="input")
            sample_rate = round(float(info.get("default_samplerate", 16_000) or 16_000))
            frames = max(1, round(sample_rate * self.duration_seconds))
            audio = sd.rec(
                frames,
                samplerate=sample_rate,
                channels=1,
                dtype="float32",
                device=device,
                blocking=True,
            )
            return self._source_result(
                audio,
                device=str(info.get("name", "Micrófono predeterminado")),
                sample_rate=sample_rate,
                available=True,
            )
        except Exception as exc:
            return self._unavailable(str(exc))

    def _probe_system_audio(self) -> tuple[dict, list[dict]]:
        try:
            sc = self._load_soundcard()
            speakers = list(sc.all_speakers())
            default = sc.default_speaker()
            outputs = [
                {
                    "name": str(item.name),
                    "default": bool(default and item.name == default.name),
                }
                for item in speakers
            ]
            speaker = default
            if self.system_device.casefold() not in {"", "default"}:
                needle = self.system_device.casefold()
                speaker = next((item for item in speakers if needle in item.name.casefold()), default)
            if speaker is None:
                return self._unavailable("Windows no informó una salida de audio predeterminada"), outputs
            sample_rate = 48_000
            frames = max(1, round(sample_rate * self.duration_seconds))
            loopback = sc.get_microphone(id=str(speaker.name), include_loopback=True)
            with loopback.recorder(samplerate=sample_rate, channels=2) as recorder:
                audio = recorder.record(numframes=frames)
            return (
                self._source_result(
                    audio,
                    device=str(speaker.name),
                    sample_rate=sample_rate,
                    available=True,
                ),
                outputs,
            )
        except Exception as exc:
            return self._unavailable(str(exc)), []

    @classmethod
    def _source_result(cls, audio, *, device: str, sample_rate: int, available: bool) -> dict:
        samples = np.asarray(audio, dtype=np.float32)
        peak = float(np.max(np.abs(samples))) if samples.size else 0.0
        rms = float(np.sqrt(np.mean(np.square(samples)))) if samples.size else 0.0
        peak_dbfs = cls._dbfs(peak)
        rms_dbfs = cls._dbfs(rms)
        if peak < 1e-7:
            status = "silent"
            detail = "La ruta abrió correctamente, pero no recibió señal durante la prueba."
        elif peak_dbfs < -50:
            status = "quiet"
            detail = "La ruta recibió una señal muy baja; revisa volumen, mute o la salida elegida."
        else:
            status = "active"
            detail = "Se recibió una señal utilizable durante la prueba."
        return {
            "status": status,
            "available": available,
            "device": device,
            "sample_rate": sample_rate,
            "channels": int(samples.shape[1]) if samples.ndim > 1 else 1,
            "peak_dbfs": peak_dbfs,
            "rms_dbfs": rms_dbfs,
            "detail": detail,
            "error": None,
        }

    @staticmethod
    def _dbfs(value: float) -> float:
        if value <= 0:
            return -120.0
        return round(max(-120.0, 20 * math.log10(value)), 1)

    @staticmethod
    def _unavailable(error: str) -> dict:
        return {
            "status": "unavailable",
            "available": False,
            "device": None,
            "sample_rate": None,
            "channels": None,
            "peak_dbfs": None,
            "rms_dbfs": None,
            "detail": "No se pudo abrir esta ruta de audio.",
            "error": " ".join(error.split()),
        }

    @staticmethod
    def _summarize(mic: dict, system: dict) -> dict:
        statuses = {mic["status"], system["status"]}
        if statuses == {"active"}:
            return {
                "status": "pass",
                "title": "MIC y SYS reciben señal",
                "detail": "Las dos rutas están disponibles y mostraron nivel durante la muestra.",
            }
        if mic["status"] == "unavailable" and system["status"] == "unavailable":
            return {
                "status": "fail",
                "title": "No se pudo abrir el audio",
                "detail": "Revisa permisos, dispositivos predeterminados y aplicaciones que usan modo exclusivo.",
            }
        return {
            "status": "attention",
            "title": "Revisa una de las rutas",
            "detail": "Silencio no significa avería: habla al MIC y reproduce audio para comprobar SYS.",
        }
