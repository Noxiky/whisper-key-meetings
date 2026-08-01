from whisper_key.infrastructure.audio_routes import AudioRouteUnavailable, resolve_input_device


class FakeSoundDevice:
    def __init__(self, devices):
        self.devices = devices

    def query_devices(self, device=None, kind=None):
        if device is None and kind is None:
            return self.devices
        if device is None and kind == "input":
            return next(item for item in self.devices if item.get("max_input_channels"))
        return next(item for item in self.devices if item.get("index") == device)


def test_default_input_follows_windows_without_pinning_a_name():
    device, name = resolve_input_device(FakeSoundDevice([]), "default", "Old MIC")

    assert device is None
    assert name is None


def test_persisted_input_name_wins_over_reused_numeric_id():
    sounddevice = FakeSoundDevice(
        [
            {"index": 4, "name": "Webcam MIC", "max_input_channels": 1},
            {"index": 9, "name": "Podcast MIC", "max_input_channels": 1},
        ]
    )

    device, name = resolve_input_device(sounddevice, 4, "Podcast MIC")

    assert device == 9
    assert name == "Podcast MIC"


def test_missing_persisted_input_never_opens_the_device_that_reused_its_id():
    sounddevice = FakeSoundDevice([{"index": 4, "name": "Webcam MIC", "max_input_channels": 1}])

    try:
        resolve_input_device(sounddevice, 4, "Podcast MIC")
    except AudioRouteUnavailable as exc:
        assert "Podcast MIC" in str(exc)
    else:
        raise AssertionError("Expected the missing explicit route to remain unavailable")
