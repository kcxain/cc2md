from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..models import Session


@dataclass
class RenderResult:
    """One or more files produced by rendering a session.

    Single-file sessions (no subagents) have one entry keyed ``index.{ext}``.
    Multi-file sessions have ``index.{ext}`` plus one file per subagent.
    """

    files: dict[str, str] = field(default_factory=dict)
    # key: relative filename  value: file content

    @property
    def is_single_file(self) -> bool:
        return len(self.files) == 1

    def single_content(self) -> str:
        """Return content for single-file results."""
        return next(iter(self.files.values()))


class BaseFormat(ABC):
    """Abstract base for output formats (Markdown, HTML, …)."""

    @abstractmethod
    def render(self, session: Session) -> RenderResult:
        """Render a session to one or more files."""
        ...

    @property
    @abstractmethod
    def file_extension(self) -> str:
        """Default file extension for this format, e.g. 'md' or 'html'."""
        ...
