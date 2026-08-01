import platform as _platform

PLATFORM = "macos" if _platform.system() == "Darwin" else "windows"
IS_MACOS = PLATFORM == "macos"
IS_WINDOWS = PLATFORM == "windows"

if IS_MACOS:
    from .macos import app, console, gpu, hotkeys, icons, instance_lock, keyboard, paths, permissions
else:
    from .windows import app, console, gpu, hotkeys, icons, instance_lock, keyboard, paths, permissions

__all__ = [
    "IS_MACOS",
    "IS_WINDOWS",
    "PLATFORM",
    "app",
    "console",
    "gpu",
    "hotkeys",
    "icons",
    "instance_lock",
    "keyboard",
    "paths",
    "permissions",
]
