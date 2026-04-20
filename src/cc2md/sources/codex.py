from __future__ import annotations

import json
from pathlib import Path

from ..models import Message
from ..models import Session
from ..models import SubConversation
from ..models import TextBlock
from ..models import ToolResultBlock
from ..models import ToolUseBlock
from .base import BaseSource
from .base import SessionMeta

CODEX_DIR = Path.home() / ".codex"
SESSIONS_DIR = CODEX_DIR / "sessions"


def _read_jsonl(path: Path) -> list[dict]:
    records: list[dict] = []
    with open(path) as f:
        for line in f:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _read_session_meta(path: Path) -> dict | None:
    try:
        with open(path) as f:
            first = f.readline()
    except OSError:
        return None
    if not first:
        return None
    try:
        record = json.loads(first)
    except json.JSONDecodeError:
        return None
    if record.get("type") != "session_meta":
        return None
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else None


def _is_subagent_source(source: object) -> bool:
    return isinstance(source, dict) and "subagent" in source


def _derive_title(text: str | None) -> str | None:
    if not text:
        return None
    title = " ".join(text.split()).strip()
    if not title:
        return None
    return title[:80]


def _parse_tool_input(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}
        return data if isinstance(data, dict) else {"value": data}
    return {"value": raw}


def _parse_assistant_text(content: object) -> list[TextBlock]:
    if isinstance(content, str):
        text = content.strip()
        return [TextBlock(text=text)] if text else []

    if not isinstance(content, list):
        return []

    blocks: list[TextBlock] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text_type = item.get("type")
        if text_type not in {"output_text", "input_text"}:
            continue
        text = item.get("text", "").strip()
        if text:
            blocks.append(TextBlock(text=text))
    return blocks


def _normalize_tool_output(output: object) -> str:
    if output is None:
        return ""
    if not isinstance(output, str):
        return json.dumps(output, ensure_ascii=False, indent=2)

    if output.startswith("Chunk ID:") and "\nOutput:\n" in output:
        output = output.split("\nOutput:\n", 1)[1]

    stripped = output.strip()
    if not stripped:
        return ""

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        return output
    return json.dumps(data, ensure_ascii=False, indent=2)


def _subagent_description(args: dict, spawn_payload: dict | None, agent_id: str) -> str:
    candidates: list[str | None] = [
        args.get("description"),
        args.get("message"),
        _first_text_item(args.get("items")),
        spawn_payload.get("prompt") if spawn_payload else None,
        spawn_payload.get("new_agent_nickname") if spawn_payload else None,
        agent_id,
    ]
    for candidate in candidates:
        if not candidate:
            continue
        text = " ".join(str(candidate).split()).strip()
        if not text:
            continue
        return text[:120]
    return agent_id


def _first_text_item(items: object) -> str | None:
    if not isinstance(items, list):
        return None
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and item.get("text"):
            return str(item["text"])
    return None


def _build_messages(records: list[dict]) -> list[Message]:
    messages: list[Message] = []
    for record in records:
        record_type = record.get("type")
        payload = record.get("payload", {})
        timestamp = record.get("timestamp")

        if record_type == "event_msg" and payload.get("type") == "user_message":
            text = payload.get("message", "").strip()
            if text:
                messages.append(
                    Message(
                        role="user",
                        blocks=[TextBlock(text=text)],
                        timestamp=timestamp,
                    )
                )
            continue

        if record_type != "response_item":
            continue

        payload_type = payload.get("type")
        if payload_type == "message" and payload.get("role") == "assistant":
            blocks = _parse_assistant_text(payload.get("content", []))
            if blocks:
                messages.append(Message(role="assistant", blocks=blocks, timestamp=timestamp))
            continue

        if payload_type == "function_call":
            messages.append(
                Message(
                    role="assistant",
                    blocks=[
                        ToolUseBlock(
                            id=payload.get("call_id", ""),
                            name=payload.get("name", ""),
                            input=_parse_tool_input(payload.get("arguments", "")),
                        )
                    ],
                    timestamp=timestamp,
                )
            )
            continue

        if payload_type == "function_call_output":
            content = _normalize_tool_output(payload.get("output", ""))
            if content:
                messages.append(
                    Message(
                        role="user",
                        blocks=[
                            ToolResultBlock(
                                tool_use_id=payload.get("call_id", ""),
                                content=content,
                            )
                        ],
                        timestamp=timestamp,
                    )
                )

    return _merge_assistant_turns(messages)


def _merge_assistant_turns(messages: list[Message]) -> list[Message]:
    merged: list[Message] = []
    for msg in messages:
        if msg.role == "assistant" and merged and merged[-1].role == "assistant":
            merged[-1].blocks.extend(msg.blocks)
            continue
        merged.append(msg)
    return merged


def _find_rollout_by_id(sessions_root: Path, session_id: str) -> Path | None:
    matches = sorted(sessions_root.rglob(f"rollout-*-{session_id}.jsonl"))
    return matches[0] if matches else None


def _load_subconversation(
    rollout_path: Path,
    agent_id: str,
    tool_use_id: str,
    description: str,
    agent_type: str,
) -> SubConversation:
    messages = _build_messages(_read_jsonl(rollout_path))
    return SubConversation(
        agent_id=agent_id,
        tool_use_id=tool_use_id,
        description=description,
        agent_type=agent_type,
        messages=messages,
    )


def _build_subconversations(
    records: list[dict],
    sessions_root: Path,
    parent_session_id: str,
) -> tuple[dict[str, SubConversation], list[SubConversation]]:
    tool_use_meta: dict[str, dict] = {}
    for record in records:
        if record.get("type") != "response_item":
            continue
        payload = record.get("payload", {})
        if payload.get("type") != "function_call" or payload.get("name") != "spawn_agent":
            continue
        tool_use_meta[payload.get("call_id", "")] = _parse_tool_input(payload.get("arguments", ""))

    linked: dict[str, SubConversation] = {}
    linked_ids: set[str] = set()
    for record in records:
        if record.get("type") != "event_msg":
            continue
        payload = record.get("payload", {})
        if payload.get("type") != "collab_agent_spawn_end":
            continue
        call_id = payload.get("call_id", "")
        agent_id = payload.get("new_thread_id")
        if not call_id or not agent_id:
            continue
        rollout_path = _find_rollout_by_id(sessions_root, agent_id)
        if rollout_path is None:
            continue
        args = tool_use_meta.get(call_id, {})
        description = _subagent_description(args, payload, agent_id)
        agent_type = (
            args.get("agent_type")
            or payload.get("new_agent_role")
            or "general-purpose"
        )
        linked[call_id] = _load_subconversation(
            rollout_path=rollout_path,
            agent_id=agent_id,
            tool_use_id=call_id,
            description=description,
            agent_type=agent_type,
        )
        linked_ids.add(agent_id)

    unlinked: list[SubConversation] = []
    for rollout_path in sorted(sessions_root.rglob("rollout-*.jsonl")):
        meta = _read_session_meta(rollout_path)
        if meta is None:
            continue
        source = meta.get("source")
        if not _is_subagent_source(source):
            continue
        thread_spawn = source.get("subagent", {}).get("thread_spawn", {})
        if thread_spawn.get("parent_thread_id") != parent_session_id:
            continue
        agent_id = meta.get("id", "")
        if not agent_id or agent_id in linked_ids:
            continue
        description = _subagent_description({}, {"new_agent_nickname": meta.get("agent_nickname")}, agent_id)
        agent_type = meta.get("agent_role") or "general-purpose"
        unlinked.append(
            _load_subconversation(
                rollout_path=rollout_path,
                agent_id=agent_id,
                tool_use_id="",
                description=description,
                agent_type=agent_type,
            )
        )

    return linked, unlinked


class CodexSource(BaseSource):
    """Source for Codex rollout sessions stored under ``~/.codex/sessions``."""

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
        base = (self._scan_dir or self._sessions_dir).expanduser()
        if not base.exists():
            return []

        sessions: list[SessionMeta] = []
        for rollout_path in sorted(base.rglob("rollout-*.jsonl")):
            meta = self._build_meta(rollout_path, base)
            if meta is not None:
                sessions.append(meta)

        sessions.sort(key=lambda session: session.timestamp or "", reverse=True)
        return sessions

    def _build_meta(self, rollout_path: Path, sessions_root: Path) -> SessionMeta | None:
        session_meta = _read_session_meta(rollout_path)
        if session_meta is None:
            return None
        if _is_subagent_source(session_meta.get("source")):
            return None

        session_id = session_meta.get("id")
        timestamp = session_meta.get("timestamp")
        cwd = session_meta.get("cwd", "")
        if not session_id or not cwd:
            return None
        if self._project_filter and self._project_filter.lower() not in cwd.lower():
            return None

        title: str | None = None
        try:
            with open(rollout_path) as f:
                for line in f:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if record.get("type") == "event_msg" and record.get("payload", {}).get("type") == "user_message":
                        title = _derive_title(record["payload"].get("message"))
                        if title:
                            break
        except OSError:
            pass

        return SessionMeta(
            ref={"rollout": rollout_path, "sessions_root": sessions_root},
            session_id=session_id,
            project=cwd,
            title=title,
            timestamp=timestamp,
            display_project=cwd,
        )

    def load(self, meta: SessionMeta) -> Session:
        return self._load_from_path(
            rollout_path=meta.ref["rollout"],
            sessions_root=meta.ref["sessions_root"],
            session_id=meta.session_id,
            project=meta.project,
            display_project=meta.get_display_project(),
            title=meta.title,
            timestamp=meta.timestamp,
        )

    def load_file(self, path: Path) -> Session:
        rollout_path = path.expanduser().resolve()
        session_meta = _read_session_meta(rollout_path) or {}
        session_id = session_meta.get("id") or rollout_path.stem
        cwd = session_meta.get("cwd", "")
        timestamp = session_meta.get("timestamp")
        title: str | None = None

        try:
            with open(rollout_path) as f:
                for line in f:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if record.get("type") == "event_msg" and record.get("payload", {}).get("type") == "user_message":
                        title = _derive_title(record["payload"].get("message"))
                        if title:
                            break
        except OSError:
            pass

        sessions_root = (self._scan_dir or self._sessions_dir).expanduser()
        return self._load_from_path(
            rollout_path=rollout_path,
            sessions_root=sessions_root,
            session_id=session_id,
            project=cwd,
            display_project=cwd,
            title=title,
            timestamp=timestamp,
        )

    def _load_from_path(
        self,
        rollout_path: Path,
        sessions_root: Path,
        session_id: str,
        project: str,
        display_project: str,
        title: str | None,
        timestamp: str | None,
    ) -> Session:
        records = _read_jsonl(rollout_path)
        messages = _build_messages(records)
        linked, unlinked = _build_subconversations(records, sessions_root, session_id)
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
