from __future__ import annotations

import json
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from ..models import Message
from ..models import Session
from ..models import SubConversation
from ..models import TextBlock
from ..models import ToolResultBlock
from ..models import ToolUseBlock
from .base import BaseSource
from .base import SessionMeta

KIMI_DIR = Path.home() / ".kimi"
SESSIONS_DIR = KIMI_DIR / "sessions"


def _read_jsonl(path: Path) -> list[dict]:
    records: list[dict] = []
    try:
        with path.open() as fh:
            for line in fh:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    records.append(value)
    except OSError:
        pass
    return records


def _read_state(session_dir: Path) -> dict:
    try:
        value = json.loads((session_dir / "state.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _record_message(record: dict) -> dict:
    message = record.get("message")
    return message if isinstance(message, dict) else record


def _record_type(record: dict) -> str:
    message = _record_message(record)
    value = message.get("type") or record.get("type")
    return value if isinstance(value, str) else ""


def _record_payload(record: dict) -> dict:
    message = _record_message(record)
    payload = message.get("payload")
    return payload if isinstance(payload, dict) else {}


def _format_timestamp(value: object) -> str | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, str) and value:
        return value
    return None


def _event_timestamp(record: dict) -> str | None:
    return _format_timestamp(record.get("timestamp"))


def _parse_tool_arguments(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    if raw is None:
        return {}
    text = str(raw)
    if not text.strip():
        return {}
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {"raw": text}
    return value if isinstance(value, dict) else {"value": value}


def _normalize_tool_result(value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, dict):
        return str(value)

    output = value.get("output")
    message = value.get("message")
    display = value.get("display")
    extras = value.get("extras")

    parts: list[str] = []
    for item in (output, message):
        if isinstance(item, str) and item.strip():
            parts.append(item.strip())
    if display:
        parts.append(json.dumps(display, ensure_ascii=False, indent=2))
    if extras:
        parts.append(json.dumps(extras, ensure_ascii=False, indent=2))
    if parts:
        return "\n\n".join(parts)
    return json.dumps(value, ensure_ascii=False, indent=2)


def _session_dirs(base: Path) -> list[Path]:
    base = base.expanduser()
    if not base.exists():
        return []
    if (base / "wire.jsonl").is_file():
        return [base]

    direct = [path for path in base.iterdir() if path.is_dir() and (path / "wire.jsonl").is_file()]
    if direct:
        return direct

    return sorted({path.parent for path in base.rglob("wire.jsonl") if path.is_file()})


def _derive_title(records: list[dict], state: dict) -> str | None:
    title = state.get("custom_title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    for record in records:
        if _record_type(record) != "TurnBegin":
            continue
        user_input = _record_payload(record).get("user_input")
        if isinstance(user_input, str) and user_input.strip():
            return " ".join(user_input.split())[:80]
    return None


def _collect_metadata(records: list[dict], state: dict) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    for record in records:
        timestamp = _event_timestamp(record)
        if timestamp:
            first_timestamp = first_timestamp or timestamp
            last_timestamp = timestamp

    if first_timestamp:
        metadata["first_event_at"] = first_timestamp
    if last_timestamp:
        metadata["last_event_at"] = last_timestamp

    approval = state.get("approval")
    if isinstance(approval, dict):
        for key in ("yolo", "afk"):
            if key in approval:
                metadata[key] = approval[key]
    todos = state.get("todos")
    if isinstance(todos, list):
        metadata["todo_count"] = len(todos)
        done_count = sum(1 for item in todos if isinstance(item, dict) and item.get("status") == "done")
        metadata["todo_done_count"] = done_count
    return metadata


def _build_messages(records: list[dict]) -> list[Message]:
    messages: list[Message] = []
    tool_blocks: dict[str, ToolUseBlock] = {}
    tool_arguments: dict[str, str] = {}
    last_tool_call_id: str | None = None

    for record in records:
        record_type = _record_type(record)
        payload = _record_payload(record)
        timestamp = _event_timestamp(record)

        if record_type == "TurnBegin":
            user_input = payload.get("user_input")
            if isinstance(user_input, str) and user_input.strip():
                messages.append(Message(role="user", blocks=[TextBlock(text=user_input.strip())], timestamp=timestamp))
            continue

        if record_type == "ContentPart":
            text = payload.get("text")
            if isinstance(text, str) and text.strip():
                messages.append(Message(role="assistant", blocks=[TextBlock(text=text.strip())], timestamp=timestamp))
            continue

        if record_type == "ToolCall":
            call_id = str(payload.get("id") or "")
            function = payload.get("function")
            function = function if isinstance(function, dict) else {}
            name = str(function.get("name") or payload.get("type") or "Tool")
            raw_arguments = function.get("arguments", "")
            tool_arguments[call_id] = str(raw_arguments or "")
            block = ToolUseBlock(
                id=call_id,
                name=name,
                input=_parse_tool_arguments(tool_arguments[call_id]),
            )
            tool_blocks[call_id] = block
            messages.append(Message(role="assistant", blocks=[block], timestamp=timestamp))
            last_tool_call_id = call_id
            continue

        if record_type == "ToolCallPart":
            if last_tool_call_id is None:
                continue
            arguments_part = payload.get("arguments_part", "")
            tool_arguments[last_tool_call_id] = tool_arguments.get(last_tool_call_id, "") + str(arguments_part or "")
            block = tool_blocks.get(last_tool_call_id)
            if block is not None:
                block.input = _parse_tool_arguments(tool_arguments[last_tool_call_id])
            continue

        if record_type == "ToolResult":
            call_id = str(payload.get("tool_call_id") or "")
            return_value = payload.get("return_value")
            content = _normalize_tool_result(return_value)
            if content:
                is_error = bool(return_value.get("is_error")) if isinstance(return_value, dict) else False
                messages.append(
                    Message(
                        role="user",
                        blocks=[
                            ToolResultBlock(
                                tool_use_id=call_id,
                                content=content,
                                is_error=is_error,
                            )
                        ],
                        timestamp=timestamp,
                    )
                )
            continue

    return _merge_assistant_turns(messages)


def _read_meta(agent_dir: Path) -> dict:
    try:
        value = json.loads((agent_dir / "meta.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _agent_tool_calls(records: list[dict]) -> dict[str, dict]:
    tool_arguments: dict[str, str] = {}
    tool_names: dict[str, str] = {}
    last_tool_call_id: str | None = None

    for record in records:
        record_type = _record_type(record)
        payload = _record_payload(record)

        if record_type == "ToolCall":
            call_id = str(payload.get("id") or "")
            if not call_id:
                continue
            function = payload.get("function")
            function = function if isinstance(function, dict) else {}
            tool_names[call_id] = str(function.get("name") or payload.get("type") or "")
            tool_arguments[call_id] = str(function.get("arguments") or "")
            last_tool_call_id = call_id
            continue

        if record_type == "ToolCallPart" and last_tool_call_id is not None:
            arguments_part = payload.get("arguments_part", "")
            tool_arguments[last_tool_call_id] = tool_arguments.get(last_tool_call_id, "") + str(arguments_part or "")

    calls: dict[str, dict] = {}
    for call_id, name in tool_names.items():
        if name != "Agent":
            continue
        calls[call_id] = _parse_tool_arguments(tool_arguments.get(call_id, ""))
    return calls


def _subagent_links(records: list[dict]) -> dict[str, str]:
    links: dict[str, str] = {}
    for record in records:
        if _record_type(record) != "SubagentEvent":
            continue
        payload = _record_payload(record)
        tool_call_id = payload.get("parent_tool_call_id")
        agent_id = payload.get("agent_id")
        if isinstance(tool_call_id, str) and tool_call_id and isinstance(agent_id, str) and agent_id:
            links.setdefault(tool_call_id, agent_id)
    return links


def _load_subconversation(
    agent_dir: Path,
    *,
    tool_use_id: str,
    tool_input: dict | None = None,
) -> SubConversation | None:
    wire_path = agent_dir / "wire.jsonl"
    if not wire_path.is_file():
        return None

    meta = _read_meta(agent_dir)
    tool_input = tool_input or {}
    agent_id = str(meta.get("agent_id") or agent_dir.name)
    description = str(tool_input.get("description") or meta.get("description") or agent_id)
    agent_type = str(tool_input.get("subagent_type") or meta.get("subagent_type") or "general-purpose")
    records = _read_jsonl(wire_path)
    return SubConversation(
        agent_id=agent_id,
        tool_use_id=tool_use_id,
        description=description,
        agent_type=agent_type,
        messages=_build_messages(records),
        metadata=_collect_metadata(records, meta),
    )


def _build_subconversations(
    records: list[dict],
    session_dir: Path,
) -> tuple[dict[str, SubConversation], list[SubConversation]]:
    subagent_root = session_dir / "subagents"
    if not subagent_root.is_dir():
        return {}, []

    agent_tool_calls = _agent_tool_calls(records)
    links = _subagent_links(records)

    linked: dict[str, SubConversation] = {}
    linked_agent_ids: set[str] = set()
    for tool_use_id, agent_id in links.items():
        agent_dir = subagent_root / agent_id
        sub = _load_subconversation(agent_dir, tool_use_id=tool_use_id, tool_input=agent_tool_calls.get(tool_use_id))
        if sub is None:
            continue
        linked[tool_use_id] = sub
        linked_agent_ids.add(agent_id)

    unlinked: list[SubConversation] = []
    for agent_dir in sorted(path for path in subagent_root.iterdir() if path.is_dir()):
        if agent_dir.name in linked_agent_ids:
            continue
        sub = _load_subconversation(agent_dir, tool_use_id="")
        if sub is not None:
            unlinked.append(sub)

    return linked, unlinked


def _merge_assistant_turns(messages: list[Message]) -> list[Message]:
    merged: list[Message] = []
    for msg in messages:
        if msg.role == "assistant" and merged and merged[-1].role == "assistant":
            merged[-1].blocks.extend(msg.blocks)
            continue
        merged.append(msg)
    return merged


class KimiCodeSource(BaseSource):
    """Source for Kimi Code sessions stored under ``~/.kimi/sessions``."""

    def __init__(
        self,
        sessions_dir: Path = SESSIONS_DIR,
        scan_dir: Path | None = None,
        project_filter: str | None = None,
    ) -> None:
        self._sessions_dir = sessions_dir
        self._scan_dir = scan_dir
        self._project_filter = project_filter

    def discover(self) -> list[SessionMeta]:
        base = self._scan_dir or self._sessions_dir
        sessions = [self._build_meta(session_dir) for session_dir in _session_dirs(base)]
        sessions = [meta for meta in sessions if meta is not None]
        sessions.sort(key=lambda session: session.sort_timestamp or session.timestamp or "", reverse=True)
        return sessions

    def _build_meta(self, session_dir: Path) -> SessionMeta | None:
        wire_path = session_dir / "wire.jsonl"
        if not wire_path.is_file():
            return None
        records = _read_jsonl(wire_path)
        state = _read_state(session_dir)
        timestamp = next((_event_timestamp(record) for record in records if _event_timestamp(record)), None)
        sort_timestamp = next((_event_timestamp(record) for record in reversed(records) if _event_timestamp(record)), timestamp)
        project = session_dir.parent.name
        display_project = str(session_dir.parent)
        if self._project_filter and self._project_filter.lower() not in display_project.lower():
            return None
        return SessionMeta(
            ref={"wire": wire_path, "session_dir": session_dir},
            session_id=session_dir.name,
            project=project,
            title=_derive_title(records, state),
            timestamp=timestamp,
            sort_timestamp=sort_timestamp,
            display_project=display_project,
        )

    def resolve_file(self, path: Path) -> SessionMeta | None:
        path = path.expanduser().resolve()
        if not path.is_file():
            return None
        if path.name == "wire.jsonl":
            return self._build_meta(path.parent)
        if path.name.startswith("context") and path.suffix == ".jsonl":
            wire_path = path.parent / "wire.jsonl"
            if wire_path.is_file():
                return self._build_meta(path.parent)
        return None

    def load(self, meta: SessionMeta) -> Session:
        return self._load_from_path(
            wire_path=meta.ref["wire"],
            session_dir=meta.ref["session_dir"],
            session_id=meta.session_id,
            project=meta.project,
            display_project=meta.get_display_project(),
            title=meta.title,
            timestamp=meta.timestamp,
        )

    def load_file(self, path: Path) -> Session:
        path = path.expanduser().resolve()
        meta = self.resolve_file(path)
        if meta is not None:
            return self.load(meta)
        wire_path = path
        session_dir = path.parent
        return self._load_from_path(
            wire_path=wire_path,
            session_dir=session_dir,
            session_id=session_dir.name,
            project=session_dir.parent.name,
            display_project=str(session_dir.parent),
            title=None,
            timestamp=None,
        )

    def _load_from_path(
        self,
        wire_path: Path,
        session_dir: Path,
        session_id: str,
        project: str,
        display_project: str,
        title: str | None,
        timestamp: str | None,
    ) -> Session:
        records = _read_jsonl(wire_path)
        state = _read_state(session_dir)
        linked, unlinked = _build_subconversations(records, session_dir)
        return Session(
            session_id=session_id,
            project=project,
            display_project=display_project,
            title=title or _derive_title(records, state),
            timestamp=timestamp,
            messages=_build_messages(records),
            metadata=_collect_metadata(records, state),
            subconversations=linked,
            unlinked_subconversations=unlinked,
        )
