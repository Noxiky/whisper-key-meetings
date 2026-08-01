import logging
import time

from .platform import hotkeys
from .state_manager import StateManager

POST_STOP_COOLDOWN_SECONDS = 0.6


class HotkeyListener:
    def __init__(
        self,
        state_manager: StateManager,
        recording_hotkey: str,
        stop_key: str,
        auto_send_key: str = None,
        cancel_combination: str = None,
        command_hotkey: str = None,
        meeting_hotkey: str = None,
        meeting_continuous_hotkey: str = None,
        meeting_mic_only_hotkey: str = None,
        meeting_sys_only_hotkey: str = None,
        recording_mode: str = "toggle",
    ):
        self.state_manager = state_manager
        self.recording_hotkey = recording_hotkey
        self.stop_key = stop_key
        self.auto_send_key = auto_send_key
        self.cancel_combination = cancel_combination
        self.command_hotkey = command_hotkey
        self.meeting_hotkey = meeting_hotkey
        self.meeting_continuous_hotkey = meeting_continuous_hotkey
        self.meeting_mic_only_hotkey = meeting_mic_only_hotkey
        self.meeting_sys_only_hotkey = meeting_sys_only_hotkey
        self.recording_mode = recording_mode
        self.keys_armed = True
        self.is_listening = False
        self._last_stop_at = 0.0
        self.logger = logging.getLogger(__name__)

        self._setup_hotkeys()

        self.start_listening()

    def _setup_hotkeys(self):
        hotkey_configs = []

        if self.recording_mode == "push_to_talk":
            hotkey_configs.append(
                {
                    "combination": self.recording_hotkey,
                    "callback": self._standard_hotkey_pressed,
                    "release_callback": self._push_to_talk_released,
                    "name": "standard (push-to-talk)",
                }
            )
        else:
            hotkey_configs.append(
                {
                    "combination": self.recording_hotkey,
                    "callback": self._standard_hotkey_pressed,
                    "release_callback": self._arm_keys_on_release,
                    "name": "standard",
                }
            )

        hotkey_configs.append(
            {
                "combination": self.stop_key,
                "callback": self._stop_key_pressed,
                "release_callback": self._arm_keys_on_release,
                "name": "stop",
            }
        )

        if self.auto_send_key:
            hotkey_configs.append(
                {
                    "combination": self.auto_send_key,
                    "callback": self._auto_send_key_pressed,
                    "release_callback": self._arm_keys_on_release,
                    "name": "auto-send",
                }
            )

        if self.cancel_combination:
            hotkey_configs.append(
                {"combination": self.cancel_combination, "callback": self._cancel_hotkey_pressed, "name": "cancel"}
            )

        if self.command_hotkey:
            if self.recording_mode == "push_to_talk":
                hotkey_configs.append(
                    {
                        "combination": self.command_hotkey,
                        "callback": self._command_hotkey_pressed,
                        "release_callback": self._push_to_talk_released,
                        "name": "command (push-to-talk)",
                    }
                )
            else:
                hotkey_configs.append(
                    {"combination": self.command_hotkey, "callback": self._command_hotkey_pressed, "name": "command"}
                )

        if self.meeting_hotkey:
            hotkey_configs.append(
                {
                    "combination": self.meeting_hotkey,
                    "callback": self._meeting_hotkey_pressed,
                    "release_callback": self._arm_keys_on_release,
                    "name": "meeting listener",
                }
            )

        if self.meeting_continuous_hotkey:
            hotkey_configs.append(
                {
                    "combination": self.meeting_continuous_hotkey,
                    "callback": self._meeting_continuous_hotkey_pressed,
                    "release_callback": self._arm_keys_on_release,
                    "name": "meeting continuous",
                }
            )

        if self.meeting_mic_only_hotkey:
            hotkey_configs.append(
                {
                    "combination": self.meeting_mic_only_hotkey,
                    "callback": self._meeting_mic_only_hotkey_pressed,
                    "release_callback": self._arm_keys_on_release,
                    "name": "meeting mic-only",
                }
            )

        if self.meeting_sys_only_hotkey:
            hotkey_configs.append(
                {
                    "combination": self.meeting_sys_only_hotkey,
                    "callback": self._meeting_sys_only_hotkey_pressed,
                    "release_callback": self._arm_keys_on_release,
                    "name": "meeting sys-only",
                }
            )

        hotkey_configs.sort(key=self._get_hotkey_combination_specificity, reverse=True)

        self.hotkey_bindings = []
        for config in hotkey_configs:
            hotkey = config["combination"].lower().strip()
            self.hotkey_bindings.append([hotkey, config["callback"], config.get("release_callback") or None, False])
            self.logger.info(f"Configured {config['name']} hotkey: {hotkey}")

        self.logger.info(f"Total hotkeys configured: {len(self.hotkey_bindings)}")

    def _get_hotkey_combination_specificity(self, hotkey_config: dict) -> int:
        combination = hotkey_config["combination"].lower()
        return len(combination.split("+"))

    def _standard_hotkey_pressed(self):
        if time.monotonic() - self._last_stop_at < POST_STOP_COOLDOWN_SECONDS:
            self.logger.debug("Standard hotkey ignored - within post-stop cooldown")
            return

        self.keys_armed = False

        recorder = getattr(self.state_manager, "audio_recorder", None)
        currently_recording = bool(recorder and recorder.get_recording_status())

        if self.recording_mode != "push_to_talk" and currently_recording:
            self.logger.info(f"Standard hotkey pressed (toggle stop): {self.recording_hotkey}")
            self._last_stop_at = time.monotonic()
            self.state_manager.stop_recording()
            return

        self.logger.info(f"Standard hotkey pressed (start): {self.recording_hotkey}")
        self.state_manager.start_recording()

    def _push_to_talk_released(self):
        self.logger.info("Push-to-talk key released")
        self.state_manager.stop_recording()

    def _stop_key_pressed(self):
        self.logger.debug(f"Stop key pressed: {self.stop_key}, keys_armed={self.keys_armed}")

        if self.keys_armed:
            self.logger.info(f"Stop key activated: {self.stop_key}")
            if self.state_manager.stop_recording():
                self._last_stop_at = time.monotonic()
        else:
            self.logger.debug("Stop key ignored - waiting for key release first")

    def _auto_send_key_pressed(self):
        self.logger.debug(f"Auto-send key pressed: {self.auto_send_key}, keys_armed={self.keys_armed}")

        if not self.state_manager.audio_recorder.get_recording_status():
            self.logger.debug("Auto-send key ignored - not currently recording")
            return

        if not self.keys_armed:
            self.logger.debug("Auto-send key ignored - waiting for key release first")
            return

        self.keys_armed = False

        self.state_manager.stop_recording(use_auto_enter=True)

    def _cancel_hotkey_pressed(self):
        self.logger.info(f"Cancel hotkey pressed: {self.cancel_combination}")
        self.state_manager.cancel_recording_hotkey_pressed()

    def _command_hotkey_pressed(self):
        self.logger.info(f"Command hotkey pressed: {self.command_hotkey}")
        self.keys_armed = False
        self.state_manager.start_command_recording()

    def _meeting_hotkey_pressed(self):
        self.logger.info(f"Meeting hotkey pressed: {self.meeting_hotkey}")
        self.keys_armed = False
        self.state_manager.toggle_meeting_recording()

    def _meeting_continuous_hotkey_pressed(self):
        self.logger.info(f"Meeting continuous hotkey pressed: {self.meeting_continuous_hotkey}")
        self.keys_armed = False
        self.state_manager.toggle_meeting_recording(auto_stop_seconds=0)

    def _meeting_mic_only_hotkey_pressed(self):
        self.logger.info(f"Meeting mic-only hotkey pressed: {self.meeting_mic_only_hotkey}")
        self.keys_armed = False
        self.state_manager.toggle_meeting_recording(
            capture_microphone=True,
            capture_system_audio=False,
            auto_stop_seconds=0,
        )

    def _meeting_sys_only_hotkey_pressed(self):
        self.logger.info(f"Meeting sys-only hotkey pressed: {self.meeting_sys_only_hotkey}")
        self.keys_armed = False
        self.state_manager.toggle_meeting_recording(
            capture_microphone=False,
            capture_system_audio=True,
            auto_stop_seconds=0,
        )

    def _arm_keys_on_release(self):
        self.logger.debug("Key released - arming stop/auto-send keys")
        self.keys_armed = True

    def start_listening(self):
        if self.is_listening:
            return

        try:
            hotkeys.register(self.hotkey_bindings)
            hotkeys.start()
            self.is_listening = True

        except Exception as e:
            self.logger.error(f"Failed to start hotkey listener: {e}")
            raise

    def stop_listening(self):
        if not self.is_listening:
            return

        try:
            hotkeys.stop()
            self.is_listening = False
            self.logger.info("Hotkey listener stopped")

        except Exception as e:
            self.logger.error(f"Error stopping hotkey listener: {e}")

    def change_hotkey_config(self, setting: str, value):
        valid_settings = [
            "recording_hotkey",
            "stop_key",
            "auto_send_key",
            "cancel_combination",
            "command_hotkey",
            "meeting_hotkey",
            "meeting_continuous_hotkey",
            "meeting_mic_only_hotkey",
            "meeting_sys_only_hotkey",
            "recording_mode",
        ]

        if setting not in valid_settings:
            raise ValueError(f"Invalid setting '{setting}'. Valid options: {valid_settings}")

        old_value = getattr(self, setting)

        if old_value == value:
            return

        setattr(self, setting, value)
        self.logger.info(f"Changed {setting}: {old_value} -> {value}")

        self.stop_listening()
        self._setup_hotkeys()
        self.start_listening()

    def replace_hotkey_config(self, values: dict) -> None:
        valid_settings = {
            "recording_hotkey",
            "stop_key",
            "auto_send_key",
            "cancel_combination",
            "command_hotkey",
            "meeting_hotkey",
            "meeting_continuous_hotkey",
            "meeting_mic_only_hotkey",
            "meeting_sys_only_hotkey",
            "recording_mode",
        }
        unknown = set(values) - valid_settings
        if unknown:
            raise ValueError(f"Invalid hotkey settings: {sorted(unknown)}")
        previous = {setting: getattr(self, setting) for setting in values}
        self.stop_listening()
        try:
            for setting, value in values.items():
                setattr(self, setting, value)
            self._setup_hotkeys()
            self.start_listening()
        except Exception:
            for setting, value in previous.items():
                setattr(self, setting, value)
            self._setup_hotkeys()
            self.start_listening()
            raise

    def is_active(self) -> bool:
        return self.is_listening
