from __future__ import annotations

import math

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from whisper_key.infrastructure.snapshot_service import DesktopFrame
from whisper_key.ui.theme import TokenStore


class StatusPill(QLabel):
    def __init__(self, text: str, status: str = "neutral", parent=None):
        super().__init__(text, parent)
        self.setObjectName("Pill")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        self.set_status(status)

    def set_status(self, status: str) -> None:
        self.setProperty("status", status)
        self.style().unpolish(self)
        self.style().polish(self)


class PixelMicActivity(QWidget):
    """A crisp pixel-inspired recording mark with deterministic animated meters."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(56, 42)
        self._phase = 0.0
        self._active = False
        self._timer = QTimer(self)
        self._timer.setInterval(90)
        self._timer.timeout.connect(self._tick)

    def set_active(self, active: bool) -> None:
        self._active = active
        if active:
            self._timer.start()
        else:
            self._timer.stop()
        self.update()

    def _tick(self) -> None:
        self._phase += 0.7
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        tokens = TokenStore(theme="dark" if self.palette().window().color().lightness() < 128 else "light")
        active = QColor(tokens.get("semantic.color.status.recording"))
        muted = QColor(tokens.get("semantic.color.border.strong"))
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        unit = max(2, min(self.width() // 22, self.height() // 14))
        origin_x = (self.width() - unit * 18) // 2
        origin_y = (self.height() - unit * 12) // 2
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(active if self._active else muted)
        painter.drawRect(origin_x + unit * 2, origin_y + unit, unit * 5, unit * 8)
        painter.setBrush(self.palette().window().color())
        painter.drawRect(origin_x + unit * 3, origin_y + unit * 2, unit * 3, unit * 5)
        painter.setBrush(active if self._active else muted)
        painter.drawRect(origin_x + unit, origin_y + unit * 6, unit, unit * 3)
        painter.drawRect(origin_x + unit * 7, origin_y + unit * 6, unit, unit * 3)
        painter.drawRect(origin_x + unit * 2, origin_y + unit * 9, unit * 5, unit)
        painter.drawRect(origin_x + unit * 4, origin_y + unit * 10, unit, unit * 2)
        for index in range(4):
            level = 2
            if self._active:
                level = 2 + round((math.sin(self._phase + index * 1.4) + 1) * 2)
            x = origin_x + unit * (10 + index * 2)
            painter.drawRect(x, origin_y + unit * (7 - level // 2), unit, unit * level)
        painter.end()


class ModeCard(QPushButton):
    selected = Signal(str)

    def __init__(self, mode: str, title: str, description: str, shortcut: str = "", parent=None):
        suffix = f"\n{description}"
        if shortcut:
            suffix += f"   ·   {shortcut}"
        super().__init__(f"{title}{suffix}", parent)
        self.mode = mode
        self.setObjectName("ModeCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName(f"Iniciar modo {title}")
        self.clicked.connect(lambda: self.selected.emit(self.mode))


class RecordingMiniController(QWidget):
    show_main_requested = Signal()
    pause_requested = Signal()
    marker_requested = Signal()
    region_capture_requested = Signal()
    finish_requested = Signal()
    dictation_stop_requested = Signal()

    COMPACT_WIDTH = 250
    EXPANDED_WIDTH = 650

    def __init__(self):
        flags = Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.FramelessWindowHint
        super().__init__(None, flags)
        self.setObjectName("MiniController")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setFixedSize(self.COMPACT_WIDTH, 68)
        self._controls_expanded = False
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 10, 8)
        layout.setSpacing(8)
        self.activity = PixelMicActivity()
        self.activity.setFixedSize(54, 46)
        layout.addWidget(self.activity)
        text = QVBoxLayout()
        text.setSpacing(0)
        self.status_label = QLabel("GRABANDO")
        self.status_label.setObjectName("RecordingStatus")
        self.timer_label = QLabel("00:00:00")
        self.timer_label.setObjectName("Timer")
        text.addWidget(self.status_label)
        text.addWidget(self.timer_label)
        layout.addLayout(text)
        layout.addStretch()
        self.controls_toggle = QPushButton("›")
        self.controls_toggle.setFixedWidth(32)
        self.controls_toggle.setAccessibleName("Mostrar controles de grabación")
        self.controls_toggle.setToolTip("Mostrar controles")
        self.controls_toggle.clicked.connect(self._toggle_controls)
        layout.addWidget(self.controls_toggle)

        self.controls = QWidget()
        controls_layout = QHBoxLayout(self.controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(8)
        self.pause_button = QPushButton("Pausar")
        self.pause_button.clicked.connect(self.pause_requested)
        self.region_button = QPushButton("Región")
        self.region_button.setToolTip("Seleccionar una región y adjuntarla sin abrir WhisperKey")
        self.region_button.setAccessibleName("Capturar región")
        self.region_button.clicked.connect(self.region_capture_requested)
        self.marker_button = QPushButton("Marcar")
        self.marker_button.clicked.connect(self.marker_requested)
        self.finish_button = QPushButton("Finalizar")
        self.finish_button.setProperty("primary", True)
        self.finish_button.clicked.connect(self._finish_clicked)
        self._dictation_mode = False
        controls_layout.addWidget(self.pause_button)
        controls_layout.addWidget(self.region_button)
        controls_layout.addWidget(self.marker_button)
        controls_layout.addWidget(self.finish_button)
        layout.addWidget(self.controls)
        self.controls.hide()
        self.setWindowOpacity(0.0)
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(180)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)

    def show_recording(self, state: dict) -> None:
        self._dictation_mode = False
        self.controls_toggle.show()
        self.pause_button.show()
        self.region_button.show()
        self.marker_button.show()
        self.finish_button.setText("Finalizar")
        self.finish_button.setEnabled(True)
        self.controls.setVisible(self._controls_expanded)
        self._apply_recording_width()
        status = state.get("status")
        active = status == "recording"
        self.activity.set_active(active)
        self.status_label.setText("GRABANDO" if active else "EN PAUSA")
        self.status_label.setObjectName("RecordingStatus" if active else "ProcessingStatus")
        self.pause_button.setText("Pausar" if active else "Continuar")
        self.timer_label.setText(format_duration(state.get("display_elapsed_ms", 0)))
        if not self.isVisible():
            screen = self.screen().availableGeometry()
            self.move(screen.right() - self.width() - 24, screen.bottom() - self.height() - 24)
            self.show()
            self._fade.stop()
            self._fade.setStartValue(0.0)
            self._fade.setEndValue(1.0)
            self._fade.start()

    def show_dictation(self, elapsed_ms: int, processing: bool = False) -> None:
        self._dictation_mode = True
        self.setFixedSize(310, 68)
        self.controls_toggle.hide()
        self.controls.show()
        self.pause_button.hide()
        self.region_button.hide()
        self.marker_button.hide()
        self.finish_button.setText("Procesando…" if processing else "Detener")
        self.finish_button.setEnabled(not processing)
        self.activity.set_active(not processing)
        self.status_label.setText("PROCESANDO" if processing else "DICTANDO")
        self.status_label.setObjectName("ProcessingStatus" if processing else "RecordingStatus")
        self.timer_label.setText(format_duration(elapsed_ms))
        if not self.isVisible():
            screen = self.screen().availableGeometry()
            self.move(screen.right() - self.width() - 24, screen.bottom() - self.height() - 24)
            self.show()
            self._fade.stop()
            self._fade.setStartValue(0.0)
            self._fade.setEndValue(1.0)
            self._fade.start()

    def _toggle_controls(self) -> None:
        if self._dictation_mode:
            return
        self._controls_expanded = not self._controls_expanded
        self.controls.setVisible(self._controls_expanded)
        self._apply_recording_width()

    def _apply_recording_width(self) -> None:
        expanded = self._controls_expanded
        self.setFixedSize(self.EXPANDED_WIDTH if expanded else self.COMPACT_WIDTH, 68)
        self.controls_toggle.setText("‹" if expanded else "›")
        self.controls_toggle.setAccessibleName(
            "Ocultar controles de grabación" if expanded else "Mostrar controles de grabación"
        )
        self.controls_toggle.setToolTip("Ocultar controles" if expanded else "Mostrar controles")
        if self.isVisible():
            screen = self.screen().availableGeometry()
            self.move(screen.right() - self.width() - 24, screen.bottom() - self.height() - 24)

    def _finish_clicked(self) -> None:
        if self._dictation_mode:
            self.dictation_stop_requested.emit()
        else:
            self.finish_requested.emit()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        self.show_main_requested.emit()
        super().mouseDoubleClickEvent(event)


class RegionCaptureOverlay(QWidget):
    region_selected = Signal(QRect)
    canceled = Signal()

    def __init__(self, frame: DesktopFrame):
        flags = Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
        super().__init__(None, flags)
        self.frame = frame
        self._origin = QPoint()
        self._selection = QRect()
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setGeometry(frame.virtual_geometry)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAccessibleName("Seleccionar región de captura")

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        painter.drawImage(self.rect(), self.frame.image)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 125))
        if not self._selection.isEmpty():
            global_selection = self._selection.translated(self.frame.virtual_geometry.topLeft())
            selected_image = self.frame.crop(global_selection)
            painter.drawImage(self._selection, selected_image)
            painter.setPen(QPen(QColor("#4DD0E1"), 2))
            painter.drawRect(self._selection.adjusted(1, 1, -1, -1))
            label = f"{selected_image.width()} × {selected_image.height()} px"
            label_rect = QRect(self._selection.left(), max(0, self._selection.top() - 28), 180, 24)
            painter.fillRect(label_rect, QColor(9, 14, 21, 220))
            painter.setPen(QColor("#FFFFFF"))
            painter.drawText(label_rect.adjusted(8, 0, -4, 0), Qt.AlignmentFlag.AlignVCenter, label)
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._origin = event.position().toPoint()
            self._selection = QRect(self._origin, self._origin)
            self.update()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._selection = QRect(self._origin, event.position().toPoint()).normalized().intersected(self.rect())
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        selection = self._selection.normalized().intersected(self.rect())
        if selection.width() < 8 or selection.height() < 8:
            self._cancel()
            return
        global_selection = selection.translated(self.frame.virtual_geometry.topLeft())
        self.region_selected.emit(global_selection)
        self.close()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self._cancel()
            return
        super().keyPressEvent(event)

    def _cancel(self) -> None:
        self.canceled.emit()
        self.close()


def format_duration(milliseconds: int) -> str:
    seconds = max(0, int(milliseconds) // 1000)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
