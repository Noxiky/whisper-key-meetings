from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class GuiHotkeyBridge(QObject):
    dictation_toggle_requested = Signal()
    dictation_cancel_requested = Signal()
    meeting_toggle_requested = Signal(bool, bool, float)
    finish_stage_requested = Signal()

    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.audio_recorder = self
        self.dictation_toggle_requested.connect(controller.toggle_dictation)
        self.dictation_cancel_requested.connect(controller.cancel_dictation)
        self.meeting_toggle_requested.connect(controller.toggle_meeting_capture)
        self.finish_stage_requested.connect(controller.finish_stage)

    def get_recording_status(self) -> bool:
        return bool(self.controller.dictation and self.controller.dictation.is_recording)

    def start_recording(self) -> bool:
        self.dictation_toggle_requested.emit()
        return True

    def stop_recording(self, use_auto_enter: bool = False) -> bool:
        del use_auto_enter
        if self.get_recording_status():
            self.dictation_toggle_requested.emit()
            return True
        # Ctrl is the default dictation stop key and also the first key in Ctrl+Win.
        # It must never finish a durable session while the user is attempting to
        # start Dictation; durable modes have their own explicit toggle/finalize
        # controls.  AppController then rejects Dictation while capture is active.
        return False

    def cancel_recording_hotkey_pressed(self) -> None:
        self.dictation_cancel_requested.emit()

    def start_command_recording(self) -> None:
        self.dictation_toggle_requested.emit()

    def toggle_meeting_recording(
        self,
        *,
        capture_microphone: bool | None = None,
        capture_system_audio: bool | None = None,
        auto_stop_seconds: float | None = None,
    ) -> bool:
        self.meeting_toggle_requested.emit(
            True if capture_microphone is None else capture_microphone,
            True if capture_system_audio is None else capture_system_audio,
            -1.0 if auto_stop_seconds is None else float(auto_stop_seconds),
        )
        return True
