from __future__ import annotations

import json
import re
from pathlib import Path

from ..models import (
    ContentBlock,
    ImageBlock,
    Message,
    Session,
    SubConversation,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
from .base import BaseSource, SessionMeta

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"

_AGENT_TOOL_NAMES = {"Agent", "Task"}
_SYSTEM_TAG_RE = re.compile(
    r"<(?:ide_opened_file|system-reminder)>.*?</(?:ide_opened_file|system-reminder)>",
    flags=re.DOTALL,
)


# ---------------------------------------------------------------------------
# Low-level parsing helpers
# ---------------------------------------------------------------------------

def _clean_text(text: str) -> str:
    return _SYSTEM_TAG_RE.sub("", text).strip()


def _decode_project_path(raw: str) -> str:
    """Heuristically reverse Claude Code's '/' → '-' path encoding."""
    if not raw:
        return raw
    candidate = "/" + raw[1:] if raw.startswith("-") else raw
    full_replace = candidate.replace("-", "/")
    if Path(full_replace).exists():
        return full_replace.lstrip("/")
    parts = raw.lstrip("-").split("-")
    reconstructed = [parts[0]]
    for part in parts[1:]:
        test = "/" + "/".join(reconstructed[:-1] + [reconstructed[-1] + "-" + part])
        if Path(test).exists():
            reconstructed[-1] += "-" + part
        else:
            reconstructed.append(part)
    return "/".join(reconstructed)


def _parse_tool_result_content(raw: str | list) -> str:
    if isinstance(raw, str):
        return raw
    parts: list[str] = []
    for item in raw:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(item["text"])
        elif isinstance(item, dict) and item.get("type") == "image":
            parts.append("*[image]*")
        else:
            parts.append(str(item))
    return "\n".join(parts)


def _parse_content_block(raw: dict) -> ContentBlock | None:
    btype = raw.get("type")
    if btype == "text":
        text = _clean_text(raw.get("text", ""))
        return TextBlock(text=text) if text else None
    if btype == "tool_use":
        return ToolUseBlock(
            id=raw.get("id", ""),
            name=raw.get("name", ""),
            input=raw.get("input", {}),
        )
    if btype == "tool_result":
        return ToolResultBlock(
            tool_use_id=raw.get("tool_use_id", ""),
            content=_parse_tool_result_content(raw.get("content", "")),
            is_error=raw.get("is_error", False),
        )
    if btype == "image":
        return ImageBlock()
    # "thinking" and other unknown block types are silently skipped
    return None


def _record_to_message(record: dict) -> Message:
    role = record["type"]
    raw_content = record.get("message", {}).get("content", [])
    ts = record.get("timestamp")

    if isinstance(raw_content, str):
        text = _clean_text(raw_content)
        blocks: list[ContentBlock] = [TextBlock(text=text)] if text else []
    else:
        blocks = [b for raw in raw_content if isinstance(raw, dict) and (b := _parse_content_block(raw))]

    return Message(role=role, blocks=blocks, timestamp=ts)


def _read_jsonl(path: Path) -> list[dict]:
    records: list[dict] = []
    with open(path) as f:
        for line in f:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


# ---------------------------------------------------------------------------
# Subconversation assembly
# ---------------------------------------------------------------------------

def _build_subconversations(
    records: list[dict],
    subagent_dir: Path | None,
) -> tuple[dict[str, SubConversation], list[SubConversation]]:
    """Build subconversation mappings from sidechain JSONLs.

    Returns ``(linked, unlinked)`` where:
    - ``linked``: tool_use_id → SubConversation (agent linked to a Tool call)
    - ``unlinked``: agents found in subagents/ dir but not matched to any tool_use

    Three linkage strategies (all run unconditionally):

    **Strategy 1** — progress records (new format):
      ``progress`` records carry ``parentToolUseID`` and ``data.agentId``.

    **Strategy 2** — toolUseResult.agentId in user records (old format):
      User records carry ``toolUseResult.agentId`` and a ``tool_result`` block
      with the matching ``tool_use_id``.

    **Strategy 3** — directory scan fallback:
      All ``agent-*.jsonl`` files in subagents/ that weren't linked by strategy
      1 or 2, and whose name doesn't start with ``aprompt_suggestion-``, are
      collected as unlinked subconversations.
    """
    if not subagent_dir or not subagent_dir.is_dir():
        return {}, []

    # --- Strategy 1: progress records (new format) --------------------------
    tool_use_to_agent: dict[str, str] = {}
    for record in records:
        if record.get("type") == "progress":
            parent_id = record.get("parentToolUseID")
            agent_id = record.get("data", {}).get("agentId")
            if parent_id and agent_id and parent_id not in tool_use_to_agent:
                tool_use_to_agent[parent_id] = agent_id

    # --- Strategy 2: toolUseResult.agentId in user records (old format) -----
    for record in records:
        if record.get("type") != "user":
            continue
        tur = record.get("toolUseResult")
        agent_id = tur.get("agentId") if isinstance(tur, dict) else None
        if not agent_id:
            continue
        content = record.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tool_use_id = block.get("tool_use_id", "")
                if tool_use_id and tool_use_id not in tool_use_to_agent:
                    tool_use_to_agent[tool_use_id] = agent_id

    # --- Description / type: assistant tool_use block OR .meta.json ---------
    tool_use_meta: dict[str, tuple[str, str]] = {}
    for record in records:
        if record.get("type") == "assistant":
            for raw in record.get("message", {}).get("content", []):
                if (
                    isinstance(raw, dict)
                    and raw.get("type") == "tool_use"
                    and raw.get("name") in _AGENT_TOOL_NAMES
                ):
                    inp = raw.get("input", {})
                    tool_use_meta[raw["id"]] = (
                        inp.get("description", ""),
                        inp.get("subagent_type", "general-purpose"),
                    )

    # --- Helper: load a SubConversation from a JSONL path -------------------
    def _load_sub(agent_id: str, tool_use_id: str, desc: str, atype: str) -> SubConversation:
        jsonl_path = subagent_dir / f"agent-{agent_id}.jsonl"
        messages = _merge_assistant_turns([
            _record_to_message(r)
            for r in _read_jsonl(jsonl_path)
            if r.get("type") in ("user", "assistant")
        ])
        return SubConversation(
            agent_id=agent_id,
            tool_use_id=tool_use_id,
            description=desc,
            agent_type=atype,
            messages=messages,
        )

    def _get_meta(tool_use_id: str, agent_id: str) -> tuple[str, str]:
        if tool_use_id in tool_use_meta:
            return tool_use_meta[tool_use_id]
        meta_path = subagent_dir / f"agent-{agent_id}.meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                return meta.get("description", ""), meta.get("agentType", "general-purpose")
            except (json.JSONDecodeError, OSError):
                pass
        return "", "general-purpose"

    # --- Assemble linked SubConversation objects ----------------------------
    linked: dict[str, SubConversation] = {}
    for tool_use_id, agent_id in tool_use_to_agent.items():
        jsonl_path = subagent_dir / f"agent-{agent_id}.jsonl"
        if not jsonl_path.exists():
            continue
        desc, atype = _get_meta(tool_use_id, agent_id)
        linked[tool_use_id] = _load_sub(agent_id, tool_use_id, desc, atype)

    # --- Strategy 3: scan dir for unlinked agents ---------------------------
    linked_agent_ids = {sub.agent_id for sub in linked.values()}
    unlinked: list[SubConversation] = []
    for jsonl_path in sorted(subagent_dir.glob("agent-*.jsonl")):
        agent_id = jsonl_path.stem[len("agent-"):]
        # Skip internal Claude Code suggestion agents
        if agent_id.startswith("aprompt_suggestion-"):
            continue
        if agent_id in linked_agent_ids:
            continue
        meta_path = subagent_dir / f"agent-{agent_id}.meta.json"
        desc, atype = "", "general-purpose"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
                desc = meta.get("description", "")
                atype = meta.get("agentType", "general-purpose")
            except (json.JSONDecodeError, OSError):
                pass
        unlinked.append(_load_sub(agent_id, "", desc, atype))

    return linked, unlinked


def _merge_assistant_turns(messages: list[Message]) -> list[Message]:
    """Merge consecutive assistant messages into one.

    Claude Code can emit several ``assistant`` records for the same turn
    (e.g. a text block followed by a tool_use block). Merging them produces
    a cleaner rendering where text and tool calls appear together.
    """
    merged: list[Message] = []
    for msg in messages:
        if msg.role == "assistant" and merged and merged[-1].role == "assistant":
            merged[-1].blocks.extend(msg.blocks)
        else:
            merged.append(msg)
    return merged


# ---------------------------------------------------------------------------
# ClaudeCodeSource
# ---------------------------------------------------------------------------

class ClaudeCodeSource(BaseSource):
    """Source for Claude Code sessions stored under ``~/.claude/projects/``.

    *scan_dir* overrides the projects directory and is auto-detected:
    - If it directly contains ``*.jsonl`` files → treated as a single project dir.
    - Otherwise → treated as a projects dir (subdirs are project dirs).
    """

    def __init__(
        self,
        projects_dir: Path = PROJECTS_DIR,
        scan_dir: Path | None = None,
        project_filter: str | None = None,
    ) -> None:
        self._projects_dir = projects_dir
        self._scan_dir = scan_dir
        self._project_filter = project_filter

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover(self) -> list[SessionMeta]:
        base = self._scan_dir if self._scan_dir is not None else self._projects_dir
        if not base.exists():
            return []

        has_jsonl = any(
            f.suffix == ".jsonl" and not f.stem.startswith("agent-")
            for f in base.iterdir()
            if f.is_file()
        )

        if has_jsonl:
            project_dirs = [base]
        else:
            project_dirs = [
                d for d in base.iterdir()
                if d.is_dir()
                and (not self._project_filter or self._project_filter.lower() in d.name.lower())
            ]

        sessions: list[SessionMeta] = []
        for project_dir in project_dirs:
            if has_jsonl and self._project_filter:
                if self._project_filter.lower() not in project_dir.name.lower():
                    continue
            for jsonl_file in project_dir.glob("*.jsonl"):
                if jsonl_file.stem.startswith("agent-"):
                    continue
                sessions.append(self._build_meta(jsonl_file, project_dir))

        sessions.sort(key=lambda s: s.sort_timestamp or s.timestamp or "", reverse=True)
        return sessions

    def _build_meta(self, jsonl_file: Path, project_dir: Path) -> SessionMeta:
        sid = jsonl_file.stem
        title: str | None = None
        timestamp: str | None = None
        try:
            with open(jsonl_file) as f:
                for line in f:
                    obj = json.loads(line)
                    if obj.get("type") == "ai-title":
                        title = obj.get("aiTitle")
                    if not timestamp and obj.get("timestamp"):
                        timestamp = obj["timestamp"]
                    if title and timestamp:
                        break
        except (json.JSONDecodeError, OSError):
            pass

        subagent_dir = project_dir / sid / "subagents"
        return SessionMeta(
            ref={"jsonl": jsonl_file, "subagent_dir": subagent_dir if subagent_dir.is_dir() else None},
            session_id=sid,
            project=project_dir.name,
            title=title,
            timestamp=timestamp,
            sort_timestamp=timestamp,
            display_project=_decode_project_path(project_dir.name),
        )

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load(self, meta: SessionMeta) -> Session:
        return self._load_from_paths(
            jsonl_path=meta.ref["jsonl"],
            subagent_dir=meta.ref.get("subagent_dir"),
            session_id=meta.session_id,
            project=meta.project,
            display_project=meta.get_display_project(),
            title=meta.title,
            timestamp=meta.timestamp,
        )

    def load_file(self, path: Path) -> Session:
        path = path.resolve()
        sid = path.stem
        project = path.parent.name
        subagent_dir = path.parent / sid / "subagents"

        title: str | None = None
        timestamp: str | None = None
        try:
            with open(path) as f:
                for line in f:
                    obj = json.loads(line)
                    if obj.get("type") == "ai-title":
                        title = obj.get("aiTitle")
                    if not timestamp and obj.get("timestamp"):
                        timestamp = obj["timestamp"]
                    if title and timestamp:
                        break
        except (json.JSONDecodeError, OSError):
            pass

        return self._load_from_paths(
            jsonl_path=path,
            subagent_dir=subagent_dir if subagent_dir.is_dir() else None,
            session_id=sid,
            project=project,
            display_project=_decode_project_path(project),
            title=title,
            timestamp=timestamp,
        )

    def _load_from_paths(
        self,
        jsonl_path: Path,
        subagent_dir: Path | None,
        session_id: str,
        project: str,
        display_project: str,
        title: str | None,
        timestamp: str | None,
    ) -> Session:
        records = _read_jsonl(jsonl_path)
        messages = [
            _record_to_message(r)
            for r in records
            if r.get("type") in ("user", "assistant")
        ]
        linked, unlinked = _build_subconversations(records, subagent_dir)
        return Session(
            session_id=session_id,
            project=project,
            display_project=display_project,
            title=title,
            timestamp=timestamp,
            messages=messages,
            subconversations=linked,
            unlinked_subconversations=unlinked,
        )
