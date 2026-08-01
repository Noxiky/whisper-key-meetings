from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

TOKEN_REFERENCE = re.compile(r"^\{([a-zA-Z0-9_.-]+)\}$")


class TokenStore:
    """Resolve DTCG-style WhisperKey tokens for a selected visual theme."""

    def __init__(self, path: Path | None = None, theme: str = "dark"):
        if theme not in {"dark", "light"}:
            raise ValueError("Theme must be 'dark' or 'light'")
        self.path = path or Path(__file__).with_name("whisperkey.tokens.json")
        self.data = json.loads(self.path.read_text(encoding="utf-8"))
        self.theme = theme

    def get(self, path: str) -> Any:
        return self._resolve_path(path, set())

    def _resolve_path(self, path: str, seen: set[str]) -> Any:
        if path in seen:
            raise ValueError(f"Circular token reference: {path}")
        current: Any = self.data
        try:
            for part in path.split("."):
                current = current[part]
        except (KeyError, TypeError) as exc:
            raise KeyError(f"Unknown design token: {path}") from exc
        if isinstance(current, dict) and "$value" in current:
            current = current["$value"]
        return self._resolve_value(current, seen | {path})

    def _resolve_value(self, value: Any, seen: set[str]) -> Any:
        if isinstance(value, str):
            match = TOKEN_REFERENCE.match(value)
            return self._resolve_path(match.group(1), seen) if match else value
        if isinstance(value, dict):
            if self.theme in value and set(value).issubset({"light", "dark"}):
                return self._resolve_value(value[self.theme], seen)
            return {key: self._resolve_value(item, seen) for key, item in value.items()}
        if isinstance(value, list):
            return [self._resolve_value(item, seen) for item in value]
        return value


def build_stylesheet(theme: str = "dark") -> str:
    t = TokenStore(theme=theme)
    canvas = t.get("semantic.color.surface.canvas")
    base = t.get("semantic.color.surface.base")
    raised = t.get("semantic.color.surface.raised")
    sunken = t.get("semantic.color.surface.sunken")
    selected = t.get("semantic.color.surface.selected")
    text = t.get("semantic.color.text.primary")
    secondary = t.get("semantic.color.text.secondary")
    muted = t.get("semantic.color.text.muted")
    accent_text = t.get("semantic.color.text.accent")
    border = t.get("semantic.color.border.default")
    border_subtle = t.get("semantic.color.border.subtle")
    focus = t.get("semantic.color.border.focus")
    primary = t.get("semantic.color.action.primary")
    primary_hover = t.get("semantic.color.action.primaryHover")
    primary_pressed = t.get("semantic.color.action.primaryPressed")
    disabled = t.get("semantic.color.action.disabled")
    recording = t.get("semantic.color.status.recording")
    processing = t.get("semantic.color.status.processing")
    danger = t.get("semantic.color.status.danger")
    radius = t.get("semantic.radius.control")
    panel_radius = t.get("semantic.radius.panel")
    font = t.get("primitive.font.family.sans")
    mono = t.get("primitive.font.family.mono")

    return f"""
    * {{
        font-family: '{font}';
        font-size: 14px;
        color: {text};
        outline: none;
    }}
    QMainWindow, QDialog, QWidget#AppRoot {{ background: {canvas}; }}
    QWidget#NavigationRail {{ background: {sunken}; border-right: 1px solid {border_subtle}; }}
    QWidget#Page, QWidget#SessionSurface {{ background: {canvas}; }}
    QFrame#Card, QFrame#Panel {{
        background: {raised};
        border: 1px solid {border_subtle};
        border-radius: {panel_radius};
    }}
    QLabel#Brand {{ font-size: 17px; font-weight: 700; }}
    QLabel#Eyebrow {{ color: {accent_text}; font-size: 11px; font-weight: 700; }}
    QLabel#PageTitle {{ font-size: 28px; font-weight: 700; }}
    QLabel#SectionTitle {{ font-size: 17px; font-weight: 600; }}
    QLabel#Muted, QLabel[muted="true"] {{ color: {muted}; }}
    QLabel#Timer {{ font-family: '{mono}'; font-size: 17px; font-weight: 600; }}
    QLabel#RecordingStatus {{ color: {recording}; font-weight: 700; }}
    QLabel#ProcessingStatus {{ color: {processing}; font-weight: 700; }}
    QLabel#DangerStatus {{ color: {danger}; font-weight: 700; }}
    QLabel#SuccessBanner {{
        color: {recording};
        background: {selected};
        border: 1px solid {recording};
        border-radius: {radius};
        padding: 9px 12px;
        font-weight: 600;
    }}
    QLabel#ErrorText {{ color: {danger}; }}
    QLabel#Pill {{
        padding: 4px 9px;
        border-radius: 10px;
        background: {border_subtle};
        color: {secondary};
        font-size: 12px;
        font-weight: 600;
    }}
    QLabel#Pill[status="active"] {{ color: {recording}; border: 1px solid {recording}; }}
    QLabel#Pill[status="warning"] {{ color: {processing}; border: 1px solid {processing}; }}
    QLabel#Pill[status="error"] {{ color: {danger}; border: 1px solid {danger}; }}
    QPushButton {{
        min-height: 38px;
        padding: 0 13px;
        border-radius: {radius};
        background: transparent;
        border: 1px solid transparent;
        font-weight: 600;
    }}
    QPushButton:hover {{ background: {selected}; }}
    QPushButton:pressed {{ background: {border_subtle}; }}
    QPushButton:focus {{ border: 1px solid {focus}; }}
    QPushButton:disabled {{ color: {muted}; background: {disabled}; }}
    QPushButton[primary="true"] {{ background: {primary}; color: {t.get("semantic.color.text.inverse")}; }}
    QPushButton[primary="true"]:hover {{ background: {primary_hover}; }}
    QPushButton[primary="true"]:pressed {{ background: {primary_pressed}; }}
    QPushButton[danger="true"] {{ color: {danger}; border: 1px solid {danger}; }}
    QPushButton#NavButton {{
        text-align: left;
        padding-left: 14px;
        color: {secondary};
    }}
    QPushButton#NavButton:checked {{ background: {selected}; color: {accent_text}; }}
    QPushButton#ModeCard {{
        min-height: 94px;
        text-align: left;
        padding: 16px;
        background: {raised};
        border: 1px solid {border_subtle};
        border-radius: {panel_radius};
        font-size: 16px;
    }}
    QPushButton#ModeCard:hover {{ border-color: {focus}; background: {selected}; }}
    QLineEdit, QTextEdit, QPlainTextEdit, QComboBox {{
        background: {base};
        border: 1px solid {border};
        border-radius: {radius};
        padding: 8px 10px;
        selection-background-color: {primary};
    }}
    QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus {{ border-color: {focus}; }}
    QLineEdit#SessionTitle {{
        background: transparent;
        border: 1px solid transparent;
        font-size: 22px;
        font-weight: 600;
        padding-left: 4px;
    }}
    QTabWidget::pane {{ border: 0; background: {canvas}; }}
    QTabBar::tab {{
        color: {secondary};
        min-height: 36px;
        padding: 0 14px;
        border-bottom: 2px solid transparent;
    }}
    QTabBar::tab:selected {{ color: {text}; border-bottom-color: {primary}; }}
    QTabBar::tab:hover {{ color: {text}; background: {selected}; }}
    QListWidget, QTreeWidget {{ background: transparent; border: 0; }}
    QListWidget::item, QTreeWidget::item {{ padding: 9px; border-radius: {radius}; }}
    QListWidget::item:selected, QTreeWidget::item:selected {{ background: {selected}; color: {text}; }}
    QHeaderView::section {{
        background: {sunken};
        color: {secondary};
        border: 0;
        border-bottom: 1px solid {border};
        padding: 7px 9px;
        font-size: 12px;
        font-weight: 600;
    }}
    QScrollBar:vertical {{ width: 10px; background: transparent; }}
    QScrollBar::handle:vertical {{ background: {border}; min-height: 32px; border-radius: 5px; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
    QToolTip {{ background: {raised}; color: {text}; border: 1px solid {border}; padding: 5px; }}
    QStatusBar {{ background: {sunken}; border-top: 1px solid {border_subtle}; color: {secondary}; }}
    QWidget#MiniController {{ background: {raised}; border: 1px solid {border}; border-radius: {panel_radius}; }}
    """
