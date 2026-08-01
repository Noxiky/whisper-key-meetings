from types import SimpleNamespace

from whisper_key.ui.hotkey_bridge import GuiHotkeyBridge


def test_dictation_stop_prefix_never_finishes_a_durable_capture():
    controller = SimpleNamespace(
        dictation=None,
        service=SimpleNamespace(session=SimpleNamespace(status=SimpleNamespace(value="recording"))),
        toggle_dictation=lambda: None,
        cancel_dictation=lambda: None,
        toggle_meeting_capture=lambda *_args: None,
        finish_stage=lambda: None,
    )
    bridge = GuiHotkeyBridge(controller)
    finished = []
    bridge.finish_stage_requested.connect(lambda: finished.append(True))

    assert bridge.stop_recording() is False
    assert finished == []
