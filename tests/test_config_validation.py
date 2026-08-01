import copy
import logging
from pathlib import Path

from ruamel.yaml import YAML

from whisper_key.config_manager import validate_config

CONFIG_PATH = Path(__file__).resolve().parents[1] / "src" / "whisper_key" / "config.defaults.yaml"


def load_defaults():
    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        return YAML(typ="safe").load(handle)


def test_dictation_is_unlimited_and_vad_stops_after_thirty_seconds_by_default():
    defaults = load_defaults()

    assert defaults["audio"]["max_duration"] == 0
    assert defaults["vad"]["vad_realtime_enabled"] is True
    assert defaults["vad"]["vad_silence_timeout_seconds"] == 30.0


def test_invalid_capture_values_reset_to_defaults():
    defaults = load_defaults()
    config = copy.deepcopy(defaults)
    config["capture"]["mode"] = "unknown"
    config["capture"]["meeting"]["system_audio_backend"] = "unknown"
    config["capture"]["meeting"]["split_on_pause_seconds"] = 500
    validate_config(config, defaults, logging.getLogger("test"))
    assert config["capture"]["mode"] == defaults["capture"]["mode"]
    assert config["capture"]["meeting"]["system_audio_backend"] == "auto"
    assert config["capture"]["meeting"]["split_on_pause_seconds"] == 1.2


def test_invalid_retention_values_reset_without_changing_preserve_all_default():
    defaults = load_defaults()
    config = copy.deepcopy(defaults)
    config["retention"]["learning"] = "delete_now"
    config["retention"]["dictation"] = "none"
    config["retention"]["marker_context_before_ms"] = -1
    config["retention"]["marker_context_after_ms"] = 999999

    validate_config(config, defaults, logging.getLogger("test"))

    assert config["retention"]["learning"] == "all"
    assert config["retention"]["dictation"] == "all"
    assert config["retention"]["marker_context_before_ms"] == 30000
    assert config["retention"]["marker_context_after_ms"] == 30000
