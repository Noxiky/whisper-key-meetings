"""Render deterministic WhisperKey UI previews without touching audio hardware."""

import sys
from pathlib import Path

from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from whisper_key.ui.controller import AppController  # noqa: E402
from whisper_key.ui.shell import MainWindow  # noqa: E402
from whisper_key.ui.theme import build_stylesheet  # noqa: E402


def render(output: Path) -> None:
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("WhisperKey UI Preview")
    app.setQuitOnLastWindowClosed(True)
    app.setStyleSheet(build_stylesheet("dark"))
    controller = AppController(output.parent / "preview-library", output.parent / "preview-models")
    window = MainWindow(controller)
    window.resize(1320, 820)
    window._on_model_state("ready", "Modelo listo · large-v3-turbo · CUDA")
    window.home.set_sessions(
        [
            {
                "title": "Revisión semanal de producto",
                "mode": "meeting",
                "status": "completed",
                "updated_at": "2026-07-17T15:42:00Z",
            },
            {
                "title": "Curso de sistemas distribuidos · etapa 2",
                "mode": "learning",
                "status": "paused",
                "updated_at": "2026-07-17T11:08:00Z",
            },
            {
                "title": "Idea: memoria de contexto personal",
                "mode": "idea",
                "status": "recoverable",
                "updated_at": "2026-07-16T22:15:00Z",
            },
        ]
    )
    window.show()
    for _ in range(8):
        app.processEvents()
    output.parent.mkdir(parents=True, exist_ok=True)
    window.grab().save(str(output))
    window.pages.setCurrentWidget(window.models)
    window.models.set_model_catalog(
        [
            {
                "key": "large-v3-turbo",
                "label": "Large-V3-Turbo (1.5GB)",
                "cached": True,
                "cache_state": "ready",
                "multilingual": True,
            },
            {
                "key": "medium",
                "label": "Medium (1.4GB)",
                "cached": False,
                "cache_state": "missing",
                "multilingual": True,
            },
            {
                "key": "medium.en",
                "label": "Medium.En (English, 1.5GB)",
                "cached": False,
                "cache_state": "missing",
                "multilingual": False,
            },
        ],
        "large-v3-turbo",
    )
    window.models.set_model_state("ready", "Modelo listo · large-v3-turbo · CUDA")
    window.models.set_model_inspection(
        {
            "kind": "preflight",
            "model_key": "large-v3-turbo",
            "allowed": True,
            "cache": {
                "state": "ready",
                "detail": "Archivos requeridos presentes y tamaños coherentes",
            },
            "disk_free_bytes": 128 * 1024**3,
            "disk_required_bytes": 0,
            "memory_kind": "VRAM",
            "memory_free_bytes": 6.7 * 1024**3,
            "memory_required_bytes": 3.6 * 1024**3,
        }
    )
    controller.refresh_diarization_state()
    for _ in range(4):
        app.processEvents()
    models_output = output.with_name(f"{output.stem}-models{output.suffix}")
    window.grab().save(str(models_output))
    window.pages.setCurrentWidget(window.settings_page)
    window.settings_page.set_hotkeys(
        {
            "recording_hotkey": "ctrl+win",
            "stop_key": "ctrl",
            "meeting_hotkey": "f9",
            "meeting_continuous_hotkey": "win+f10",
            "meeting_mic_only_hotkey": "win+f11",
            "meeting_sys_only_hotkey": "win+f12",
        }
    )
    window.settings_page.set_retention(
        {
            "dictation": "all",
            "meeting": "all",
            "learning": "all",
            "reading": "all",
            "idea": "all",
            "marker_context_before_ms": 30000,
            "marker_context_after_ms": 30000,
        }
    )
    for _ in range(4):
        app.processEvents()
    settings_output = output.with_name(f"{output.stem}-settings{output.suffix}")
    window.grab().save(str(settings_output))
    window.settings_page.set_audio_diagnostics(
        {
            "state": "complete",
            "summary": {
                "status": "attention",
                "title": "MIC listo · reproduce audio para SYS",
                "detail": "El micrófono recibió señal; la salida abrió correctamente pero estuvo en silencio.",
            },
            "mic": {
                "status": "active",
                "device": "Microphone (USB Audio Device)",
                "peak_dbfs": -17.8,
                "detail": "Se recibió una señal utilizable durante la prueba.",
                "error": None,
            },
            "system": {
                "status": "silent",
                "device": "Speakers (Realtek(R) Audio)",
                "peak_dbfs": -120.0,
                "detail": "La ruta abrió correctamente, pero no recibió señal durante la prueba.",
                "error": None,
            },
            "system_outputs": [{"name": "Speakers (Realtek(R) Audio)", "default": True}],
            "devices": [
                {
                    "device_id": 4,
                    "name": "Microphone (USB Audio Device)",
                    "hostapi": "Windows WASAPI",
                    "input_channels": 1,
                    "output_channels": 0,
                    "sample_rate": 48000,
                    "default_input": True,
                    "default_output": False,
                },
                {
                    "device_id": 7,
                    "name": "Speakers (Realtek(R) Audio)",
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
    window.settings_page.scroll.verticalScrollBar().setValue(window.settings_page.scroll.verticalScrollBar().maximum())
    for _ in range(4):
        app.processEvents()
    diagnostics_output = output.with_name(f"{output.stem}-diagnostics{output.suffix}")
    window.grab().save(str(diagnostics_output))
    window.resize(980, 680)
    for _ in range(4):
        app.processEvents()
    diagnostics_compact = output.with_name(f"{output.stem}-diagnostics-compact{output.suffix}")
    window.grab().save(str(diagnostics_compact))
    window.resize(1320, 820)
    window.settings_page.scroll.verticalScrollBar().setValue(0)
    window.pages.setCurrentWidget(window.dictation)
    window.dictation.set_history(
        [
            {
                "created_at": "2026-07-17T15:51:00Z",
                "duration_ms": 18_200,
                "text": "Esta idea se conserva en el historial y puede volver a copiarse.",
                "delivery": "clipboard",
            },
            {
                "created_at": "2026-07-17T15:47:00Z",
                "duration_ms": 8_400,
                "text": "Эта идея сохранена на русском языке.",
                "delivery": "pasted",
            },
        ]
    )
    for _ in range(4):
        app.processEvents()
    dictation_output = output.with_name(f"{output.stem}-dictation{output.suffix}")
    window.grab().save(str(dictation_output))
    acceptance_report = controller.acceptance.set_environment(
        {
            "app_version": "0.9.0",
            "model": "large-v3-turbo",
            "device": "cuda",
            "compute_type": "float16",
            "model_load_ms": 2140,
        }
    )
    acceptance_report = controller.acceptance.evaluate_dictation(
        "p7_spanish",
        {
            "dictation_id": "preview-es",
            "audio_path": "audio/preview-es.wav",
            "duration_ms": 5100,
            "text": "La aplicación conserva exactamente mis palabras en español sin traducirlas.",
            "transcription": {
                "detected_language": "es",
                "language_probability": 0.97,
                "processing_ms": 1480,
                "audio_duration_ms": 5100,
                "real_time_factor": 0.29,
                "model": "large-v3-turbo",
                "device": "cuda",
                "compute_type": "float16",
                "model_load_ms": 2140,
                "inference_index": 1,
                "cold_inference": True,
                "performance": {
                    "process": {
                        "status": "measured",
                        "peak_bytes": 2350 * 1024**2,
                        "delta_bytes": 142 * 1024**2,
                    },
                    "gpu": {
                        "status": "measured",
                        "scope": "gpu_total_used",
                        "peak_vram_used_bytes": 5890 * 1024**2,
                        "delta_vram_used_bytes": 624 * 1024**2,
                        "peak_temperature_c": 68,
                    },
                },
            },
        },
    )
    window.acceptance.set_report(acceptance_report)
    window.pages.setCurrentWidget(window.acceptance)
    for _ in range(4):
        app.processEvents()
    acceptance_output = output.with_name(f"{output.stem}-acceptance{output.suffix}")
    window.grab().save(str(acceptance_output))
    window.resize(980, 680)
    for _ in range(4):
        app.processEvents()
    acceptance_compact = output.with_name(f"{output.stem}-acceptance-compact{output.suffix}")
    window.grab().save(str(acceptance_compact))
    window.resize(1320, 820)
    window.pages.setCurrentWidget(window.session)
    window.session.title.setText("Clase de arquitectura · etapa 2")
    window._on_session_state(
        {
            "mode": "learning",
            "status": "recording",
            "display_elapsed_ms": 3_724_000,
            "busy": False,
            "health": {
                "mic": {"status": "active", "detail": "Audio persisted and queued", "fatal": False},
                "system": {"status": "active", "detail": "Audio persisted and queued", "fatal": False},
            },
            "persistence_backlog": 0,
            "transcription_backlog": 2,
        }
    )
    window.session.set_transcript(
        "SYS  61:45\n"
        "La consistencia eventual no significa que el sistema ignore el orden; significa que convergerá.\n\n"
        "MIC  61:52\n"
        "Entonces aquí la pregunta importante es qué garantía observa el usuario durante la convergencia.\n\n"
        "SYS  62:01\n"
        "Exacto. Debemos distinguir la garantía interna de la experiencia visible."
    )
    window.session.set_markers(
        [
            {"at_ms": 3_690_000, "kind": "important", "note": "Diferencia entre garantía y UX"},
            {"at_ms": 3_712_000, "kind": "question", "note": "Revisar monotonic reads"},
        ]
    )
    for _ in range(8):
        app.processEvents()
    session_output = output.with_name(f"{output.stem}-session{output.suffix}")
    window.grab().save(str(session_output))
    mini_output = output.with_name(f"{output.stem}-mini{output.suffix}")
    window.mini.grab().save(str(mini_output))
    window.mini.controls_toggle.click()
    for _ in range(4):
        app.processEvents()
    mini_expanded_output = output.with_name(f"{output.stem}-mini-expanded{output.suffix}")
    window.mini.grab().save(str(mini_expanded_output))
    window.mini.hide()
    window._on_session_state(
        {
            "mode": "learning",
            "status": "completed",
            "display_elapsed_ms": 3_724_000,
            "busy": False,
            "health": {},
            "persistence_backlog": 0,
            "transcription_backlog": 0,
            "retention": {
                "audio": "marker_context",
                "marker_context_before_ms": 30000,
                "marker_context_after_ms": 30000,
            },
        }
    )
    for job, status, detail in (
        ("integrity", "complete", "integrity.json"),
        ("marker_context", "complete", "4 excerpts"),
        ("diarization", "skipped", "speakers.json"),
        ("clean", "complete", "transcript.clean.md"),
        ("markers", "complete", "markers.md"),
        ("mode", "complete", "exports/learning.md"),
        ("html", "complete", "exports/session.html"),
        ("handoff", "complete", "handoff.md"),
        ("handoff_verify", "complete", "handoff/handoff.json · 9 archivos · 842301 bytes"),
    ):
        window.session.update_processing_job(job, status, detail)
    window.session._handoff_available = True
    window.session.handoff_summary.setText(
        "Preparado · 8 entradas · 3 adjuntos · nox-learn-anything → nox-html-learning"
    )
    window.session.verify_handoff.setEnabled(True)
    window.session.copy_handoff.setEnabled(True)
    window.session.open_handoff.setEnabled(True)
    window.session.tabs.setCurrentIndex(6)
    for _ in range(4):
        app.processEvents()
    export_output = output.with_name(f"{output.stem}-export{output.suffix}")
    window.grab().save(str(export_output))
    window.resize(980, 680)
    for _ in range(4):
        app.processEvents()
    compact_export = output.with_name(f"{output.stem}-export-compact{output.suffix}")
    window.grab().save(str(compact_export))
    window.close()
    QCoreApplication.processEvents()


if __name__ == "__main__":
    render(Path(sys.argv[1] if len(sys.argv) > 1 else "artifacts/ui/home-dark.png").resolve())
