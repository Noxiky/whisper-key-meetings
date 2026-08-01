import json

import pytest

from whisper_key.application import AcceptanceService, character_error_rate, word_error_rate


def entry(text, language, *, rtf=0.5):
    return {
        "dictation_id": "11111111-1111-4111-8111-111111111111",
        "text": text,
        "audio_path": "audio/2026/07/example.wav",
        "duration_ms": 2000,
        "transcription": {
            "detected_language": language,
            "language_probability": 0.98,
            "processing_ms": 1000,
            "audio_duration_ms": 2000,
            "real_time_factor": rtf,
            "model": "large-v3-turbo",
            "device": "cuda",
            "compute_type": "float16",
            "model_load_ms": 1820,
            "inference_index": 1,
            "cold_inference": True,
            "script_retry": False,
            "performance": {
                "process": {
                    "status": "measured",
                    "peak_bytes": 2 * 1024**3,
                    "delta_bytes": 128 * 1024**2,
                },
                "gpu": {
                    "status": "measured",
                    "scope": "gpu_total_used",
                    "peak_vram_used_bytes": 6 * 1024**3,
                    "delta_vram_used_bytes": 512 * 1024**2,
                    "peak_temperature_c": 71,
                },
            },
        },
    }


def scenario(report, scenario_id):
    return next(item for item in report["scenarios"] if item["scenario_id"] == scenario_id)


def test_error_rates_ignore_case_punctuation_and_spacing():
    assert word_error_rate("Hola,   Mundo.", "hola mundo") == 0
    assert character_error_rate("Hola,   Mundo.", "hola mundo") == 0
    assert word_error_rate("uno dos tres", "uno tres") == pytest.approx(1 / 3)


def test_exact_spanish_benchmark_persists_metrics_and_valid_schema(tmp_path, schema_validator):
    service = AcceptanceService(tmp_path / "acceptance")
    report = service.evaluate_dictation(
        "p7_spanish",
        entry("La aplicación conserva exactamente mis palabras en español sin traducirlas.", "es"),
    )

    result = scenario(report, "p7_spanish")["result"]
    assert result["status"] == "pass"
    assert result["evidence"]["word_error_rate"] == 0
    assert result["evidence"]["real_time_factor"] == 0.5
    assert result["evidence"]["model_load_ms"] == 1820
    assert result["evidence"]["cold_inference"] is True
    assert result["evidence"]["performance"]["gpu"]["peak_temperature_c"] == 71
    schema_validator("acceptance-report.schema.json").validate(report)
    reloaded = AcceptanceService(tmp_path / "acceptance").report()
    assert scenario(reloaded, "p7_spanish")["result"]["status"] == "pass"


def test_russian_translation_is_a_failure_even_when_detector_says_russian(tmp_path):
    service = AcceptanceService(tmp_path / "acceptance")
    report = service.evaluate_dictation(
        "p7_russian",
        entry("The application keeps my words in Russian without translating them.", "ru"),
    )

    evidence = scenario(report, "p7_russian")["result"]["evidence"]
    assert scenario(report, "p7_russian")["result"]["status"] == "fail"
    assert evidence["language_preserved"] is False


def test_code_switch_requires_both_latin_and_cyrillic_scripts(tmp_path):
    service = AcceptanceService(tmp_path / "acceptance")
    exact = "Hoy necesito review the plan и сохранить результат sin traducir."
    passed = service.evaluate_dictation("p7_code_switch", entry(exact, "es"))
    failed = service.evaluate_dictation(
        "p7_code_switch",
        entry("Hoy necesito review the plan and save the result without translating.", "en"),
    )

    assert scenario(passed, "p7_code_switch")["result"]["status"] == "pass"
    assert scenario(failed, "p7_code_switch")["result"]["status"] == "fail"


def test_manual_result_keeps_automatic_evidence_and_export_is_relative(tmp_path):
    service = AcceptanceService(tmp_path / "acceptance")
    service.evaluate_dictation(
        "p7_english",
        entry("The application keeps my exact words in English without translating them.", "en"),
    )
    report = service.record_manual("p7_english", "review", "Confirmar con otra toma")
    service.record_manual("p3_dual", "pass", "MIC y SYS separados")
    paths = service.export_paths()

    result = scenario(report, "p7_english")["result"]
    assert result["reviewed_by_user"] is True
    assert result["evidence"]["word_error_rate"] == 0
    markdown = service.markdown_path.read_text(encoding="utf-8")
    assert str(tmp_path) not in markdown
    assert paths["json"].endswith("acceptance-report.json")
    assert "RAM pico del proceso" in markdown
    assert "Temperatura GPU pico" in markdown
    event_lines = service.events_path.read_text(encoding="utf-8").splitlines()
    assert all(json.loads(line)["event_id"] for line in event_lines)


def test_automatic_engine_failure_is_not_attributed_to_user_review(tmp_path):
    service = AcceptanceService(tmp_path / "acceptance")

    report = service.record_automatic_failure("p7_spanish", "CUDA worker stopped")
    result = scenario(report, "p7_spanish")["result"]

    assert result["status"] == "fail"
    assert result["reviewed_by_user"] is False
    assert result["evidence"]["error"] == "CUDA worker stopped"
    assert "dictation_benchmark_error" in service.events_path.read_text(encoding="utf-8")


def test_acceptance_report_adds_new_hardware_scenarios_without_losing_results(tmp_path):
    root = tmp_path / "acceptance"
    service = AcceptanceService(root)
    service.record_manual("p3_dual", "pass", "rutas separadas")

    reloaded = AcceptanceService(root).report()

    assert len(reloaded["scenarios"]) == 24
    assert scenario(reloaded, "p3_dual")["result"]["status"] == "pass"
    assert scenario(reloaded, "p3_route_reconnect")["result"] is None
    assert scenario(reloaded, "p3_sleep_wake")["result"] is None
    assert scenario(reloaded, "p7_long_performance")["result"] is None
