from types import SimpleNamespace

from whisper_key.ui import controller as controller_module
from whisper_key.ui.controller import AppController


class ImmediateThread:
    def __init__(self, *, target, **_kwargs):
        self.target = target

    def start(self):
        self.target()


class FakeConfig:
    @staticmethod
    def get_whisper_config():
        return {"model": "large-v3-turbo", "device": "cuda", "compute_type": "float16", "language": "auto"}

    @staticmethod
    def get_audio_config():
        return {"input_device": 4, "input_device_name": "Private microphone name"}

    @staticmethod
    def get_meeting_capture_config():
        return {"system_audio_device": "Private speakers name"}

    @staticmethod
    def get_hotkey_config():
        return {"recording_hotkey": "ctrl+win", "stop_key": "ctrl", "private": "excluded"}


def test_controller_exports_allowlisted_diagnostics_without_blocking_capture(monkeypatch, tmp_path):
    controller = AppController(tmp_path / "library", tmp_path / "models")
    controller.config = FakeConfig()
    controller.engine = SimpleNamespace(
        model_key="large-v3-turbo",
        device="cuda",
        compute_type="float16",
        model=object(),
        model_load_ms=1234,
    )
    controller.dictation = SimpleNamespace(is_recording=True)
    controller.service = SimpleNamespace(session=SimpleNamespace(status=SimpleNamespace(value="paused")))
    controller._ready = True
    captured = {}

    class FakeBundle:
        @staticmethod
        def create(**kwargs):
            captured.update(kwargs)
            return {"path": str(tmp_path / "diagnostics.zip"), "bytes": 12, "sha256": "a" * 64}

    controller.diagnostics_bundle = FakeBundle()
    monkeypatch.setattr(controller_module.threading, "Thread", ImmediateThread)
    operations = []
    statuses = []
    controller.operation_finished.connect(lambda operation, result: operations.append((operation, result)))
    controller.settings_status_changed.connect(lambda state, message: statuses.append((state, message)))

    controller.export_diagnostics_bundle()

    assert operations[0][0] == "diagnostics_bundle"
    assert statuses[-1] == ("success", "Diagnóstico · paquete privado creado")
    assert captured["application"]["dictation_active"] is True
    assert captured["application"]["session_status"] == "paused"
    assert captured["safe_settings"]["hotkeys"] == {
        "recording_hotkey": "ctrl+win",
        "stop_key": "ctrl",
    }
    assert controller._diagnostics_exporting is False
