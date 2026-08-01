import importlib.metadata
import importlib.resources
import os
import subprocess
import sys
import tomllib
from pathlib import Path

_DLL_DIRECTORY_HANDLES = []


class OptionalComponent:
    def __init__(self, component):
        self._component = component

    def __getattr__(self, name):
        if self._component and hasattr(self._component, name):
            attr = getattr(self._component, name)
            return attr
        else:
            # Return a no-op function for missing methods/attributes
            return lambda *args, **kwargs: None


def beautify_hotkey(hotkey_string: str) -> str:
    if not hotkey_string:
        return ""

    return hotkey_string.replace("+", "+").upper()


def parse_hotkey(hotkey_string: str) -> list:
    if not hotkey_string:
        return []
    return hotkey_string.lower().split("+")


def is_installed_package():
    # Check if running from an installed package
    return bool(getattr(sys, "frozen", False)) or "site-packages" in __file__


def get_user_app_data_path():
    from .platform import paths

    whisperkey_dir = paths.get_app_data_path()
    whisperkey_dir.mkdir(parents=True, exist_ok=True)
    return str(whisperkey_dir)


def open_file(path):
    from .platform import paths

    paths.open_file(path)


def resolve_asset_path(relative_path: str) -> str:
    if not relative_path or os.path.isabs(relative_path):
        return relative_path

    if is_installed_package():
        files = importlib.resources.files("whisper_key")
        return str(files / relative_path)

    return str(Path(__file__).parent / relative_path)


def setup_portaudio_path():
    if sys.platform != "win32":
        return

    path_entries = []
    assets_dir = Path(resolve_asset_path("platform/windows/assets"))
    if assets_dir.exists():
        path_entries.append(assets_dir)

    runtime_roots = [Path(sys.prefix) / "Lib" / "site-packages"]
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        runtime_roots.insert(0, Path(frozen_root))

    for runtime_root in runtime_roots:
        nvidia_root = runtime_root / "nvidia"
        if not nvidia_root.exists():
            continue
        for package_name in ("cublas", "cuda_runtime", "cuda_nvrtc", "cudnn"):
            bin_dir = nvidia_root / package_name / "bin"
            if not bin_dir.exists() or bin_dir in path_entries:
                continue
            path_entries.append(bin_dir)
            if hasattr(os, "add_dll_directory"):
                # Keep the handles alive for the lifetime of the process. Closing
                # one removes that directory from Windows' DLL search path.
                _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(str(bin_dir)))

    if path_entries:
        os.environ["PATH"] = os.pathsep.join(str(p) for p in path_entries) + os.pathsep + os.environ.get("PATH", "")


def restart_or_exit(message_restart, message_exit):
    pyapp_exe = os.environ.get("PYAPP", "")
    if os.path.isfile(pyapp_exe):
        print(message_restart)
        subprocess.Popen([pyapp_exe], creationflags=subprocess.CREATE_NEW_CONSOLE)
    else:
        print(message_exit)
    sys.exit(0)


def get_version():
    if is_installed_package():
        try:
            return importlib.metadata.version("whisper-key-local")
        except importlib.metadata.PackageNotFoundError:
            # PyInstaller does not guarantee that distribution metadata is
            # collected, so frozen builds keep a safe embedded fallback.
            return "0.9.0"

    pyproject_path = Path(__file__).parent.parent.parent / "pyproject.toml"
    with open(pyproject_path, "rb") as f:
        data = tomllib.load(f)
        return f"{data['project']['version']}-dev"
