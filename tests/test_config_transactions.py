import logging

import pytest
from ruamel.yaml import YAML

import whisper_key.config_manager as config_module
from whisper_key.config_manager import ConfigManager


def manager_without_loading(tmp_path):
    manager = ConfigManager.__new__(ConfigManager)
    manager.user_settings_path = str(tmp_path / "user_settings.yaml")
    manager.logger = logging.getLogger("test-config-transactions")
    return manager


def test_user_config_write_replaces_atomically_and_leaves_no_temporary_file(tmp_path):
    manager = manager_without_loading(tmp_path)
    manager._write_user_config({"audio": {"input_device": 7}})

    content = (tmp_path / "user_settings.yaml").read_text(encoding="utf-8")
    parsed = YAML().load(content)
    assert parsed["audio"]["input_device"] == 7
    assert not list(tmp_path.glob(".user_settings.*.tmp"))


def test_failed_atomic_replace_preserves_previous_settings(monkeypatch, tmp_path):
    manager = manager_without_loading(tmp_path)
    target = tmp_path / "user_settings.yaml"
    target.write_text("original\n", encoding="utf-8")
    monkeypatch.setattr(config_module.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("locked")))

    with pytest.raises(OSError, match="locked"):
        manager._write_user_config({"audio": {"input_device": 9}})

    assert target.read_text(encoding="utf-8") == "original\n"
    assert not list(tmp_path.glob(".user_settings.*.tmp"))


def test_nested_path_update_is_one_transaction_and_rolls_back_on_failure(tmp_path):
    manager = manager_without_loading(tmp_path)
    manager.config = {
        "audio": {"input_device": "default", "input_device_name": None},
        "capture": {"meeting": {"system_audio_device": "default"}},
    }
    calls = []
    manager._save_user_overrides = lambda: calls.append("saved")
    manager.update_user_paths(
        {
            "audio.input_device": 3,
            "audio.input_device_name": "Podcast MIC",
            "capture.meeting.system_audio_device": "Speakers",
        }
    )

    assert calls == ["saved"]
    assert manager.config["audio"]["input_device"] == 3
    assert manager.config["audio"]["input_device_name"] == "Podcast MIC"
    assert manager.config["capture"]["meeting"]["system_audio_device"] == "Speakers"

    def fail():
        raise OSError("disk full")

    manager._save_user_overrides = fail
    with pytest.raises(OSError, match="disk full"):
        manager.update_user_paths(
            {
                "audio.input_device": 8,
                "audio.input_device_name": "Webcam MIC",
                "capture.meeting.system_audio_device": "HDMI",
            }
        )
    assert manager.config["audio"]["input_device"] == 3
    assert manager.config["audio"]["input_device_name"] == "Podcast MIC"
    assert manager.config["capture"]["meeting"]["system_audio_device"] == "Speakers"
