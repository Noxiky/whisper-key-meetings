from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from PySide6.QtCore import QLockFile, QSettings, QStandardPaths, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from whisper_key.ui.controller import AppController
from whisper_key.ui.shell import MainWindow
from whisper_key.ui.theme import build_stylesheet
from whisper_key.utils import resolve_asset_path, setup_portaudio_path


def _configure_logging() -> None:
    from whisper_key.utils import get_user_app_data_path

    log_path = Path(get_user_app_data_path()) / "whisperkey-gui.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8")],
    )


def _build_tray(app: QApplication, window: MainWindow, controller: AppController) -> QSystemTrayIcon:
    icon_path = resolve_asset_path("platform/windows/assets/whisperkey-icon.ico")
    tray = QSystemTrayIcon(QIcon(icon_path), app)
    tray.setToolTip("WhisperKey · captura local")
    menu = QMenu()
    show = menu.addAction("Abrir WhisperKey")
    show.triggered.connect(window.show_and_raise)
    pause = menu.addAction("Pausar / continuar")
    pause.triggered.connect(controller.pause_or_resume)
    marker = menu.addAction("Marcar momento importante")
    marker.triggered.connect(lambda: controller.add_marker("important", "Bandeja"))
    menu.addSeparator()
    quit_action = menu.addAction("Salir")
    quit_action.triggered.connect(app.quit)
    tray.setContextMenu(menu)
    tray.activated.connect(
        lambda reason: window.show_and_raise() if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None
    )
    tray.show()
    return tray


def _acquire_instance_lock() -> QLockFile | None:
    runtime = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    lock_path = Path(runtime) / "whisperkey-gui.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(lock_path))
    lock.setStaleLockTime(0)
    return lock if lock.tryLock(50) else None


def main() -> int:
    setup_portaudio_path()
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--preview", action="store_true", help="Show the UI without loading audio or AI")
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--startup-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--library", type=Path, help="Override the local session library")
    args, qt_args = parser.parse_known_args()
    app = QApplication([sys.argv[0], *qt_args])
    # Packaged smoke tests must be able to validate a candidate while the
    # installed app remains open. Production and preview launches keep the
    # normal single-instance lock.
    application_name = "WhisperKeyCandidateSmoke" if args.smoke_test else "WhisperKey"
    app.setApplicationName(application_name)
    app.setApplicationDisplayName("WhisperKey")
    app.setOrganizationName("WhisperKey")
    app.setQuitOnLastWindowClosed(False)
    icon = QIcon(resolve_asset_path("platform/windows/assets/whisperkey-icon.ico"))
    app.setWindowIcon(icon)
    lock = _acquire_instance_lock()
    if lock is None:
        return 2
    from whisper_key.platform import instance_lock

    legacy_lock = instance_lock.acquire_lock("WhisperKeyLocal")
    if legacy_lock is None and not (args.preview or args.smoke_test):
        return 3
    _configure_logging()
    settings = QSettings("WhisperKey", "WhisperKey")
    theme = settings.value("appearance/theme", "dark")
    app.setStyleSheet(build_stylesheet(theme))
    controller = AppController(args.library)
    window = MainWindow(controller)
    window.settings_page.theme.setCurrentIndex(1 if theme == "light" else 0)
    tray = _build_tray(app, window, controller)
    app._whisperkey_resources = (lock, legacy_lock, tray, controller, window)  # type: ignore[attr-defined]
    app.aboutToQuit.connect(controller.shutdown)
    if not args.startup_test:
        window.show()
    if args.startup_test:
        controller.operation_finished.connect(
            lambda operation, result: app.exit(0 if result else 4) if operation == "initialize" else None
        )
        QTimer.singleShot(180_000, lambda: app.exit(5))
    if args.preview or args.smoke_test:
        window._on_model_state("ready", "Vista previa · modelo no cargado")
        controller.refresh_library()
    else:
        QTimer.singleShot(50, controller.initialize)
    if args.smoke_test:
        QTimer.singleShot(800, app.quit)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
