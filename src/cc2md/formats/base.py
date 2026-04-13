from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Session


class BaseFormat(ABC):
    """Abstract base for output formats (Markdown, HTML, …)."""

    @abstractmethod
    def render(self, session: Session) -> str:
        """Render a session to a string in this format."""
        ...

    @property
    @abstractmethod
    def file_extension(self) -> str:
        """Default file extension for this format, e.g. 'md' or 'html'."""
        ...
