from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import Session


@dataclass
class SessionMeta:
    """Lightweight session descriptor returned by source discovery."""

    ref: Any          # opaque source-specific reference used by load()
    session_id: str
    project: str
    title: str | None = None
    timestamp: str | None = None
    sort_timestamp: str | None = None
    display_project: str | None = None

    def get_display_project(self) -> str:
        return self.display_project or self.project


class BaseSource(ABC):
    """Abstract base for session sources (Claude Code, Codex, …)."""

    @abstractmethod
    def discover(self) -> list[SessionMeta]:
        """Return metadata for all discoverable sessions, newest first."""
        ...

    @abstractmethod
    def load(self, meta: SessionMeta) -> Session:
        """Load a full session from its metadata descriptor."""
        ...

    @abstractmethod
    def load_file(self, path: Path) -> Session:
        """Load a session directly from a file path, bypassing discovery."""
        ...

    def find(self, sessions: list[SessionMeta], query: str) -> SessionMeta | None:
        """Find a session by 1-based index, UUID prefix, or title substring."""
        try:
            idx = int(query) - 1
            if 0 <= idx < len(sessions):
                return sessions[idx]
        except ValueError:
            pass
        for s in sessions:
            if s.session_id.startswith(query):
                return s
        for s in sessions:
            if s.title and query.lower() in s.title.lower():
                return s
        return None
