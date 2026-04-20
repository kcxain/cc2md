from __future__ import annotations

import json
import re
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
from .base import BaseFormat, RenderResult

_AGENT_TOOL_NAMES = {"Agent", "Task", "spawn_agent"}


def _safe_filename(text: str, max_len: int = 40) -> str:
    """Convert arbitrary text to a safe filename stem."""
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    text = re.sub(r"[\s_]+", "-", text)
    return text[:max_len].rstrip("-")


def _subagent_filename(description: str, agent_id: str, used: set[str], ext: str) -> str:
    name = _safe_filename(description or agent_id)
    suffixes = [agent_id[:7], agent_id[-7:], agent_id[:12], agent_id]
    for suffix in suffixes:
        filename = f"{name}-{suffix}.{ext}"
        if filename not in used:
            used.add(filename)
            return filename
    filename = f"{name}.{ext}"
    used.add(filename)
    return filename


def _truncate_lines(lines: list[str], keep: int = 60) -> list[str]:
    if len(lines) <= keep:
        return lines
    head = keep // 2
    tail = keep - head
    omitted = len(lines) - keep
    return lines[:head] + [f"... ({omitted} lines omitted) ..."] + lines[-tail:]


def _prefixed_lines(text: str, prefix: str) -> list[str]:
    return [f"{prefix}{line}" for line in text.split("\n")]


def _render_diff_block(lines: list[str]) -> str:
    truncated = _truncate_lines(lines)
    return "```diff\n" + "\n".join(truncated) + "\n```"


def _render_add_diff(content: str) -> str:
    return _render_diff_block(_prefixed_lines(content, "+ "))


def _render_delete_diff(content: str) -> str:
    return _render_diff_block(_prefixed_lines(content, "- "))


def _render_replace_diff(old: str, new: str) -> str:
    return _render_diff_block(_prefixed_lines(old, "- ") + _prefixed_lines(new, "+ "))


def _render_multi_edit_diff(edits: list[dict]) -> str:
    lines: list[str] = []
    for index, edit in enumerate(edits, 1):
        old = str(edit.get("old_string", edit.get("oldText", "")))
        new = str(edit.get("new_string", edit.get("newText", "")))
        replace_all = edit.get("replace_all")
        if index > 1:
            lines.append("@@")
        old_lines = _prefixed_lines(old, "- ")
        new_lines = _prefixed_lines(new, "+ ")
        if replace_all:
            lines.append(f"# replace_all={replace_all}")
        lines.extend(old_lines + new_lines)
    return _render_diff_block(lines)


def _extract_patch_text(inp: dict) -> str:
    for key in ("patch", "input", "content", "diff"):
        value = inp.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _format_metadata_value(value: object) -> str:
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _codex_metadata_lines(metadata: dict[str, object]) -> list[str]:
    if not metadata:
        return []

    ordered_fields = [
        ("model", "Model"),
        ("model_provider", "Model Provider"),
        ("reasoning_effort", "Reasoning Effort"),
        ("model_context_window", "Context Window"),
        ("originator", "Originator"),
        ("cli_version", "CLI Version"),
        ("session_source", "Session Source"),
        ("collaboration_mode", "Collaboration Mode"),
        ("approval_policy", "Approval Policy"),
        ("sandbox_policy", "Sandbox"),
        ("personality", "Personality"),
        ("plan_type", "Plan"),
        ("summary", "Summary"),
        ("input_tokens", "Input Tokens"),
        ("cached_input_tokens", "Cached Input Tokens"),
        ("output_tokens", "Output Tokens"),
        ("reasoning_output_tokens", "Reasoning Output Tokens"),
        ("total_tokens", "Total Tokens"),
        ("last_turn_tokens", "Last Turn Tokens"),
        ("primary_rate_limit_used_percent", "Primary Rate Limit Used %"),
        ("git_branch", "Git Branch"),
        ("git_commit", "Git Commit"),
    ]

    lines = ["## Codex Metadata", ""]
    for key, label in ordered_fields:
        if key not in metadata:
            continue
        lines.append(f"**{label}:** `{_format_metadata_value(metadata[key])}`  ")
    lines.extend(["", "---", ""])
    return lines


class MarkdownFormat(BaseFormat):
    """Renders a Session as GitHub-flavoured Markdown.

    Sessions with subagents produce a **directory**:
      index.md                  — main conversation with links to subagent files
      {desc}-{agent_id}.md      — one file per subagent

    Sessions without subagents produce a single file.
    """

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

    def render(self, session: Session) -> RenderResult:
        has_subs = self.include_subagents and bool(
            session.subconversations or session.unlinked_subconversations
        )
        if has_subs:
            return self._render_multi(session)
        lines = self._render_header(session)
        for msg in session.messages:
            self._append_message(msg, session, lines, subagent_links=None)
        return RenderResult(files={f"index.{self.file_extension}": "\n".join(lines)})

    # ------------------------------------------------------------------
    # Multi-file rendering
    # ------------------------------------------------------------------

    def _render_multi(self, session: Session) -> RenderResult:
        # Build tool_use_id → subagent filename map (linked)
        subagent_links: dict[str, str] = {}
        used_filenames: set[str] = set()
        for tool_use_id, sub in session.subconversations.items():
            filename = _subagent_filename(
                description=sub.description or sub.agent_id,
                agent_id=sub.agent_id,
                used=used_filenames,
                ext=self.file_extension,
            )
            subagent_links[tool_use_id] = filename

        # Main file
        lines = self._render_header(session)
        for msg in session.messages:
            self._append_message(msg, session, lines, subagent_links=subagent_links)

        files: dict[str, str] = {}

        # One file per linked subagent
        for tool_use_id, filename in subagent_links.items():
            sub = session.subconversations[tool_use_id]
            files[filename] = self._render_subconversation_page(sub)

        # One file per unlinked subagent (with links appended to main file)
        unlinked_links: list[str] = []
        for sub in session.unlinked_subconversations:
            filename = _subagent_filename(
                description=sub.description or sub.agent_id,
                agent_id=sub.agent_id,
                used=used_filenames,
                ext=self.file_extension,
            )
            desc = sub.description or sub.agent_id
            unlinked_links.append(f"[→ Subagent: {desc}]({filename})\n")
            files[filename] = self._render_subconversation_page(sub)

        if unlinked_links:
            lines.append("---\n")
            lines.append("## Other Subagent Conversations\n")
            lines.extend(unlinked_links)

        files[f"index.{self.file_extension}"] = "\n".join(lines)
        return RenderResult(files=files)

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
        lines.extend(_codex_metadata_lines(session.metadata))
        return lines

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------

    def _append_message(
        self,
        msg: Message,
        session: Session,
        lines: list[str],
        subagent_links: dict[str, str] | None,
    ) -> None:
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
                    if not (isinstance(block, ToolUseBlock) and block.name in _AGENT_TOOL_NAMES):
                        continue
                    sub = session.subconversations.get(block.id)
                    if not sub:
                        continue
                    if subagent_links:
                        # Multi-file mode: insert a link
                        filename = subagent_links[block.id]
                        desc = sub.description or "Subagent"
                        lines.append(f"[→ Subagent: {desc}]({filename})\n")
                    else:
                        # Single-file mode: inline <details>
                        lines.append("<details><summary>Subagent Conversation</summary>\n")
                        lines.append(self._render_subconversation_inline(sub))
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
        lower_name = name.lower()
        inp = block.input
        lines = [f"**Tool: {name}**"]

        if name == "Bash":
            desc = inp.get("description", "")
            if desc:
                lines.append(f"*{desc}*")
            lines.append(f"```bash\n{inp.get('command', '')}\n```")

        elif name == "Read":
            lines.append(f"Reading `{inp.get('file_path', '')}`")

        elif name in {"Write", "Add"} or lower_name in {"write", "add", "create_file"}:
            fp = inp.get("file_path", "")
            content = inp.get("content", "")
            action = "Adding" if name == "Add" or lower_name in {"add", "create_file"} else "Writing"
            lines.append(f"{action} `{fp}`")
            if content:
                lines.append(_render_add_diff(content))

        elif name == "Edit" or lower_name in {"edit", "replace_in_file"}:
            fp = inp.get("file_path", "")
            old = inp.get("old_string", "")
            new = inp.get("new_string", "")
            lines.append(f"Editing `{fp}`")
            if old or new:
                lines.append(_render_replace_diff(old, new))

        elif name == "MultiEdit" or lower_name in {"multiedit", "multi_edit"}:
            fp = inp.get("file_path", "")
            edits = inp.get("edits", [])
            lines.append(f"Editing `{fp}`")
            if isinstance(edits, list) and edits:
                normalized = [edit for edit in edits if isinstance(edit, dict)]
                if normalized:
                    lines.append(_render_multi_edit_diff(normalized))

        elif lower_name in {"apply_patch", "applypatch"}:
            patch = _extract_patch_text(inp)
            lines.append("Applying patch")
            if patch:
                lines.append(_render_diff_block(patch.split("\n")))

        elif name in {"Delete", "DeleteFile"} or lower_name in {"delete", "delete_file", "remove_file"}:
            fp = inp.get("file_path", inp.get("path", ""))
            content = str(
                inp.get("old_string")
                or inp.get("old_content")
                or inp.get("content")
                or ""
            )
            lines.append(f"Deleting `{fp}`")
            if content:
                lines.append(_render_delete_diff(content))

        elif name == "Grep":
            lines.append(f"Searching for `{inp.get('pattern', '')}` in `{inp.get('path', '.')}`")

        elif name == "Glob":
            lines.append(f"Finding files matching `{inp.get('pattern', '')}`")

        elif name in _AGENT_TOOL_NAMES:
            subtype = inp.get("subagent_type", inp.get("agent_type", "general-purpose"))
            desc = inp.get("description", inp.get("message", ""))
            lines.append(f"Spawning **{subtype}** agent: *{desc}*")
            prompt = inp.get("prompt", inp.get("message", ""))
            if prompt:
                if len(prompt) > 500:
                    prompt = prompt[:500] + "..."
                lines.append(f"\n> {prompt}")

        elif name == "exec_command":
            desc = inp.get("justification", "")
            if desc:
                lines.append(f"*{desc}*")
            lines.append(f"```bash\n{inp.get('cmd', '')}\n```")

        elif name == "write_stdin":
            session_id = inp.get("session_id", "")
            chars = inp.get("chars", "")
            lines.append(f"Sending input to session `{session_id}`")
            if chars:
                lines.append(f"```text\n{chars}\n```")

        elif name == "wait_agent":
            targets = inp.get("targets", [])
            lines.append("Waiting for subagents")
            if targets:
                lines.append(f"```json\n{json.dumps(targets, ensure_ascii=False, indent=2)}\n```")

        elif name == "send_input":
            target = inp.get("target", "")
            lines.append(f"Sending input to agent `{target}`")
            if inp.get("message"):
                lines.append(f"```text\n{inp['message']}\n```")

        elif name == "close_agent":
            lines.append(f"Closing agent `{inp.get('target', '')}`")

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
    # Subconversation rendering
    # ------------------------------------------------------------------

    def _render_subconversation_messages(self, sub: SubConversation, lines: list[str]) -> None:
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

    def _render_subconversation_inline(self, sub: SubConversation) -> str:
        """Compact rendering for single-file (inline) mode."""
        desc = sub.description or "Subagent"
        lines = [f"#### Subagent: {desc}", f"*Type: {sub.agent_type or 'unknown'}*\n"]
        self._render_subconversation_messages(sub, lines)
        return "\n".join(lines)

    def _render_subconversation_page(self, sub: SubConversation) -> str:
        """Standalone page rendering for multi-file mode."""
        desc = sub.description or "Subagent"
        lines = [
            f"# Subagent: {desc}",
            "",
            f"**Type:** {sub.agent_type or 'unknown'}  ",
            f"**Agent ID:** `{sub.agent_id}`  ",
            "",
            "---",
            "",
        ]
        self._render_subconversation_messages(sub, lines)
        return "\n".join(lines)
