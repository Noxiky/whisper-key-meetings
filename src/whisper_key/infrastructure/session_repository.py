import json
import os
import re
import unicodedata
from pathlib import Path
from uuid import uuid4

from whisper_key.domain.session import Session


class SessionJournalError(RuntimeError):
    pass


SESSION_ID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
MAX_SESSION_JSON_BYTES = 2 * 1024 * 1024
MAX_EVENT_LINE_BYTES = 4 * 1024 * 1024
SESSION_MODES = {"dictation", "meeting", "learning", "reading", "idea"}
SESSION_STATUSES = {
    "draft",
    "preparing",
    "recording",
    "paused",
    "recoverable",
    "processing",
    "completed",
    "error",
}


def is_valid_session_id(value: object) -> bool:
    return isinstance(value, str) and bool(SESSION_ID_PATTERN.fullmatch(value))


def read_session_metadata(path: Path, allowed_root: Path) -> dict:
    """Read bounded, display-safe metadata from a portable session folder."""
    path = Path(path)
    root = Path(allowed_root).resolve()
    resolved = path.resolve()
    if root not in resolved.parents:
        raise ValueError("Session metadata escapes the library")
    if path.stat().st_size > MAX_SESSION_JSON_BYTES:
        raise ValueError("Session metadata is too large")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not is_valid_session_id(value.get("session_id")):
        raise ValueError("Session metadata has an invalid identifier")
    if value.get("mode") not in SESSION_MODES or value.get("status") not in SESSION_STATUSES:
        raise ValueError("Session metadata has an invalid mode or status")
    title = value.get("title")
    if title is not None and (not isinstance(title, str) or len(title) > 200):
        raise ValueError("Session metadata has an invalid title")
    for field in ("created_at", "updated_at"):
        item = value.get(field)
        if item is not None and (not isinstance(item, str) or len(item) > 64):
            raise ValueError(f"Session metadata has an invalid {field}")
    duration = value.get("captured_duration_ms", 0)
    if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
        raise ValueError("Session metadata has an invalid duration")
    return value


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class SessionRepository:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.inbox = self.root / "inbox"
        self.sessions = self.root / "sessions"
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.sessions.mkdir(parents=True, exist_ok=True)

    def create_folder(self, session_id: str) -> Path:
        if not is_valid_session_id(session_id):
            raise ValueError("Invalid session identifier")
        folder = self.inbox / session_id
        folder.mkdir(parents=True, exist_ok=False)
        for relative in ("attachments", "audio/mic", "audio/sys", "audio/imported", "audio/excerpts", "exports"):
            (folder / relative).mkdir(parents=True, exist_ok=True)
        return folder

    def save_session(self, folder: Path, session: Session) -> None:
        folder = self._require_session_folder(folder)
        content = json.dumps(session.to_dict(), ensure_ascii=False, indent=2) + "\n"
        atomic_write_text(folder / "session.json", content)

    def load_session(self, folder: Path) -> Session:
        folder = self._require_session_folder(folder)
        value = read_session_metadata(folder / "session.json", folder)
        return Session.from_dict(value)

    def append_event(self, folder: Path, event: dict) -> None:
        folder = self._require_session_folder(folder)
        timeline = folder / "timeline.jsonl"
        encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        with timeline.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())

    def read_events(self, folder: Path, tolerate_truncated_last: bool = True) -> list[dict]:
        folder = self._require_session_folder(folder)
        timeline = folder / "timeline.jsonl"
        if not timeline.exists():
            return []
        events = []
        with timeline.open("rb") as handle:
            line_number = 0
            line = handle.readline(MAX_EVENT_LINE_BYTES + 1)
            while line:
                line_number += 1
                if len(line) > MAX_EVENT_LINE_BYTES and not line.endswith(b"\n"):
                    raise SessionJournalError(f"Timeline record {line_number} is too large")
                following = handle.readline(MAX_EVENT_LINE_BYTES + 1)
                if line.strip():
                    try:
                        events.append(json.loads(line.decode("utf-8")))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        if tolerate_truncated_last and not following:
                            break
                        raise SessionJournalError(f"Invalid timeline record {line_number}") from exc
                line = following
        return events

    def write_projection(self, folder: Path, relative_path: str, content: str) -> Path:
        folder = self._require_session_folder(folder)
        destination = (folder / relative_path).resolve()
        root = folder.resolve()
        if destination != root and root not in destination.parents:
            raise ValueError("Projection path escapes session folder")
        atomic_write_text(destination, content)
        return destination

    def find_folder(self, session_id: str) -> Path:
        if not is_valid_session_id(session_id):
            raise FileNotFoundError(session_id)
        direct = self.inbox / session_id
        if direct.is_dir():
            return direct
        for session_file in self.sessions.rglob("session.json"):
            try:
                if read_session_metadata(session_file, self.sessions).get("session_id") == session_id:
                    return session_file.parent
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                continue
        raise FileNotFoundError(session_id)

    def promote(self, folder: Path, session: Session) -> Path:
        folder = self._require_session_folder(folder)
        created_date = session.created_at[:10]
        year, month, _day = created_date.split("-")
        normalized = unicodedata.normalize("NFKD", session.title or "untitled").encode("ascii", "ignore").decode()
        slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")[:80] or "untitled"
        destination = self.sessions / year / month / f"{created_date}-{slug}-{session.session_id[:8]}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            stored = self.load_session(destination)
            if stored.session_id == session.session_id:
                return destination
            raise FileExistsError(destination)
        os.replace(folder, destination)
        return destination

    def _require_session_folder(self, folder: Path) -> Path:
        resolved = Path(folder).resolve()
        inbox = self.inbox.resolve()
        sessions = self.sessions.resolve()
        if resolved == inbox or resolved == sessions:
            raise ValueError("A session operation requires a child folder")
        if inbox not in resolved.parents and sessions not in resolved.parents:
            raise ValueError("Session folder escapes the library")
        return resolved
