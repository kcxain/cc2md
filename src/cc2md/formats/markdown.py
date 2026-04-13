from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ..models import (
    ImageBlock,
    Message,
    Session,
    SubConversation,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from .base import BaseFormat

_AGENT_TOOL_NAMES = {"Agent", "Task"}


class MarkdownFormat(BaseFormat):
    """Renders a Session as GitHub-flavoured Markdown."""

    def __init__(
        self,
        include_subagents: bool = True,
        include_tool_results: bool = True,
    ) -> None:
        self.include_subagents = include_subagents
        self.include_tool_results = include_tool_results

    @property
    def file_extension(self) -> str:
        return "md"

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def render(self, session: Session) -> str:
        lines = self._render_header(session)
        for msg in session.messages:
            self._append_message(msg, session, lines)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------

    def _render_header(self, session: Session) -> list[str]:
        title = session.title or "Untitled Session"
        ts = session.timestamp or ""
        if ts:
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                ts = dt.strftime("%Y-%m-%d %H:%M UTC")
            except ValueError:
                pass

        proj = session.display_project or session.project
        lines = [
            f"# {title}",
            "",
            f"**Session:** `{session.session_id}`  ",
            f"**Project:** `{proj}`  ",
        ]
        if ts:
            lines.append(f"**Date:** {ts}  ")
        lines.extend(["", "---", ""])
        return lines

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    def _append_message(self, msg: Message, session: Session, lines: list[str]) -> None:
        if msg.role == "user":
            if msg.is_tool_result_only:
                if self.include_tool_results:
                    rendered = self._render_blocks(msg.blocks)
                    if rendered.strip():
                        lines.append(
                            f"<details><summary>Tool Result</summary>\n\n{rendered}\n\n</details>\n"
                        )
            else:
                rendered = self._render_blocks(msg.blocks)
                if rendered.strip():
                    lines.append(f"## User\n\n{rendered}\n")

        elif msg.role == "assistant":
            rendered = self._render_blocks(msg.blocks)
            if rendered.strip():
                lines.append(f"## Assistant\n\n{rendered}\n")

            if self.include_subagents:
                for block in msg.blocks:
                    if isinstance(block, ToolUseBlock) and block.name in _AGENT_TOOL_NAMES:
                        sub = session.subconversations.get(block.id)
                        if sub:
                            lines.append("<details><summary>Subagent Conversation</summary>\n")
                            lines.append(self._render_subconversation(sub))
                            lines.append("</details>\n")

    # ------------------------------------------------------------------
    # Block rendering
    # ------------------------------------------------------------------

    def _render_blocks(self, blocks: list) -> str:
        parts: list[str] = []
        for block in blocks:
            if isinstance(block, TextBlock):
                if block.text:
                    parts.append(block.text)
            elif isinstance(block, ToolUseBlock):
                parts.append(self._render_tool_use(block))
            elif isinstance(block, ToolResultBlock):
                if self.include_tool_results:
                    r = self._render_tool_result(block)
                    if r:
                        parts.append(r)
            elif isinstance(block, ImageBlock):
                parts.append("*[image]*")
        return "\n\n".join(parts)

    def _render_tool_use(self, block: ToolUseBlock) -> str:
        name = block.name
        inp = block.input
        lines = [f"**Tool: {name}**"]

        if name == "Bash":
            desc = inp.get("description", "")
            if desc:
                lines.append(f"*{desc}*")
            lines.append(f"```bash\n{inp.get('command', '')}\n```")

        elif name == "Read":
            lines.append(f"Reading `{inp.get('file_path', '')}`")

        elif name == "Write":
            fp = inp.get("file_path", "")
            content = inp.get("content", "")
            lines.append(f"Writing `{fp}`")
            if content:
                cl = content.split("\n")
                if len(cl) > 30:
                    preview = (
                        "\n".join(cl[:15])
                        + f"\n\n... ({len(cl) - 30} lines omitted) ...\n\n"
                        + "\n".join(cl[-15:])
                    )
                else:
                    preview = content
                ext = Path(fp).suffix.lstrip(".")
                lines.append(f"```{ext}\n{preview}\n```")

        elif name == "Edit":
            fp = inp.get("file_path", "")
            old = inp.get("old_string", "")
            new = inp.get("new_string", "")
            lines.append(f"Editing `{fp}`")
            if old or new:
                lines.append("```diff")
                for ln in old.split("\n"):
                    lines.append(f"- {ln}")
                for ln in new.split("\n"):
                    lines.append(f"+ {ln}")
                lines.append("```")

        elif name == "Grep":
            lines.append(f"Searching for `{inp.get('pattern', '')}` in `{inp.get('path', '.')}`")

        elif name == "Glob":
            lines.append(f"Finding files matching `{inp.get('pattern', '')}`")

        elif name in _AGENT_TOOL_NAMES:
            subtype = inp.get("subagent_type", "general-purpose")
            desc = inp.get("description", "")
            lines.append(f"Spawning **{subtype}** agent: *{desc}*")
            prompt = inp.get("prompt", "")
            if prompt:
                if len(prompt) > 500:
                    prompt = prompt[:500] + "..."
                lines.append(f"\n> {prompt}")

        elif name in ("WebSearch", "WebFetch"):
            lines.append(f"`{inp.get('query', inp.get('url', ''))}`")

        else:
            if inp:
                lines.append(f"```json\n{json.dumps(inp, indent=2)[:500]}\n```")

        return "\n".join(lines)

    def _render_tool_result(self, block: ToolResultBlock) -> str:
        if not block.content:
            return ""
        prefix = "**Error:**\n" if block.is_error else ""
        content = block.content
        lines = content.split("\n")
        if len(lines) > 50:
            content = (
                "\n".join(lines[:25])
                + f"\n\n... ({len(lines) - 50} lines omitted) ...\n\n"
                + "\n".join(lines[-25:])
            )
        return f"{prefix}```\n{content}\n```"

    # ------------------------------------------------------------------
    # Subconversations
    # ------------------------------------------------------------------

    def _render_subconversation(self, sub: SubConversation) -> str:
        desc = sub.description or "Subagent"
        lines = [f"#### Subagent: {desc}", f"*Type: {sub.agent_type or 'unknown'}*\n"]
        for msg in sub.messages:
            if msg.is_tool_result_only:
                if self.include_tool_results:
                    rendered = self._render_blocks(msg.blocks)
                    if rendered.strip():
                        lines.append(
                            f"<details><summary>Tool Result</summary>\n\n{rendered}\n\n</details>\n"
                        )
                continue
            rendered = self._render_blocks(msg.blocks)
            if not rendered.strip():
                continue
            if msg.role == "user":
                lines.append(f"**Prompt:**\n\n{rendered}\n")
            else:
                lines.append(f"{rendered}\n")
        return "\n".join(lines)
