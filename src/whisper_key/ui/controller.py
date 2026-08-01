from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtGui import QImage

from whisper_key.application import (
    AcceptanceService,
    AudioImportService,
    DiarizationService,
    DictationService,
    DurableCapturePipeline,
    MeetingCaptureCoordinator,
    ProcessingService,
    RetentionService,
    SessionService,
)
from whisper_key.clipboard_manager import ClipboardManager
from whisper_key.config_manager import ConfigManager
from whisper_key.domain.audio import SourceHealth
from whisper_key.domain.mode_policy import policy_for
from whisper_key.domain.session import SessionMode, SessionStatus
from whisper_key.infrastructure import (
    AudioDiagnosticsService,
    DiagnosticsBundleService,
    DictationHistoryStore,
    ModelPreflightService,
    PerformanceSampler,
    SessionIndex,
    SnapshotService,
    read_session_metadata,
)
from whisper_key.infrastructure.diarization_models import DiarizationModelManager
from whisper_key.infrastructure.sherpa_diarization import SherpaDiarizationAdapter
from whisper_key.meeting_live_transcriber import MeetingLiveTranscriber
from whisper_key.meeting_recorder import MeetingRecorder
from whisper_key.model_registry import ModelRegistry
from whisper_key.utils import (
    get_user_app_data_path,
    get_version,
    open_file,
    resolve_asset_path,
    setup_portaudio_path,
)
from whisper_key.voice_activity_detection import VadManager
from whisper_key.whisper_engine import WhisperEngine


def discover_sessions(root: Path) -> list[dict[str, Any]]:
    """Return safe, newest-first metadata for both archived and recoverable sessions."""
    candidates = [root / "inbox", root / "sessions"]
    found: list[dict[str, Any]] = []
    for base in candidates:
        if not base.exists():
            continue
        for session_path in base.rglob("session.json"):
            try:
                value = read_session_metadata(session_path, base)
                value["folder"] = str(session_path.parent)
                found.append(value)
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError, TypeError):
                continue
    return sorted(found, key=lambda item: item.get("updated_at", item.get("created_at", "")), reverse=True)


class AppController(QObject):
    model_state_changed = Signal(str, str)
    session_state_changed = Signal(dict)
    transcript_changed = Signal(str)
    provisional_changed = Signal(str)
    markers_changed = Signal(list)
    media_changed = Signal(list)
    library_changed = Signal(list)
    search_results_changed = Signal(list)
    recoveries_changed = Signal(list)
    diagnostics_ready = Signal(str)
    audio_diagnostics_changed = Signal(dict)
    audio_routes_changed = Signal(dict)
    dictation_state_changed = Signal(str, str, str)
    dictation_history_changed = Signal(list)
    dictation_silence_timeout_requested = Signal()
    processing_job_changed = Signal(str, str, str)
    diarization_state_changed = Signal(str, str)
    models_catalog_changed = Signal(list, str)
    model_inspection_changed = Signal(dict)
    hotkeys_config_changed = Signal(dict)
    settings_status_changed = Signal(str, str)
    retention_config_changed = Signal(dict)
    acceptance_changed = Signal(dict)
    error_raised = Signal(str, str)
    operation_finished = Signal(str, object)
    audio_import_state_changed = Signal(str, str, int)

    def __init__(self, library_root: Path | None = None, model_root: Path | None = None):
        super().__init__()
        app_data = Path(get_user_app_data_path())
        self.app_data_root = app_data
        self.library_root = library_root or app_data / "library"
        self.diarization_model_root = model_root or app_data / "models" / "diarization"
        self.diarization_models = DiarizationModelManager(self.diarization_model_root)
        self.index = SessionIndex(self.library_root)
        self.dictation_history = DictationHistoryStore(self.library_root / "dictations")
        self.snapshot_service = SnapshotService()
        self.diagnostics_bundle = DiagnosticsBundleService(app_data)
        self.audio_diagnostics: AudioDiagnosticsService | None = None
        self.retention_service = RetentionService(self.library_root)
        self.acceptance = AcceptanceService(self.library_root / "acceptance", get_version())
        self.config: ConfigManager | None = None
        self.service: SessionService | None = None
        self.pipeline: DurableCapturePipeline | None = None
        self.coordinator: MeetingCaptureCoordinator | None = None
        self.dictation: DictationService | None = None
        self.clipboard: ClipboardManager | None = None
        self.processing: ProcessingService | None = None
        self.engine: WhisperEngine | None = None
        self.registry: ModelRegistry | None = None
        self.model_preflight: ModelPreflightService | None = None
        self.last_external_window: int | None = None
        self._dictation_started_at = 0.0
        self._pending_acceptance_scenario: str | None = None
        self._acceptance_performance: PerformanceSampler | None = None
        self.hotkey_listener = None
        self.hotkey_bridge = None
        self.model_label = ""
        self._ready = False
        self._busy = False
        self._shutdown = False
        self._diarization_installing = False
        self._diagnostics_running = False
        self._diagnostics_exporting = False
        self._last_audio_diagnostics: dict = {}
        self._model_verifying = False
        self._model_inspection_generation = 0
        self._health: dict[str, dict] = {}
        self._logger = logging.getLogger(__name__)
        self.dictation_silence_timeout_requested.connect(self._handle_dictation_silence_timeout)

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def busy(self) -> bool:
        return self._busy

    @Slot()
    def initialize(self) -> None:
        if self._ready or self._busy:
            return
        self._busy = True
        self.model_state_changed.emit("loading", "Preparando Faster Whisper y CUDA…")
        self._run_background("initialize", self._initialize_components)

    def _initialize_components(self) -> dict:
        setup_portaudio_path()
        retention_recoveries = self.retention_service.recover_incomplete()
        if retention_recoveries:
            self._logger.warning("Recovered incomplete retention operations: %s", retention_recoveries)
        self.config = ConfigManager()
        whisper_config = self.config.get_whisper_config()
        vad_config = self.config.get_vad_config()
        registry = ModelRegistry(whisper_models_config=whisper_config.get("models", {}))
        model_preflight = ModelPreflightService(registry)
        self.registry = registry
        self.model_preflight = model_preflight
        self.models_catalog_changed.emit(self._model_catalog(), whisper_config["model"])
        startup_preflight = model_preflight.preflight(
            whisper_config["model"],
            device=whisper_config["device"],
            compute_type=whisper_config["compute_type"],
        )
        self.model_inspection_changed.emit({"kind": "preflight", **startup_preflight.to_dict()})
        startup_disk_ok = startup_preflight.disk_free_bytes >= startup_preflight.disk_required_bytes
        if startup_preflight.cache.state == "corrupt" or not startup_disk_ok:
            raise RuntimeError(f"El modelo configurado no supera la verificación previa: {startup_preflight.detail}")
        if not startup_preflight.allowed:
            self._logger.warning(
                "Startup model memory preflight warning; attempting the configured model: %s",
                startup_preflight.detail,
            )
        vad = VadManager(
            vad_precheck_enabled=vad_config["vad_precheck_enabled"],
            vad_realtime_enabled=vad_config["vad_realtime_enabled"],
            vad_onset_threshold=vad_config["vad_onset_threshold"],
            vad_offset_threshold=vad_config["vad_offset_threshold"],
            vad_min_speech_duration=vad_config["vad_min_speech_duration"],
            vad_silence_timeout_seconds=vad_config["vad_silence_timeout_seconds"],
        )
        engine = WhisperEngine(
            model_key=whisper_config["model"],
            device=whisper_config["device"],
            compute_type=whisper_config["compute_type"],
            language=whisper_config["language"],
            beam_size=whisper_config["beam_size"],
            initial_prompt=whisper_config.get("initial_prompt", ""),
            hotwords=whisper_config.get("hotwords", []),
            strip_trailing_period=whisper_config.get("strip_trailing_period", False),
            vad_manager=vad,
            model_registry=registry,
        )
        self.engine = engine
        meeting_config = self.config.get_meeting_capture_config()
        transcriber = MeetingLiveTranscriber(
            whisper_engine=engine,
            auto_stop_silence_seconds=meeting_config.get("auto_stop_silence_seconds", 0),
        )
        service = SessionService(self.library_root)
        pipeline = DurableCapturePipeline(service, transcriber, on_health=self._on_health)
        recorder = MeetingRecorder(
            config=meeting_config,
            audio_config=self.config.get_audio_config(),
            live_transcriber=transcriber,
        )
        self.service = service
        self.pipeline = pipeline
        self.coordinator = MeetingCaptureCoordinator(service, pipeline, recorder)
        transcriber.set_silence_timeout_callback(lambda _seconds: self.finish_stage())
        self.processing = ProcessingService(self.diarization_model_root)
        clipboard_config = self.config.get_clipboard_config()
        self.clipboard = ClipboardManager(
            auto_paste=clipboard_config["auto_paste"],
            delivery_method=clipboard_config["delivery_method"],
            paste_hotkey=clipboard_config["paste_hotkey"],
            paste_pre_paste_delay=clipboard_config["paste_pre_paste_delay"],
            paste_preserve_clipboard=clipboard_config["paste_preserve_clipboard"],
            paste_clipboard_restore_delay=clipboard_config["paste_clipboard_restore_delay"],
            type_also_copy_to_clipboard=clipboard_config["type_also_copy_to_clipboard"],
            type_auto_enter_delay=clipboard_config["type_auto_enter_delay"],
            type_auto_enter_delay_per_100_chars=clipboard_config["type_auto_enter_delay_per_100_chars"],
            macos_key_simulation_delay=clipboard_config["macos_key_simulation_delay"],
        )
        audio_config = self.config.get_audio_config()
        self.audio_diagnostics = AudioDiagnosticsService(
            input_device=audio_config.get("input_device"),
            input_device_name=audio_config.get("input_device_name"),
            system_device=meeting_config.get("system_audio_device", "default"),
        )
        self.dictation = DictationService(
            engine,
            input_device=audio_config.get("input_device"),
            input_device_name=audio_config.get("input_device_name"),
            max_seconds=max(0, float(audio_config.get("max_duration") or 0)),
            vad_manager=vad,
            on_silence_timeout=self.dictation_silence_timeout_requested.emit,
        )
        self.model_label = f"{whisper_config['model']} · {whisper_config['device'].upper()}"
        self._ready = True
        self.dictation_history_changed.emit(self.dictation_history.list_entries())
        self.models_catalog_changed.emit(self._model_catalog(), engine.model_key)
        startup_inspection = startup_preflight.to_dict()
        startup_inspection["active"] = True
        startup_inspection["allowed"] = True
        startup_inspection["detail"] += " · modelo activo"
        self.model_inspection_changed.emit({"kind": "preflight", **startup_inspection})
        self.hotkeys_config_changed.emit(self.config.get_hotkey_config())
        self.retention_config_changed.emit(self.config.get_retention_config())
        self.audio_routes_changed.emit(
            {
                "input_device": audio_config.get("input_device", "default"),
                "input_device_name": audio_config.get("input_device_name"),
                "system_audio_device": meeting_config.get("system_audio_device", "default"),
            }
        )
        self.acceptance_changed.emit(
            self.acceptance.set_environment(
                {
                    "app_version": get_version(),
                    "model": engine.model_key,
                    "device": engine.device,
                    "compute_type": engine.compute_type,
                    "model_load_ms": engine.model_load_ms,
                    "memory_kind": startup_preflight.memory_kind,
                    "memory_free_bytes": startup_preflight.memory_free_bytes,
                    "memory_required_bytes": startup_preflight.memory_required_bytes,
                }
            )
        )
        return {"model": self.model_label, "library": str(self.library_root)}

    @Slot(str)
    def start_mode(self, mode: str) -> None:
        selected = SessionMode(mode)
        policy = policy_for(selected)
        self._start_capture(selected, policy.capture_microphone, policy.capture_system_audio, 0.0)

    @Slot(str)
    def import_audio_file(self, source_path: str) -> None:
        if not self._ready or self._busy or not self.service or not self.engine or not self.config:
            self.audio_import_state_changed.emit(
                "error",
                "El modelo todavía no está disponible o WhisperKey está ocupado.",
                0,
            )
            return
        if self.dictation and self.dictation.is_recording:
            self.error_raised.emit(
                "Finaliza primero el dictado",
                "La importación usa el mismo modelo local y comenzará cuando el dictado haya terminado.",
            )
            return
        session = self.service.session
        if session and session.status != SessionStatus.COMPLETED:
            self.error_raised.emit(
                "Hay una sesión pendiente",
                "Finaliza, nombra o recupera la sesión actual antes de importar un archivo.",
            )
            return

        self._busy = True
        self.model_state_changed.emit("busy", "Preparando el archivo de audio…")

        def action() -> dict:
            retention_config = self.config.get_retention_config()
            retention = {
                "audio": retention_config[SessionMode.LEARNING.value],
                "marker_context_before_ms": retention_config["marker_context_before_ms"],
                "marker_context_after_ms": retention_config["marker_context_after_ms"],
            }
            importer = AudioImportService(
                self.service,
                self.engine,
                on_progress=lambda progress: self.audio_import_state_changed.emit(
                    progress.state,
                    progress.detail,
                    progress.percent,
                ),
            )
            result = importer.import_file(Path(source_path), retention=retention)
            return {
                "session_id": result.session_id,
                "folder": str(result.folder),
                "duration_ms": result.duration_ms,
                "transcript_segments": result.transcript_segments,
            }

        self._run_background("audio_import", action)

    def _start_capture(
        self,
        selected: SessionMode,
        capture_microphone: bool,
        capture_system: bool,
        auto_stop_seconds: float,
    ) -> None:
        if not self._ready or self._busy or not self.coordinator:
            return
        self._busy = True
        self.model_state_changed.emit("busy", f"Iniciando {selected.value}…")

        def action() -> dict:
            self._health = {}
            retention_config = self.config.get_retention_config()
            retention = {
                "audio": retention_config[selected.value],
                "marker_context_before_ms": retention_config["marker_context_before_ms"],
                "marker_context_after_ms": retention_config["marker_context_after_ms"],
            }
            self.coordinator.start(
                selected,
                capture_microphone,
                capture_system,
                auto_stop_seconds,
                retention=retention,
            )
            return {"mode": selected.value}

        self._run_background("start", action)

    @Slot(bool, bool, float)
    def toggle_meeting_capture(
        self,
        capture_microphone: bool,
        capture_system_audio: bool,
        auto_stop_seconds: float,
    ) -> None:
        session = self.service.session if self.service else None
        if session and session.status in {SessionStatus.RECORDING, SessionStatus.PAUSED}:
            self.finish_stage()
            return
        if session and session.status == SessionStatus.RECOVERABLE:
            self.error_raised.emit("Sesión pendiente", "Nombra o continúa la sesión recuperable antes de iniciar otra.")
            return
        if auto_stop_seconds < 0 and self.config:
            auto_stop_seconds = self.config.get_meeting_capture_config().get("auto_stop_silence_seconds", 0)
        self._start_capture(
            SessionMode.MEETING,
            capture_microphone,
            capture_system_audio,
            auto_stop_seconds,
        )

    @property
    def dictation_elapsed_ms(self) -> int:
        if not self.dictation or not self.dictation.is_recording:
            return 0
        return round((time.monotonic() - self._dictation_started_at) * 1000)

    @Slot()
    def toggle_dictation(self) -> None:
        if not self._ready or not self.dictation or self._busy:
            return
        if self.dictation.is_recording:
            self._stop_dictation()
            return
        session = self.service.session if self.service else None
        if session and session.status == SessionStatus.RECORDING:
            self.error_raised.emit(
                "Pausa la sesión primero",
                "El dictado corto puede usar el micrófono cuando la sesión durable está en pausa.",
            )
            return
        try:
            self.dictation.start()
        except Exception as exc:
            self.error_raised.emit("No se pudo iniciar el dictado", str(exc))
            return
        if self._pending_acceptance_scenario:
            self._start_acceptance_performance()
        self._dictation_started_at = time.monotonic()
        self.dictation_state_changed.emit("recording", "Escuchando · vuelve a pulsar para transcribir", "")

    @Slot()
    def _handle_dictation_silence_timeout(self) -> None:
        if not self.dictation or not self.dictation.is_recording or self._busy:
            return
        timeout = 30
        if self.config:
            timeout = int(self.config.get_vad_config().get("vad_silence_timeout_seconds", timeout))
        self._stop_dictation(f"{timeout}s de silencio · transcribiendo localmente…")

    def _stop_dictation(self, processing_message: str = "Transcribiendo localmente…") -> None:
        if not self.dictation:
            return
        self._busy = True
        benchmark_scenario = self._pending_acceptance_scenario
        self.dictation_state_changed.emit("processing", processing_message, "")

        def worker() -> None:
            performance = None
            try:
                text = self.dictation.stop_and_transcribe()
                performance = self._finish_acceptance_performance()
                delivery = "benchmark" if benchmark_scenario else "not_delivered"
                if text and self.clipboard and not benchmark_scenario:
                    target_restored = self._restore_delivery_window()
                    if target_restored:
                        success = self.clipboard.deliver_transcription(text)
                        if success:
                            delivery = "pasted" if self.clipboard.auto_paste else "clipboard"
                    if delivery == "not_delivered" and self.clipboard.copy_with_notification(text):
                        delivery = "clipboard"
                transcription = dict(self.dictation.whisper_engine.last_transcription_metrics)
                if performance:
                    transcription["performance"] = performance
                entry = self.dictation_history.append(
                    text=text,
                    audio=self.dictation.last_audio,
                    sample_rate=self.dictation.last_sample_rate,
                    delivery=delivery,
                    transcription=transcription,
                )
                self.dictation_history_changed.emit(self.dictation_history.list_entries())
                if benchmark_scenario:
                    self.acceptance_changed.emit(self.acceptance.evaluate_dictation(benchmark_scenario, entry))
                messages = {
                    "pasted": "Pegado en la aplicación anterior",
                    "clipboard": "No se pudo confirmar el pegado · copiado al portapapeles",
                    "not_delivered": "No se pudo pegar ni copiar · usa el botón Copiar",
                    "benchmark": "Prueba guardada localmente · revisa las métricas",
                }
                message = messages[delivery]
                state = "complete" if text else "empty"
                self.dictation_state_changed.emit(state, message if text else "No se detectó voz", text)
                self.operation_finished.emit(
                    "dictation",
                    {
                        "text": text,
                        "delivered": delivery == "pasted",
                        "delivery": delivery,
                        "acceptance_scenario": benchmark_scenario,
                    },
                )
            except Exception as exc:
                performance = performance or self._finish_acceptance_performance()
                self._logger.exception("Dictation failed")
                if self.dictation.last_audio.size:
                    try:
                        self.dictation_history.append(
                            text="",
                            audio=self.dictation.last_audio,
                            sample_rate=self.dictation.last_sample_rate,
                            delivery="failed",
                            error=str(exc),
                            transcription={
                                **dict(self.dictation.whisper_engine.last_transcription_metrics),
                                **({"performance": performance} if performance else {}),
                            },
                        )
                        self.dictation_history_changed.emit(self.dictation_history.list_entries())
                    except Exception:
                        self._logger.exception("Could not preserve failed dictation audio")
                self.error_raised.emit("El dictado falló", str(exc))
                if benchmark_scenario:
                    self.acceptance_changed.emit(
                        self.acceptance.record_automatic_failure(
                            benchmark_scenario,
                            str(exc),
                            performance=performance,
                        )
                    )
                self.dictation_state_changed.emit("error", str(exc), "")
            finally:
                self._finish_acceptance_performance()
                if benchmark_scenario:
                    self._pending_acceptance_scenario = None
                self._busy = False

        threading.Thread(target=worker, name="wk-dictation", daemon=True).start()

    @Slot()
    def cancel_dictation(self) -> None:
        if not self.dictation or not self.dictation.is_recording:
            return
        self.dictation.cancel()
        self._finish_acceptance_performance()
        self._pending_acceptance_scenario = None
        self.dictation_state_changed.emit("canceled", "Dictado cancelado", "")

    @Slot(str)
    def toggle_acceptance_dictation(self, scenario_id: str) -> None:
        scenarios = {item["scenario_id"]: item for item in self.acceptance.report()["scenarios"]}
        scenario = scenarios.get(scenario_id)
        if not scenario or scenario.get("kind") != "dictation_benchmark":
            self.error_raised.emit("Prueba desconocida", "La comprobación seleccionada no admite dictado.")
            return
        if self.dictation and self.dictation.is_recording:
            if self._pending_acceptance_scenario != scenario_id:
                self.error_raised.emit(
                    "Hay otro dictado activo",
                    "Termina o cancela el dictado actual antes de iniciar esta comprobación.",
                )
                return
            self._stop_dictation()
            return
        self._pending_acceptance_scenario = scenario_id
        self.toggle_dictation()
        if not self.dictation or not self.dictation.is_recording:
            self._pending_acceptance_scenario = None

    def _start_acceptance_performance(self) -> None:
        self._finish_acceptance_performance()
        sampler = PerformanceSampler(
            gpu_enabled=bool(self.engine and self.engine.device == "cuda"),
        )
        try:
            sampler.start()
        except Exception:
            self._logger.exception("Could not start acceptance performance sampling")
            return
        self._acceptance_performance = sampler

    def _finish_acceptance_performance(self) -> dict | None:
        sampler = self._acceptance_performance
        self._acceptance_performance = None
        if not sampler:
            return None
        try:
            return sampler.stop()
        except Exception:
            self._logger.exception("Could not finish acceptance performance sampling")
            return None

    @Slot(str, str, str)
    def record_acceptance_result(self, scenario_id: str, status: str, note: str) -> None:
        try:
            report = self.acceptance.record_manual(scenario_id, status, note)
        except (KeyError, ValueError) as exc:
            self.error_raised.emit("No se guardó la comprobación", str(exc))
            return
        self.acceptance_changed.emit(report)

    @Slot()
    def export_acceptance_report(self) -> None:
        result = self.acceptance.export_paths()
        self.acceptance_changed.emit(self.acceptance.report())
        self.operation_finished.emit("acceptance_export", result)

    @Slot()
    def refresh_acceptance(self) -> None:
        self.acceptance_changed.emit(self.acceptance.report())

    @Slot(str)
    def copy_dictation_text(self, text: str) -> None:
        if not text.strip() or not self.clipboard:
            return
        if self.clipboard.copy_with_notification(text):
            self.dictation_state_changed.emit("complete", "Copiado al portapapeles", text)
        else:
            self.error_raised.emit("No se pudo copiar", "Windows no permitió actualizar el portapapeles.")

    @Slot()
    def install_hotkeys(self) -> None:
        if self.hotkey_listener or not self.config:
            return
        from whisper_key.hotkey_listener import HotkeyListener
        from whisper_key.ui.hotkey_bridge import GuiHotkeyBridge

        config = self.config.get_hotkey_config()
        self.hotkey_bridge = GuiHotkeyBridge(self)
        self.hotkey_listener = HotkeyListener(
            state_manager=self.hotkey_bridge,
            recording_hotkey=config["recording_hotkey"],
            stop_key=config["stop_key"],
            auto_send_key=config.get("auto_send_key"),
            cancel_combination=config.get("cancel_combination"),
            command_hotkey=config.get("command_hotkey"),
            meeting_hotkey=config.get("meeting_hotkey"),
            meeting_continuous_hotkey=config.get("meeting_continuous_hotkey"),
            meeting_mic_only_hotkey=config.get("meeting_mic_only_hotkey"),
            meeting_sys_only_hotkey=config.get("meeting_sys_only_hotkey"),
            recording_mode=config.get("recording_mode", "toggle"),
        )

    @Slot(dict)
    def save_hotkeys(self, values: dict) -> None:
        if not self.config or not self.hotkey_listener:
            self.error_raised.emit("Atajos no disponibles", "Espera a que el modelo termine de cargar.")
            return
        editable = {
            "recording_hotkey",
            "stop_key",
            "meeting_hotkey",
            "meeting_continuous_hotkey",
            "meeting_mic_only_hotkey",
            "meeting_sys_only_hotkey",
        }
        if set(values) != editable:
            self.error_raised.emit("Atajos incompletos", "Revisa todos los campos de atajos.")
            return
        normalized = {key: str(value).lower().replace(" ", "").strip("+") for key, value in values.items()}
        for label, combination in normalized.items():
            parts = combination.split("+") if combination else []
            if not parts or any(not re.fullmatch(r"[a-z0-9]+", part) for part in parts):
                self.error_raised.emit(
                    "Atajo inválido",
                    f"{label}: usa formato como ctrl+win, f9 o win+f10.",
                )
                return
        duplicates = {value for value in normalized.values() if list(normalized.values()).count(value) > 1}
        if duplicates:
            self.error_raised.emit(
                "Atajos repetidos",
                f"Cada acción necesita una combinación distinta: {', '.join(sorted(duplicates))}",
            )
            return
        previous = {key: getattr(self.hotkey_listener, key) for key in normalized}
        try:
            self.hotkey_listener.replace_hotkey_config(normalized)
            try:
                self.config.update_user_settings("hotkey", normalized)
            except Exception:
                self.hotkey_listener.replace_hotkey_config(previous)
                raise
        except Exception as exc:
            self._logger.exception("Hotkey update failed")
            self.error_raised.emit("No se pudieron guardar los atajos", str(exc))
            return
        self.hotkeys_config_changed.emit(self.config.get_hotkey_config())
        self.settings_status_changed.emit("success", "Atajos guardados y activados")

    @Slot(dict)
    def save_retention_settings(self, values: dict) -> None:
        if not self.config:
            self.error_raised.emit("Retención no disponible", "Espera a que WhisperKey termine de iniciar.")
            return
        expected = {
            "dictation",
            "meeting",
            "learning",
            "reading",
            "idea",
            "marker_context_before_ms",
            "marker_context_after_ms",
        }
        if set(values) != expected:
            self.error_raised.emit("Retención incompleta", "Revisa todos los modos y el contexto.")
            return
        policies = {"all", "until_verified", "marker_context", "none"}
        if any(values[mode] not in policies for mode in ("dictation", "meeting", "learning", "reading", "idea")):
            self.error_raised.emit("Retención inválida", "Uno de los modos contiene una política desconocida.")
            return
        if values["dictation"] != "all":
            self.error_raised.emit(
                "Historial de dictado protegido",
                "El dictado rápido conserva su WAV para que el historial sea recuperable.",
            )
            return
        for key in ("marker_context_before_ms", "marker_context_after_ms"):
            value = values[key]
            if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 600000:
                self.error_raised.emit("Contexto inválido", "El contexto debe estar entre 0 y 600 segundos.")
                return
        try:
            self.config.update_user_settings("retention", values)
        except Exception as exc:
            self._logger.exception("Retention settings update failed")
            self.error_raised.emit("No se guardó la retención", str(exc))
            return
        self.retention_config_changed.emit(self.config.get_retention_config())
        self.settings_status_changed.emit(
            "success",
            "Retención guardada · se aplicará a sesiones nuevas",
        )

    def _model_catalog(self) -> list[dict]:
        if not self.registry or not self.model_preflight:
            return []
        catalog = []
        for definition in self.registry.whisper_models.values():
            if not definition.enabled:
                continue
            english_only = definition.key.endswith(".en") or definition.key.startswith("distil-")
            if definition.key == "distil-large-v3.5":
                english_only = True
            catalog.append(
                {
                    "key": definition.key,
                    "label": definition.label,
                    "group": definition.group,
                    "cached": self.registry.is_model_cached(definition.key),
                    "cache_state": self.model_preflight.inspect_cache(definition.key).state,
                    "multilingual": not english_only,
                }
            )
        return catalog

    @Slot(str)
    def select_model(self, model_key: str) -> None:
        if self._busy or not self.registry or not self.model_preflight or not self.config:
            return
        definition = self.registry.get_model(model_key)
        if not definition or not definition.enabled:
            self.error_raised.emit("Modelo no disponible", model_key)
            return
        if self.engine and model_key == self.engine.model_key:
            self.inspect_model(model_key)
            self.model_state_changed.emit("ready", f"Modelo ya activo · {definition.label}")
            return
        whisper_config = self.config.get_whisper_config()
        preflight = self.model_preflight.preflight(
            model_key,
            device=whisper_config["device"],
            compute_type=whisper_config["compute_type"],
        )
        self.model_inspection_changed.emit({"kind": "preflight", **preflight.to_dict()})
        if not preflight.allowed:
            self.error_raised.emit("El modelo no puede cargarse con seguridad", preflight.detail)
            return
        if not self.engine:
            self.config.update_user_setting("whisper", "model", model_key)
            self.initialize()
            return
        if self.dictation and self.dictation.is_recording:
            self.error_raised.emit("Termina el dictado", "Cambia de modelo cuando el micrófono esté libre.")
            return
        session = self.service.session if self.service else None
        if session and session.status in {SessionStatus.RECORDING, SessionStatus.PAUSED}:
            self.error_raised.emit("Hay una sesión activa", "Finaliza la sesión antes de cambiar el modelo.")
            return
        self._busy = True
        self.model_state_changed.emit("loading", f"Preparando {definition.label}…")

        def progress(message: str) -> None:
            self.model_state_changed.emit("loading", f"{definition.label} · {message}")

        self.engine.change_model(model_key, progress)

        def monitor() -> None:
            while self.engine and self.engine.is_loading():
                time.sleep(0.1)
            success = bool(self.engine and self.engine.model_key == model_key and self.engine.model is not None)
            if success:
                self.config.update_user_setting("whisper", "model", model_key)
                whisper_config = self.config.get_whisper_config()
                self.model_label = f"{model_key} · {whisper_config['device'].upper()}"
                self.model_state_changed.emit("ready", f"Modelo listo · {self.model_label}")
                self.acceptance_changed.emit(
                    self.acceptance.set_environment(
                        {
                            "app_version": get_version(),
                            "model": self.engine.model_key,
                            "device": self.engine.device,
                            "compute_type": self.engine.compute_type,
                            "model_load_ms": self.engine.model_load_ms,
                            "memory_kind": preflight.memory_kind,
                            "memory_free_bytes": preflight.memory_free_bytes,
                            "memory_required_bytes": preflight.memory_required_bytes,
                        }
                    )
                )
                self.operation_finished.emit("model_changed", model_key)
            else:
                self.model_state_changed.emit("error", f"No se pudo cargar {definition.label}")
                self.error_raised.emit(
                    "No se pudo cambiar el modelo",
                    "El modelo anterior sigue activo. "
                    f"{self.engine.last_load_error or 'Revisa conexión, caché y el registro.'}",
                )
            self._busy = False
            current = self.engine.model_key if self.engine else ""
            self.models_catalog_changed.emit(self._model_catalog(), current)
            if current:
                self.inspect_model(current)

        threading.Thread(target=monitor, name="wk-model-change", daemon=True).start()

    @Slot(str)
    def inspect_model(self, model_key: str) -> None:
        if not self.model_preflight or not self.config or not model_key:
            return
        self._model_inspection_generation += 1
        generation = self._model_inspection_generation
        whisper_config = self.config.get_whisper_config()

        def worker() -> None:
            try:
                result = self.model_preflight.preflight(
                    model_key,
                    device=whisper_config["device"],
                    compute_type=whisper_config["compute_type"],
                ).to_dict()
                if self.engine and self.engine.model_key == model_key and self.engine.model is not None:
                    result["active"] = True
                    result["allowed"] = True
                    result["detail"] += " · modelo activo"
            except Exception as exc:
                result = {
                    "model_key": model_key,
                    "allowed": False,
                    "detail": f"No se pudo inspeccionar el modelo: {exc}",
                }
            if generation == self._model_inspection_generation:
                self.model_inspection_changed.emit({"kind": "preflight", **result})

        threading.Thread(target=worker, name="wk-model-inspect", daemon=True).start()

    @Slot(str)
    def verify_model(self, model_key: str) -> None:
        if self._model_verifying or not self.model_preflight or not model_key:
            return
        self._model_verifying = True
        self.model_inspection_changed.emit(
            {
                "kind": "verification",
                "model_key": model_key,
                "status": "progress",
                "progress": 0.0,
                "detail": "Verificando model.bin…",
            }
        )

        def progress(value: float) -> None:
            self.model_inspection_changed.emit(
                {
                    "kind": "verification",
                    "model_key": model_key,
                    "status": "progress",
                    "progress": value,
                    "detail": f"Verificando model.bin · {round(value * 100)}%",
                }
            )

        def worker() -> None:
            try:
                result = self.model_preflight.verify_cache(model_key, progress)
            except Exception as exc:
                result = {
                    "model_key": model_key,
                    "status": "error",
                    "detail": str(exc),
                    "verified_bytes": 0,
                }
            finally:
                self._model_verifying = False
            self.model_inspection_changed.emit({"kind": "verification", **result})
            self.models_catalog_changed.emit(
                self._model_catalog(),
                self.engine.model_key if self.engine else "",
            )

        threading.Thread(target=worker, name="wk-model-verify", daemon=True).start()

    @Slot()
    def open_model_cache(self) -> None:
        if not self.registry:
            return
        path = Path(self.registry.get_hf_cache_path())
        path.mkdir(parents=True, exist_ok=True)
        open_file(str(path))

    @Slot()
    def refresh_diarization_state(self) -> None:
        if self._diarization_installing:
            return
        installed, detail = self.diarization_models.verify_installed()
        if installed:
            self.diarization_state_changed.emit(
                "ready",
                "Instalada · separa voces anónimas al terminar una sesión",
            )
        else:
            has_any_files = self.diarization_model_root.exists() and any(self.diarization_model_root.iterdir())
            self.diarization_state_changed.emit(
                "error" if has_any_files else "missing",
                (
                    f"Instalación incompleta · {detail}"
                    if has_any_files
                    else "No instalada · la captura conserva etiquetas MIC/SYS"
                ),
            )

    @Slot()
    def install_diarization_models(self) -> None:
        if self._diarization_installing:
            return
        self._diarization_installing = True
        self.diarization_state_changed.emit("installing", "Preparando descarga verificada…")

        def progress(message: str, completed: float, total: int) -> None:
            percent = round(completed / max(1, total) * 100)
            self.diarization_state_changed.emit("installing", f"{message} · {percent}%")

        def worker() -> None:
            try:
                self.diarization_models.install(progress)
                sample_rate = SherpaDiarizationAdapter(self.diarization_model_root).validate_runtime()
                self.diarization_state_changed.emit(
                    "ready",
                    f"Instalada, verificada y cargada a {sample_rate // 1000} kHz · "
                    "se aplicará en el próximo postproceso",
                )
            except Exception as exc:
                self._logger.exception("Diarization model installation failed")
                self.diarization_state_changed.emit("error", f"Instalación fallida · {exc}")
                self.error_raised.emit("No se pudo instalar la diarización", str(exc))
            finally:
                self._diarization_installing = False

        threading.Thread(target=worker, name="wk-diarization-install", daemon=True).start()

    @Slot(int)
    def remember_external_window(self, window_id: int) -> None:
        if window_id:
            self.last_external_window = window_id

    def _restore_delivery_window(self) -> bool:
        if not self.last_external_window:
            return False
        try:
            import win32con
            import win32gui

            if not win32gui.IsWindow(self.last_external_window):
                return False
            win32gui.ShowWindow(self.last_external_window, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(self.last_external_window)
            time.sleep(0.12)
            return win32gui.GetForegroundWindow() == self.last_external_window
        except Exception:
            self._logger.exception("Could not restore dictation target window")
            return False

    @Slot()
    def pause_or_resume(self) -> None:
        if self._busy or not self.coordinator or not self.service or not self.service.session:
            return
        status = self.service.session.status
        if status not in {SessionStatus.RECORDING, SessionStatus.PAUSED}:
            return
        self._busy = True
        if status == SessionStatus.RECORDING:
            self._run_background("pause", lambda: self.coordinator.pause())
        else:
            self._run_background("resume", lambda: self.coordinator.resume())

    @Slot(str)
    def continue_session(self, session_id: str) -> None:
        if self._busy or not self.coordinator or not self.service:
            return
        self._busy = True
        self.model_state_changed.emit("busy", "Recuperando la sesión y abriendo una nueva etapa…")

        def action() -> dict:
            folder = self.service.repository.find_folder(session_id)
            metadata = json.loads((folder / "session.json").read_text(encoding="utf-8"))
            mode = SessionMode(metadata["mode"])
            policy = policy_for(mode)
            self._health = {}
            self.coordinator.continue_session(
                session_id,
                policy.capture_microphone,
                policy.capture_system_audio,
            )
            return {"mode": mode.value, "session_id": session_id}

        self._run_background("continue", action)

    @Slot(str)
    def open_session(self, session_id: str) -> None:
        if self._busy or not self.service:
            return
        current = self.service.session
        if current and current.status in {SessionStatus.RECORDING, SessionStatus.PAUSED}:
            self.error_raised.emit(
                "Hay una captura activa",
                "Pausa/finaliza la captura actual antes de abrir otra sesión.",
            )
            return
        self._busy = True
        self.model_state_changed.emit("busy", "Abriendo sesión local…")

        def action() -> str:
            session = self.service.load(session_id)
            if session.status in {
                SessionStatus.RECORDING,
                SessionStatus.PAUSED,
                SessionStatus.PREPARING,
                SessionStatus.PROCESSING,
            }:
                self.service.recover()
            return str(self.service.folder)

        self._run_background("open_session", action)

    @Slot(str, str)
    def add_marker(self, kind: str = "important", note: str = "") -> None:
        if not self.service or not self.service.session:
            return
        try:
            self.service.add_marker(kind, note.strip() or None)
            self.publish_snapshot()
        except Exception as exc:
            self.error_raised.emit("No se pudo crear el marcador", str(exc))

    @Slot(str, str)
    def arm_spoken_note(self, kind: str = "important", note: str = "") -> None:
        if not self.service or not self.service.session:
            return
        try:
            marker_id = self.service.arm_spoken_note(kind, note.strip() or None)
            self.operation_finished.emit("spoken_note_armed", marker_id)
            self.publish_snapshot()
        except Exception as exc:
            self.error_raised.emit("No se pudo preparar la nota hablada", str(exc))

    @Slot(QImage)
    def add_snapshot(self, image: QImage) -> None:
        if not self.service or not self.service.session:
            return
        detached = image.copy()

        def worker() -> None:
            try:
                result = self.snapshot_service.persist(self.service, detached)
            except Exception as exc:
                self._logger.exception("Screenshot persistence failed")
                self.error_raised.emit("No se pudo guardar la captura", str(exc))
                return
            self.operation_finished.emit("snapshot", result)
            self.publish_snapshot()

        threading.Thread(target=worker, name="wk-snapshot", daemon=True).start()

    @Slot(bool)
    def preview_retention(self, verified: bool = False) -> None:
        if self._busy or not self.service or not self.service.session:
            return
        self._busy = True
        self.model_state_changed.emit("busy", "Verificando audio y preparando una vista previa segura…")
        self._run_background(
            "retention_preview",
            lambda: self.retention_service.preview(self.service, verified=verified).to_dict(),
        )

    @Slot(str, bool)
    def apply_retention(self, preview_id: str, verified: bool = False) -> None:
        if self._busy or not self.service or not self.service.session:
            return
        self._busy = True
        self.model_state_changed.emit("busy", "Moviendo audio a la papelera recuperable…")

        def action() -> dict:
            result = self.retention_service.apply(self.service, preview_id, verified=verified)
            if result.get("moved") and self.processing:
                self._refresh_derived_package(("integrity", "handoff", "handoff_verify"))
            return result

        self._run_background("retention_apply", action)

    @Slot()
    def restore_retention(self) -> None:
        if self._busy or not self.service or not self.service.session:
            return
        self._busy = True
        self.model_state_changed.emit("busy", "Restaurando audio conservado en la papelera…")

        def action() -> dict:
            result = self.retention_service.restore_latest(self.service)
            if result.get("restored") and self.processing:
                self._refresh_derived_package(("integrity", "handoff", "handoff_verify"))
            return result

        self._run_background("retention_restore", action)

    @Slot(str)
    def save_clean_revision(self, content: str) -> None:
        if self._busy or not self.service or not self.service.folder or not content.strip():
            return
        self._busy = True
        self.model_state_changed.emit("busy", "Guardando revisión y actualizando el paquete…")

        def action() -> str:
            folder = self.service.folder
            manifest_path = folder / "clean-revisions.json"
            manifest = (
                json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest_path.exists()
                else {"schema_version": 1, "revisions": []}
            )
            revisions = manifest.setdefault("revisions", [])
            revision = max((int(item.get("revision", 0)) for item in revisions), default=0) + 1
            relative = f"revisions/clean-v{revision}.md"
            normalized = content.rstrip() + "\n"
            self.service.repository.write_projection(folder, relative, normalized)
            self.service.repository.write_projection(folder, "transcript.clean.md", normalized)
            revisions.append(
                {
                    "revision": revision,
                    "kind": "manual",
                    "relative_path": relative,
                    "sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
                }
            )
            manifest["active_revision"] = revision
            self.service.repository.write_projection(
                folder,
                "clean-revisions.json",
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            )
            if self.processing:
                self._refresh_derived_package(("mode", "html", "integrity", "handoff", "handoff_verify"))
            return relative

        self._run_background("clean_saved", action)

    @Slot(str)
    def run_processing_job(self, job: str) -> None:
        if self._busy or not self.service or not self.service.folder or not self.processing:
            return
        if job not in self.processing.JOBS:
            self.error_raised.emit("Trabajo desconocido", job)
            return
        if not self.service.session or self.service.session.status != SessionStatus.COMPLETED:
            self.error_raised.emit(
                "Finaliza primero la sesión",
                "El procesamiento derivado solo se ejecuta sobre una sesión cerrada y durable.",
            )
            return
        self._busy = True
        self.model_state_changed.emit("busy", f"Ejecutando {job}…")

        def action() -> dict:
            jobs = ("handoff", "handoff_verify") if job == "handoff" else (job,)
            statuses = self._refresh_derived_package(jobs)
            return {"job": job, "folder": str(self.service.folder), "statuses": statuses}

        self._run_background("processing_job", action)

    @Slot(dict)
    def save_speaker_names(self, names: dict) -> None:
        if self._busy or not self.service or not self.service.folder or not self.processing:
            return
        self._busy = True
        self.model_state_changed.emit("busy", "Guardando nombres y actualizando documentos…")

        def action() -> dict:
            folder = self.service.folder
            path = folder / "speakers.json"
            if not path.is_file():
                raise FileNotFoundError("La sesión todavía no tiene asignaciones de hablantes")
            current = json.loads(path.read_text(encoding="utf-8"))
            revised = DiarizationService.revise_names(current, names)
            current_revision = int(current.get("revision", 1))
            previous_relative = f"revisions/speakers-v{current_revision}.json"
            if not (folder / previous_relative).exists():
                self.service.repository.write_projection(
                    folder,
                    previous_relative,
                    json.dumps(current, ensure_ascii=False, indent=2) + "\n",
                )
            relative = f"revisions/speakers-v{revised['revision']}.json"
            content = json.dumps(revised, ensure_ascii=False, indent=2) + "\n"
            self.service.repository.write_projection(folder, relative, content)
            self.service.repository.write_projection(folder, "speakers.json", content)
            self._refresh_derived_package(("clean", "mode", "html", "integrity", "handoff", "handoff_verify"))
            return {"revision": revised, "folder": str(folder)}

        self._run_background("speakers_saved", action)

    @Slot(str)
    def finalize(self, title: str) -> None:
        if self._busy or not self.coordinator:
            return
        normalized = " ".join(title.split())
        if not normalized:
            self.error_raised.emit(
                "Ponle un nombre",
                "El audio está seguro, pero la sesión necesita un nombre para archivarse.",
            )
            return
        self._busy = True
        self.model_state_changed.emit("busy", "Cerrando audio y construyendo el documento literal…")
        self._run_background("finalize", lambda: str(self.coordinator.finalize(normalized)))

    @Slot()
    def finish_stage(self) -> None:
        if self._busy or not self.coordinator:
            return
        self._busy = True
        self.model_state_changed.emit("busy", "Asegurando el audio antes de pedir el nombre…")
        self._run_background("finish_stage", self.coordinator.finish_stage)

    @Slot()
    def publish_snapshot(self) -> None:
        service = self.service
        session = service.session if service else None
        if not service or not session:
            self.library_changed.emit(discover_sessions(self.library_root))
            return
        state = session.to_dict()
        state["display_elapsed_ms"] = service.live_captured_duration_ms()
        state["model"] = self.model_label
        state["busy"] = self._busy
        state["health"] = dict(self._health)
        state["persistence_backlog"] = self.pipeline.persistence_backlog if self.pipeline else 0
        state["transcription_backlog"] = self.pipeline.transcription_backlog if self.pipeline else 0
        events = service.repository.read_events(service.folder) if service.folder else []
        live_entries = []
        for event in events:
            payload = event.get("payload", {})
            if event.get("type") == "transcript_final":
                live_entries.append(
                    f"{payload['source']}  {self._format_offset(payload['started_at_ms'])}\n{payload['raw_text']}"
                )
            elif event.get("type") == "snapshot_created":
                live_entries.append(
                    f"CAPTURA  {self._format_offset(payload.get('at_ms', 0))}\n"
                    f"Guardada · {payload.get('relative_path', '')}"
                )
            elif event.get("type") == "marker_created":
                note = payload.get("note") or payload.get("kind", "marcador")
                live_entries.append(f"MARCADOR  {self._format_offset(payload.get('at_ms', 0))}\n{note}")
            elif event.get("type") == "spoken_note":
                live_entries.append(
                    f"NOTA HABLADA  {self._format_offset(payload.get('started_at_ms', 0))}\n"
                    f"{payload.get('raw_text', '')}"
                )
        transcript = "\n\n".join(live_entries)
        provisional = ""
        for event in reversed(events):
            if event.get("type") == "transcript_final":
                break
            if event.get("type") == "transcript_provisional":
                provisional = event["payload"].get("raw_text", "")
                break
        spoken_notes = {
            event["payload"]["marker_id"]: event["payload"].get("raw_text", "")
            for event in events
            if event.get("type") == "spoken_note"
        }
        markers = []
        for event in events:
            if event.get("type") != "marker_created":
                continue
            marker = dict(event["payload"])
            marker["spoken_note"] = spoken_notes.get(marker["marker_id"])
            markers.append(marker)
        media = []
        for event in events:
            if event.get("type") != "snapshot_created":
                continue
            payload = dict(event["payload"])
            if service.folder and payload.get("relative_path"):
                candidate = (service.folder / payload["relative_path"]).resolve()
                if service.folder.resolve() in candidate.parents:
                    payload["absolute_path"] = str(candidate)
            media.append(payload)
        self.session_state_changed.emit(state)
        self.transcript_changed.emit(transcript)
        self.provisional_changed.emit(provisional)
        self.markers_changed.emit(markers)
        self.media_changed.emit(media)

    @Slot()
    def refresh_library(self) -> None:
        def worker() -> None:
            try:
                self.index.rebuild()
                sessions = self.index.search()
            except Exception as exc:
                self._logger.exception("Session index refresh failed")
                self.error_raised.emit("No se pudo actualizar la biblioteca", str(exc))
                sessions = discover_sessions(self.library_root)
            self.library_changed.emit(sessions)
            self.search_results_changed.emit(sessions)
            active_id = self.service.session.session_id if self.service and self.service.session else None
            recoverable = [
                item
                for item in sessions
                if item.get("status") in {"draft", "recording", "paused", "processing", "recoverable"}
                and item.get("session_id") != active_id
            ]
            self.recoveries_changed.emit(recoverable)

        threading.Thread(target=worker, name="wk-library-index", daemon=True).start()

    @Slot(str)
    def search_library(self, query: str) -> None:
        def worker() -> None:
            try:
                results = self.index.search(query)
            except Exception as exc:
                self._logger.exception("Session search failed")
                self.error_raised.emit("La búsqueda falló", str(exc))
                return
            self.search_results_changed.emit(results)

        threading.Thread(target=worker, name="wk-library-search", daemon=True).start()

    @Slot()
    def run_device_diagnostics(self) -> None:
        if self._diagnostics_running or self._busy:
            return
        if not self._ready or not self.audio_diagnostics:
            self.error_raised.emit("Diagnóstico no disponible", "Espera a que WhisperKey termine de iniciar.")
            return
        if self.dictation and self.dictation.is_recording:
            self.error_raised.emit("Hay un dictado activo", "Termínalo o cancélalo antes de comprobar el audio.")
            return
        session = self.service.session if self.service else None
        if session and session.status == SessionStatus.RECORDING:
            self.error_raised.emit(
                "Hay una sesión grabando",
                "Pausa o finaliza la sesión antes de comprobar MIC y SYS.",
            )
            return
        self._diagnostics_running = True
        self._busy = True
        self.audio_diagnostics_changed.emit(
            {
                "state": "running",
                "summary": {
                    "status": "running",
                    "title": "Escuchando MIC y SYS…",
                    "detail": "Habla al micrófono y reproduce audio durante unos segundos.",
                },
            }
        )

        def worker() -> None:
            try:
                result = self.audio_diagnostics.run()
                result["state"] = "complete"
                self._last_audio_diagnostics = result
                result["model"] = {
                    "status": "ready" if self.engine and self.engine.model else "unavailable",
                    "model": self.engine.model_key if self.engine else None,
                    "device": self.engine.device if self.engine else None,
                    "compute_type": self.engine.compute_type if self.engine else None,
                }
                summary = result["summary"]
                lines = [summary["title"], summary["detail"]]
                for label, key in (("MIC", "mic"), ("SYS", "system")):
                    source = result[key]
                    level = source.get("peak_dbfs")
                    level_text = f"{level:.1f} dBFS" if isinstance(level, (int, float)) else "sin nivel"
                    lines.append(
                        f"{label}: {source.get('status')} · {source.get('device') or 'sin dispositivo'} · {level_text}"
                    )
                lines.append("Privacidad: muestras descartadas · sin WAV · sin transcripción · sin nube")
                message = "\n".join(lines)
            except Exception as exc:
                message = f"No se pudieron consultar los dispositivos: {exc}"
                result = {
                    "state": "error",
                    "summary": {
                        "status": "fail",
                        "title": "El diagnóstico no terminó",
                        "detail": str(exc),
                    },
                }
            finally:
                self._diagnostics_running = False
                self._busy = False
            self.diagnostics_ready.emit(message)
            self.audio_diagnostics_changed.emit(result)

        threading.Thread(target=worker, name="wk-device-diagnostics", daemon=True).start()

    @Slot(dict)
    def save_audio_routes(self, values: dict) -> None:
        if set(values) != {"input_device", "system_audio_device"}:
            self.error_raised.emit("Rutas incompletas", "Selecciona MIC y SYS antes de guardar.")
            return
        if self._busy or not self.config or not self.dictation or not self.coordinator:
            self.error_raised.emit("Audio ocupado", "Espera a que termine la operación actual.")
            return
        session = self.service.session if self.service else None
        if self.dictation.is_recording or (session and session.status == SessionStatus.RECORDING):
            self.error_raised.emit(
                "Hay audio activo",
                "Termina el dictado o pausa la sesión antes de cambiar dispositivos.",
            )
            return
        input_device = values["input_device"]
        system_device = str(values["system_audio_device"] or "default").strip()
        if input_device != "default":
            if isinstance(input_device, bool):
                self.error_raised.emit("MIC inválido", "Elige una entrada detectada o el valor predeterminado.")
                return
            try:
                input_device = int(input_device)
            except (TypeError, ValueError):
                self.error_raised.emit("MIC inválido", "Elige una entrada detectada o el valor predeterminado.")
                return
        known_devices = self._last_audio_diagnostics.get("devices", [])
        selected_input = next(
            (
                device
                for device in known_devices
                if device.get("device_id") == input_device and device.get("input_channels")
            ),
            None,
        )
        if input_device != "default" and selected_input is None:
            self.error_raised.emit("MIC no comprobado", "Ejecuta Probar MIC y SYS y elige una entrada detectada.")
            return
        input_device_name = str(selected_input.get("name", "")).strip() or None if selected_input else None
        known_system_outputs = self._last_audio_diagnostics.get("system_outputs", [])
        system_output_names = {str(output.get("name", "")) for output in known_system_outputs if output.get("name")}
        if not system_output_names:
            # Compatibility with diagnostic reports created before SYS loopback
            # enumeration was separated from PortAudio device enumeration.
            system_output_names = {
                str(device.get("name", ""))
                for device in known_devices
                if device.get("output_channels") and device.get("name")
            }
        if system_device != "default" and system_device not in system_output_names:
            self.error_raised.emit("SYS no comprobado", "Ejecuta Probar MIC y SYS y elige una salida detectada.")
            return
        try:
            self.config.update_user_paths(
                {
                    "audio.input_device": input_device,
                    "audio.input_device_name": input_device_name,
                    "capture.meeting.system_audio_device": system_device,
                }
            )
        except Exception as exc:
            self._logger.exception("Audio route update failed")
            self.error_raised.emit("No se guardaron las rutas", str(exc))
            return

        normalized_input = None if input_device == "default" else input_device
        self.dictation.input_device = normalized_input
        self.dictation.input_device_name = input_device_name
        recorder = self.coordinator.recorder
        recorder.audio_config["input_device"] = input_device
        recorder.audio_config["input_device_name"] = input_device_name
        recorder.config["system_audio_device"] = system_device
        if self.audio_diagnostics:
            self.audio_diagnostics.input_device = normalized_input
            self.audio_diagnostics.input_device_name = input_device_name
            self.audio_diagnostics.system_device = system_device
        current = {
            "input_device": input_device,
            "input_device_name": input_device_name,
            "system_audio_device": system_device,
        }
        self.audio_routes_changed.emit(current)
        self.settings_status_changed.emit("success", "Audio guardado · se aplicará a la próxima captura")

    @Slot()
    def open_library_folder(self) -> None:
        self.library_root.mkdir(parents=True, exist_ok=True)
        open_file(str(self.library_root))

    @Slot()
    def open_privacy_notice(self) -> None:
        path = Path(resolve_asset_path("assets/PRIVACY_RECORDING_NOTICE.md"))
        if not path.is_file():
            self.error_raised.emit("Aviso no disponible", "El archivo de privacidad no está en esta instalación.")
            return
        open_file(str(path))

    @Slot()
    def export_diagnostics_bundle(self) -> None:
        if self._diagnostics_exporting:
            return
        self._diagnostics_exporting = True
        self.settings_status_changed.emit("working", "Diagnóstico · preparando paquete privado…")

        def worker() -> None:
            try:
                whisper = self.config.get_whisper_config() if self.config else {}
                audio = self.config.get_audio_config() if self.config else {}
                meeting = self.config.get_meeting_capture_config() if self.config else {}
                hotkeys = self.config.get_hotkey_config() if self.config else {}
                session = self.service.session if self.service else None
                result = self.diagnostics_bundle.create(
                    version=get_version(),
                    application={
                        "ready": self._ready,
                        "busy": self._busy,
                        "model": self.engine.model_key if self.engine else None,
                        "device": self.engine.device if self.engine else None,
                        "compute_type": self.engine.compute_type if self.engine else None,
                        "model_loaded": bool(self.engine and self.engine.model),
                        "model_load_ms": getattr(self.engine, "model_load_ms", None),
                        "dictation_active": bool(self.dictation and self.dictation.is_recording),
                        "session_status": session.status.value if session else None,
                    },
                    safe_settings={
                        "model": whisper.get("model"),
                        "device": whisper.get("device"),
                        "compute_type": whisper.get("compute_type"),
                        "language": whisper.get("language"),
                        "hotkeys": {
                            key: hotkeys.get(key)
                            for key in (
                                "recording_hotkey",
                                "stop_key",
                                "meeting_hotkey",
                                "meeting_continuous_hotkey",
                                "meeting_mic_only_hotkey",
                                "meeting_sys_only_hotkey",
                            )
                            if hotkeys.get(key) is not None
                        },
                        "audio_routes": {
                            "input_device": audio.get("input_device", "default"),
                            "input_device_name": audio.get("input_device_name"),
                            "system_audio_device": meeting.get("system_audio_device", "default"),
                        },
                    },
                    audio_diagnostics=self._last_audio_diagnostics,
                )
            except Exception as exc:
                self._logger.exception("Diagnostics bundle export failed")
                self.error_raised.emit("No se creó el diagnóstico", str(exc))
                self.settings_status_changed.emit("error", "Diagnóstico · no se pudo crear")
            else:
                self.operation_finished.emit("diagnostics_bundle", result)
                self.settings_status_changed.emit("success", "Diagnóstico · paquete privado creado")
            finally:
                self._diagnostics_exporting = False

        threading.Thread(target=worker, name="wk-diagnostics-bundle", daemon=True).start()

    @Slot()
    def shutdown(self) -> None:
        self._shutdown = True
        self._finish_acceptance_performance()
        if self.dictation and self.dictation.is_recording:
            self.dictation.cancel()
        if self.hotkey_listener:
            self.hotkey_listener.stop_listening()
        if self.coordinator:
            try:
                self.coordinator.interrupt()
            except Exception:
                self._logger.exception("Could not interrupt capture during shutdown")

    def _run_background(self, operation: str, action) -> None:
        def worker() -> None:
            try:
                result = action()
            except Exception as exc:
                self._logger.exception("WhisperKey operation failed: %s", operation)
                if operation == "initialize":
                    self.model_state_changed.emit(
                        "error",
                        "El motor no inició · revisa el modelo o elige otro",
                    )
                elif operation == "audio_import":
                    self.audio_import_state_changed.emit("error", f"Importación detenida · {exc}", 0)
                self.error_raised.emit("WhisperKey encontró un problema", str(exc))
                result = None
            finally:
                self._busy = False
            if operation == "initialize" and self._ready:
                self.model_state_changed.emit("ready", f"Modelo listo · {self.model_label}")
            elif self._ready:
                self.model_state_changed.emit("ready", f"Modelo listo · {self.model_label}")
            if operation in {"finalize", "audio_import"} and result and self.processing:
                # Persist the whole queue before the UI reports completion. A shutdown from this
                # point onward can be resumed deterministically at the next launch.
                self.processing.queue_jobs(self.service)
            self.operation_finished.emit(operation, result)
            self.publish_snapshot()
            self.refresh_library()
            if operation in {"finalize", "audio_import"} and result and self.processing:
                threading.Thread(
                    target=self._run_processing_jobs,
                    name="wk-processing",
                    daemon=True,
                ).start()
            elif operation == "initialize" and self._ready:
                threading.Thread(
                    target=self._resume_interrupted_processing,
                    name="wk-processing-resume",
                    daemon=True,
                ).start()

        threading.Thread(target=worker, name=f"wk-{operation}", daemon=True).start()

    def _run_processing_jobs(self) -> None:
        try:
            self.processing.run_all(
                self.service,
                callback=lambda job, status, detail: self.processing_job_changed.emit(job, status, detail),
            )
        finally:
            self.publish_snapshot()
            self.refresh_library()

    def _resume_interrupted_processing(self) -> None:
        """Resume only sessions that were durably queued or interrupted mid-job."""
        resumed = False
        for metadata in discover_sessions(self.library_root):
            if metadata.get("status") != SessionStatus.COMPLETED.value:
                continue
            folder = Path(str(metadata.get("folder", "")))
            timeline = folder / "timeline.jsonl"
            if not timeline.is_file():
                continue
            try:
                events = SessionService(self.library_root).repository.read_events(folder)
                latest: dict[str, str] = {}
                for event in events:
                    if event.get("type") == "processing_job":
                        payload = event.get("payload", {})
                        latest[str(payload.get("job", ""))] = str(payload.get("status", ""))
                if not any(status in {"queued", "processing"} for status in latest.values()):
                    continue
                service = SessionService(self.library_root)
                service.load(str(metadata["session_id"]))
                ProcessingService(self.diarization_model_root).run_pending(service)
                resumed = True
            except Exception:
                self._logger.exception("Could not resume processing for %s", metadata.get("session_id"))
        if resumed:
            self.refresh_library()

    def _refresh_derived_package(self, jobs: tuple[str, ...]) -> dict[str, str]:
        if not self.processing or not self.service:
            return {}
        statuses = {}
        for job in jobs:
            status, _detail = self.processing.run_job(
                self.service,
                job,
                callback=lambda job_name, status, detail: self.processing_job_changed.emit(
                    job_name,
                    status,
                    detail,
                ),
            )
            statuses[job] = status
        return statuses

    def _on_health(self, health: SourceHealth) -> None:
        self._health[health.source_id] = {
            "status": health.status,
            "detail": health.detail,
            "fatal": health.fatal,
        }

    @staticmethod
    def _format_offset(value: int) -> str:
        seconds = max(0, value // 1000)
        return f"{seconds // 60:02d}:{seconds % 60:02d}"
