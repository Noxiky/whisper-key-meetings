import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from whisper_key.application import AcceptanceService
from whisper_key.ui.controller import AppController
from whisper_key.ui.shell import MainWindow
from whisper_key.ui.theme import TokenStore, build_stylesheet
from whisper_key.ui.widgets import RecordingMiniController


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


def test_tokens_resolve_three_tiers_and_both_themes():
    dark = TokenStore(theme="dark")
    light = TokenStore(theme="light")

    assert dark.get("component.button.primary.background") == dark.get("semantic.color.action.primary")
    assert light.get("component.button.primary.background") == light.get("semantic.color.action.primary")
    assert dark.get("semantic.color.surface.canvas") != light.get("semantic.color.surface.canvas")
    assert "QPushButton" in build_stylesheet("dark")


def test_recording_mini_controller_is_compact_and_exposes_region_capture(qt_app):
    mini = RecordingMiniController()
    mini.show_recording({"status": "recording", "display_elapsed_ms": 65_000})
    qt_app.processEvents()

    assert mini.width() == mini.COMPACT_WIDTH
    assert mini.activity.isVisible()
    assert mini.status_label.text() == "GRABANDO"
    assert mini.timer_label.text() == "00:01:05"
    assert mini.controls.isHidden()
    assert mini.controls_toggle.accessibleName() == "Mostrar controles de grabación"

    mini.controls_toggle.click()
    qt_app.processEvents()

    assert mini.width() == mini.EXPANDED_WIDTH
    assert not mini.controls.isHidden()
    assert not mini.region_button.isHidden()
    assert mini.controls_toggle.accessibleName() == "Ocultar controles de grabación"

    requested = []
    mini.region_capture_requested.connect(lambda: requested.append(True))
    mini.region_button.click()
    assert requested == [True]
    mini.close()


def test_main_window_renders_real_recording_state_without_hardware(qt_app, tmp_path):
    controller = AppController(tmp_path, tmp_path / "models")
    window = MainWindow(controller)
    window.resize(1100, 700)
    window.show()
    state = {
        "mode": "learning",
        "status": "recording",
        "display_elapsed_ms": 65_000,
        "busy": False,
        "health": {
            "mic": {"status": "active", "detail": "ok", "fatal": False},
            "system": {"status": "unavailable", "detail": "no loopback", "fatal": False},
        },
        "persistence_backlog": 1,
        "transcription_backlog": 3,
    }
    window._on_session_state(state)
    window.session.set_transcript("MIC  00:00\nTexto final")
    window.session.set_provisional("Texto todavía provisional")
    qt_app.processEvents()

    assert window.session.timer.text() == "00:01:05"
    assert window.session.status.text() == "Grabando"
    assert window.session.mic.text() == "MIC · activo"
    assert "unavailable" in window.session.system.text()
    assert not window.session.provisional.isHidden()
    assert all(card.accessibleName() for card in window.home.mode_cards)
    imported = []
    import_source = tmp_path / "lesson.wav"
    import_source.write_bytes(b"test")
    window.home.audio_import_requested.connect(imported.append)
    assert window.home._submit_audio_path(str(import_source))
    assert imported == [str(import_source)]
    window.home.set_import_state("transcribing", "Transcribiendo", 42)
    assert not window.home.import_progress.isHidden()
    assert window.home.import_progress.value() == 42
    assert window.models.install.isEnabled()
    assert "47 MB" in window.models.install.text()
    window.models.set_model_catalog(
        [
            {
                "key": "large-v3-turbo",
                "label": "Large-V3-Turbo (1.5GB)",
                "cached": True,
                "cache_state": "ready",
                "multilingual": True,
            }
        ],
        "large-v3-turbo",
    )
    window.models.set_model_state("ready", "Modelo listo")
    window.models.set_model_inspection(
        {
            "kind": "preflight",
            "model_key": "large-v3-turbo",
            "allowed": True,
            "cache": {"state": "ready", "detail": "Caché completa"},
            "disk_free_bytes": 20 * 1024**3,
            "disk_required_bytes": 0,
            "memory_kind": "VRAM",
            "memory_free_bytes": 6 * 1024**3,
            "memory_required_bytes": 4 * 1024**3,
        }
    )
    assert window.models.verify_model.isEnabled()
    assert "VRAM libre" in window.models.preflight_detail.text()
    window.settings_page.set_retention(
        {
            "dictation": "all",
            "meeting": "all",
            "learning": "marker_context",
            "reading": "until_verified",
            "idea": "none",
            "marker_context_before_ms": 45000,
            "marker_context_after_ms": 15000,
        }
    )
    assert window.settings_page.retention_fields["learning"].currentData() == "marker_context"
    assert window.settings_page.context_before.value() == 45
    assert window.settings_page.context_after.value() == 15
    window.settings_page.set_audio_routes(
        {
            "input_device": 99,
            "input_device_name": "Studio MIC",
            "system_audio_device": "Speakers",
        }
    )
    window.settings_page.set_audio_diagnostics(
        {
            "state": "complete",
            "summary": {
                "status": "attention",
                "title": "Revisa una de las rutas",
                "detail": "Habla al MIC y reproduce audio para comprobar SYS.",
            },
            "mic": {
                "status": "active",
                "device": "Studio MIC",
                "peak_dbfs": -18.2,
                "detail": "Se recibió señal.",
                "error": None,
            },
            "system": {
                "status": "silent",
                "device": "Speakers",
                "peak_dbfs": -120.0,
                "detail": "La ruta abrió, pero no recibió señal.",
                "error": None,
            },
            "system_outputs": [{"name": "Speakers", "default": True}],
            "devices": [
                {
                    "device_id": 4,
                    "name": "Studio MIC",
                    "hostapi": "Windows WASAPI",
                    "input_channels": 1,
                    "output_channels": 0,
                    "sample_rate": 16000,
                    "default_input": True,
                    "default_output": False,
                },
                {
                    "device_id": 7,
                    "name": "Speakers",
                    "hostapi": "Windows WASAPI",
                    "input_channels": 0,
                    "output_channels": 2,
                    "sample_rate": 48000,
                    "default_input": False,
                    "default_output": True,
                },
            ],
        }
    )
    assert window.settings_page.diagnostic_source_pills["mic"].text() == "Señal recibida"
    assert window.settings_page.diagnostic_source_pills["system"].text() == "Silencio"
    assert window.settings_page.diagnostic_devices.topLevelItemCount() == 2
    assert "pico -18.2 dBFS" in window.settings_page.diagnostic_source_details["mic"].text()
    assert window.settings_page.mic_route.currentData() == 4
    assert window.settings_page.system_route.currentData() == "Speakers"
    window.settings_page.set_settings_status("success", "Audio guardado · próxima captura")
    assert window.settings_page.audio_route_status.text().startswith("Audio guardado")
    window.settings_page.set_settings_status("success", "Diagnóstico · paquete privado creado")
    assert window.settings_page.diagnostics_bundle_status.text().startswith("Diagnóstico")

    window.dictation.set_history(
        [
            {
                "dictation_id": "one",
                "created_at": "2026-07-17T10:00:00Z",
                "duration_ms": 1500,
                "text": "idea recuperable",
                "delivery": "clipboard",
            }
        ]
    )
    window.session.set_clean_document("# Documento limpio\n\nTexto legible")
    window.session.set_speakers(
        {
            "revision": 2,
            "speakers": [{"speaker_id": "speaker_1", "display_name": "Profesora Ana"}],
            "assignments": [],
        }
    )
    window._on_dictation_state("recording", "Escuchando", "")
    qt_app.processEvents()

    assert window.dictation.history_count.text() == "1 dictado"
    assert "Documento limpio" in window.session.clean_preview.toPlainText()
    assert window.session.speakers.topLevelItemCount() == 1
    assert window.session._speaker_fields["speaker_1"].text() == "Profesora Ana"
    assert window.mini.isVisible()
    assert window.mini.status_label.text() == "DICTANDO"

    session_folder = tmp_path / "opened-session"
    session_folder.mkdir()
    (session_folder / "transcript.raw.md").write_text("# Literal\n\nEvidencia", encoding="utf-8")
    (session_folder / "transcript.clean.md").write_text("# Limpio\n\nLectura", encoding="utf-8")
    (session_folder / "handoff").mkdir()
    (session_folder / "handoff" / "handoff.json").write_text(
        json.dumps(
            {
                "inputs": [{"path": "transcript.raw.md"}, {"path": "transcript.clean.md"}],
                "attachments": [{"path": "attachments/screen.png"}],
            }
        ),
        encoding="utf-8",
    )
    (session_folder / "handoff.md").write_text("# Handoff", encoding="utf-8")
    event = {
        "type": "processing_job",
        "payload": {"job": "clean", "status": "complete", "output": "transcript.clean.md"},
    }
    (session_folder / "timeline.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
    window.session.prepare_for_open()
    window.session.load_documents(str(session_folder))

    assert "Evidencia" in window.session.literal.toPlainText()
    assert "Lectura" in window.session.clean_preview.toPlainText()
    assert window.session.jobs.topLevelItemCount() == 1
    assert "2 entradas · 1 adjuntos" in window.session.handoff_summary.text()
    window.session.update_state({"mode": "learning", "status": "completed", "busy": False})
    assert window.session.prepare_handoff.isEnabled()
    assert window.session.verify_handoff.isEnabled()
    assert window.session.copy_handoff.isEnabled()
    window.session.update_processing_job("handoff_verify", "failed", "SHA-256 changed")
    assert "cambió o está incompleto" in window.session.handoff_summary.text()

    requested = []
    window.library.session_requested.connect(requested.append)
    window.library.set_sessions(
        [
            {
                "session_id": "session-to-open",
                "title": "Sesión reabrible",
                "mode": "learning",
                "status": "completed",
            }
        ]
    )
    selected = window.library.list.topLevelItem(0)
    window.library.list.itemDoubleClicked.emit(selected, 0)
    assert requested == ["session-to-open"]

    window.mini.hide()
    window.resize(980, 680)
    window.pages.setCurrentWidget(window.settings_page)
    qt_app.processEvents()
    assert window.settings_page.scroll.horizontalScrollBar().maximum() == 0
    window.close()


def test_acceptance_page_exposes_fixed_language_benchmark_and_metrics(qt_app, tmp_path):
    service = AcceptanceService(tmp_path / "acceptance")
    report = service.evaluate_dictation(
        "p7_russian",
        {
            "dictation_id": "ru-one",
            "audio_path": "audio/ru-one.wav",
            "duration_ms": 4200,
            "text": "Приложение точно сохраняет мои слова на русском языке без перевода.",
            "transcription": {
                "detected_language": "ru",
                "language_probability": 0.98,
                "real_time_factor": 0.31,
                "model": "large-v3-turbo",
                "device": "cuda",
                "compute_type": "float16",
                "model_load_ms": 2200,
                "inference_index": 1,
                "cold_inference": True,
                "performance": {
                    "process": {
                        "status": "measured",
                        "peak_bytes": 2 * 1024**3,
                        "delta_bytes": 96 * 1024**2,
                    },
                    "gpu": {
                        "status": "measured",
                        "scope": "gpu_total_used",
                        "peak_vram_used_bytes": 5 * 1024**3,
                        "delta_vram_used_bytes": 384 * 1024**2,
                        "peak_temperature_c": 69,
                    },
                },
            },
        },
    )
    controller = AppController(tmp_path / "library", tmp_path / "models")
    window = MainWindow(controller)
    window.acceptance.set_report(report)

    scenario_items = []
    for group_index in range(window.acceptance.tree.topLevelItemCount()):
        group = window.acceptance.tree.topLevelItem(group_index)
        scenario_items.extend(group.child(index) for index in range(group.childCount()))
    russian = next(item for item in scenario_items if item.data(0, Qt.ItemDataRole.UserRole) == "p7_russian")
    window.acceptance.tree.setCurrentItem(russian)
    qt_app.processEvents()

    assert len(scenario_items) == 24
    assert "Приложение" in window.acceptance.reference.toPlainText()
    assert "WER 0.0%" in window.acceptance.metrics.text()
    assert "Idioma ru" in window.acceptance.metrics.text()
    assert "RAM 2.00 GiB" in window.acceptance.metrics.text()
    assert "VRAM total 5.00 GiB" in window.acceptance.metrics.text()
    assert "GPU 69 °C" in window.acceptance.metrics.text()
    assert not window.acceptance.benchmark.isHidden()
    window.resize(980, 680)
    qt_app.processEvents()
    assert window.acceptance.detail_scroll.horizontalScrollBar().maximum() == 0
    assert window.acceptance.detail_scroll.verticalScrollBar().maximum() > 0
    window.close()
