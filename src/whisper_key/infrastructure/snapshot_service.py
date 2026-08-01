from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

import numpy as np
from PySide6.QtCore import QRect, QRectF, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter

if TYPE_CHECKING:
    from whisper_key.application.session_service import SessionService


class ProtectedCaptureError(RuntimeError):
    pass


@dataclass(frozen=True)
class DesktopFrame:
    image: QImage
    virtual_geometry: QRect
    scale: float

    def crop(self, global_region: QRect) -> QImage:
        region = global_region.normalized().intersected(self.virtual_geometry)
        if region.isEmpty():
            return QImage()
        pixel_region = QRect(
            round((region.x() - self.virtual_geometry.x()) * self.scale),
            round((region.y() - self.virtual_geometry.y()) * self.scale),
            round(region.width() * self.scale),
            round(region.height() * self.scale),
        )
        return self.image.copy(pixel_region)


class DesktopCapture:
    @staticmethod
    def capture_virtual_desktop() -> DesktopFrame:
        screens = QGuiApplication.screens()
        if not screens:
            raise RuntimeError("No display is available")
        virtual = QRect(screens[0].geometry())
        for screen in screens[1:]:
            virtual = virtual.united(screen.geometry())
        scale = max(float(screen.devicePixelRatio()) for screen in screens)
        image = QImage(
            max(1, round(virtual.width() * scale)),
            max(1, round(virtual.height() * scale)),
            QImage.Format.Format_RGBA8888,
        )
        image.fill(Qt.GlobalColor.black)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        for screen in screens:
            geometry = screen.geometry()
            captured = screen.grabWindow(0).toImage()
            target = QRectF(
                (geometry.x() - virtual.x()) * scale,
                (geometry.y() - virtual.y()) * scale,
                geometry.width() * scale,
                geometry.height() * scale,
            )
            painter.drawImage(target, captured)
        painter.end()
        return DesktopFrame(image, virtual, scale)

    @staticmethod
    def capture_window(window_id: int) -> QImage:
        # Qt's grabWindow(hwnd) can return a completely black frame for ordinary
        # hardware-accelerated Chromium/Windows apps.  Capture the composed desktop
        # pixels and crop to the foreground window instead; protected video may
        # still be blank, but the surrounding window remains honest evidence.
        try:
            import win32gui

            left, top, right, bottom = win32gui.GetWindowRect(window_id)
            if right > left and bottom > top:
                frame = DesktopCapture.capture_virtual_desktop()
                return frame.crop(QRect(left, top, right - left, bottom - top))
        except Exception:
            pass
        screen = QGuiApplication.primaryScreen()
        if not screen:
            raise RuntimeError("No display is available")
        return screen.grabWindow(window_id).toImage()


class SnapshotService:
    def persist(self, service: SessionService, image: QImage, at_ms: int | None = None) -> dict:
        if not service.folder:
            raise RuntimeError("Session folder is required")
        normalized = image.convertToFormat(QImage.Format.Format_RGBA8888)
        if normalized.isNull() or normalized.width() < 2 or normalized.height() < 2:
            raise ValueError("The selected screenshot region is empty")
        if self._looks_protected(normalized):
            raise ProtectedCaptureError(
                "Windows devolvió una imagen completamente negra. Puede ser contenido protegido (protected) "
                "o una ventana acelerada que no permite captura directa. Prueba 'Seleccionar región'; "
                "el audio sigue grabándose."
            )
        attachments = service.folder / "attachments"
        attachments.mkdir(parents=True, exist_ok=True)
        index = self._next_index(attachments)
        destination = attachments / f"{index:04d}-screen.png"
        temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
        try:
            if not normalized.save(str(temporary), "PNG"):
                raise OSError("Qt could not encode the screenshot")
            with temporary.open("r+b") as handle:
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        relative = destination.relative_to(service.folder).as_posix()
        attachment_id = service.add_attachment(
            kind="screenshot",
            relative_path=relative,
            media_type="image/png",
            sha256=digest,
            at_ms=service.current_offset_ms() if at_ms is None else at_ms,
            width=normalized.width(),
            height=normalized.height(),
        )
        return {
            "attachment_id": attachment_id,
            "relative_path": relative,
            "width": normalized.width(),
            "height": normalized.height(),
            "sha256": digest,
        }

    @staticmethod
    def _next_index(folder: Path) -> int:
        indexes = []
        for path in folder.glob("*-screen.png"):
            match = re.match(r"^(\d+)-screen[.]png$", path.name)
            if match:
                indexes.append(int(match.group(1)))
        return max(indexes, default=0) + 1

    @staticmethod
    def _looks_protected(image: QImage) -> bool:
        sample = image.scaled(64, 64, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.FastTransformation)
        array = np.frombuffer(sample.bits(), dtype=np.uint8, count=sample.sizeInBytes()).reshape(
            sample.height(), sample.bytesPerLine()
        )[:, : sample.width() * 4]
        rgba = array.reshape(sample.height(), sample.width(), 4)
        if int(rgba[:, :, 3].max()) == 0:
            return True
        rgb = rgba[:, :, :3].astype(np.float32)
        return float(rgb.mean()) < 4.0 and float(rgb.std()) < 2.0
