from __future__ import annotations

from dataclasses import dataclass, field
from typing import Union


@dataclass
class TextBlock:
    text: str


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict


@dataclass
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False


@dataclass
class ImageBlock:
    pass


ContentBlock = Union[TextBlock, ToolUseBlock, ToolResultBlock, ImageBlock]


@dataclass
class Message:
    role: str  # "user" | "assistant"
    blocks: list[ContentBlock] = field(default_factory=list)
    timestamp: str | None = None

    @property
    def is_tool_result_only(self) -> bool:
        """True when all blocks are ToolResultBlock (tool response, not direct user input)."""
        return bool(self.blocks) and all(isinstance(b, ToolResultBlock) for b in self.blocks)


@dataclass
class SubConversation:
    """A nested agent conversation spawned by a ToolUseBlock."""

    agent_id: str
    tool_use_id: str  # id of the ToolUseBlock that spawned this
    description: str
    agent_type: str
    messages: list[Message] = field(default_factory=list)


@dataclass
class Session:
    """A complete conversation session."""

    session_id: str
    project: str           # raw source identifier (e.g. encoded path)
    title: str | None
    timestamp: str | None
    messages: list[Message] = field(default_factory=list)
    subconversations: dict[str, SubConversation] = field(default_factory=dict)
    # key: tool_use_id of the ToolUseBlock that spawned the subconversation
    display_project: str | None = None  # human-readable project name
