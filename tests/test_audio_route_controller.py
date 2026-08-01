from types import SimpleNamespace

from whisper_key.domain.session import SessionStatus
from whisper_key.ui.controller import AppController


class FakeConfig:
    def __init__(self, error=None):
        self.updates = []
        self.error = error

    def update_user_paths(self, values):
        if self.error:
            raise self.error
        self.updates.append(values)


def ready_controller(tmp_path):
    controller = AppController(tmp_path / "library", tmp_path / "models")
    controller.config = FakeConfig()
    controller.dictation = SimpleNamespace(input_device=None, is_recording=False)
    recorder = SimpleNamespace(audio_config={}, config={})
    controller.coordinator = SimpleNamespace(recorder=recorder)
    controller.service = SimpleNamespace(session=None)
    controller.audio_diagnostics = SimpleNamespace(input_device=None, system_device="default")
    controller._last_audio_diagnostics = {
        "devices": [
            {"device_id": 4, "name": "USB MIC", "input_channels": 1, "output_channels": 0},
            {"device_id": 8, "name": "Studio Speakers", "input_channels": 0, "output_channels": 2},
        ],
        "system_outputs": [{"name": "Studio Speakers", "default": True}],
    }
    return controller, recorder


def test_audio_routes_persist_once_and_apply_to_all_next_captures(tmp_path):
    controller, recorder = ready_controller(tmp_path)
    published = []
    controller.audio_routes_changed.connect(published.append)

    controller.save_audio_routes({"input_device": 4, "system_audio_device": "Studio Speakers"})

    assert controller.config.updates == [
        {
            "audio.input_device": 4,
            "audio.input_device_name": "USB MIC",
            "capture.meeting.system_audio_device": "Studio Speakers",
        }
    ]
    assert controller.dictation.input_device == 4
    assert controller.dictation.input_device_name == "USB MIC"
    assert recorder.audio_config["input_device"] == 4
    assert recorder.audio_config["input_device_name"] == "USB MIC"
    assert recorder.config["system_audio_device"] == "Studio Speakers"
    assert controller.audio_diagnostics.input_device == 4
    assert controller.audio_diagnostics.input_device_name == "USB MIC"
    assert controller.audio_diagnostics.system_device == "Studio Speakers"
    assert published[-1] == {
        "input_device": 4,
        "input_device_name": "USB MIC",
        "system_audio_device": "Studio Speakers",
    }


def test_audio_route_change_rejects_unknown_or_active_devices(tmp_path):
    controller, _recorder = ready_controller(tmp_path)
    errors = []
    controller.error_raised.connect(lambda title, detail: errors.append((title, detail)))

    controller.save_audio_routes({"input_device": 99, "system_audio_device": "Studio Speakers"})
    assert controller.config.updates == []
    assert errors[-1][0] == "MIC no comprobado"

    controller.service.session = SimpleNamespace(status=SessionStatus.RECORDING)
    controller.save_audio_routes({"input_device": "default", "system_audio_device": "default"})
    assert controller.config.updates == []
    assert errors[-1][0] == "Hay audio activo"


def test_audio_route_change_accepts_defaults_without_diagnostics(tmp_path):
    controller, recorder = ready_controller(tmp_path)
    controller._last_audio_diagnostics = {}

    controller.save_audio_routes({"input_device": "default", "system_audio_device": "default"})

    assert controller.config.updates[-1] == {
        "audio.input_device": "default",
        "audio.input_device_name": None,
        "capture.meeting.system_audio_device": "default",
    }
    assert controller.dictation.input_device is None
    assert recorder.audio_config["input_device"] == "default"
    assert recorder.config["system_audio_device"] == "default"


def test_audio_route_transaction_failure_does_not_change_runtime(tmp_path):
    controller, recorder = ready_controller(tmp_path)
    controller.config = FakeConfig(RuntimeError("disk unavailable"))
    errors = []
    controller.error_raised.connect(lambda title, detail: errors.append((title, detail)))

    controller.save_audio_routes({"input_device": 4, "system_audio_device": "Studio Speakers"})

    assert controller.dictation.input_device is None
    assert recorder.audio_config == {}
    assert recorder.config == {}
    assert errors[-1] == ("No se guardaron las rutas", "disk unavailable")
