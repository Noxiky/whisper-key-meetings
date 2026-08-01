from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QTextBrowser,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from whisper_key.application import SUPPORTED_AUDIO_EXTENSIONS
from whisper_key.infrastructure import DesktopCapture
from whisper_key.ui.controller import AppController
from whisper_key.ui.theme import build_stylesheet
from whisper_key.ui.widgets import (
    ModeCard,
    PixelMicActivity,
    RecordingMiniController,
    RegionCaptureOverlay,
    StatusPill,
    format_duration,
)
from whisper_key.utils import open_file

MODE_LABELS = {
    "dictation": "Dictado",
    "meeting": "Reunión",
    "learning": "Aprendizaje",
    "reading": "Lectura",
    "idea": "Idea",
}


class HomePage(QWidget):
    mode_requested = Signal(str)
    session_requested = Signal(str)
    audio_import_requested = Signal(str)

    def __init__(self):
        super().__init__()
        self.setObjectName("Page")
        self.setAcceptDrops(True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 28, 32, 28)
        outer.setSpacing(22)
        header = QHBoxLayout()
        header_text = QVBoxLayout()
        eyebrow = QLabel("CAPTURA LOCAL · AUDIO RETENIDO")
        eyebrow.setObjectName("Eyebrow")
        title = QLabel("¿Qué quieres capturar?")
        title.setObjectName("PageTitle")
        subtitle = QLabel("Una sesión puede pausarse, continuar más tarde y convertirse en un documento durable.")
        subtitle.setObjectName("Muted")
        header_text.addWidget(eyebrow)
        header_text.addWidget(title)
        header_text.addWidget(subtitle)
        header.addLayout(header_text)
        header.addStretch()
        self.model_pill = StatusPill("Preparando modelo…", "warning")
        header.addWidget(self.model_pill, 0, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(header)

        modes = QGridLayout()
        modes.setHorizontalSpacing(12)
        modes.setVerticalSpacing(12)
        definitions = [
            ("meeting", "Reunión", "MIC + audio del sistema, contexto completo"),
            ("learning", "Aprendizaje", "Clases, videos y marcadores de comprensión"),
            ("reading", "Lectura", "Lectura en voz alta, comentarios y preguntas"),
            ("idea", "Idea", "Pensamiento libre convertido en material durable"),
            ("dictation", "Dictado", "Habla, transcribe y entrega al portapapeles"),
        ]
        self.mode_cards: list[ModeCard] = []
        for index, definition in enumerate(definitions):
            card = ModeCard(*definition)
            card.selected.connect(self.mode_requested)
            self.mode_cards.append(card)
            modes.addWidget(card, index // 2, index % 2)
        outer.addLayout(modes)

        self.import_card = QFrame()
        self.import_card.setObjectName("Panel")
        import_layout = QHBoxLayout(self.import_card)
        import_copy = QVBoxLayout()
        import_title = QLabel("Transcribir un archivo")
        import_title.setObjectName("SectionTitle")
        self.import_detail = QLabel(
            "Arrastra aquí MP3, M4A, WAV, FLAC, OGG, WebM o MP4. Se procesa localmente sin usar MIC ni SYS."
        )
        self.import_detail.setObjectName("Muted")
        self.import_detail.setWordWrap(True)
        import_copy.addWidget(import_title)
        import_copy.addWidget(self.import_detail)
        import_layout.addLayout(import_copy, 1)
        self.import_progress = QProgressBar()
        self.import_progress.setRange(0, 100)
        self.import_progress.setFixedWidth(150)
        self.import_progress.setVisible(False)
        import_layout.addWidget(self.import_progress)
        self.import_button = QPushButton("Elegir audio")
        self.import_button.setAccessibleName("Elegir un archivo de audio para transcribir")
        self.import_button.clicked.connect(self._choose_audio)
        import_layout.addWidget(self.import_button)
        outer.addWidget(self.import_card)

        recording_notice = QLabel(
            "Al iniciar una sesión durable, tú controlas la grabación. Informa a las personas presentes y "
            "obtén el consentimiento que corresponda; WhisperKey mantiene visible el estado y procesa localmente."
        )
        recording_notice.setObjectName("Muted")
        recording_notice.setWordWrap(True)
        recording_notice.setAccessibleName("Aviso de consentimiento para grabación")
        outer.addWidget(recording_notice)

        recent_header = QHBoxLayout()
        recent_title = QLabel("Sesiones recientes")
        recent_title.setObjectName("SectionTitle")
        recent_header.addWidget(recent_title)
        recent_header.addStretch()
        self.recent_count = QLabel("0 sesiones")
        self.recent_count.setObjectName("Muted")
        recent_header.addWidget(self.recent_count)
        outer.addLayout(recent_header)
        self.recent = QTreeWidget()
        self.recent.setHeaderLabels(["Nombre", "Modo", "Estado", "Actualizada"])
        self.recent.setRootIsDecorated(False)
        self.recent.setAlternatingRowColors(False)
        self.recent.header().setStretchLastSection(False)
        self.recent.header().resizeSection(0, 380)
        self.recent.header().resizeSection(1, 130)
        self.recent.header().resizeSection(2, 130)
        self.recent.header().resizeSection(3, 170)
        self.recent.itemDoubleClicked.connect(self._open_recent)
        outer.addWidget(self.recent, 1)

    def set_model_state(self, state: str, message: str) -> None:
        status = "active" if state == "ready" else "warning"
        if state == "error":
            status = "error"
        self.model_pill.setText(message)
        self.model_pill.set_status(status)
        ready = state == "ready"
        for card in self.mode_cards:
            card.setEnabled(ready)
        self.import_button.setEnabled(ready)

    def set_import_state(self, state: str, detail: str, percent: int) -> None:
        active = state not in {"complete", "error", "idle"}
        self.import_progress.setVisible(active or state == "complete")
        self.import_progress.setValue(max(0, min(100, percent)))
        self.import_button.setEnabled(not active)
        self.import_detail.setText(detail)

    def _choose_audio(self) -> None:
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Transcribir un archivo",
            "",
            "Audio y video (*.aac *.flac *.m4a *.mka *.mov *.mp3 *.mp4 *.ogg *.opus *.wav *.webm *.wma)",
        )
        if path:
            self._submit_audio_path(path)

    def dragEnterEvent(self, event) -> None:
        urls = event.mimeData().urls() if event.mimeData().hasUrls() else []
        if len(urls) == 1 and urls[0].isLocalFile():
            suffix = Path(urls[0].toLocalFile()).suffix.casefold()
            if suffix in SUPPORTED_AUDIO_EXTENSIONS:
                event.acceptProposedAction()
                return
        event.ignore()

    def dropEvent(self, event) -> None:
        urls = event.mimeData().urls() if event.mimeData().hasUrls() else []
        if len(urls) == 1 and urls[0].isLocalFile():
            if self._submit_audio_path(urls[0].toLocalFile()):
                event.acceptProposedAction()
                return
        event.ignore()

    def _submit_audio_path(self, path: str) -> bool:
        source = Path(path)
        if (
            not self.import_button.isEnabled()
            or not source.is_file()
            or source.suffix.casefold() not in SUPPORTED_AUDIO_EXTENSIONS
        ):
            return False
        self.set_import_state("preparing", f"Preparando {source.name}…", 0)
        self.audio_import_requested.emit(str(source))
        return True

    def set_sessions(self, sessions: list[dict]) -> None:
        self.recent.clear()
        for session in sessions[:8]:
            title = session.get("title") or "Sin nombre"
            updated = (session.get("updated_at") or "").replace("T", " ")[:16]
            item = QTreeWidgetItem(
                [
                    title,
                    MODE_LABELS.get(session.get("mode", ""), session.get("mode", "")),
                    session.get("status", ""),
                    updated,
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, session.get("session_id"))
            self.recent.addTopLevelItem(item)
        count = len(sessions)
        self.recent_count.setText(f"{count} sesión" if count == 1 else f"{count} sesiones")

    def _open_recent(self, item: QTreeWidgetItem) -> None:
        session_id = item.data(0, Qt.ItemDataRole.UserRole)
        if session_id:
            self.session_requested.emit(session_id)


class LibraryPage(QWidget):
    session_requested = Signal(str)

    def __init__(self, controller: AppController):
        super().__init__()
        self.controller = controller
        self.setObjectName("Page")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 28, 32, 28)
        header = QHBoxLayout()
        text = QVBoxLayout()
        title = QLabel("Sesiones")
        title.setObjectName("PageTitle")
        subtitle = QLabel("Doble clic abre la sesión dentro de WhisperKey. 'Ver archivos' muestra su carpeta portable.")
        subtitle.setWordWrap(True)
        subtitle.setObjectName("Muted")
        text.addWidget(title)
        text.addWidget(subtitle)
        header.addLayout(text)
        header.addStretch()
        open_session = QPushButton("Abrir sesión")
        open_session.setProperty("primary", True)
        open_session.clicked.connect(self._open_current)
        open_button = QPushButton("Ver archivos")
        open_button.clicked.connect(controller.open_library_folder)
        refresh_button = QPushButton("Actualizar")
        refresh_button.clicked.connect(controller.refresh_library)
        header.addWidget(open_session)
        header.addWidget(open_button)
        header.addWidget(refresh_button)
        layout.addLayout(header)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Buscar por nombre, modo o contenido literal…")
        self.search.setClearButtonEnabled(True)
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(180)
        self._search_timer.timeout.connect(lambda: controller.search_library(self.search.text()))
        self.search.textChanged.connect(lambda: self._search_timer.start())
        layout.addWidget(self.search)
        self.list = QTreeWidget()
        self.list.setHeaderLabels(["Nombre", "Modo", "Estado", "Duración", "Fecha"])
        self.list.setRootIsDecorated(False)
        self.list.header().resizeSection(0, 430)
        self.list.header().resizeSection(1, 140)
        self.list.header().resizeSection(2, 130)
        self.list.header().resizeSection(3, 110)
        self.list.itemDoubleClicked.connect(self._open_selected)
        layout.addWidget(self.list)

    def set_sessions(self, sessions: list[dict]) -> None:
        self.list.clear()
        for session in sessions:
            item = QTreeWidgetItem(
                [
                    session.get("title") or "Sin nombre",
                    MODE_LABELS.get(session.get("mode", ""), session.get("mode", "")),
                    session.get("status", ""),
                    format_duration(session.get("captured_duration_ms", 0)),
                    (session.get("created_at") or "").replace("T", " ")[:16],
                ]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, session.get("session_id"))
            self.list.addTopLevelItem(item)

    def _open_selected(self, item: QTreeWidgetItem) -> None:
        session_id = item.data(0, Qt.ItemDataRole.UserRole)
        if session_id:
            self.session_requested.emit(session_id)

    def _open_current(self) -> None:
        item = self.list.currentItem()
        if item:
            self._open_selected(item)


class DictationHistoryCard(QFrame):
    copy_requested = Signal(str)

    def __init__(self, entry: dict):
        super().__init__()
        self.setObjectName("Card")
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        created = (entry.get("created_at") or "").replace("T", " ")[:19]
        delivery_labels = {
            "pasted": "Pegado",
            "clipboard": "Portapapeles",
            "not_delivered": "Sin entregar",
            "failed": "Falló",
            "benchmark": "Prueba local",
        }
        meta = QLabel(
            f"{created}  ·  {format_duration(entry.get('duration_ms', 0))}  ·  "
            f"{delivery_labels.get(entry.get('delivery'), entry.get('delivery', ''))}"
        )
        meta.setObjectName("Muted")
        header.addWidget(meta)
        header.addStretch()
        copy_button = QPushButton("Copiar")
        text = entry.get("text") or ""
        copy_button.setEnabled(bool(text))
        copy_button.clicked.connect(lambda: self.copy_requested.emit(text))
        header.addWidget(copy_button)
        layout.addLayout(header)
        content = QLabel(text or "No se detectó texto; el audio quedó conservado.")
        content.setWordWrap(True)
        content.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(content)
        if entry.get("error"):
            error = QLabel(entry["error"])
            error.setObjectName("ErrorText")
            error.setWordWrap(True)
            layout.addWidget(error)


class DictationPage(QWidget):
    toggle_requested = Signal()
    copy_requested = Signal(str)

    def __init__(self):
        super().__init__()
        self.setObjectName("Page")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 28, 32, 28)
        layout.setSpacing(18)
        eyebrow = QLabel("RÁPIDO · MICRÓFONO")
        eyebrow.setObjectName("Eyebrow")
        title = QLabel("Dictado")
        title.setObjectName("PageTitle")
        body = QLabel(
            "Habla con normalidad. Cada dictado terminado queda en el historial local con su audio, "
            "su texto y el estado de entrega para que puedas volver a copiarlo."
        )
        body.setWordWrap(True)
        body.setObjectName("Muted")
        layout.addWidget(eyebrow)
        layout.addWidget(title)
        layout.addWidget(body)
        panel = QFrame()
        panel.setObjectName("Card")
        panel_layout = QHBoxLayout(panel)
        self.activity = PixelMicActivity()
        panel_layout.addWidget(self.activity)
        panel_text = QVBoxLayout()
        self.panel_title = QLabel("Listo para escuchar")
        self.panel_title.setObjectName("SectionTitle")
        self.panel_desc = QLabel("Solo micrófono · transcripción local · historial durable")
        self.panel_desc.setObjectName("Muted")
        self.timer = QLabel("00:00:00")
        self.timer.setObjectName("Timer")
        panel_text.addWidget(self.panel_title)
        panel_text.addWidget(self.panel_desc)
        panel_text.addWidget(self.timer)
        panel_layout.addLayout(panel_text)
        panel_layout.addStretch()
        self.start = QPushButton("Iniciar dictado")
        self.start.setProperty("primary", True)
        self.start.clicked.connect(self.toggle_requested)
        panel_layout.addWidget(self.start)
        layout.addWidget(panel)
        self.result = QTextEdit()
        self.result.setReadOnly(True)
        self.result.setPlaceholderText("El último dictado aparecerá aquí y se entregará a la aplicación anterior.")
        self.result.setMaximumHeight(220)
        layout.addWidget(self.result)
        history_header = QHBoxLayout()
        history_title = QLabel("Historial de dictados")
        history_title.setObjectName("SectionTitle")
        self.history_count = QLabel("0 dictados")
        self.history_count.setObjectName("Muted")
        history_header.addWidget(history_title)
        history_header.addStretch()
        history_header.addWidget(self.history_count)
        layout.addLayout(history_header)
        self.history_scroll = QScrollArea()
        self.history_scroll.setWidgetResizable(True)
        self.history_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.history_content = QWidget()
        self.history_layout = QVBoxLayout(self.history_content)
        self.history_layout.setContentsMargins(0, 0, 0, 0)
        self.history_layout.setSpacing(10)
        self.history_layout.addStretch()
        self.history_scroll.setWidget(self.history_content)
        layout.addWidget(self.history_scroll, 1)

    def update_state(self, state: str, message: str, text: str) -> None:
        recording = state == "recording"
        self.activity.set_active(recording)
        self.panel_title.setText(
            "Grabando" if recording else "Procesando" if state == "processing" else "Dictado listo"
        )
        self.panel_desc.setText(message)
        self.start.setText("Detener y transcribir" if recording else "Iniciar otro dictado")
        self.start.setEnabled(state != "processing")
        if text:
            self.result.setPlainText(text)

    def set_elapsed(self, milliseconds: int) -> None:
        self.timer.setText(format_duration(milliseconds))

    def set_history(self, entries: list[dict]) -> None:
        while self.history_layout.count():
            item = self.history_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not entries:
            empty = QLabel("Todavía no hay dictados. El siguiente aparecerá aquí.")
            empty.setObjectName("Muted")
            self.history_layout.addWidget(empty)
        else:
            for entry in entries:
                card = DictationHistoryCard(entry)
                card.copy_requested.connect(self.copy_requested)
                self.history_layout.addWidget(card)
        self.history_layout.addStretch()
        count = len(entries)
        self.history_count.setText(f"{count} dictado" if count == 1 else f"{count} dictados")


class ModelsPage(QWidget):
    def __init__(self, controller: AppController):
        super().__init__()
        self.controller = controller
        self._model_state = "loading"
        self._preflight_allowed = True
        self.setObjectName("Page")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 28, 32, 28)
        title = QLabel("Modelos")
        title.setObjectName("PageTitle")
        layout.addWidget(title)
        description = QLabel(
            "Los modelos se ejecutan localmente. La diarización será un módulo instalable y nunca bloqueará la captura."
        )
        description.setWordWrap(True)
        description.setObjectName("Muted")
        layout.addWidget(description)
        card = QFrame()
        card.setObjectName("Card")
        card_layout = QHBoxLayout(card)
        labels = QVBoxLayout()
        model_name = QLabel("Transcripción principal")
        model_name.setObjectName("SectionTitle")
        self.model_detail = QLabel("Preparando…")
        self.model_detail.setObjectName("Muted")
        labels.addWidget(model_name)
        labels.addWidget(self.model_detail)
        model_actions = QHBoxLayout()
        self.model_combo = QComboBox()
        self.model_combo.setMinimumWidth(360)
        self.model_combo.currentIndexChanged.connect(
            lambda: (
                self.controller.inspect_model(self.model_combo.currentData())
                if self.model_combo.currentData()
                else None
            )
        )
        self.use_model = QPushButton("Usar / instalar")
        self.use_model.setProperty("primary", True)
        self.use_model.clicked.connect(lambda: self.controller.select_model(self.model_combo.currentData()))
        cache = QPushButton("Abrir caché")
        cache.clicked.connect(self.controller.open_model_cache)
        self.verify_model = QPushButton("Verificar archivos")
        self.verify_model.clicked.connect(lambda: self.controller.verify_model(self.model_combo.currentData()))
        model_actions.addWidget(self.model_combo)
        model_actions.addWidget(self.use_model)
        model_actions.addWidget(cache)
        model_actions.addWidget(self.verify_model)
        labels.addLayout(model_actions)
        self.preflight_detail = QLabel("Selecciona un modelo para revisar caché, disco y memoria.")
        self.preflight_detail.setObjectName("Muted")
        self.preflight_detail.setWordWrap(True)
        self.verify_progress = QProgressBar()
        self.verify_progress.setRange(0, 100)
        self.verify_progress.setTextVisible(True)
        self.verify_progress.setVisible(False)
        labels.addWidget(self.preflight_detail)
        labels.addWidget(self.verify_progress)
        card_layout.addLayout(labels)
        card_layout.addStretch()
        self.model_status = StatusPill("Cargando", "warning")
        card_layout.addWidget(self.model_status)
        layout.addWidget(card)
        diarization = QFrame()
        diarization.setObjectName("Card")
        dia_layout = QHBoxLayout(diarization)
        dia_text = QVBoxLayout()
        dia_title = QLabel("Diarización anónima")
        dia_title.setObjectName("SectionTitle")
        self.dia_desc = QLabel(
            "Opcional para reuniones con varias personas: propone Speaker 1 / Speaker 2 al finalizar. "
            "No mejora el dictado y la captura funciona sin instalarlo."
        )
        self.dia_desc.setWordWrap(True)
        self.dia_desc.setObjectName("Muted")
        dia_text.addWidget(dia_title)
        dia_text.addWidget(self.dia_desc)
        dia_layout.addLayout(dia_text)
        dia_layout.addStretch()
        self.install = QPushButton("Instalar · ~47 MB")
        self.install.clicked.connect(self.controller.install_diarization_models)
        dia_layout.addWidget(self.install)
        layout.addWidget(diarization)
        layout.addStretch()

    def set_model_state(self, state: str, message: str) -> None:
        self._model_state = state
        self.model_detail.setText(message)
        self.use_model.setEnabled(state not in {"loading", "busy"} and self._preflight_allowed)
        if state == "ready":
            self.model_status.setText("Listo")
            self.model_status.set_status("active")
        else:
            self.model_status.setText("Preparando")
            self.model_status.set_status("warning")

    def set_diarization_state(self, state: str, message: str) -> None:
        guidance = (
            " Instálala si quieres separar personas como Speaker 1 / Speaker 2 en reuniones; "
            "no es necesaria para dictado, español/ruso ni capturas."
        )
        self.dia_desc.setText(message.rstrip(".") + "." + guidance)
        self.install.setEnabled(state != "installing")
        if state == "ready":
            self.install.setText("Reinstalar")
        elif state == "installing":
            self.install.setText("Instalando…")
        elif state == "error":
            self.install.setText("Reintentar")
        else:
            self.install.setText("Instalar · ~47 MB")

    def set_model_catalog(self, catalog: list[dict], current: str) -> None:
        self.model_combo.clear()
        current_index = 0
        for index, model in enumerate(catalog):
            capability = "multilingüe" if model.get("multilingual") else "solo inglés"
            cache_state = model.get("cache_state", "ready" if model.get("cached") else "missing")
            storage = {
                "ready": "instalado",
                "incomplete": "descarga incompleta · reanudar",
                "corrupt": "caché dañada",
                "missing": "descargar al usar",
            }.get(cache_state, cache_state)
            self.model_combo.addItem(
                f"{model.get('label', model.get('key'))} · {capability} · {storage}",
                model.get("key"),
            )
            if model.get("key") == current:
                current_index = index
        if self.model_combo.count():
            self.model_combo.setCurrentIndex(current_index)

    def set_model_inspection(self, result: dict) -> None:
        if result.get("model_key") != self.model_combo.currentData():
            return
        if result.get("kind") == "verification":
            status = result.get("status")
            if status == "progress":
                self.verify_progress.setVisible(True)
                self.verify_progress.setValue(round(float(result.get("progress", 0)) * 100))
                self.verify_model.setEnabled(False)
                self.preflight_detail.setText(result.get("detail", "Verificando…"))
                return
            self.verify_progress.setVisible(False)
            self.verify_model.setEnabled(True)
            self.preflight_detail.setText(result.get("detail", "Verificación terminada"))
            if status in {"corrupt", "error"}:
                self._preflight_allowed = False
                self.use_model.setEnabled(False)
                self.model_status.setText("Reparar")
                self.model_status.set_status("warning")
            return

        self.verify_progress.setVisible(False)
        cache = result.get("cache", {})
        cache_state = cache.get("state", "unknown")
        self.verify_model.setEnabled(cache_state == "ready")
        self._preflight_allowed = bool(result.get("allowed", False))
        self.use_model.setEnabled(self._preflight_allowed and self._model_state not in {"loading", "busy"})
        disk = (
            f"Disco libre {self._format_bytes(result.get('disk_free_bytes', 0))}"
            f" / requerido {self._format_bytes(result.get('disk_required_bytes', 0))}"
        )
        memory_free = result.get("memory_free_bytes")
        memory = (
            f"{result.get('memory_kind', 'Memoria')} libre "
            f"{self._format_bytes(memory_free)} / requerido "
            f"{self._format_bytes(result.get('memory_required_bytes', 0))}"
            if memory_free is not None
            else f"{result.get('memory_kind', 'Memoria')} libre no detectable"
        )
        self.preflight_detail.setText(f"{cache.get('detail', result.get('detail', ''))}\n{disk} · {memory}")
        if not self._preflight_allowed:
            self.model_status.setText("Revisar")
            self.model_status.set_status("warning")

    @staticmethod
    def _format_bytes(value: int | None) -> str:
        if value is None:
            return "desconocido"
        amount = float(max(0, value))
        for unit in ("B", "MiB", "GiB"):
            divisor = 1 if unit == "B" else 1024**2 if unit == "MiB" else 1024**3
            converted = amount / divisor
            if unit == "B" and amount < 1024**2:
                return f"{converted:.0f} {unit}"
            if unit == "MiB" and amount < 1024**3:
                return f"{converted:.0f} {unit}"
            if unit == "GiB":
                return f"{converted:.1f} {unit}"
        return f"{amount / 1024**3:.1f} GiB"


class SettingsPage(QWidget):
    theme_requested = Signal(str)
    hotkeys_save_requested = Signal(dict)
    retention_save_requested = Signal(dict)
    audio_routes_save_requested = Signal(dict)

    def __init__(self, controller: AppController):
        super().__init__()
        self.controller = controller
        self.setObjectName("Page")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        self.scroll = scroll
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content.setObjectName("Page")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(32, 28, 32, 28)
        layout.setSpacing(16)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        title = QLabel("Ajustes")
        title.setObjectName("PageTitle")
        layout.addWidget(title)
        appearance = QFrame()
        appearance.setObjectName("Card")
        row = QHBoxLayout(appearance)
        labels = QVBoxLayout()
        heading = QLabel("Apariencia")
        heading.setObjectName("SectionTitle")
        hint = QLabel("Tema oscuro o claro, ambos derivados del mismo sistema de tokens.")
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        labels.addWidget(heading)
        labels.addWidget(hint)
        row.addLayout(labels)
        row.addStretch()
        self.theme = QComboBox()
        self.theme.addItem("Oscuro", "dark")
        self.theme.addItem("Claro", "light")
        self.theme.currentIndexChanged.connect(lambda: self.theme_requested.emit(self.theme.currentData()))
        row.addWidget(self.theme)
        layout.addWidget(appearance)
        hotkeys = QFrame()
        hotkeys.setObjectName("Card")
        hotkey_layout = QVBoxLayout(hotkeys)
        hotkey_title = QLabel("Atajos globales")
        hotkey_title.setObjectName("SectionTitle")
        hotkey_hint = QLabel(
            "Funcionan aunque WhisperKey esté minimizado. Escribe combinaciones como ctrl+win o win+f10."
        )
        hotkey_hint.setObjectName("Muted")
        hotkey_hint.setWordWrap(True)
        hotkey_layout.addWidget(hotkey_title)
        hotkey_layout.addWidget(hotkey_hint)
        grid = QGridLayout()
        definitions = [
            ("Dictado", "recording_hotkey"),
            ("Detener dictado", "stop_key"),
            ("Reunión con auto-parada", "meeting_hotkey"),
            ("Reunión continua", "meeting_continuous_hotkey"),
            ("Reunión solo MIC", "meeting_mic_only_hotkey"),
            ("Reunión solo SYS", "meeting_sys_only_hotkey"),
        ]
        self.hotkey_fields: dict[str, QLineEdit] = {}
        for row, (label, key) in enumerate(definitions):
            field = QLineEdit()
            field.setPlaceholderText("ctrl+win")
            field.setAccessibleName(f"Atajo: {label}")
            self.hotkey_fields[key] = field
            grid.addWidget(QLabel(label), row, 0)
            grid.addWidget(field, row, 1)
        hotkey_layout.addLayout(grid)
        save_hotkeys = QPushButton("Guardar y activar atajos")
        save_hotkeys.setProperty("primary", True)
        save_hotkeys.clicked.connect(
            lambda: self.hotkeys_save_requested.emit({key: field.text() for key, field in self.hotkey_fields.items()})
        )
        self.hotkey_status = QLabel()
        self.hotkey_status.setObjectName("Muted")
        actions = QHBoxLayout()
        actions.addWidget(self.hotkey_status)
        actions.addStretch()
        actions.addWidget(save_hotkeys)
        hotkey_layout.addLayout(actions)
        layout.addWidget(hotkeys)
        storage = QFrame()
        storage.setObjectName("Card")
        storage_row = QHBoxLayout(storage)
        storage_labels = QVBoxLayout()
        storage_title = QLabel("Biblioteca local")
        storage_title.setObjectName("SectionTitle")
        storage_path = QLabel(str(controller.library_root))
        storage_path.setObjectName("Muted")
        storage_path.setWordWrap(True)
        storage_path.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        storage_labels.addWidget(storage_title)
        storage_labels.addWidget(storage_path)
        storage_row.addLayout(storage_labels)
        storage_row.addStretch()
        open_button = QPushButton("Abrir")
        open_button.clicked.connect(controller.open_library_folder)
        storage_row.addWidget(open_button)
        layout.addWidget(storage)
        retention = QFrame()
        retention.setObjectName("Card")
        retention_layout = QVBoxLayout(retention)
        retention_title = QLabel("Retención de audio por modo")
        retention_title.setObjectName("SectionTitle")
        retention_hint = QLabel(
            "El valor predeterminado conserva todo. Los cambios solo afectan sesiones nuevas; "
            "WhisperKey siempre muestra una lista exacta antes de retirar audio. "
            "El Dictado rápido conserva su WAV junto al historial."
        )
        retention_hint.setObjectName("Muted")
        retention_hint.setWordWrap(True)
        retention_layout.addWidget(retention_title)
        retention_layout.addWidget(retention_hint)
        retention_grid = QGridLayout()
        self.retention_fields: dict[str, QComboBox] = {}
        retention_modes = [
            ("Dictado durable", "dictation"),
            ("Reunión", "meeting"),
            ("Aprendizaje", "learning"),
            ("Lectura", "reading"),
            ("Idea", "idea"),
        ]
        retention_options = [
            ("Conservar todo (recomendado)", "all"),
            ("Conservar hasta verificar el literal", "until_verified"),
            ("Conservar solo contexto de marcadores", "marker_context"),
            ("Retirar audio después de finalizar", "none"),
        ]
        for row_index, (label, key) in enumerate(retention_modes):
            combo = QComboBox()
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(18)
            for option_label, option_value in retention_options:
                combo.addItem(option_label, option_value)
            combo.setAccessibleName(f"Retención: {label}")
            if key == "dictation":
                combo.setEnabled(False)
                combo.setToolTip("El historial de Dictado conserva texto y WAV para poder recuperarlos")
            self.retention_fields[key] = combo
            retention_grid.addWidget(QLabel(label), row_index, 0)
            retention_grid.addWidget(combo, row_index, 1)
        context_row = len(retention_modes)
        self.context_before = QSpinBox()
        self.context_after = QSpinBox()
        for field in (self.context_before, self.context_after):
            field.setRange(0, 600)
            field.setSuffix(" s")
        retention_grid.addWidget(QLabel("Contexto antes del marcador"), context_row, 0)
        retention_grid.addWidget(self.context_before, context_row, 1)
        retention_grid.addWidget(QLabel("Contexto después del marcador"), context_row + 1, 0)
        retention_grid.addWidget(self.context_after, context_row + 1, 1)
        retention_layout.addLayout(retention_grid)
        retention_actions = QHBoxLayout()
        self.retention_status = QLabel()
        self.retention_status.setObjectName("Muted")
        save_retention = QPushButton("Guardar retención")
        save_retention.clicked.connect(
            lambda: self.retention_save_requested.emit(
                {
                    **{key: field.currentData() for key, field in self.retention_fields.items()},
                    "marker_context_before_ms": self.context_before.value() * 1000,
                    "marker_context_after_ms": self.context_after.value() * 1000,
                }
            )
        )
        retention_actions.addWidget(self.retention_status)
        retention_actions.addStretch()
        retention_actions.addWidget(save_retention)
        retention_layout.addLayout(retention_actions)
        layout.addWidget(retention)
        privacy = QFrame()
        privacy.setObjectName("Card")
        privacy_row = QVBoxLayout(privacy)
        privacy_title = QLabel("Privacidad predeterminada")
        privacy_title.setObjectName("SectionTitle")
        privacy_text = QLabel(
            "Todo el audio se conserva. El literal no se reemplaza; los documentos limpios son derivados explícitos. "
            "Antes de grabar a otras personas, informa y obtén el consentimiento que corresponda."
        )
        privacy_text.setWordWrap(True)
        privacy_text.setObjectName("Muted")
        privacy_row.addWidget(privacy_title)
        privacy_row.addWidget(privacy_text)
        privacy_actions = QVBoxLayout()
        open_privacy = QPushButton("Leer aviso de privacidad y grabación")
        open_privacy.clicked.connect(controller.open_privacy_notice)
        export_diagnostics = QPushButton("Crear paquete de diagnóstico")
        export_diagnostics.setToolTip(
            "Crea un ZIP local sin audio, sesiones, dictados, capturas, transcripciones ni mensajes de log"
        )
        export_diagnostics.clicked.connect(controller.export_diagnostics_bundle)
        self.diagnostics_bundle_status = QLabel()
        self.diagnostics_bundle_status.setObjectName("Muted")
        privacy_actions.addWidget(open_privacy)
        privacy_actions.addWidget(export_diagnostics)
        privacy_row.addLayout(privacy_actions)
        privacy_row.addWidget(self.diagnostics_bundle_status)
        layout.addWidget(privacy)
        diagnostics = QFrame()
        diagnostics.setObjectName("Card")
        diagnostics_layout = QVBoxLayout(diagnostics)
        diagnostics_header = QHBoxLayout()
        diagnostics_titles = QVBoxLayout()
        diagnostics_title = QLabel("Comprobación de audio")
        diagnostics_title.setObjectName("SectionTitle")
        diagnostics_hint = QLabel(
            "Escucha aproximadamente 3 segundos. Habla al MIC y reproduce algo para comprobar SYS. "
            "Las muestras se descartan: no se guardan, transcriben ni suben."
        )
        diagnostics_hint.setObjectName("Muted")
        diagnostics_hint.setWordWrap(True)
        diagnostics_titles.addWidget(diagnostics_title)
        diagnostics_titles.addWidget(diagnostics_hint)
        self.run_diagnostics = QPushButton("Probar MIC y SYS")
        self.run_diagnostics.setAccessibleName("Iniciar comprobación privada de micrófono y audio del sistema")
        self.run_diagnostics.clicked.connect(controller.run_device_diagnostics)
        diagnostics_header.addLayout(diagnostics_titles, 1)
        diagnostics_header.addStretch()
        diagnostics_header.addWidget(self.run_diagnostics, 0, Qt.AlignmentFlag.AlignTop)
        self.diagnostics_status = StatusPill("Sin comprobar", "neutral")
        self.diagnostics_progress = QProgressBar()
        self.diagnostics_progress.setRange(0, 0)
        self.diagnostics_progress.setTextVisible(False)
        self.diagnostics_progress.setMaximumHeight(5)
        self.diagnostics_progress.hide()
        self.diagnostic_summary = QLabel(
            "Esta prueba confirma que Windows permite abrir cada ruta y si recibió nivel; silencio no es una avería."
        )
        self.diagnostic_summary.setObjectName("Muted")
        self.diagnostic_summary.setWordWrap(True)
        diagnostics_layout.addLayout(diagnostics_header)
        diagnostics_layout.addWidget(self.diagnostics_progress)
        diagnostics_layout.addWidget(self.diagnostics_status, 0, Qt.AlignmentFlag.AlignLeft)
        diagnostics_layout.addWidget(self.diagnostic_summary)

        source_grid = QGridLayout()
        self.diagnostic_source_pills: dict[str, StatusPill] = {}
        self.diagnostic_source_details: dict[str, QLabel] = {}
        for column, (source_id, title_text, explanation) in enumerate(
            (
                ("mic", "MIC · tu voz", "Entrada predeterminada de Windows"),
                ("system", "SYS · lo que reproduce el PC", "Loopback de la salida predeterminada"),
            )
        ):
            source = QFrame()
            source.setObjectName("Card")
            source_layout = QVBoxLayout(source)
            source_title = QLabel(title_text)
            source_title.setObjectName("Eyebrow")
            source_title.setWordWrap(True)
            pill = StatusPill("Esperando", "neutral")
            detail = QLabel(explanation)
            detail.setObjectName("Muted")
            detail.setWordWrap(True)
            source_layout.addWidget(source_title)
            source_layout.addWidget(pill, 0, Qt.AlignmentFlag.AlignLeft)
            source_layout.addWidget(detail)
            self.diagnostic_source_pills[source_id] = pill
            self.diagnostic_source_details[source_id] = detail
            source_grid.addWidget(source, 0, column)
        diagnostics_layout.addLayout(source_grid)

        routing_title = QLabel("Rutas que usará WhisperKey")
        routing_title.setObjectName("Eyebrow")
        routing_hint = QLabel(
            "Predeterminado sigue los cambios de Windows. Una selección explícita permanece hasta que la cambies."
        )
        routing_hint.setObjectName("Muted")
        routing_hint.setWordWrap(True)
        diagnostics_layout.addWidget(routing_title)
        diagnostics_layout.addWidget(routing_hint)
        routing_grid = QGridLayout()
        self.mic_route = QComboBox()
        self.system_route = QComboBox()
        for route_combo in (self.mic_route, self.system_route):
            route_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            route_combo.setMinimumContentsLength(18)
        self.mic_route.setAccessibleName("Ruta de micrófono para Dictado y sesiones")
        self.system_route.setAccessibleName("Ruta de salida para audio del sistema")
        self.mic_route.addItem("Predeterminado de Windows", "default")
        self.system_route.addItem("Predeterminado de Windows", "default")
        routing_grid.addWidget(QLabel("MIC · entrada"), 0, 0)
        routing_grid.addWidget(self.mic_route, 0, 1)
        routing_grid.addWidget(QLabel("SYS · salida"), 1, 0)
        routing_grid.addWidget(self.system_route, 1, 1)
        diagnostics_layout.addLayout(routing_grid)
        routing_actions = QHBoxLayout()
        self.audio_route_status = QLabel()
        self.audio_route_status.setObjectName("Muted")
        save_routes = QPushButton("Aplicar rutas")
        save_routes.clicked.connect(
            lambda: self.audio_routes_save_requested.emit(
                {
                    "input_device": self.mic_route.currentData(),
                    "system_audio_device": self.system_route.currentData(),
                }
            )
        )
        routing_actions.addWidget(self.audio_route_status)
        routing_actions.addStretch()
        routing_actions.addWidget(save_routes)
        diagnostics_layout.addLayout(routing_actions)

        devices_title = QLabel("Rutas detectadas")
        devices_title.setObjectName("Eyebrow")
        self.diagnostic_devices = QTreeWidget()
        self.diagnostic_devices.setHeaderLabels(["Ruta", "Dispositivo", "API", "Frecuencia"])
        self.diagnostic_devices.setMaximumHeight(170)
        self.diagnostic_devices.setRootIsDecorated(False)
        self.diagnostic_devices.setAlternatingRowColors(True)
        self.diagnostic_devices.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.diagnostic_devices.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.diagnostic_devices.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.diagnostic_devices.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.diagnostic_devices.header().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        diagnostics_layout.addWidget(devices_title)
        diagnostics_layout.addWidget(self.diagnostic_devices)
        layout.addWidget(diagnostics)
        layout.addStretch()

    def set_hotkeys(self, values: dict) -> None:
        for key, field in self.hotkey_fields.items():
            field.setText(values.get(key, ""))

    def set_retention(self, values: dict) -> None:
        for key, field in self.retention_fields.items():
            index = field.findData(values.get(key, "all"))
            field.setCurrentIndex(max(0, index))
        self.context_before.setValue(int(values.get("marker_context_before_ms", 30000)) // 1000)
        self.context_after.setValue(int(values.get("marker_context_after_ms", 30000)) // 1000)

    def set_settings_status(self, state: str, message: str) -> None:
        if message.startswith("Diagnóstico"):
            target = self.diagnostics_bundle_status
        elif message.startswith("Retención"):
            target = self.retention_status
        elif message.startswith("Audio"):
            target = self.audio_route_status
        else:
            target = self.hotkey_status
        target.setText(message)
        target.setObjectName("RecordingStatus" if state == "success" else "DangerStatus")
        target.style().unpolish(target)
        target.style().polish(target)

    def set_audio_routes(self, values: dict) -> None:
        self._audio_routes = {
            "input_device": values.get("input_device", "default"),
            "input_device_name": values.get("input_device_name"),
            "system_audio_device": values.get("system_audio_device", "default"),
        }
        self._select_or_append_route(
            self.mic_route,
            self._audio_routes["input_device"],
            "MIC configurado",
        )
        self._select_or_append_route(
            self.system_route,
            self._audio_routes["system_audio_device"],
            "SYS configurado",
        )

    @staticmethod
    def _select_or_append_route(combo: QComboBox, value, prefix: str) -> None:
        index = combo.findData(value)
        if index < 0:
            combo.addItem(f"{prefix} · no detectado ({value})", value)
            index = combo.count() - 1
        combo.setCurrentIndex(index)

    def _populate_audio_routes(
        self,
        devices: list[dict],
        system_outputs: list[dict] | None = None,
    ) -> None:
        configured = getattr(
            self,
            "_audio_routes",
            {
                "input_device": "default",
                "input_device_name": None,
                "system_audio_device": "default",
            },
        )
        selected = {
            "input_device": self.mic_route.currentData()
            if self.mic_route.currentData() is not None
            else configured["input_device"],
            "system_audio_device": self.system_route.currentData()
            if self.system_route.currentData() is not None
            else configured["system_audio_device"],
        }
        self.mic_route.blockSignals(True)
        self.system_route.blockSignals(True)
        self.mic_route.clear()
        self.system_route.clear()
        self.mic_route.addItem("Predeterminado de Windows", "default")
        self.system_route.addItem("Predeterminado de Windows", "default")
        configured_input_name = str(configured.get("input_device_name") or "").strip().casefold()
        matched_configured_input = None
        for device in devices:
            if device.get("input_channels"):
                self.mic_route.addItem(device.get("name", "MIC"), device.get("device_id"))
                if configured_input_name and str(device.get("name", "")).strip().casefold() == configured_input_name:
                    matched_configured_input = device.get("device_id")
        if matched_configured_input is not None and configured.get("input_device") != "default":
            selected["input_device"] = matched_configured_input
        outputs = system_outputs
        if outputs is None:
            outputs = [
                {"name": device.get("name", "Salida"), "default": device.get("default_output")}
                for device in devices
                if device.get("output_channels")
            ]
        for output in outputs:
            name = output.get("name")
            if not name:
                continue
            label = f"{name} · predeterminada" if output.get("default") else str(name)
            self.system_route.addItem(label, str(name))
        self._select_or_append_route(self.mic_route, selected["input_device"], "MIC configurado")
        self._select_or_append_route(
            self.system_route,
            selected["system_audio_device"],
            "SYS configurado",
        )
        self.mic_route.blockSignals(False)
        self.system_route.blockSignals(False)

    def set_audio_diagnostics(self, report: dict) -> None:
        state = report.get("state")
        summary = report.get("summary") or {}
        if state == "running":
            self.run_diagnostics.setEnabled(False)
            self.run_diagnostics.setText("Escuchando…")
            self.diagnostics_progress.show()
            self.diagnostics_status.setText(summary.get("title", "Comprobando audio…"))
            self.diagnostics_status.set_status("warning")
            self.diagnostic_summary.setText(summary.get("detail", ""))
            for pill in self.diagnostic_source_pills.values():
                pill.setText("Midiendo")
                pill.set_status("warning")
            return

        self.run_diagnostics.setEnabled(True)
        self.run_diagnostics.setText("Repetir prueba")
        self.diagnostics_progress.hide()
        summary_status = summary.get("status", "fail")
        self.diagnostics_status.setText(summary.get("title", "Diagnóstico incompleto"))
        self.diagnostics_status.set_status(
            "active" if summary_status == "pass" else ("error" if summary_status == "fail" else "warning")
        )
        self.diagnostic_summary.setText(summary.get("detail", ""))
        labels = {
            "active": ("Señal recibida", "active"),
            "quiet": ("Señal muy baja", "warning"),
            "silent": ("Silencio", "warning"),
            "unavailable": ("No disponible", "error"),
        }
        for source_id in ("mic", "system"):
            source = report.get(source_id) or {}
            label, pill_status = labels.get(source.get("status"), ("Sin resultado", "neutral"))
            self.diagnostic_source_pills[source_id].setText(label)
            self.diagnostic_source_pills[source_id].set_status(pill_status)
            peak = source.get("peak_dbfs")
            level = f" · pico {peak:.1f} dBFS" if isinstance(peak, (int, float)) else ""
            error = f" · {source['error']}" if source.get("error") else ""
            self.diagnostic_source_details[source_id].setText(
                f"{source.get('device') or 'Sin dispositivo'}{level}\n{source.get('detail', '')}{error}"
            )

        self.diagnostic_devices.clear()
        for device in report.get("devices", []):
            directions = []
            if device.get("input_channels"):
                directions.append("MIC")
            if device.get("output_channels"):
                directions.append("OUT")
            default_labels = []
            if device.get("default_input"):
                default_labels.append("MIC predet.")
            if device.get("default_output"):
                default_labels.append("OUT predet.")
            route = "/".join(directions)
            if default_labels:
                route += f" · {', '.join(default_labels)}"
            self.diagnostic_devices.addTopLevelItem(
                QTreeWidgetItem(
                    [
                        route,
                        device.get("name", ""),
                        device.get("hostapi", ""),
                        f"{device.get('sample_rate', 0)} Hz",
                    ]
                )
            )
        self._populate_audio_routes(
            report.get("devices", []),
            report.get("system_outputs"),
        )


class AcceptancePage(QWidget):
    benchmark_requested = Signal(str)
    result_requested = Signal(str, str, str)
    export_requested = Signal()

    STATUS_LABELS = {
        "not_run": "Pendiente",
        "pass": "Aprobada",
        "fail": "Falló",
        "review": "Revisar",
        "skipped": "Omitida",
    }

    def __init__(self):
        super().__init__()
        self.setObjectName("Page")
        self._report: dict = {}
        self._current_scenario_id: str | None = None
        self._recording_scenario_id: str | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(32, 28, 32, 28)
        outer.setSpacing(14)
        heading = QHBoxLayout()
        titles = QVBoxLayout()
        eyebrow = QLabel("EVIDENCIA LOCAL · SIN SUBIR AUDIO")
        eyebrow.setObjectName("Eyebrow")
        title = QLabel("Comprobación real")
        title.setObjectName("PageTitle")
        subtitle = QLabel(
            "Mide idiomas con frases fijas y registra las pruebas que requieren tu voz, "
            "tu audio y tus aplicaciones reales."
        )
        subtitle.setObjectName("Muted")
        subtitle.setWordWrap(True)
        titles.addWidget(eyebrow)
        titles.addWidget(title)
        titles.addWidget(subtitle)
        heading.addLayout(titles, 1)
        self.summary = StatusPill("Cargando pruebas…", "warning")
        heading.addWidget(self.summary, 0, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(heading)

        body = QHBoxLayout()
        body.setSpacing(14)
        self.tree = QTreeWidget()
        self.tree.setObjectName("AcceptanceTree")
        self.tree.setHeaderLabels(["Gate / prueba", "Estado"])
        self.tree.setMinimumWidth(300)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tree.itemSelectionChanged.connect(self._show_selected)
        body.addWidget(self.tree, 2)

        detail = QFrame()
        detail.setObjectName("Card")
        detail_layout = QVBoxLayout(detail)
        detail_layout.setSpacing(10)
        self.detail_gate = QLabel("Selecciona una prueba")
        self.detail_gate.setObjectName("Eyebrow")
        self.detail_title = QLabel("Evidencia pendiente")
        self.detail_title.setObjectName("SectionTitle")
        self.detail_title.setWordWrap(True)
        self.instructions = QLabel("Aquí aparecerán los pasos y el resultado medido.")
        self.instructions.setObjectName("Muted")
        self.instructions.setWordWrap(True)
        detail_layout.addWidget(self.detail_gate)
        detail_layout.addWidget(self.detail_title)
        detail_layout.addWidget(self.instructions)

        reference_label = QLabel("Frase exacta")
        reference_label.setObjectName("Eyebrow")
        self.reference = QTextBrowser()
        self.reference.setMaximumHeight(82)
        self.reference.setPlaceholderText("Esta prueba se valida manualmente; no necesita frase.")
        detail_layout.addWidget(reference_label)
        detail_layout.addWidget(self.reference)

        self.metrics = QLabel("Todavía no hay medición.")
        self.metrics.setObjectName("Muted")
        self.metrics.setWordWrap(True)
        self.metrics.setMinimumHeight(62)
        detail_layout.addWidget(self.metrics)
        transcript_label = QLabel("Última transcripción")
        transcript_label.setObjectName("Eyebrow")
        self.transcript = QTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setMaximumHeight(110)
        self.transcript.setPlaceholderText("La transcripción medida aparecerá aquí.")
        detail_layout.addWidget(transcript_label)
        detail_layout.addWidget(self.transcript)

        self.note = QLineEdit()
        self.note.setPlaceholderText("Nota breve: qué observaste, ventana usada o motivo del fallo")
        detail_layout.addWidget(self.note)
        self.benchmark = QPushButton("Iniciar dictado de prueba")
        self.benchmark.setProperty("primary", True)
        self.benchmark.clicked.connect(self._toggle_benchmark)
        detail_layout.addWidget(self.benchmark)
        result_row = QHBoxLayout()
        self.pass_button = QPushButton("Aprobar")
        self.fail_button = QPushButton("Marcar fallo")
        self.review_button = QPushButton("Revisar después")
        self.skip_button = QPushButton("Omitir esta prueba")
        self.pass_button.clicked.connect(lambda: self._mark("pass"))
        self.fail_button.clicked.connect(lambda: self._mark("fail"))
        self.review_button.clicked.connect(lambda: self._mark("review"))
        self.skip_button.clicked.connect(lambda: self._mark("skipped"))
        result_row.addWidget(self.pass_button)
        result_row.addWidget(self.fail_button)
        result_row.addWidget(self.review_button)
        detail_layout.addLayout(result_row)
        detail_layout.addWidget(self.skip_button)
        detail_layout.addStretch()
        self.detail_scroll = QScrollArea()
        self.detail_scroll.setObjectName("AcceptanceDetailScroll")
        self.detail_scroll.setWidgetResizable(True)
        self.detail_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.detail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.detail_scroll.setWidget(detail)
        body.addWidget(self.detail_scroll, 3)
        outer.addLayout(body, 1)

        footer = QHBoxLayout()
        self.environment = QLabel("El entorno se registrará cuando termine de cargar el modelo.")
        self.environment.setObjectName("Muted")
        self.environment.setWordWrap(True)
        footer.addWidget(self.environment, 1)
        export = QPushButton("Exportar informe")
        export.clicked.connect(self.export_requested)
        footer.addWidget(export)
        outer.addLayout(footer)
        self._set_controls_enabled(False)

    def set_report(self, report: dict) -> None:
        self._report = report or {}
        selected = self._current_scenario_id
        self.tree.blockSignals(True)
        self.tree.clear()
        groups: dict[str, QTreeWidgetItem] = {}
        for scenario in self._report.get("scenarios", []):
            gate = scenario.get("gate", "—")
            if gate not in groups:
                groups[gate] = QTreeWidgetItem([gate, ""])
                groups[gate].setExpanded(True)
                self.tree.addTopLevelItem(groups[gate])
            status = (scenario.get("result") or {}).get("status", "not_run")
            item = QTreeWidgetItem(
                [scenario.get("title", scenario.get("scenario_id", "")), self.STATUS_LABELS.get(status, status)]
            )
            item.setData(0, Qt.ItemDataRole.UserRole, scenario.get("scenario_id"))
            item.setToolTip(0, scenario.get("title", ""))
            item.setToolTip(1, self.STATUS_LABELS.get(status, status))
            groups[gate].addChild(item)
            if scenario.get("scenario_id") == selected:
                self.tree.setCurrentItem(item)
        self.tree.blockSignals(False)
        summary = self._report.get("summary", {})
        total = summary.get("total", len(self._report.get("scenarios", [])))
        passed = summary.get("pass", 0)
        failed = summary.get("fail", 0)
        pending = summary.get("not_run", total - passed - failed)
        self.summary.setText(f"{passed}/{total} aprobadas · {pending} pendientes")
        self.summary.set_status("error" if failed else ("active" if total and passed == total else "warning"))
        env = self._report.get("environment") or {}
        if env:
            model_load = env.get("model_load_ms")
            load_label = f" · carga {model_load / 1000:.2f} s" if isinstance(model_load, (int, float)) else ""
            self.environment.setText(
                f"{env.get('model', 'modelo desconocido')} · {str(env.get('device', '—')).upper()} · "
                f"{env.get('compute_type', '—')}{load_label} · app {env.get('app_version', '—')}"
            )
        if not self.tree.currentItem():
            first = next(
                (groups[key].child(0) for key in groups if groups[key].childCount()),
                None,
            )
            if first:
                self.tree.setCurrentItem(first)
        else:
            self._show_selected()

    def _selected_scenario(self) -> dict | None:
        item = self.tree.currentItem()
        scenario_id = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if not scenario_id:
            return None
        return next(
            (value for value in self._report.get("scenarios", []) if value.get("scenario_id") == scenario_id),
            None,
        )

    def _show_selected(self) -> None:
        scenario = self._selected_scenario()
        if not scenario:
            self._set_controls_enabled(False)
            return
        self._current_scenario_id = scenario["scenario_id"]
        result = scenario.get("result") or {}
        evidence = result.get("evidence") or {}
        status_label = self.STATUS_LABELS.get(result.get("status", "not_run"))
        self.detail_gate.setText(f"{scenario.get('gate')} · {scenario.get('category')} · {status_label}")
        self.detail_title.setText(scenario.get("title", ""))
        self.instructions.setText(scenario.get("instructions", ""))
        self.reference.setPlainText(scenario.get("reference", ""))
        self.transcript.setPlainText(evidence.get("transcript", ""))
        if evidence.get("error"):
            self.metrics.setText(f"Error del motor · {evidence['error']}")
        elif evidence:
            wer = evidence.get("word_error_rate")
            cer = evidence.get("character_error_rate")
            rtf = evidence.get("real_time_factor")
            probability = evidence.get("language_probability")
            parts = [
                f"WER {wer * 100:.1f}%" if isinstance(wer, (int, float)) else "WER —",
                f"CER {cer * 100:.1f}%" if isinstance(cer, (int, float)) else "CER —",
                f"RTF {rtf:.2f}" if isinstance(rtf, (int, float)) else "RTF —",
                f"Idioma {evidence.get('detected_language') or '—'}",
                f"confianza {probability * 100:.0f}%" if isinstance(probability, (int, float)) else "confianza —",
            ]
            model_load = evidence.get("model_load_ms")
            if isinstance(model_load, (int, float)):
                parts.append(f"carga {model_load / 1000:.2f} s")
            inference_index = evidence.get("inference_index")
            if evidence.get("cold_inference"):
                parts.append("primera inferencia")
            elif isinstance(inference_index, int):
                parts.append(f"inferencia #{inference_index}")
            performance = evidence.get("performance") or {}
            process = performance.get("process") or {}
            if process.get("status") == "measured":
                peak = process.get("peak_bytes")
                delta = process.get("delta_bytes")
                if isinstance(peak, (int, float)):
                    label = f"RAM {peak / (1024**3):.2f} GiB"
                    if isinstance(delta, (int, float)):
                        label += f" (Δ {delta / (1024**2):.0f} MiB)"
                    parts.append(label)
            elif performance:
                parts.append("RAM —")
            gpu = performance.get("gpu") or {}
            if gpu.get("status") == "measured":
                peak = gpu.get("peak_vram_used_bytes")
                delta = gpu.get("delta_vram_used_bytes")
                if isinstance(peak, (int, float)):
                    label = f"VRAM total {peak / (1024**3):.2f} GiB"
                    if isinstance(delta, (int, float)):
                        label += f" (Δ {delta / (1024**2):.0f} MiB)"
                    parts.append(label)
                temperature = gpu.get("peak_temperature_c")
                if isinstance(temperature, (int, float)):
                    parts.append(f"GPU {temperature:.0f} °C")
            elif gpu.get("status") == "unavailable":
                parts.append("VRAM/temperatura —")
            self.metrics.setText(" · ".join(parts))
        else:
            self.metrics.setText(result.get("note") or "Todavía no hay medición.")
        self.note.setText(result.get("note", "") if result.get("reviewed_by_user") else "")
        is_benchmark = scenario.get("kind") == "dictation_benchmark"
        self.benchmark.setVisible(is_benchmark)
        self._set_controls_enabled(True)
        self.benchmark.setEnabled(is_benchmark)

    def _set_controls_enabled(self, enabled: bool) -> None:
        self.pass_button.setEnabled(enabled)
        self.fail_button.setEnabled(enabled)
        self.review_button.setEnabled(enabled)
        self.skip_button.setEnabled(enabled)
        self.note.setEnabled(enabled)
        self.benchmark.setEnabled(enabled)

    def _toggle_benchmark(self) -> None:
        scenario = self._selected_scenario()
        if scenario and scenario.get("kind") == "dictation_benchmark":
            self._recording_scenario_id = scenario["scenario_id"]
            self.benchmark_requested.emit(scenario["scenario_id"])

    def _mark(self, status: str) -> None:
        scenario = self._selected_scenario()
        if scenario:
            self.result_requested.emit(scenario["scenario_id"], status, self.note.text())

    def set_dictation_state(self, state: str, message: str, text: str) -> None:
        if not self._recording_scenario_id:
            return
        if state == "recording":
            self.benchmark.setText("Detener y medir")
            self.metrics.setText("Escuchando la frase de comprobación…")
        elif state == "processing":
            self.benchmark.setText("Midiendo…")
            self.benchmark.setEnabled(False)
            self.metrics.setText(message)
        elif state in {"complete", "empty", "error", "canceled"}:
            self.benchmark.setText("Repetir dictado de prueba")
            self.benchmark.setEnabled(True)
            if text:
                self.transcript.setPlainText(text)
            self._recording_scenario_id = None


class SessionPage(QWidget):
    pause_requested = Signal()
    marker_requested = Signal(str, str)
    spoken_marker_requested = Signal(str, str)
    finalize_requested = Signal(str)
    open_folder_requested = Signal()
    snapshot_requested = Signal(str)
    clean_save_requested = Signal(str)
    speaker_names_save_requested = Signal(dict)
    retention_preview_requested = Signal()
    retention_restore_requested = Signal()
    processing_job_requested = Signal(str)
    open_handoff_requested = Signal()
    copy_handoff_requested = Signal()

    def __init__(self):
        super().__init__()
        self.current_status = ""
        self._session_folder: Path | None = None
        self._handoff_available = False
        self.setObjectName("SessionSurface")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 20, 28, 20)
        layout.setSpacing(12)
        top = QHBoxLayout()
        self.activity = PixelMicActivity()
        self.activity.setFixedSize(62, 48)
        top.addWidget(self.activity)
        title_stack = QVBoxLayout()
        self.mode_label = QLabel("SESIÓN")
        self.mode_label.setObjectName("Eyebrow")
        self.title = QLineEdit()
        self.title.setObjectName("SessionTitle")
        self.title.setPlaceholderText("Ponle un nombre a esta sesión…")
        title_stack.addWidget(self.mode_label)
        title_stack.addWidget(self.title)
        top.addLayout(title_stack, 1)
        self.status = StatusPill("Preparando", "warning")
        self.timer = QLabel("00:00:00")
        self.timer.setObjectName("Timer")
        top.addWidget(self.status)
        top.addWidget(self.timer)
        layout.addLayout(top)
        telemetry = QHBoxLayout()
        self.mic = StatusPill("MIC · esperando")
        self.system = StatusPill("SYS · esperando")
        self.queue = QLabel("Colas 0 / 0")
        self.queue.setObjectName("Muted")
        self.duration_detail = QLabel("Audio 00:00:00 · Pausas/inactividad 00:00:00 · Procesamiento 00:00:00")
        self.duration_detail.setObjectName("Muted")
        telemetry.addWidget(self.mic)
        telemetry.addWidget(self.system)
        telemetry.addWidget(self.queue)
        telemetry.addWidget(self.duration_detail)
        telemetry.addStretch()
        layout.addLayout(telemetry)
        self.capture_feedback = QLabel()
        self.capture_feedback.setObjectName("SuccessBanner")
        self.capture_feedback.setWordWrap(True)
        self.capture_feedback.setVisible(False)
        layout.addWidget(self.capture_feedback)

        self.tabs = QTabWidget()
        self.live = QTextEdit()
        self.live.setReadOnly(True)
        self.live.setPlaceholderText("La transcripción final irá apareciendo aquí. El audio ya se está guardando.")
        self.literal = QTextEdit()
        self.literal.setReadOnly(True)
        self.literal.setPlaceholderText(
            "Aquí aparecerá exactamente lo transcrito, con fuentes y tiempos; nunca se reemplaza por un resumen."
        )
        self.clean = QTextEdit()
        self.clean.setPlaceholderText(
            "Documento legible con espacios y estructura normalizados, sin cambiar el significado."
        )
        self.clean_preview = QTextBrowser()
        self.clean_preview.setOpenExternalLinks(False)
        self.markers = QListWidget()
        self.media = QListWidget()
        self.media.setIconSize(QSize(132, 84))
        self.media.itemDoubleClicked.connect(self._open_media)
        self.media.addItem("Las capturas de pantalla y fotos aparecerán aquí, ligadas al timeline.")
        export = QWidget()
        export_layout = QVBoxLayout(export)
        export_title = QLabel("Archivos y continuación con IA")
        export_title.setObjectName("SectionTitle")
        export_hint = QLabel(
            "WhisperKey prepara documentos legibles y un paquete de instrucciones para Codex/Claude. "
            "No envía nada automáticamente: 'handoff.md' solo explica qué archivos usar."
        )
        export_hint.setWordWrap(True)
        export_hint.setObjectName("Muted")
        export_layout.addWidget(export_title)
        export_layout.addWidget(export_hint)
        self.jobs = QTreeWidget()
        self._job_items: dict[str, QTreeWidgetItem] = {}
        self.jobs.setHeaderLabels(["Trabajo", "Estado", "Detalle"])
        self.jobs.setRootIsDecorated(False)
        self.jobs.header().resizeSection(0, 190)
        self.jobs.header().resizeSection(1, 120)
        export_layout.addWidget(self.jobs)
        job_actions = QHBoxLayout()
        self.retry_job = QPushButton("Reintentar seleccionado")
        self.retry_job.setToolTip("Vuelve a ejecutar solo el trabajo seleccionado; no reabre la sesión")
        self.retry_job.clicked.connect(self._retry_selected_job)
        job_actions.addWidget(self.retry_job)
        job_actions.addStretch()
        export_layout.addLayout(job_actions)

        handoff_block = QVBoxLayout()
        handoff_text = QVBoxLayout()
        handoff_heading = QLabel("Paquete para Codex / Claude")
        handoff_heading.setObjectName("SectionTitle")
        self.handoff_summary = QLabel(
            "Aún no preparado · no se envía nada automáticamente y no requiere una API de pago."
        )
        self.handoff_summary.setObjectName("Muted")
        self.handoff_summary.setWordWrap(True)
        handoff_text.addWidget(handoff_heading)
        handoff_text.addWidget(self.handoff_summary)
        handoff_block.addLayout(handoff_text)
        self.prepare_handoff = QPushButton("Preparar / actualizar")
        self.prepare_handoff.setToolTip("Congela snapshots, archivos seleccionados y hashes del paquete")
        self.prepare_handoff.clicked.connect(lambda: self.processing_job_requested.emit("handoff"))
        self.verify_handoff = QPushButton("Verificar")
        self.verify_handoff.clicked.connect(lambda: self.processing_job_requested.emit("handoff_verify"))
        self.copy_handoff = QPushButton("Copiar instrucciones")
        self.copy_handoff.clicked.connect(self.copy_handoff_requested)
        self.open_handoff = QPushButton("Abrir handoff.md")
        self.open_handoff.clicked.connect(self.open_handoff_requested)
        self.prepare_handoff.setEnabled(False)
        self.verify_handoff.setEnabled(False)
        self.copy_handoff.setEnabled(False)
        self.open_handoff.setEnabled(False)
        handoff_actions = QHBoxLayout()
        handoff_actions.addWidget(self.prepare_handoff)
        handoff_actions.addWidget(self.verify_handoff)
        handoff_actions.addWidget(self.copy_handoff)
        handoff_actions.addWidget(self.open_handoff)
        handoff_actions.addStretch()
        handoff_block.addLayout(handoff_actions)
        export_layout.addLayout(handoff_block)
        retention_row = QHBoxLayout()
        retention_text = QVBoxLayout()
        retention_heading = QLabel("Audio original")
        retention_heading.setObjectName("SectionTitle")
        self.retention_summary = QLabel("Política: conservar todo")
        self.retention_summary.setObjectName("Muted")
        self.retention_summary.setWordWrap(True)
        retention_text.addWidget(retention_heading)
        retention_text.addWidget(self.retention_summary)
        retention_row.addLayout(retention_text, 1)
        self.retention_review = QPushButton("Revisar retención")
        self.retention_review.setToolTip("Muestra rutas, tamaños y verificaciones antes de mover audio")
        self.retention_review.clicked.connect(self.retention_preview_requested)
        self.retention_restore = QPushButton("Restaurar audio")
        self.retention_restore.setToolTip("Restaura la aplicación de retención más reciente")
        self.retention_restore.clicked.connect(self.retention_restore_requested)
        retention_row.addWidget(self.retention_review)
        retention_row.addWidget(self.retention_restore)
        export_layout.addLayout(retention_row)
        export_layout.addStretch()
        export_buttons = QHBoxLayout()
        copy_button = QPushButton("Copiar texto visible")
        copy_button.clicked.connect(self._copy_visible)
        folder_button = QPushButton("Abrir carpeta")
        folder_button.clicked.connect(self.open_folder_requested)
        export_buttons.addWidget(copy_button)
        export_buttons.addWidget(folder_button)
        export_buttons.addStretch()
        export_layout.addLayout(export_buttons)
        live_tab = QWidget()
        live_layout = QVBoxLayout(live_tab)
        live_layout.setContentsMargins(0, 0, 0, 0)
        live_layout.setSpacing(8)
        live_layout.addWidget(self.live, 1)
        self.provisional = QLabel()
        self.provisional.setObjectName("Muted")
        self.provisional.setWordWrap(True)
        self.provisional.setVisible(False)
        live_layout.addWidget(self.provisional)
        self.tabs.addTab(live_tab, "En vivo")
        self.tabs.addTab(self.literal, "Literal")
        clean_tab = QWidget()
        clean_layout = QVBoxLayout(clean_tab)
        clean_layout.setContentsMargins(0, 0, 0, 0)
        clean_layout.addWidget(self.clean_preview, 1)
        clean_layout.addWidget(self.clean, 1)
        self.clean.setVisible(False)
        clean_actions = QHBoxLayout()
        clean_note = QLabel("Vista legible. Editar crea una revisión; el literal permanece intacto.")
        clean_note.setObjectName("Muted")
        self.edit_clean = QPushButton("Editar Markdown")
        self.edit_clean.clicked.connect(self._toggle_clean_editor)
        save_clean = QPushButton("Guardar revisión")
        save_clean.clicked.connect(lambda: self.clean_save_requested.emit(self.clean.toPlainText()))
        save_clean.setVisible(False)
        self.save_clean = save_clean
        clean_actions.addWidget(clean_note)
        clean_actions.addStretch()
        clean_actions.addWidget(self.edit_clean)
        clean_actions.addWidget(save_clean)
        clean_layout.addLayout(clean_actions)
        self.tabs.addTab(clean_tab, "Limpio")
        speakers_tab = QWidget()
        speakers_layout = QVBoxLayout(speakers_tab)
        speakers_layout.setContentsMargins(0, 0, 0, 0)
        speakers_header = QHBoxLayout()
        speakers_text = QVBoxLayout()
        speakers_title = QLabel("Nombres de hablantes")
        speakers_title.setObjectName("SectionTitle")
        self.speaker_revision = QLabel(
            "La diarización propone etiquetas anónimas; aquí puedes convertirlas en nombres legibles."
        )
        self.speaker_revision.setObjectName("Muted")
        self.speaker_revision.setWordWrap(True)
        speakers_text.addWidget(speakers_title)
        speakers_text.addWidget(self.speaker_revision)
        speakers_header.addLayout(speakers_text)
        speakers_header.addStretch()
        self.save_speakers = QPushButton("Guardar nombres")
        self.save_speakers.setProperty("primary", True)
        self.save_speakers.setEnabled(False)
        self.save_speakers.clicked.connect(self._save_speaker_names)
        speakers_header.addWidget(self.save_speakers)
        speakers_layout.addLayout(speakers_header)
        self.speakers = QTreeWidget()
        self.speakers.setHeaderLabels(["Etiqueta técnica", "Nombre visible"])
        self.speakers.setRootIsDecorated(False)
        self.speakers.header().resizeSection(0, 240)
        self._speaker_fields: dict[str, QLineEdit] = {}
        speakers_layout.addWidget(self.speakers, 1)
        self.speakers_tab_index = self.tabs.addTab(speakers_tab, "Hablantes")
        self.tabs.addTab(self.markers, "Marcadores")
        self.media_tab_index = self.tabs.addTab(self.media, "Medios")
        self.tabs.addTab(export, "Exportar")
        self.tabs.setTabToolTip(0, "Feed inmediato: transcripción provisional/final, marcadores y capturas")
        self.tabs.setTabToolTip(1, "Palabras transcritas sin reescribir ni resumir")
        self.tabs.setTabToolTip(2, "Documento determinista preparado para leer y editar")
        self.tabs.setTabToolTip(3, "Nombres editables separados de la evidencia literal")
        self.tabs.setTabToolTip(6, "Archivos portables y estado del procesamiento")
        layout.addWidget(self.tabs, 1)

        controls = QVBoxLayout()
        marker_controls = QHBoxLayout()
        self.pause = QPushButton("Pausar")
        self.pause.clicked.connect(self.pause_requested)
        self.marker_kind = QComboBox()
        for label, value in [
            ("Importante", "important"),
            ("No entendí", "not_understood"),
            ("Pregunta", "question"),
            ("Investigar", "investigate"),
            ("Cita", "quote"),
            ("Desacuerdo", "disagreement"),
            ("Acción", "action"),
        ]:
            self.marker_kind.addItem(label, value)
        self.marker_note = QLineEdit()
        self.marker_note.setPlaceholderText("Nota opcional del marcador")
        marker = QPushButton("Añadir marcador")
        marker.clicked.connect(
            lambda: self.marker_requested.emit(self.marker_kind.currentData(), self.marker_note.text())
        )
        self.spoken_marker = QPushButton("Marcar + nota hablada")
        self.spoken_marker.setToolTip("Guarda el marcador ahora y enlaza la próxima frase detectada por el micrófono")
        self.spoken_marker.clicked.connect(
            lambda: self.spoken_marker_requested.emit(self.marker_kind.currentData(), self.marker_note.text())
        )
        self.finish = QPushButton("Finalizar sesión")
        self.finish.setProperty("primary", True)
        self.finish.clicked.connect(lambda: self.finalize_requested.emit(self.title.text()))
        marker_controls.addWidget(self.marker_kind)
        marker_controls.addWidget(self.marker_note, 1)
        marker_controls.addWidget(marker)
        marker_controls.addWidget(self.spoken_marker)
        controls.addLayout(marker_controls)
        session_actions = QHBoxLayout()
        session_actions.addWidget(self.pause)
        snapshot = QPushButton("Capturar pantalla")
        snapshot.setToolTip("Adjunta una región o ventana sin pausar el audio")
        snapshot_menu = QMenu(snapshot)
        full_action = snapshot_menu.addAction("Pantalla completa")
        full_action.triggered.connect(lambda: self.snapshot_requested.emit("full"))
        region_action = snapshot_menu.addAction("Seleccionar región")
        region_action.triggered.connect(lambda: self.snapshot_requested.emit("region"))
        window_action = snapshot_menu.addAction("Ventana activa")
        window_action.triggered.connect(lambda: self.snapshot_requested.emit("window"))
        snapshot.setMenu(snapshot_menu)
        session_actions.addWidget(snapshot)
        session_actions.addStretch()
        session_actions.addWidget(self.finish)
        controls.addLayout(session_actions)
        layout.addLayout(controls)

    def update_state(self, state: dict) -> None:
        status = state.get("status", "")
        self.current_status = status
        active = status == "recording"
        paused = status == "paused"
        completed = status == "completed"
        self.activity.set_active(active)
        self.mode_label.setText(MODE_LABELS.get(state.get("mode", ""), "SESIÓN").upper())
        if state.get("title") and not self.title.text():
            self.title.setText(state["title"])
        labels = {
            "recording": ("Grabando", "active"),
            "paused": ("En pausa", "warning"),
            "processing": ("Procesando", "warning"),
            "completed": ("Completada", "active"),
            "recoverable": ("Necesita nombre", "warning"),
        }
        label, pill = labels.get(status, (status.title(), "neutral"))
        self.status.setText(label)
        self.status.set_status(pill)
        self.timer.setText(format_duration(state.get("display_elapsed_ms", 0)))
        self.pause.setText("Continuar" if paused else "Pausar")
        self.pause.setEnabled(active or paused)
        self.spoken_marker.setEnabled(active)
        self.finish.setEnabled(not completed and not state.get("busy", False))
        retention = state.get("retention", {})
        retention_labels = {
            "all": "conservar todo",
            "until_verified": "conservar hasta verificar el literal",
            "marker_context": "conservar solo contexto de marcadores",
            "none": "retirar audio después de finalizar",
        }
        retention_label = retention_labels.get(retention.get("audio"), "sin configurar")
        self.retention_summary.setText(
            f"Política de esta sesión: {retention_label}. "
            "La revisión nunca elimina sin mostrar primero las rutas exactas."
        )
        self.retention_review.setEnabled(completed and not state.get("busy", False))
        self.retention_restore.setEnabled(completed and not state.get("busy", False))
        self.prepare_handoff.setEnabled(completed and not state.get("busy", False))
        self.verify_handoff.setEnabled(completed and self._handoff_available and not state.get("busy", False))
        self.copy_handoff.setEnabled(self._handoff_available)
        self.open_handoff.setEnabled(self._handoff_available)
        self.title.setReadOnly(completed)
        health = state.get("health", {})
        self._set_source(self.mic, "MIC", health.get("mic"), expected=True)
        self._set_source(
            self.system,
            "SYS",
            health.get("system"),
            expected=state.get("mode") in {"meeting", "learning"},
        )
        self.queue.setText(
            f"Colas audio/STT {state.get('persistence_backlog', 0)} / {state.get('transcription_backlog', 0)}"
        )
        self.duration_detail.setText(
            f"Audio {format_duration(state.get('captured_duration_ms', 0))} · "
            f"Pausas/inactividad {format_duration(state.get('paused_duration_ms', 0))} · "
            f"Procesamiento {format_duration(state.get('processing_duration_ms', 0))}"
        )

    def set_transcript(self, transcript: str) -> None:
        if self.live.toPlainText() == transcript:
            return
        scrollbar = self.live.verticalScrollBar()
        follow_tail = scrollbar.value() >= scrollbar.maximum() - 20
        self.live.setPlainText(transcript)
        if follow_tail:
            scrollbar.setValue(scrollbar.maximum())

    def set_provisional(self, transcript: str) -> None:
        self.provisional.setText(f"Provisional · {transcript}" if transcript else "")
        self.provisional.setVisible(bool(transcript))

    def set_markers(self, markers: list[dict]) -> None:
        self.markers.clear()
        for marker in markers:
            offset = format_duration(marker.get("at_ms", 0))
            note = marker.get("note") or "Sin nota"
            spoken = marker.get("spoken_note")
            suffix = f"\n    🎙 {spoken}" if spoken else ""
            self.markers.addItem(f"{offset}   {marker.get('kind', '')}   ·   {note}{suffix}")

    def set_media(self, media: list[dict]) -> None:
        self.media.clear()
        if not media:
            self.media.addItem("Aún no hay capturas ni extractos ligados a esta sesión.")
            self.tabs.setTabText(self.media_tab_index, "Medios")
            return
        for attachment in media:
            offset = format_duration(attachment.get("at_ms", 0))
            kind = attachment.get("kind", "archivo")
            path = attachment.get("relative_path", "")
            item = self._media_item(f"{offset}   {kind}   ·   {path}", attachment)
            self.media.addItem(item)
        self.tabs.setTabText(self.media_tab_index, f"Medios ({len(media)})" if media else "Medios")

    @staticmethod
    def _media_item(label: str, attachment: dict):
        item = QListWidgetItem(label)
        absolute = attachment.get("absolute_path")
        if absolute and Path(absolute).is_file() and attachment.get("media_type", "").startswith("image/"):
            item.setIcon(QIcon(absolute))
        item.setData(Qt.ItemDataRole.UserRole, absolute)
        return item

    def _open_media(self, item) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if path and Path(path).is_file():
            from whisper_key.utils import open_file

            open_file(path)

    def show_capture_feedback(self, result: dict) -> None:
        self.capture_feedback.setText(
            f"✓ Captura guardada · {result.get('width', 0)} × {result.get('height', 0)} px · "
            f"{result.get('relative_path', '')}"
        )
        self.capture_feedback.setVisible(True)
        QTimer.singleShot(8000, lambda: self.capture_feedback.setVisible(False))

    def show_final_document(self, folder: str | None) -> None:
        if not folder:
            return
        self.load_documents(folder)
        literal = Path(folder) / "transcript.raw.md"
        if literal.exists():
            self.tabs.setCurrentWidget(self.literal)

    def prepare_for_open(self) -> None:
        self._session_folder = None
        self._handoff_available = False
        self.title.setReadOnly(False)
        self.title.clear()
        self.live.clear()
        self.literal.clear()
        self.clean.clear()
        self.clean_preview.clear()
        self.clean.setVisible(False)
        self.clean_preview.setVisible(True)
        self.save_clean.setVisible(False)
        self.edit_clean.setText("Editar Markdown")
        self.provisional.clear()
        self.provisional.setVisible(False)
        self.markers.clear()
        self.media.clear()
        self.jobs.clear()
        self._job_items.clear()
        self.handoff_summary.setText(
            "Aún no preparado · no se envía nada automáticamente y no requiere una API de pago."
        )
        self.verify_handoff.setEnabled(False)
        self.copy_handoff.setEnabled(False)
        self.open_handoff.setEnabled(False)
        self.speakers.clear()
        self._speaker_fields.clear()
        self.save_speakers.setEnabled(False)
        self.capture_feedback.setVisible(False)

    def load_documents(self, folder: str) -> None:
        root = Path(folder)
        self._session_folder = root
        literal = root / "transcript.raw.md"
        clean = root / "transcript.clean.md"
        if literal.is_file():
            self.literal.setPlainText(literal.read_text(encoding="utf-8"))
        if clean.is_file():
            self.set_clean_document(clean.read_text(encoding="utf-8"))
        self.load_handoff(root)
        speakers = root / "speakers.json"
        if speakers.is_file():
            try:
                self.set_speakers(json.loads(speakers.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                self.set_speakers(None)
        timeline = root / "timeline.jsonl"
        if not timeline.is_file():
            return
        latest_jobs = {}
        for line in timeline.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "processing_job":
                payload = event.get("payload", {})
                latest_jobs[payload.get("job", "")] = payload
        for job, payload in latest_jobs.items():
            if not job:
                continue
            detail = payload.get("output") or payload.get("error") or ""
            self.update_processing_job(job, payload.get("status", ""), detail)

    def update_processing_job(self, job: str, status: str, detail: str) -> None:
        labels = {
            "integrity": "Integridad de audio",
            "marker_context": "Contexto de marcadores",
            "diarization": "Diarización",
            "clean": "Markdown limpio",
            "markers": "Índice de marcadores",
            "mode": "Documento del modo",
            "handoff": "Instrucciones para Codex/Claude",
            "handoff_verify": "Verificación del handoff",
            "html": "HTML offline",
        }
        item = self._job_items.get(job)
        if item is None:
            item = QTreeWidgetItem([job, "queued", ""])
            item.setText(0, labels.get(job, job))
            item.setData(0, Qt.ItemDataRole.UserRole, job)
            self.jobs.addTopLevelItem(item)
            self._job_items[job] = item
        item.setText(1, status)
        item.setText(2, detail)
        if job == "handoff" and status == "complete" and self._session_folder:
            self.load_handoff(self._session_folder)
        elif job == "handoff_verify":
            if status == "complete":
                self.handoff_summary.setText(f"Listo y verificado · {detail}")
            elif status == "failed":
                self.handoff_summary.setText(f"El paquete cambió o está incompleto · {detail}")

    def load_handoff(self, root: Path) -> None:
        manifest_path = root / "handoff" / "handoff.json"
        instructions = root / "handoff.md"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._handoff_available = False
            self.handoff_summary.setText("Aún no preparado · pulsa ‘Preparar / actualizar’ al finalizar.")
        else:
            inputs = manifest.get("inputs", [])
            attachments = manifest.get("attachments", [])
            self._handoff_available = instructions.is_file()
            self.handoff_summary.setText(
                f"Preparado · {len(inputs)} entradas · {len(attachments)} adjuntos · "
                "nox-learn-anything → nox-html-learning"
            )
        completed = self.current_status == "completed"
        self.verify_handoff.setEnabled(completed and self._handoff_available)
        self.copy_handoff.setEnabled(self._handoff_available)
        self.open_handoff.setEnabled(self._handoff_available)

    def _retry_selected_job(self) -> None:
        item = self.jobs.currentItem()
        if item is None:
            return
        job = item.data(0, Qt.ItemDataRole.UserRole)
        if job:
            self.processing_job_requested.emit(str(job))

    def set_clean_document(self, markdown: str) -> None:
        self.clean.setPlainText(markdown)
        self.clean_preview.setMarkdown(markdown)

    def set_speakers(self, revision: dict | None) -> None:
        self.speakers.clear()
        self._speaker_fields.clear()
        if not revision or not revision.get("speakers"):
            self.speaker_revision.setText(
                "Todavía no hay hablantes. Finaliza el procesamiento o instala diarización para separar voces."
            )
            self.save_speakers.setEnabled(False)
            self.tabs.setTabText(self.speakers_tab_index, "Hablantes")
            return
        self.speaker_revision.setText(
            f"Revisión {revision.get('revision', 1)} · editar nombres no modifica el literal ni el audio."
        )
        for speaker in revision["speakers"]:
            speaker_id = speaker.get("speaker_id", "")
            item = QTreeWidgetItem([speaker_id, ""])
            self.speakers.addTopLevelItem(item)
            field = QLineEdit(speaker.get("display_name", speaker_id))
            field.setAccessibleName(f"Nombre visible para {speaker_id}")
            self.speakers.setItemWidget(item, 1, field)
            self._speaker_fields[speaker_id] = field
        self.save_speakers.setEnabled(True)
        self.tabs.setTabText(self.speakers_tab_index, f"Hablantes ({len(self._speaker_fields)})")

    def _save_speaker_names(self) -> None:
        self.speaker_names_save_requested.emit(
            {speaker_id: field.text() for speaker_id, field in self._speaker_fields.items()}
        )

    def _toggle_clean_editor(self) -> None:
        editing = self.clean.isVisible()
        self.clean.setVisible(not editing)
        self.clean_preview.setVisible(editing)
        self.save_clean.setVisible(not editing)
        self.edit_clean.setText("Ver documento" if not editing else "Editar Markdown")
        if editing:
            self.clean_preview.setMarkdown(self.clean.toPlainText())

    def _set_source(self, pill: StatusPill, label: str, health: dict | None, expected: bool) -> None:
        if not expected:
            pill.setText(f"{label} · no usado")
            pill.set_status("neutral")
        elif not health:
            pill.setText(f"{label} · conectando")
            pill.set_status("warning")
        elif health.get("status") == "active":
            pill.setText(f"{label} · activo")
            pill.set_status("active")
        else:
            pill.setText(f"{label} · {health.get('status')}")
            pill.setToolTip(health.get("detail", ""))
            pill.set_status("error" if health.get("fatal") or health.get("status") == "unavailable" else "warning")

    def _copy_visible(self) -> None:
        current = self.tabs.currentWidget()
        if isinstance(current, QTextEdit):
            QApplication.clipboard().setText(current.toPlainText())


class MainWindow(QMainWindow):
    def __init__(self, controller: AppController):
        super().__init__()
        self.controller = controller
        self.settings = QSettings("WhisperKey", "WhisperKey")
        self.setWindowTitle("WhisperKey")
        self.setMinimumSize(980, 680)
        self.resize(1320, 820)
        root = QWidget()
        root.setObjectName("AppRoot")
        self.setCentralWidget(root)
        root_layout = QHBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)
        self.navigation = self._build_navigation()
        root_layout.addWidget(self.navigation)
        self.pages = QStackedWidget()
        self.home = HomePage()
        self.library = LibraryPage(controller)
        self.dictation = DictationPage()
        self.models = ModelsPage(controller)
        self.settings_page = SettingsPage(controller)
        self.acceptance = AcceptancePage()
        self.session = SessionPage()
        for page in (
            self.home,
            self.library,
            self.dictation,
            self.models,
            self.settings_page,
            self.acceptance,
            self.session,
        ):
            self.pages.addWidget(page)
        root_layout.addWidget(self.pages, 1)
        self.mini = RecordingMiniController()
        self._region_capture_active = False
        self._wire_actions()
        self._setup_shortcuts()
        self._poll = QTimer(self)
        self._poll.setInterval(500)
        self._poll.timeout.connect(self._poll_state)
        self._poll.start()
        self._foreground_poll = QTimer(self)
        self._foreground_poll.setInterval(250)
        self._foreground_poll.timeout.connect(self._remember_foreground_window)
        self._foreground_poll.start()
        self.statusBar().showMessage("WhisperKey inicia sin enviar tu audio a la nube")
        self._offered_recoveries: set[str] = set()

    def _build_navigation(self) -> QWidget:
        rail = QWidget()
        rail.setObjectName("NavigationRail")
        rail.setFixedWidth(224)
        layout = QVBoxLayout(rail)
        layout.setContentsMargins(14, 18, 14, 14)
        layout.setSpacing(6)
        brand_row = QHBoxLayout()
        brand_mark = PixelMicActivity()
        brand_mark.setFixedSize(42, 34)
        brand = QLabel("WhisperKey")
        brand.setObjectName("Brand")
        brand_row.addWidget(brand_mark)
        brand_row.addWidget(brand)
        brand_row.addStretch()
        layout.addLayout(brand_row)
        layout.addSpacing(14)
        self.nav_group = QButtonGroup(self)
        self.nav_group.setExclusive(True)
        items = [
            ("Inicio", 0),
            ("Sesiones", 1),
            ("Dictado", 2),
            ("Modelos", 3),
            ("Ajustes", 4),
            ("Comprobar", 5),
        ]
        self.nav_buttons = []
        for label, index in items:
            button = QPushButton(label)
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, page=index: self.pages.setCurrentIndex(page))
            self.nav_group.addButton(button, index)
            self.nav_buttons.append(button)
            layout.addWidget(button)
        self.nav_buttons[0].setChecked(True)
        layout.addStretch()
        privacy = QLabel("LOCAL FIRST\nAudio completo · raw intacto")
        privacy.setObjectName("Muted")
        layout.addWidget(privacy)
        return rail

    def _wire_actions(self) -> None:
        self.home.mode_requested.connect(self._start_mode)
        self.home.audio_import_requested.connect(self._start_audio_import)
        self.home.session_requested.connect(self.controller.open_session)
        self.library.session_requested.connect(self.controller.open_session)
        self.dictation.toggle_requested.connect(self.controller.toggle_dictation)
        self.dictation.copy_requested.connect(self.controller.copy_dictation_text)
        self.session.pause_requested.connect(self.controller.pause_or_resume)
        self.session.marker_requested.connect(self.controller.add_marker)
        self.session.spoken_marker_requested.connect(self.controller.arm_spoken_note)
        self.session.finalize_requested.connect(self._finalize_from_ui)
        self.session.open_folder_requested.connect(self._open_active_folder)
        self.session.snapshot_requested.connect(self._request_snapshot)
        self.session.clean_save_requested.connect(self.controller.save_clean_revision)
        self.session.speaker_names_save_requested.connect(self.controller.save_speaker_names)
        self.session.retention_preview_requested.connect(lambda: self.controller.preview_retention(False))
        self.session.retention_restore_requested.connect(self._request_retention_restore)
        self.session.processing_job_requested.connect(self.controller.run_processing_job)
        self.session.open_handoff_requested.connect(self._open_handoff)
        self.session.copy_handoff_requested.connect(self._copy_handoff)
        self.settings_page.theme_requested.connect(self.apply_theme)
        self.settings_page.hotkeys_save_requested.connect(self.controller.save_hotkeys)
        self.settings_page.retention_save_requested.connect(self.controller.save_retention_settings)
        self.settings_page.audio_routes_save_requested.connect(self.controller.save_audio_routes)
        self.acceptance.benchmark_requested.connect(self.controller.toggle_acceptance_dictation)
        self.acceptance.result_requested.connect(self.controller.record_acceptance_result)
        self.acceptance.export_requested.connect(self.controller.export_acceptance_report)
        self.controller.model_state_changed.connect(self._on_model_state)
        self.controller.session_state_changed.connect(self._on_session_state)
        self.controller.transcript_changed.connect(self.session.set_transcript)
        self.controller.provisional_changed.connect(self.session.set_provisional)
        self.controller.markers_changed.connect(self.session.set_markers)
        self.controller.media_changed.connect(self.session.set_media)
        self.controller.library_changed.connect(self.home.set_sessions)
        self.controller.search_results_changed.connect(self.library.set_sessions)
        self.controller.recoveries_changed.connect(self._offer_recovery)
        self.controller.audio_diagnostics_changed.connect(self.settings_page.set_audio_diagnostics)
        self.controller.dictation_state_changed.connect(self._on_dictation_state)
        self.controller.dictation_state_changed.connect(self.acceptance.set_dictation_state)
        self.controller.dictation_history_changed.connect(self.dictation.set_history)
        self.controller.processing_job_changed.connect(self._on_processing_job)
        self.controller.diarization_state_changed.connect(self.models.set_diarization_state)
        self.controller.models_catalog_changed.connect(self.models.set_model_catalog)
        self.controller.model_inspection_changed.connect(self.models.set_model_inspection)
        self.controller.hotkeys_config_changed.connect(self.settings_page.set_hotkeys)
        self.controller.retention_config_changed.connect(self.settings_page.set_retention)
        self.controller.audio_routes_changed.connect(self.settings_page.set_audio_routes)
        self.controller.acceptance_changed.connect(self.acceptance.set_report)
        self.controller.settings_status_changed.connect(self.settings_page.set_settings_status)
        self.controller.error_raised.connect(self._show_error)
        self.controller.operation_finished.connect(self._operation_finished)
        self.controller.audio_import_state_changed.connect(self._on_audio_import_state)
        self.mini.show_main_requested.connect(self.show_and_raise)
        self.mini.pause_requested.connect(self.controller.pause_or_resume)
        self.mini.marker_requested.connect(lambda: self.controller.add_marker("important", "Mini control"))
        self.mini.region_capture_requested.connect(self._request_region_snapshot_from_mini)
        self.mini.finish_requested.connect(lambda: self._finalize_from_ui(self.session.title.text()))
        self.mini.dictation_stop_requested.connect(self.controller.toggle_dictation)
        self.controller.refresh_diarization_state()
        self.controller.refresh_acceptance()

    def _setup_shortcuts(self) -> None:
        QShortcut(
            QKeySequence("Ctrl+Shift+M"),
            self,
            activated=lambda: self.controller.add_marker("important", "Atajo"),
        )
        QShortcut(QKeySequence("Ctrl+Shift+Space"), self, activated=self.controller.pause_or_resume)
        QShortcut(QKeySequence("Ctrl+L"), self, activated=lambda: self.pages.setCurrentWidget(self.library))

    def _start_mode(self, mode: str) -> None:
        if mode == "dictation":
            self.pages.setCurrentWidget(self.dictation)
            self.nav_buttons[2].setChecked(True)
            self.controller.toggle_dictation()
            return
        self.pages.setCurrentWidget(self.session)
        for button in self.nav_buttons:
            button.setChecked(False)
        self.session.title.clear()
        self.session.live.clear()
        self.session.literal.clear()
        self.session.clean.clear()
        self.session.clean_preview.clear()
        self.controller.start_mode(mode)

    def _start_audio_import(self, path: str) -> None:
        self.statusBar().showMessage(f"Preparando {Path(path).name}")
        self.controller.import_audio_file(path)

    def _on_audio_import_state(self, state: str, detail: str, percent: int) -> None:
        self.home.set_import_state(state, detail, percent)
        suffix = f" · {percent}%" if percent else ""
        self.statusBar().showMessage(f"{detail}{suffix}")

    def _on_model_state(self, state: str, message: str) -> None:
        self.home.set_model_state(state, message)
        self.models.set_model_state(state, message)
        self.statusBar().showMessage(message)

    def _on_session_state(self, state: dict) -> None:
        self.session.update_state(state)
        if self.controller.dictation and self.controller.dictation.is_recording:
            return
        status = state.get("status")
        if self._region_capture_active:
            self.mini.hide()
            return
        if status in {"recording", "paused"}:
            self.mini.show_recording(state)
        elif self.mini.isVisible():
            self.mini.hide()

    def _on_dictation_state(self, state: str, message: str, text: str) -> None:
        self.dictation.update_state(state, message, text)
        if state == "recording":
            self.mini.show_dictation(self.controller.dictation_elapsed_ms)
        elif state == "processing":
            self.mini.show_dictation(self.controller.dictation_elapsed_ms, processing=True)
        elif self.mini.isVisible():
            self.mini.hide()

    def _operation_finished(self, operation: str, result) -> None:
        if operation == "initialize" and result:
            self.controller.install_hotkeys()
        elif operation in {"start", "continue"} and result:
            self.pages.setCurrentWidget(self.session)
        elif operation == "open_session" and result:
            self.session.prepare_for_open()
            self.session.load_documents(str(result))
            self.pages.setCurrentWidget(self.session)
            for button in self.nav_buttons:
                button.setChecked(False)
            self.statusBar().showMessage("Sesión abierta en el editor", 6000)
        elif operation == "finalize" and result:
            self.pages.setCurrentWidget(self.session)
            self.session.show_final_document(str(result))
            self.statusBar().showMessage(f"Sesión guardada en {result}", 10000)
        elif operation == "audio_import" and result:
            self.session.prepare_for_open()
            self.session.load_documents(result["folder"])
            self.pages.setCurrentWidget(self.session)
            for button in self.nav_buttons:
                button.setChecked(False)
            self.home.set_import_state("complete", "Audio importado y transcrito", 100)
            self.statusBar().showMessage(
                f"Audio transcrito · {result['transcript_segments']} fragmentos · sesión guardada",
                10000,
            )
        elif operation == "finish_stage":
            self._show_naming_gate()
        elif operation == "snapshot" and result:
            self.session.show_capture_feedback(result)
            self.statusBar().showMessage(f"Captura guardada · {result['relative_path']}", 7000)
        elif operation == "diagnostics_bundle" and result:
            path = Path(result["path"])
            open_file(str(path.parent))
            self.statusBar().showMessage(
                f"Diagnóstico privado creado · {path.name} · {result['sha256'][:12]}",
                10000,
            )
        elif operation == "spoken_note_armed":
            self.statusBar().showMessage(
                "Marcador guardado · la próxima frase MIC será la nota hablada",
                9000,
            )
        elif operation == "retention_preview" and result:
            self._show_retention_preview(result)
        elif operation == "retention_apply" and result:
            moved = result.get("moved", 0)
            self.statusBar().showMessage(
                f"Retención aplicada · {moved} archivo(s) en papelera recuperable",
                10000,
            )
            QMessageBox.information(
                self,
                "Audio protegido",
                f"Se movieron {moved} archivo(s). Puedes restaurarlos desde esta misma sesión.",
            )
        elif operation == "retention_restore" and result:
            restored = result.get("restored", 0)
            self.statusBar().showMessage(f"Audio restaurado · {restored} archivo(s)", 10000)
            QMessageBox.information(
                self,
                "Restauración terminada",
                f"Se restauraron {restored} archivo(s) de audio.",
            )
        elif operation == "dictation" and result:
            session = self.controller.service.session if self.controller.service else None
            if session and session.status.value == "paused":
                self.pages.setCurrentWidget(self.session)
            self.statusBar().showMessage("Dictado entregado", 5000)
        elif operation == "acceptance_export" and result:
            folder = str(Path(result["markdown"]).parent)
            open_file(folder)
            self.statusBar().showMessage(f"Informe de comprobación exportado · {folder}", 10000)
        elif operation == "clean_saved" and result:
            self.session.set_clean_document(self.session.clean.toPlainText())
            self.session.clean.setVisible(False)
            self.session.clean_preview.setVisible(True)
            self.session.save_clean.setVisible(False)
            self.session.edit_clean.setText("Editar Markdown")
            self.statusBar().showMessage(f"Revisión limpia guardada · {result}", 6000)
        elif operation == "speakers_saved" and result:
            self.session.set_speakers(result["revision"])
            self.session.load_documents(result["folder"])
            self.statusBar().showMessage(
                f"Nombres guardados · revisión {result['revision'].get('revision')}",
                7000,
            )
        elif operation == "processing_job" and result:
            self.session.load_documents(result["folder"])
            statuses = result.get("statuses", {})
            failed = [job for job, status in statuses.items() if status == "failed"]
            message = f"Trabajo con error · {', '.join(failed)}" if failed else f"Trabajo actualizado · {result['job']}"
            self.statusBar().showMessage(message, 7000)

    def _on_processing_job(self, job: str, status: str, detail: str) -> None:
        self.session.update_processing_job(job, status, detail)
        if job == "clean" and status == "complete" and self.controller.service and self.controller.service.folder:
            path = self.controller.service.folder / "transcript.clean.md"
            if path.exists():
                self.session.set_clean_document(path.read_text(encoding="utf-8"))

    def _show_retention_preview(self, preview: dict) -> None:
        if preview.get("requires_verification") and not preview.get("verified"):
            answer = QMessageBox.question(
                self,
                "Verificar antes de retirar audio",
                "Esta política exige confirmar que revisaste el documento Literal. "
                "¿Ya comprobaste que la transcripción necesaria está guardada?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self.controller.preview_retention(True)
            return
        if preview.get("blocked_reason"):
            QMessageBox.warning(self, "Retención detenida", preview["blocked_reason"])
            return
        candidates = preview.get("candidates", [])
        if not candidates:
            QMessageBox.information(self, "Retención", preview.get("message", "No hay cambios."))
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Vista previa exacta de retención")
        box.setText(f"{len(candidates)} archivo(s) · {self._format_bytes(preview.get('total_bytes', 0))}")
        box.setInformativeText(
            "Estos archivos se moverán fuera de la sesión a la papelera recuperable de WhisperKey. "
            "El literal, los documentos y los extractos de marcadores no se modificarán."
        )
        box.setDetailedText(
            "\n".join(
                f"{item['relative_path']}  ·  {self._format_bytes(item['bytes'])}  ·  sha256 {item['sha256']}"
                for item in candidates
            )
        )
        apply_button = box.addButton("Mover a papelera recuperable", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() == apply_button:
            self.controller.apply_retention(
                preview["preview_id"],
                bool(preview.get("verified", False)),
            )

    def _request_retention_restore(self) -> None:
        answer = QMessageBox.question(
            self,
            "Restaurar audio",
            "WhisperKey restaurará la aplicación de retención más reciente sin sobrescribir archivos. ¿Continuar?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.controller.restore_retention()

    @staticmethod
    def _format_bytes(value: int) -> str:
        amount = float(max(0, value))
        for unit in ("B", "KiB", "MiB", "GiB"):
            if amount < 1024 or unit == "GiB":
                return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
            amount /= 1024
        return f"{amount:.1f} GiB"

    def apply_theme(self, theme: str) -> None:
        QApplication.instance().setStyleSheet(build_stylesheet(theme))
        self.settings.setValue("appearance/theme", theme)

    def show_and_raise(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _open_active_folder(self) -> None:
        if self.controller.service and self.controller.service.folder:
            from whisper_key.utils import open_file

            open_file(str(self.controller.service.folder))

    def _open_handoff(self) -> None:
        service = self.controller.service
        path = service.folder / "handoff.md" if service and service.folder else None
        if not path or not path.is_file():
            self._show_error("Handoff no preparado", "Pulsa ‘Preparar / actualizar’ antes de abrirlo.")
            return
        from whisper_key.utils import open_file

        open_file(str(path))

    def _copy_handoff(self) -> None:
        service = self.controller.service
        path = service.folder / "handoff.md" if service and service.folder else None
        if not path or not path.is_file():
            self._show_error("Handoff no preparado", "Pulsa ‘Preparar / actualizar’ antes de copiarlo.")
            return
        QApplication.clipboard().setText(path.read_text(encoding="utf-8"))
        self.statusBar().showMessage("Instrucciones de handoff copiadas", 6000)

    def _show_error(self, title: str, detail: str) -> None:
        QMessageBox.critical(self, title, detail)

    def _request_snapshot(self, capture_kind: str) -> None:
        if self.session.current_status not in {"recording", "paused"}:
            self._show_error("No hay captura activa", "Abre o continúa una sesión antes de adjuntar una pantalla.")
            return
        self.hide()
        if capture_kind == "full":
            QTimer.singleShot(220, self._capture_full_screen)
        elif capture_kind == "window":
            QTimer.singleShot(220, self._capture_active_window)
        else:
            QTimer.singleShot(180, self._begin_region_capture)

    def _request_region_snapshot_from_mini(self) -> None:
        if self.session.current_status not in {"recording", "paused"}:
            return
        restore_main = self.isVisible() and not self.isMinimized()
        self._region_capture_active = True
        self.hide()
        self.mini.hide()
        QTimer.singleShot(
            180,
            lambda: self._begin_region_capture(restore_main=restore_main, restore_mini=True),
        )

    def _capture_active_window(self) -> None:
        try:
            import win32gui

            window_id = win32gui.GetForegroundWindow()
            if not window_id:
                raise RuntimeError("Windows no informó una ventana activa")
            image = DesktopCapture.capture_window(window_id)
        except Exception as exc:
            self.show_and_raise()
            self._show_error("No se pudo capturar la ventana", str(exc))
            return
        self.show_and_raise()
        self.controller.add_snapshot(image)

    def _capture_full_screen(self) -> None:
        try:
            image = DesktopCapture.capture_virtual_desktop().image
        except Exception as exc:
            self.show_and_raise()
            self._show_error("No se pudo capturar la pantalla", str(exc))
            return
        self.show_and_raise()
        self.controller.add_snapshot(image)

    def _poll_state(self) -> None:
        self.controller.publish_snapshot()
        elapsed = self.controller.dictation_elapsed_ms
        self.dictation.set_elapsed(elapsed)
        if self.controller.dictation and self.controller.dictation.is_recording:
            self.mini.show_dictation(elapsed)

    def _remember_foreground_window(self) -> None:
        try:
            import win32gui

            foreground = win32gui.GetForegroundWindow()
            owned = {int(self.winId()), int(self.mini.winId())}
            overlay = getattr(self, "_region_overlay", None)
            if overlay:
                owned.add(int(overlay.winId()))
            if foreground and foreground not in owned:
                self.controller.remember_external_window(foreground)
        except Exception:
            return

    def _begin_region_capture(self, restore_main: bool = True, restore_mini: bool = False) -> None:
        try:
            frame = DesktopCapture.capture_virtual_desktop()
        except Exception as exc:
            self._finish_region_capture(restore_main, restore_mini)
            if restore_main:
                self._show_error("No se pudo leer la pantalla", str(exc))
            return
        overlay = RegionCaptureOverlay(frame)
        self._region_overlay = overlay

        def selected(region) -> None:
            image = frame.crop(region)
            self._finish_region_capture(restore_main, restore_mini)
            self.controller.add_snapshot(image)

        overlay.region_selected.connect(selected)
        overlay.canceled.connect(lambda: self._finish_region_capture(restore_main, restore_mini))
        overlay.show()
        overlay.raise_()
        overlay.activateWindow()
        overlay.setFocus()

    def _finish_region_capture(self, restore_main: bool, restore_mini: bool) -> None:
        self._region_capture_active = False
        if restore_main:
            self.show_and_raise()
        if restore_mini:
            self.controller.publish_snapshot()

    def _finalize_from_ui(self, title: str) -> None:
        if title.strip():
            self.controller.finalize(title)
        elif self.session.current_status == "recoverable":
            self._show_naming_gate()
        else:
            self.controller.finish_stage()

    def _show_naming_gate(self) -> None:
        title, accepted = QInputDialog.getText(
            self,
            "Nombra la sesión",
            "El audio ya está seguro. Escribe un nombre significativo para archivarla:",
            text=self.session.title.text(),
        )
        if accepted and title.strip():
            self.session.title.setText(" ".join(title.split()))
            self.controller.finalize(title)
        else:
            self.statusBar().showMessage("Sesión guardada como pendiente · necesita un nombre", 10000)

    def _offer_recovery(self, sessions: list[dict]) -> None:
        candidate = next(
            (item for item in sessions if item.get("session_id") not in self._offered_recoveries),
            None,
        )
        if not candidate:
            return
        session_id = candidate["session_id"]
        self._offered_recoveries.add(session_id)
        title = candidate.get("title") or "Sesión sin nombre"
        answer = QMessageBox.question(
            self,
            "Hay una captura recuperable",
            f"“{title}” no terminó de forma normal. El audio guardado está seguro.\n\n"
            "¿Quieres continuarla como una nueva etapa ahora?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.pages.setCurrentWidget(self.session)
            for button in self.nav_buttons:
                button.setChecked(False)
            self.controller.continue_session(session_id)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        session = self.controller.service.session if self.controller.service else None
        if session and session.status.value in {"recording", "paused"}:
            self.hide()
            event.ignore()
            self.statusBar().showMessage("La captura sigue activa en el mini control")
            return
        event.accept()


def create_quit_action(window: MainWindow) -> QAction:
    action = QAction("Salir de WhisperKey", window)
    action.triggered.connect(QApplication.instance().quit)
    return action
