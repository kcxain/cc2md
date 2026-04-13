from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TextIO


CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"

# Tool names that spawn subagents
_AGENT_TOOL_NAMES = {"Agent", "Task"}

_SYSTEM_TAG_RE = re.compile(
    r"<(?:ide_opened_file|system-reminder)>.*?</(?:ide_opened_file|system-reminder)>",
    flags=re.DOTALL,
)


@dataclass
class SessionInfo:
    """Metadata about a discovered session."""

    path: Path
    session_id: str
    project_path: str
    title: str | None = None
    timestamp: str | None = None
    subagent_dir: Path | None = None

    @property
    def display_project(self) -> str:
        """Human-readable project path.

        Claude Code encodes project paths by replacing '/' with '-',
        e.g. '/Users/nick/my-project' becomes '-Users-nick-my-project'.
        """
        raw = self.project_path
        if not raw:
            return raw

        candidate = "/" + raw[1:] if raw.startswith("-") else raw
        full_replace = candidate.replace("-", "/")
        if Path(full_replace).exists():
            return full_replace.lstrip("/")

        parts = raw.lstrip("-").split("-")
        reconstructed = [parts[0]]
        for part in parts[1:]:
            test_hyphen = "/" + "/".join(reconstructed[:-1] + [reconstructed[-1] + "-" + part])
            if Path(test_hyphen).exists():
                reconstructed[-1] += "-" + part
            else:
                reconstructed.append(part)

        return "/".join(reconstructed)


class ContentFormatter:
    """Formats JSONL content blocks into Markdown."""

    @classmethod
    def clean_text(cls, text: str) -> str:
        return _SYSTEM_TAG_RE.sub("", text).strip()

    @classmethod
    def format_tool_use(cls, block: dict) -> str:
        name = block.get("name", "Unknown")
        inp = block.get("input", {})
        lines = [f"**Tool: {name}**"]

        if name == "Bash":
            cmd = inp.get("command", "")
            desc = inp.get("description", "")
            if desc:
                lines.append(f"*{desc}*")
            lines.append(f"```bash\n{cmd}\n```")
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
            desc = inp.get("description", "")
            subtype = inp.get("subagent_type", "general-purpose")
            lines.append(f"Spawning **{subtype}** agent: *{desc}*")
            prompt = inp.get("prompt", "")
            if prompt:
                if len(prompt) > 500:
                    prompt = prompt[:500] + "..."
                lines.append(f"\n> {prompt}")
        elif name in ("WebSearch", "WebFetch"):
            query = inp.get("query", inp.get("url", ""))
            lines.append(f"`{query}`")
        else:
            if inp:
                lines.append(f"```json\n{json.dumps(inp, indent=2)[:500]}\n```")

        return "\n".join(lines)

    @classmethod
    def format_tool_result(cls, block: dict) -> str:
        content = block.get("content", "")
        is_error = block.get("is_error", False)

        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item["text"])
                elif isinstance(item, dict) and item.get("type") == "image":
                    parts.append("*[image]*")
                else:
                    parts.append(str(item))
            content = "\n".join(parts)

        if not content:
            return ""

        prefix = "**Error:**\n" if is_error else ""
        lines = content.split("\n")
        if len(lines) > 50:
            content = (
                "\n".join(lines[:25])
                + f"\n\n... ({len(lines) - 50} lines omitted) ...\n\n"
                + "\n".join(lines[-25:])
            )

        return f"{prefix}```\n{content}\n```"

    @classmethod
    def format_content_blocks(
        cls,
        content: list | str,
        skip_tool_results: bool = False,
    ) -> str:
        if isinstance(content, str):
            return content

        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif not isinstance(block, dict):
                continue
            elif block.get("type") == "text":
                text = cls.clean_text(block["text"])
                if text:
                    parts.append(text)
            elif block.get("type") == "tool_use":
                parts.append(cls.format_tool_use(block))
            elif block.get("type") == "tool_result" and not skip_tool_results:
                r = cls.format_tool_result(block)
                if r:
                    parts.append(r)

        return "\n\n".join(parts)

    @classmethod
    def is_pure_tool_result(cls, content: list | str) -> bool:
        """Return True if the content consists entirely of tool_result blocks."""
        return (
            isinstance(content, list)
            and bool(content)
            and all(
                isinstance(b, dict) and b.get("type") == "tool_result"
                for b in content
            )
        )


class SubagentConversation:
    """A subagent conversation loaded from a sidechain JSONL file."""

    def __init__(
        self,
        agent_id: str,
        jsonl_path: Path,
        description: str = "",
        agent_type: str = "",
    ) -> None:
        self.agent_id = agent_id
        self.jsonl_path = jsonl_path
        self.description = description
        self.agent_type = agent_type

    def _load_messages(self) -> list[dict]:
        messages = []
        with open(self.jsonl_path) as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") in ("user", "assistant"):
                    messages.append(obj)
        return messages

    def to_markdown(self, include_tool_results: bool = True) -> str:
        desc = self.description or "Subagent"
        atype = self.agent_type or "unknown"
        lines = [f"#### Subagent: {desc}", f"*Type: {atype}*\n"]

        for msg in self._load_messages():
            role = msg.get("type", "")
            content = msg.get("message", {}).get("content", [])

            if role == "user" and ContentFormatter.is_pure_tool_result(content):
                if include_tool_results:
                    formatted = ContentFormatter.format_content_blocks(content)
                    if formatted.strip():
                        lines.append(
                            f"<details><summary>Tool Result</summary>\n\n{formatted}\n\n</details>\n"
                        )
                continue

            formatted = ContentFormatter.format_content_blocks(
                content, skip_tool_results=not include_tool_results
            )
            if not formatted.strip():
                continue

            if role == "user":
                lines.append(f"**Prompt:**\n\n{formatted}\n")
            elif role == "assistant":
                lines.append(f"{formatted}\n")

        return "\n".join(lines)


class Session:
    """A Claude Code session, with support for nested subagent conversations."""

    def __init__(self, info: SessionInfo) -> None:
        self.info = info

    def _load_all_records(self) -> list[dict]:
        records = []
        with open(self.info.path) as f:
            for line in f:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return records

    def _build_subagents(self, records: list[dict]) -> dict[str, SubagentConversation]:
        """Build tool_use_id -> SubagentConversation from progress records.

        Progress records in the main JSONL carry ``parentToolUseID`` (the id of
        the Task/Agent tool_use that spawned the subagent) and
        ``data.agentId`` (e.g. "abc8bed").  The subagent's own JSONL lives at
        subagents/agent-{agentId}.jsonl.
        """
        if not self.info.subagent_dir:
            return {}

        # parentToolUseID -> agentId  (first occurrence wins)
        tool_use_to_agent: dict[str, str] = {}
        for record in records:
            if record.get("type") == "progress":
                parent_id = record.get("parentToolUseID")
                agent_id = record.get("data", {}).get("agentId")
                if parent_id and agent_id and parent_id not in tool_use_to_agent:
                    tool_use_to_agent[parent_id] = agent_id

        # tool_use_id -> (description, subagent_type) from assistant tool_use blocks
        tool_use_meta: dict[str, tuple[str, str]] = {}
        for record in records:
            if record.get("type") == "assistant":
                content = record.get("message", {}).get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if (
                            isinstance(block, dict)
                            and block.get("type") == "tool_use"
                            and block.get("name") in _AGENT_TOOL_NAMES
                        ):
                            tid = block["id"]
                            inp = block.get("input", {})
                            tool_use_meta[tid] = (
                                inp.get("description", ""),
                                inp.get("subagent_type", "general-purpose"),
                            )

        subagents: dict[str, SubagentConversation] = {}
        for tool_use_id, agent_id in tool_use_to_agent.items():
            jsonl_path = self.info.subagent_dir / f"agent-{agent_id}.jsonl"
            if not jsonl_path.exists():
                continue
            desc, atype = tool_use_meta.get(tool_use_id, ("", ""))
            subagents[tool_use_id] = SubagentConversation(
                agent_id=agent_id,
                jsonl_path=jsonl_path,
                description=desc,
                agent_type=atype,
            )

        return subagents

    def _render_header(self) -> list[str]:
        title = self.info.title or "Untitled Session"
        ts = self.info.timestamp or ""
        if ts:
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                ts = dt.strftime("%Y-%m-%d %H:%M UTC")
            except ValueError:
                pass

        lines = [
            f"# {title}",
            "",
            f"**Session:** `{self.info.session_id}`  ",
            f"**Project:** `{self.info.display_project}`  ",
        ]
        if ts:
            lines.append(f"**Date:** {ts}  ")
        lines.extend(["", "---", ""])
        return lines

    def to_markdown(
        self,
        include_subagents: bool = True,
        include_tool_results: bool = True,
    ) -> str:
        records = self._load_all_records()
        subagents = self._build_subagents(records) if include_subagents else {}
        messages = [r for r in records if r.get("type") in ("user", "assistant")]

        lines = self._render_header()

        for msg in messages:
            role = msg.get("type", "")
            content = msg.get("message", {}).get("content", [])

            if role == "user":
                if ContentFormatter.is_pure_tool_result(content):
                    if include_tool_results:
                        formatted = ContentFormatter.format_content_blocks(content)
                        if formatted.strip():
                            lines.append(
                                f"<details><summary>Tool Result</summary>\n\n{formatted}\n\n</details>\n"
                            )
                    continue

                formatted = ContentFormatter.format_content_blocks(
                    content, skip_tool_results=not include_tool_results
                )
                if formatted.strip():
                    lines.append(f"## User\n\n{formatted}\n")

            elif role == "assistant":
                formatted = ContentFormatter.format_content_blocks(
                    content, skip_tool_results=not include_tool_results
                )
                if formatted.strip():
                    lines.append(f"## Assistant\n\n{formatted}\n")

                # Insert subagent conversations right after the message that spawned them
                if include_subagents and isinstance(content, list):
                    for block in content:
                        if (
                            isinstance(block, dict)
                            and block.get("type") == "tool_use"
                            and block.get("name") in _AGENT_TOOL_NAMES
                        ):
                            tid = block.get("id", "")
                            if tid in subagents:
                                lines.append(
                                    "<details><summary>Subagent Conversation</summary>\n"
                                )
                                lines.append(subagents[tid].to_markdown(include_tool_results))
                                lines.append("</details>\n")

        return "\n".join(lines)


class SessionDiscoverer:
    """Discovers and indexes Claude Code sessions on disk."""

    def __init__(self, projects_dir: Path = PROJECTS_DIR) -> None:
        self.projects_dir = projects_dir

    def discover(
        self,
        project_filter: str | None = None,
        scan_dir: Path | None = None,
    ) -> list[SessionInfo]:
        """Discover sessions.

        If *scan_dir* is given it takes precedence over ``self.projects_dir``.
        The directory is auto-detected:

        * If it directly contains ``*.jsonl`` files it is treated as a single
          **project directory** (one level above session files).
        * Otherwise it is treated as a **projects directory** whose immediate
          subdirectories are project directories (the normal
          ``~/.claude/projects/`` layout).
        """
        base = scan_dir if scan_dir is not None else self.projects_dir
        if not base.exists():
            return []

        # Auto-detect: does the dir itself contain session jsonl files?
        has_jsonl = any(
            f.suffix == ".jsonl" and not f.stem.startswith("agent-")
            for f in base.iterdir()
            if f.is_file()
        )

        project_dirs: list[Path]
        if has_jsonl:
            # The dir IS the project directory
            project_dirs = [base]
        else:
            # The dir contains project subdirectories
            project_dirs = [
                d for d in base.iterdir()
                if d.is_dir()
                and (not project_filter or project_filter.lower() in d.name.lower())
            ]

        sessions: list[SessionInfo] = []
        for project_dir in project_dirs:
            if project_filter and not has_jsonl:
                pass  # already filtered above
            elif project_filter and has_jsonl:
                # When treating a single dir as the project dir, the filter
                # applies to the dir name itself
                if project_filter.lower() not in project_dir.name.lower():
                    continue

            for jsonl_file in project_dir.glob("*.jsonl"):
                sid = jsonl_file.stem
                if sid.startswith("agent-"):
                    continue

                info = SessionInfo(
                    path=jsonl_file,
                    session_id=sid,
                    project_path=project_dir.name,
                )

                subagent_dir = project_dir / sid / "subagents"
                if subagent_dir.is_dir():
                    info.subagent_dir = subagent_dir

                try:
                    with open(jsonl_file) as f:
                        for line in f:
                            obj = json.loads(line)
                            if obj.get("type") == "ai-title":
                                info.title = obj.get("aiTitle")
                            if not info.timestamp and obj.get("timestamp"):
                                info.timestamp = obj["timestamp"]
                            if info.title and info.timestamp:
                                break
                except (json.JSONDecodeError, OSError):
                    pass

                sessions.append(info)

        sessions.sort(key=lambda s: s.timestamp or "", reverse=True)
        return sessions

    def find(self, sessions: list[SessionInfo], query: str) -> SessionInfo | None:
        """Find a session by index, UUID prefix, or title substring."""
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

    def print_table(self, sessions: list[SessionInfo], out: TextIO = sys.stdout) -> None:
        if not sessions:
            print("No sessions found.", file=out)
            return

        print(f"{'#':<4} {'Date':<20} {'ID':<12} {'Title':<50} {'Project'}", file=out)
        print("-" * 120, file=out)
        for i, s in enumerate(sessions, 1):
            ts = ""
            if s.timestamp:
                try:
                    dt = datetime.fromisoformat(s.timestamp.replace("Z", "+00:00"))
                    ts = dt.strftime("%Y-%m-%d %H:%M")
                except ValueError:
                    ts = s.timestamp[:16]
            title = (s.title or "Untitled")[:50]
            sid = s.session_id[:10] + ".."
            proj = s.display_project
            parts = proj.split("/")
            if len(parts) > 3:
                proj = "/".join(parts[-3:])
            print(f"{i:<4} {ts:<20} {sid:<12} {title:<50} {proj}", file=out)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert Claude Code chat sessions to Markdown",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Example: cc2md --list | cc2md --latest -o chat.md | cc2md --all --output-dir ./exports/",
    )
    parser.add_argument(
        "session",
        nargs="?",
        help="Session UUID (prefix), index number from --list, or title substring",
    )
    parser.add_argument("--list", "-l", action="store_true", help="List all sessions")
    parser.add_argument("--latest", action="store_true", help="Convert the most recent session")
    parser.add_argument("--all", action="store_true", help="Convert all sessions")
    parser.add_argument("--project", "-p", help="Filter by project path substring")
    parser.add_argument("--output", "-o", help="Output file path (default: stdout)")
    parser.add_argument("--no-subagents", action="store_true", help="Exclude subagent conversations")
    parser.add_argument("--no-tool-results", action="store_true", help="Exclude tool results")
    parser.add_argument("--output-dir", "-d", help="Output directory (for --all mode)")
    parser.add_argument(
        "--dir",
        metavar="PATH",
        help=(
            "Directory to scan for sessions instead of ~/.claude/projects/. "
            "Auto-detected: if it directly contains *.jsonl files it is treated "
            "as a project directory; otherwise as a projects directory."
        ),
    )

    args = parser.parse_args()

    scan_dir = Path(args.dir) if args.dir else None
    discoverer = SessionDiscoverer()
    sessions = discoverer.discover(project_filter=args.project, scan_dir=scan_dir)

    if args.list:
        discoverer.print_table(sessions)
        return

    if not args.session and not args.latest and not args.all:
        parser.print_help()
        print("\nUse --list to see available sessions.", file=sys.stderr)
        sys.exit(1)

    include_subagents = not args.no_subagents
    include_tool_results = not args.no_tool_results

    if args.all:
        out_dir = Path(args.output_dir) if args.output_dir else Path(".")
        out_dir.mkdir(parents=True, exist_ok=True)
        for info in sessions:
            md = Session(info).to_markdown(include_subagents, include_tool_results)
            ts = ""
            if info.timestamp:
                try:
                    dt = datetime.fromisoformat(info.timestamp.replace("Z", "+00:00"))
                    ts = dt.strftime("%Y-%m-%d")
                except ValueError:
                    pass
            safe_title = (info.title or "untitled").replace(" ", "-").replace("/", "-")[:50]
            filename = f"{ts}-{safe_title}.md" if ts else f"{info.session_id[:8]}-{safe_title}.md"
            out_path = out_dir / filename
            out_path.write_text(md)
            print(f"Wrote {out_path}", file=sys.stderr)
        return

    if args.latest:
        if not sessions:
            print("No sessions found.", file=sys.stderr)
            sys.exit(1)
        info = sessions[0]
    else:
        info = discoverer.find(sessions, args.session)
        if not info:
            print(f"Session not found: {args.session}", file=sys.stderr)
            print("Use --list to see available sessions.", file=sys.stderr)
            sys.exit(1)

    md = Session(info).to_markdown(include_subagents, include_tool_results)

    if args.output:
        Path(args.output).write_text(md)
        print(f"Wrote {args.output}", file=sys.stderr)
    else:
        print(md)


if __name__ == "__main__":
    main()
