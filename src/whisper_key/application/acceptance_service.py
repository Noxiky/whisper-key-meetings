from __future__ import annotations

import json
import os
import re
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from whisper_key.infrastructure.session_repository import atomic_write_text

SCENARIOS = (
    {
        "scenario_id": "p7_spanish",
        "gate": "P7",
        "category": "Idioma",
        "kind": "dictation_benchmark",
        "title": "Español sin traducción",
        "instructions": "Lee exactamente la frase y detén el dictado. Se medirán WER, CER, idioma y RTF.",
        "reference": "La aplicación conserva exactamente mis palabras en español sin traducirlas.",
        "expected_language": "es",
    },
    {
        "scenario_id": "p7_english",
        "gate": "P7",
        "category": "Idioma",
        "kind": "dictation_benchmark",
        "title": "English without translation",
        "instructions": "Read the phrase exactly, then stop Dictation. WER, CER, language and RTF are measured.",
        "reference": "The application keeps my exact words in English without translating them.",
        "expected_language": "en",
    },
    {
        "scenario_id": "p7_russian",
        "gate": "P7",
        "category": "Idioma",
        "kind": "dictation_benchmark",
        "title": "Русский без перевода",
        "instructions": "Прочитайте фразу точно и остановите диктовку. Проверяется кириллица и отсутствие перевода.",
        "reference": "Приложение точно сохраняет мои слова на русском языке без перевода.",
        "expected_language": "ru",
    },
    {
        "scenario_id": "p7_code_switch",
        "gate": "P7",
        "category": "Idioma",
        "kind": "dictation_benchmark",
        "title": "Cambio natural ES / EN / RU",
        "instructions": "Lee la frase completa sin separar artificialmente los idiomas.",
        "reference": "Hoy necesito review the plan и сохранить результат sin traducir.",
        "expected_language": "mixed",
    },
    {
        "scenario_id": "p7_long_performance",
        "gate": "P7",
        "category": "Rendimiento",
        "kind": "manual",
        "title": "Sesión larga y estabilidad CUDA",
        "instructions": (
            "Graba al menos 30 minutos con actividad real. Anota calentamiento, pico RAM/VRAM, "
            "temperatura si está disponible, RTF sostenido y cualquier deriva o segmento perdido."
        ),
    },
    {
        "scenario_id": "p0_dictation_paste",
        "gate": "P0",
        "category": "Dictado",
        "kind": "manual",
        "title": "Dictado y pegado",
        "instructions": "Dicta hacia una aplicación editable y confirma que el texto aparece una sola vez.",
    },
    {
        "scenario_id": "p0_auto_send",
        "gate": "P0",
        "category": "Dictado",
        "kind": "manual",
        "title": "Autoenvío",
        "instructions": "Prueba el atajo de autoenvío y confirma que no se envía antes de terminar la transcripción.",
    },
    {
        "scenario_id": "p0_cancel",
        "gate": "P0",
        "category": "Dictado",
        "kind": "manual",
        "title": "Cancelar dictado",
        "instructions": "Inicia y cancela un dictado; no debe pegar texto ni dejar el micrófono ocupado.",
    },
    {
        "scenario_id": "p3_mic_only",
        "gate": "P3",
        "category": "Audio",
        "kind": "manual",
        "title": "Reunión solo MIC",
        "instructions": "Usa Win+F11, habla y confirma audio/transcript MIC sin una pista SYS falsa.",
    },
    {
        "scenario_id": "p3_sys_only",
        "gate": "P3",
        "category": "Audio",
        "kind": "manual",
        "title": "Reunión solo SYS",
        "instructions": "Usa Win+F12 con audio reproducido y confirma transcript SYS sin capturar el micrófono.",
    },
    {
        "scenario_id": "p3_dual",
        "gate": "P3",
        "category": "Audio",
        "kind": "manual",
        "title": "Reunión MIC + SYS",
        "instructions": "Usa Win+F10 y confirma pistas separadas, orden temporal y continuidad si una fuente calla.",
    },
    {
        "scenario_id": "p3_silence_stop",
        "gate": "P3",
        "category": "Audio",
        "kind": "manual",
        "title": "Parada por silencio",
        "instructions": "Activa la parada automática y confirma cierre seguro sin perder el último segmento.",
    },
    {
        "scenario_id": "p3_route_reconnect",
        "gate": "P3",
        "category": "Audio",
        "kind": "manual",
        "title": "Desconectar y recuperar una ruta",
        "instructions": (
            "Durante MIC+SYS desconecta y vuelve a conectar una ruta USB. Confirma estado no disponible, "
            "reintento automático, regreso a activo y continuidad de la otra pista."
        ),
    },
    {
        "scenario_id": "p3_bluetooth_route",
        "gate": "P3",
        "category": "Audio",
        "kind": "manual",
        "title": "Cambio de ruta Bluetooth",
        "instructions": (
            "Conecta/desconecta el dispositivo Bluetooth durante una sesión y confirma que la aplicación "
            "no cambia una selección explícita por otra ruta ni pierde la sesión."
        ),
    },
    {
        "scenario_id": "p3_sleep_wake",
        "gate": "P3",
        "category": "Audio",
        "kind": "manual",
        "title": "Suspender y reactivar Windows",
        "instructions": (
            "Suspende y reactiva el PC durante una captura desechable. Confirma recuperación visible de "
            "las rutas o una sesión recuperable sin corrupción."
        ),
    },
    {
        "scenario_id": "p3_overload",
        "gate": "P3",
        "category": "Audio",
        "kind": "manual",
        "title": "Sobrecarga y recuperación",
        "instructions": (
            "Genera carga de GPU durante una captura y confirma que el audio durable continúa, la cola "
            "de transcripción muestra presión y el trabajo pendiente puede recuperarse."
        ),
    },
    {
        "scenario_id": "p5_pause_dictation_resume",
        "gate": "P5",
        "category": "Sesión",
        "kind": "manual",
        "title": "Pausa → dictado → continuar",
        "instructions": "Pausa Aprendizaje, usa Dictado corto y continúa; tiempos y etapas deben permanecer correctos.",
    },
    {
        "scenario_id": "p5_stage_continue",
        "gate": "P5",
        "category": "Sesión",
        "kind": "manual",
        "title": "Cerrar y continuar como nueva etapa",
        "instructions": "Cierra una etapa, reabre la sesión y continúa sin crear un documento distinto.",
    },
    {
        "scenario_id": "p5_screenshot_region",
        "gate": "P5",
        "category": "Captura",
        "kind": "manual",
        "title": "Captura de región",
        "instructions": "Captura una región durante audio y confirma feedback, miniatura y archivo portable.",
    },
    {
        "scenario_id": "p5_screenshot_window",
        "gate": "P5",
        "category": "Captura",
        "kind": "manual",
        "title": "Captura de ventana",
        "instructions": "Captura otra ventana y confirma imagen correcta o explicación de contenido protegido.",
    },
    {
        "scenario_id": "p5_screenshot_full",
        "gate": "P5",
        "category": "Captura",
        "kind": "manual",
        "title": "Pantalla completa / multimonitor",
        "instructions": "Captura la pantalla completa y revisa escala, monitor esperado y ausencia de cuadro negro.",
    },
    {
        "scenario_id": "p5_spoken_marker",
        "gate": "P5",
        "category": "Marcadores",
        "kind": "manual",
        "title": "Marcador con nota hablada",
        "instructions": "Crea un marcador hablado y confirma nota, timestamp y extracto ±30 segundos.",
    },
    {
        "scenario_id": "p5_diarization",
        "gate": "P5",
        "category": "Voces",
        "kind": "manual",
        "title": "Diarización y nombres",
        "instructions": "Instala diarización, procesa dos voces, renómbralas y prueba un reintento tras fallo.",
    },
    {
        "scenario_id": "p2_retention_restore",
        "gate": "P2",
        "category": "Retención",
        "kind": "manual",
        "title": "Retención y restauración",
        "instructions": "Con audio desechable, revisa el preview, aplica una política no predeterminada y restaura.",
    },
)


class AcceptanceService:
    SCHEMA_VERSION = 1
    STATUSES = {"not_run", "pass", "fail", "review", "skipped"}

    def __init__(self, root: Path, app_version: str = "0.9.0", clock=None):
        self.root = Path(root)
        self.report_path = self.root / "acceptance-report.json"
        self.markdown_path = self.root / "acceptance-report.md"
        self.events_path = self.root / "acceptance-events.jsonl"
        self.app_version = app_version
        self.clock = clock or (lambda: datetime.now(UTC))
        self.root.mkdir(parents=True, exist_ok=True)
        self._report = self._load_or_create()
        self._persist()

    def report(self) -> dict:
        return json.loads(json.dumps(self._report, ensure_ascii=False))

    def set_environment(self, values: dict) -> dict:
        allowed = {
            "model",
            "device",
            "compute_type",
            "app_version",
            "memory_kind",
            "memory_free_bytes",
            "memory_required_bytes",
            "model_load_ms",
        }
        self._report["environment"] = {key: values[key] for key in allowed if key in values}
        self._touch()
        self._append_event("environment", None, None, self._report["environment"])
        self._persist()
        return self.report()

    def record_manual(self, scenario_id: str, status: str, note: str = "") -> dict:
        if status not in self.STATUSES - {"not_run"}:
            raise ValueError(f"Unknown acceptance status: {status}")
        scenario = self._scenario(scenario_id)
        previous = scenario.get("result") or {}
        scenario["result"] = {
            "status": status,
            "recorded_at": self._now(),
            "reviewed_by_user": True,
            "note": " ".join(note.split()),
            "evidence": previous.get("evidence"),
        }
        self._touch()
        self._append_event("manual_result", scenario_id, status, scenario["result"])
        self._persist()
        return self.report()

    def evaluate_dictation(self, scenario_id: str, entry: dict) -> dict:
        scenario = self._scenario(scenario_id)
        if scenario["kind"] != "dictation_benchmark":
            raise ValueError("Scenario is not a dictation benchmark")
        reference = scenario["reference"]
        hypothesis = str(entry.get("text", ""))
        wer = word_error_rate(reference, hypothesis)
        cer = character_error_rate(reference, hypothesis)
        metrics = entry.get("transcription") if isinstance(entry.get("transcription"), dict) else {}
        language_preserved = _language_preserved(
            scenario.get("expected_language"),
            hypothesis,
            metrics.get("detected_language"),
        )
        passed = bool(hypothesis) and wer <= 0.25 and cer <= 0.20 and language_preserved is True
        status = "pass" if passed else "fail"
        evidence = {
            "dictation_id": entry.get("dictation_id"),
            "audio_path": entry.get("audio_path"),
            "reference": reference,
            "transcript": hypothesis,
            "word_error_rate": round(wer, 4),
            "character_error_rate": round(cer, 4),
            "language_preserved": language_preserved,
            "detected_language": metrics.get("detected_language"),
            "language_probability": metrics.get("language_probability"),
            "processing_ms": metrics.get("processing_ms"),
            "audio_duration_ms": metrics.get("audio_duration_ms") or entry.get("duration_ms"),
            "real_time_factor": metrics.get("real_time_factor"),
            "model": metrics.get("model"),
            "device": metrics.get("device"),
            "compute_type": metrics.get("compute_type"),
            "model_load_ms": metrics.get("model_load_ms"),
            "inference_index": metrics.get("inference_index"),
            "cold_inference": metrics.get("cold_inference"),
            "script_retry": metrics.get("script_retry", False),
            "performance": metrics.get("performance"),
        }
        scenario["result"] = {
            "status": status,
            "recorded_at": self._now(),
            "reviewed_by_user": False,
            "note": "Evaluación automática de una frase de benchmark fija.",
            "evidence": evidence,
        }
        self._touch()
        self._append_event("dictation_benchmark", scenario_id, status, evidence)
        self._persist()
        return self.report()

    def record_automatic_failure(
        self,
        scenario_id: str,
        detail: str,
        *,
        performance: dict | None = None,
    ) -> dict:
        scenario = self._scenario(scenario_id)
        if scenario["kind"] != "dictation_benchmark":
            raise ValueError("Scenario is not a dictation benchmark")
        evidence = {
            "error": " ".join(detail.split()),
            "performance": performance,
        }
        scenario["result"] = {
            "status": "fail",
            "recorded_at": self._now(),
            "reviewed_by_user": False,
            "note": "El motor no completó la medición.",
            "evidence": evidence,
        }
        self._touch()
        self._append_event("dictation_benchmark_error", scenario_id, "fail", evidence)
        self._persist()
        return self.report()

    def export_paths(self) -> dict[str, str]:
        self._persist()
        return {"json": str(self.report_path), "markdown": str(self.markdown_path)}

    def _load_or_create(self) -> dict:
        stored = None
        if self.report_path.is_file():
            try:
                stored = json.loads(self.report_path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                stored = None
        now = self._now()
        previous = {
            item.get("scenario_id"): item.get("result")
            for item in (stored or {}).get("scenarios", [])
            if isinstance(item, dict)
        }
        scenarios = []
        for definition in SCENARIOS:
            item = dict(definition)
            item["result"] = previous.get(item["scenario_id"])
            scenarios.append(item)
        return {
            "schema_version": self.SCHEMA_VERSION,
            "report_id": (stored or {}).get("report_id") or str(uuid4()),
            "created_at": (stored or {}).get("created_at") or now,
            "updated_at": now,
            "app_version": self.app_version,
            "environment": (stored or {}).get("environment", {}),
            "scenarios": scenarios,
            "summary": {},
        }

    def _persist(self) -> None:
        counts = {status: 0 for status in self.STATUSES}
        for scenario in self._report["scenarios"]:
            result = scenario.get("result")
            counts[result.get("status", "not_run") if result else "not_run"] += 1
        self._report["summary"] = {"total": len(self._report["scenarios"]), **counts}
        atomic_write_text(
            self.report_path,
            json.dumps(self._report, ensure_ascii=False, indent=2) + "\n",
        )
        atomic_write_text(self.markdown_path, self._render_markdown())

    def _render_markdown(self) -> str:
        summary = self._report["summary"]
        lines = [
            "# WhisperKey · aceptación Windows",
            "",
            f"Actualizado: {self._report['updated_at']}",
            "",
            "Este reporte contiene frases de prueba fijas y resultados elegidos explícitamente. "
            "WhisperKey no lo envía automáticamente.",
            "",
            "## Resumen",
            "",
            f"- Aprobadas: {summary['pass']} / {summary['total']}",
            f"- Fallidas: {summary['fail']}",
            f"- Para revisar: {summary['review']}",
            f"- Sin ejecutar: {summary['not_run']}",
            "",
        ]
        for scenario in self._report["scenarios"]:
            result = scenario.get("result") or {"status": "not_run"}
            lines.extend(
                [
                    f"## {scenario['gate']} · {scenario['title']}",
                    "",
                    f"Estado: **{result['status']}**",
                    "",
                ]
            )
            evidence = result.get("evidence") or {}
            if evidence:
                performance = evidence.get("performance") or {}
                process = performance.get("process") or {}
                gpu = performance.get("gpu") or {}
                lines.extend(
                    [
                        f"- WER: {evidence.get('word_error_rate', '—')}",
                        f"- CER: {evidence.get('character_error_rate', '—')}",
                        f"- Idioma: {evidence.get('detected_language') or '—'}",
                        f"- RTF: {evidence.get('real_time_factor', '—')}",
                        f"- Carga del modelo (ms): {evidence.get('model_load_ms', '—')}",
                        f"- Índice de inferencia: {evidence.get('inference_index', '—')}",
                        f"- Primera inferencia: {evidence.get('cold_inference', '—')}",
                        f"- RAM pico del proceso (bytes): {process.get('peak_bytes', '—')}",
                        f"- VRAM pico GPU total (bytes): {gpu.get('peak_vram_used_bytes', '—')}",
                        f"- Temperatura GPU pico (°C): {gpu.get('peak_temperature_c', '—')}",
                        f"- Texto: {evidence.get('transcript') or '—'}",
                        "",
                    ]
                )
            if result.get("note"):
                lines.extend([f"Nota: {result['note']}", ""])
        return "\n".join(lines).rstrip() + "\n"

    def _scenario(self, scenario_id: str) -> dict:
        for scenario in self._report["scenarios"]:
            if scenario["scenario_id"] == scenario_id:
                return scenario
        raise KeyError(scenario_id)

    def _append_event(self, kind: str, scenario_id: str | None, status: str | None, payload: dict) -> None:
        event = {
            "event_id": str(uuid4()),
            "occurred_at": self._now(),
            "kind": kind,
            "scenario_id": scenario_id,
            "status": status,
            "payload": payload,
        }
        with self.events_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _touch(self) -> None:
        self._report["updated_at"] = self._now()

    def _now(self) -> str:
        value = self.clock()
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def word_error_rate(reference: str, hypothesis: str) -> float:
    expected = _normalized_words(reference)
    actual = _normalized_words(hypothesis)
    if not expected:
        return 0.0 if not actual else 1.0
    return _edit_distance(expected, actual) / len(expected)


def character_error_rate(reference: str, hypothesis: str) -> float:
    expected = list(" ".join(_normalized_words(reference)))
    actual = list(" ".join(_normalized_words(hypothesis)))
    if not expected:
        return 0.0 if not actual else 1.0
    return _edit_distance(expected, actual) / len(expected)


def _normalized_words(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    cleaned = "".join(character if character.isalnum() else " " for character in normalized)
    return re.findall(r"\S+", cleaned)


def _edit_distance(expected: list, actual: list) -> int:
    previous = list(range(len(actual) + 1))
    for expected_index, expected_value in enumerate(expected, 1):
        current = [expected_index]
        for actual_index, actual_value in enumerate(actual, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[actual_index] + 1,
                    previous[actual_index - 1] + (expected_value != actual_value),
                )
            )
        previous = current
    return previous[-1]


def _language_preserved(expected: str | None, text: str, detected: str | None) -> bool | None:
    letters = [character for character in text if character.isalpha()]
    if not letters:
        return False
    cyrillic = sum("\u0400" <= character <= "\u052f" for character in letters)
    latin = sum(("a" <= character.casefold() <= "z") or ("\u00c0" <= character <= "\u024f") for character in letters)
    if expected == "ru":
        return detected == "ru" and cyrillic / len(letters) >= 0.50
    if expected in {"es", "en"}:
        return detected == expected
    if expected == "mixed":
        return cyrillic > 0 and latin > 0
    return None
