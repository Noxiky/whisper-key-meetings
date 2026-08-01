import sys
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QRect
from PySide6.QtGui import QColor, QImage

from whisper_key.application import SessionService
from whisper_key.infrastructure import DesktopCapture, DesktopFrame, ProtectedCaptureError, SnapshotService


def test_snapshot_is_relative_hashed_and_does_not_pause_capture(tmp_path):
    service = SessionService(tmp_path)
    service.create("learning")
    service.start_stage()
    image = QImage(120, 80, QImage.Format.Format_RGBA8888)
    image.fill(QColor("#367A91"))

    result = SnapshotService().persist(service, image)

    assert result["relative_path"] == "attachments/0001-screen.png"
    assert (service.folder / result["relative_path"]).exists()
    assert len(result["sha256"]) == 64
    assert service.session.status.value == "recording"
    event = service.repository.read_events(service.folder)[-1]
    assert event["type"] == "snapshot_created"
    assert event["payload"]["width"] == 120
    assert event["payload"]["height"] == 80


def test_black_protected_frame_is_explained_and_not_committed(tmp_path):
    service = SessionService(tmp_path)
    service.create("reading")
    service.start_stage()
    image = QImage(100, 100, QImage.Format.Format_RGBA8888)
    image.fill(QColor("#000000"))

    with pytest.raises(ProtectedCaptureError, match="protected"):
        SnapshotService().persist(service, image)

    assert not list((service.folder / "attachments").glob("*.png"))
    assert all(event["type"] != "snapshot_created" for event in service.repository.read_events(service.folder))


def test_desktop_frame_maps_negative_monitor_coordinates_at_device_scale():
    image = QImage(200, 100, QImage.Format.Format_RGBA8888)
    image.fill(QColor("#5588AA"))
    frame = DesktopFrame(image=image, virtual_geometry=QRect(-100, 0, 100, 50), scale=2.0)

    cropped = frame.crop(QRect(-75, 10, 25, 20))

    assert cropped.size().width() == 50
    assert cropped.size().height() == 40


def test_window_capture_crops_composed_desktop_instead_of_trusting_grab_window(monkeypatch):
    image = QImage(300, 200, QImage.Format.Format_RGBA8888)
    image.fill(QColor("#336699"))
    frame = DesktopFrame(image=image, virtual_geometry=QRect(-100, -50, 300, 200), scale=1.0)
    monkeypatch.setitem(
        sys.modules,
        "win32gui",
        SimpleNamespace(GetWindowRect=lambda _window_id: (-50, -20, 70, 60)),
    )
    monkeypatch.setattr(DesktopCapture, "capture_virtual_desktop", staticmethod(lambda: frame))

    captured = DesktopCapture.capture_window(42)

    assert captured.width() == 120
    assert captured.height() == 80
    assert captured.pixelColor(0, 0) == QColor("#336699")
