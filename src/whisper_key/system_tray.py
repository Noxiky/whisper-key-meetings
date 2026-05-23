import logging
import os
import signal
import threading
import time
from typing import Any, Optional, TYPE_CHECKING
from pathlib import Path

from .utils import open_file
from .platform import permissions, icons, console
from .floating_overlay import FloatingOverlay

try:
    import pystray
    from PIL import Image, ImageDraw
    TRAY_AVAILABLE = True
except ImportError:
    TRAY_AVAILABLE = False
    pystray = None
    Image = None
    ImageDraw = None

if TYPE_CHECKING:
    from .state_manager import StateManager
    from .config_manager import ConfigManager

class SystemTray:
    def __init__(self,
                 state_manager: 'StateManager',
                 tray_config: dict = None,
                 config_manager: Optional['ConfigManager'] = None,
                 model_registry = None,
                 console_config: dict = None):

        self.state_manager = state_manager
        self.tray_config = tray_config or {}
        self.config_manager = config_manager
        self.model_registry = model_registry
        self.console_config = console_config or {}
        self.logger = logging.getLogger(__name__)
               
        self.icon = None
        self.overlay = None
        self.is_running = False
        self.current_state = "idle"
        self.available = True
        self._pulse_thread = None
        self._pulse_stop = threading.Event()
        self._animated_icons = {}
        
        if self._check_tray_availability():
            self._load_icons_to_cache()
    
    def _check_tray_availability(self) -> bool:
        if not self.tray_config['enabled']:
            self.logger.warning("   ✗ System tray disabled in configuration")
            self.available = False

        elif not TRAY_AVAILABLE:
            self.logger.warning("   ✗ System tray not available - pystray or Pillow not installed")
            self.available = False

        return self.available

    def _load_icons_to_cache(self):
        try:
            self.icons = icons.get_tray_icons()
        except Exception as e:
            self.logger.error(f"Failed to load tray icons: {e}")
            self.icons = {
                "idle": self._create_fallback_icon("idle"),
                "recording": self._create_fallback_icon("recording"),
                "processing": self._create_fallback_icon("processing"),
            }
        self._animated_icons = self._build_animated_icons()
        
    def _build_animated_icons(self):
        animated = {}
        for state in ("recording", "processing"):
            base_icon = self.icons.get(state) or self.icons.get("idle")
            animated[state] = self._create_pulse_frames(base_icon, state)
        animated["meeting"] = animated["recording"]
        return animated

    def _create_pulse_frames(self, base_icon: Any, state: str) -> list:
        if Image is None or ImageDraw is None:
            return [base_icon]

        colors = {
            "recording": (40, 220, 100),
            "processing": (255, 176, 48),
        }
        accent = colors.get(state, (255, 255, 255))
        frames = []
        size = max(base_icon.size)
        scale = max(1, size // 32)
        wave_patterns = (
            (1, 2, 1),
            (2, 3, 2),
            (3, 4, 3),
            (2, 5, 2),
            (3, 4, 3),
            (2, 3, 2),
        )

        for frame_index, pattern in enumerate(wave_patterns):
            frame = Image.new("RGBA", (size, size), (0, 0, 0, 0))
            mic = base_icon.convert("RGBA")
            if mic.size != (size, size):
                mic = mic.resize((size, size), Image.NEAREST)
            frame.alpha_composite(mic, (0, 0))
            draw = ImageDraw.Draw(frame)

            for side in (-1, 1):
                anchor_x = size // 2 + side * int(size * 0.38)
                for idx, height_units in enumerate(pattern):
                    bar_height = height_units * scale * 2
                    bar_width = max(1, scale)
                    gap = idx * scale * 2
                    x0 = anchor_x + side * gap
                    if side < 0:
                        x0 -= bar_width
                    y0 = size // 2 - bar_height // 2
                    y1 = size // 2 + bar_height // 2
                    draw.rectangle(
                        (x0, y0, x0 + bar_width - 1, y1),
                        fill=accent + (180 - idx * 30,),
                    )

            if state == "processing":
                sparkle = [
                    (int(size * 0.76), int(size * 0.18)),
                    (int(size * 0.80), int(size * 0.22)),
                    (int(size * 0.74), int(size * 0.25)),
                ][frame_index % 3]
                draw.rectangle(
                    (sparkle[0], sparkle[1], sparkle[0] + scale, sparkle[1] + scale),
                    fill=accent + (230,),
                )

            frames.append(frame)
        return frames

    def _start_pulse_animation(self, state: str):
        if state not in self._animated_icons:
            return
        if self._pulse_thread and self._pulse_thread.is_alive():
            return
        self._pulse_stop.clear()

        def animate():
            frame_index = 0
            while not self._pulse_stop.is_set() and self.current_state in self._animated_icons:
                frames = self._animated_icons.get(self.current_state) or []
                if frames and self.icon:
                    self.icon.icon = frames[frame_index % len(frames)]
                    frame_index += 1
                time.sleep(0.18)

        self._pulse_thread = threading.Thread(target=animate, daemon=True)
        self._pulse_thread.start()

    def _stop_pulse_animation(self):
        self._pulse_stop.set()
        self._pulse_thread = None
    
    def _create_fallback_icon(self, state: str) -> Image.Image:
        colors = {
            'idle': (128, 128, 128),      # Gray
            'recording': (34, 139, 34),   # Green  
            'processing': (255, 165, 0)   # Orange
        }
        
        color = colors.get(state, (128, 128, 128))  # Default to gray
        icon = Image.new('RGBA', (16, 16), color + (255,))

        return icon
    
    def _build_model_menu_items(self, current_model: str, is_model_loading: bool) -> list:
        items = []

        if not self.model_registry:
            return items

        def make_model_selector(model_key):
            return lambda icon, item: self._select_model(model_key)

        def make_is_current(model_key):
            return lambda item: model_key == current_model

        def model_selection_enabled(item):
            return not is_model_loading

        first_group = True
        for group in self.model_registry.get_groups_ordered():
            models = self.model_registry.get_models_by_group(group)
            if not models:
                continue

            if not first_group:
                items.append(pystray.Menu.SEPARATOR)
            first_group = False

            for model in models:
                items.append(pystray.MenuItem(
                    model.label,
                    make_model_selector(model.key),
                    radio=True,
                    checked=make_is_current(model.key),
                    enabled=model_selection_enabled
                ))

        return items

    def _create_menu(self):
        try:
            app_state = self.state_manager.get_application_state()
            is_model_loading = app_state.get('model_loading', False)

            auto_paste_enabled = self.config_manager.get_setting('clipboard', 'auto_paste')
            current_model = self.config_manager.get_setting('whisper', 'model')

            available_hosts = self.state_manager.get_available_audio_hosts()
            current_host = self.state_manager.get_current_audio_host()

            def is_current_host(host_name):
                return lambda item: current_host == host_name

            def switch_host(host_name):
                return lambda icon, item: self._select_audio_host(host_name)

            audio_host_items = []
            if available_hosts:
                for host in available_hosts:
                    host_name = host['name']
                    audio_host_items.append(
                        pystray.MenuItem(
                            host_name,
                            switch_host(host_name),
                            radio=True,
                            checked=is_current_host(host_name)
                        )
                    )

            available_devices = self.state_manager.get_available_audio_devices(current_host)
            current_device = self.state_manager.get_current_audio_device_id()

            def is_current_device(dev_id):
                return lambda item: current_device == dev_id

            def switch_device(dev_id, dev_name):
                return lambda icon, item: self._select_audio_device(dev_id, dev_name)

            audio_device_items = []

            if available_devices:
                for device in available_devices:
                    device_id = device['id']
                    device_name = device['name']

                    audio_device_items.append(
                        pystray.MenuItem(
                            device_name,
                            switch_device(device_id, device['name']),
                            radio=True,
                            checked=is_current_device(device_id)
                        )
                    )

            model_sub_menu_items = self._build_model_menu_items(current_model, is_model_loading)

            voice_commands_enabled = self.config_manager.get_setting('voice_commands', 'enabled')

            menu_items = []

            if console.owns_console():
                menu_items.append(pystray.MenuItem("Show Console", self._show_console, default=True))
                menu_items.append(pystray.Menu.SEPARATOR)

            menu_items += [
                pystray.MenuItem("Open log file...", self._open_log_file),
                pystray.MenuItem("Open model cache...", self._open_model_cache),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Open config folder...", self._open_config_folder),
                pystray.MenuItem("Open settings file...", self._open_config_file),
                pystray.MenuItem("Open commands file...", self._open_commands_file) if voice_commands_enabled else None,
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    "Audio Host",
                    pystray.Menu(*audio_host_items)
                ) if audio_host_items else None,
                pystray.MenuItem(
                    f"Audio Source",
                    pystray.Menu(*audio_device_items)
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Auto-paste", lambda icon, item: self._set_transcription_mode(True), radio=True, checked=lambda item: auto_paste_enabled),
                pystray.MenuItem("Copy to clipboard", lambda icon, item: self._set_transcription_mode(False), radio=True, checked=lambda item: not auto_paste_enabled),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(f"Model: {current_model.title()}", pystray.Menu(*model_sub_menu_items)),
            ]

            menu_items.extend([
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Exit", self._quit_application_from_tray)
            ])

            menu = pystray.Menu(*[item for item in menu_items if item is not None])

            return menu 
                
        except Exception as e:
            self.logger.error(f"Error in _create_menu: {e}")
            raise

    def _open_config_folder(self, icon=None, item=None):
        try:
            config_dir = os.path.dirname(self.config_manager.user_settings_path)
            open_file(config_dir)
        except Exception as e:
            self.logger.error(f"Failed to open config folder: {e}")

    def _open_config_file(self, icon=None, item=None):
        try:
            open_file(self.config_manager.user_settings_path)
        except Exception as e:
            self.logger.error(f"Failed to open config file: {e}")

    def _open_commands_file(self, icon=None, item=None):
        try:
            commands_path = os.path.join(
                os.path.dirname(self.config_manager.user_settings_path),
                "commands.yaml"
            )
            open_file(commands_path)
        except Exception as e:
            self.logger.error(f"Failed to open commands file: {e}")

    def _open_log_file(self, icon=None, item=None):
        try:
            log_path = self.config_manager.get_log_file_path()
            open_file(log_path)
        except Exception as e:
            self.logger.error(f"Failed to open log file: {e}")

    def _open_model_cache(self, icon=None, item=None):
        try:
            cache_path = self.model_registry.get_hf_cache_path()
            os.makedirs(cache_path, exist_ok=True)
            open_file(cache_path)
        except Exception as e:
            self.logger.error(f"Failed to open model cache: {e}")

    def _set_transcription_mode(self, auto_paste: bool):
        if auto_paste:
            if not permissions.check_accessibility_permission():
                if not permissions.handle_missing_permission(self.config_manager):
                    return
                auto_paste = False

        self.state_manager.update_transcription_mode(auto_paste)
        self.icon.menu = self._create_menu()

    def _select_model(self, model_key: str):
        try:
            success = self.state_manager.request_model_change(model_key)

            if success:
                self.config_manager.update_user_setting('whisper', 'model', model_key)
                self.icon.menu = self._create_menu()
            else:
                self.logger.warning(f"Request to change model to {model_key} was not accepted")

        except Exception as e:
            self.logger.error(f"Error selecting model {model_key}: {e}")

    def _select_audio_host(self, host_name: str):
        try:
            success = self.state_manager.set_audio_host(host_name)
            if success:
                self.icon.menu = self._create_menu()
            else:
                self.logger.warning(f"Request to change audio host to {host_name} was not accepted")
        except Exception as e:
            self.logger.error(f"Error selecting audio host {host_name}: {e}")

    def _select_audio_device(self, device_id: int, device_name: str):
        success = self.state_manager.request_audio_device_change(device_id, device_name)

        if success:
            self.config_manager.update_user_setting('audio', 'input_device', device_id)
            self.icon.menu = self._create_menu()
        else:
            self.logger.warning(f"Request to change audio device to {device_id} was not accepted")

    def _show_console(self, icon=None, item=None):
        console.show()

    def apply_console_settings(self):
        if not console.owns_console() or not self.available:
            return
        if self.console_config.get('start_hidden', False):
            console.hide()
        console.start_minimize_monitor(console.hide)

    def _quit_application_from_tray(self, icon=None, item=None):        
        os.kill(os.getpid(), signal.SIGINT)

    def _quit_application_from_overlay(self):
        os.kill(os.getpid(), signal.SIGINT)
    
    def update_state(self, new_state: str):
        if not TRAY_AVAILABLE or not self.is_running:
            return
        
        self.current_state = new_state
        
        try:
            if self.overlay:
                self.overlay.update_state(new_state)

            if new_state in self._animated_icons:
                self._start_pulse_animation(new_state)
            else:
                self._stop_pulse_animation()
                if self.icon:
                    self.icon.icon = self.icons[new_state]
            self.icon.menu = self._create_menu()
        except Exception as e:
            self.logger.error(f"Failed to update tray icon: {e}")

    def refresh_menu(self):
        if not self.icon:
            return

        try:
            self.icon.menu = self._create_menu()
        except Exception as e:
            self.logger.error(f"Failed to refresh tray menu: {e}")
    
    def start(self):
        if not self.available:
            return False

        if self.is_running:
            self.logger.warning("System tray is already running")
            return True

        try:
            idle_icon = self.icons.get("idle")
            menu = self._create_menu()

            self.icon = pystray.Icon(
                name="whisper-key",
                icon=idle_icon,
                title="Whisper Key",
                menu=menu
            )

            self.icon.run_detached()

            self.overlay = FloatingOverlay(
                icons=self.icons,
                animated_icons=self._animated_icons,
                on_close=self._quit_application_from_overlay,
                logger=self.logger,
            )
            self.overlay.start()

            self.is_running = True
            print("   ✓ System tray icon is running...")
            print("   ✓ Floating status overlay is visible...")

            return True

        except Exception as e:
            self.logger.error(f"Failed to start system tray: {e}")
            return False
    
    def stop(self):
        if not self.is_running:
            return

        try:
            self._stop_pulse_animation()
            if self.overlay:
                self.overlay.stop()
            self.icon.stop()
            self.is_running = False

        except Exception as e:
            self.logger.error(f"Error stopping system tray: {e}")
