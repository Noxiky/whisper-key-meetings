from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from .session_repository import read_session_metadata


class SessionIndex:
    """Rebuildable SQLite search cache; session folders remain the source of truth."""

    def __init__(self, library_root: Path):
        self.library_root = Path(library_root)
        self.path = self.library_root / ".index" / "whisperkey.sqlite3"

    def rebuild(self) -> int:
        evidence = self._read_evidence()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            self._create_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM sessions")
            connection.executemany(
                """
                INSERT INTO sessions (
                    session_id, title, mode, status, created_at, updated_at,
                    captured_duration_ms, folder, transcript
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item["session_id"],
                        item.get("title"),
                        item.get("mode", ""),
                        item.get("status", ""),
                        item.get("created_at", ""),
                        item.get("updated_at", item.get("created_at", "")),
                        item.get("captured_duration_ms", 0),
                        item["folder"],
                        item.get("transcript", ""),
                    )
                    for item in evidence
                ],
            )
            connection.commit()
        return len(evidence)

    def search(self, query: str = "", limit: int = 200) -> list[dict[str, Any]]:
        if limit < 1 or limit > 1000:
            raise ValueError("Search limit must be between 1 and 1000")
        if not self.path.exists():
            self.rebuild()
        normalized = " ".join(query.split()).strip()
        parameters: list[Any] = []
        where = ""
        if normalized:
            escaped = normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            where = "WHERE title LIKE ? ESCAPE '\\' OR transcript LIKE ? ESCAPE '\\' OR mode LIKE ? ESCAPE '\\'"
            parameters.extend([pattern, pattern, pattern])
        parameters.append(limit)
        with closing(self._connect()) as connection:
            self._create_schema(connection)
            rows = connection.execute(
                f"""
                SELECT session_id, title, mode, status, created_at, updated_at,
                       captured_duration_ms, folder
                FROM sessions
                {where}
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [dict(row) for row in rows]

    def _read_evidence(self) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        for base in (self.library_root / "inbox", self.library_root / "sessions"):
            if not base.exists():
                continue
            for session_path in base.rglob("session.json"):
                try:
                    value = read_session_metadata(session_path, base)
                    value["folder"] = str(session_path.parent)
                    literal = session_path.parent / "transcript.raw.md"
                    value["transcript"] = self._read_index_text(literal)
                    found.append(value)
                except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                    continue
        return found

    @staticmethod
    def _read_index_text(path: Path, limit_bytes: int = 8 * 1024 * 1024) -> str:
        if not path.is_file():
            return ""
        with path.open("rb") as handle:
            content = handle.read(limit_bytes + 1)
        truncated = len(content) > limit_bytes
        text = content[:limit_bytes].decode("utf-8", errors="replace")
        return text + ("\n[index truncated for safety]" if truncated else "")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        return connection

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                title TEXT,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                captured_duration_ms INTEGER NOT NULL,
                folder TEXT NOT NULL,
                transcript TEXT NOT NULL
            )
            """
        )
        connection.execute("CREATE INDEX IF NOT EXISTS sessions_updated_idx ON sessions(updated_at DESC)")
