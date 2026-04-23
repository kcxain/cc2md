from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from .formats.base import RenderResult
from .formats.markdown import MarkdownFormat
from .models import Session
from .sources.base import SessionMeta
from .sources.claude_code import ClaudeCodeSource
from .sources.codex import CodexSource


def _print_table(sessions: list[SessionMeta]) -> None:
    if not sessions:
        print("No sessions found.")
        return
    print(f"{'#':<4} {'Date':<20} {'ID':<12} {'Title':<50} {'Project'}")
    print("-" * 120)
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
        proj = s.get_display_project()
        parts = proj.split("/")
        if len(parts) > 3:
            proj = "/".join(parts[-3:])
        print(f"{i:<4} {ts:<20} {sid:<12} {title:<50} {proj}")


def _session_stem(meta: SessionMeta) -> str:
    """Derive a filesystem-safe stem from session metadata."""
    ts = ""
    if meta.timestamp:
        try:
            dt = datetime.fromisoformat(meta.timestamp.replace("Z", "+00:00"))
            ts = dt.strftime("%Y-%m-%d")
        except ValueError:
            pass
    safe_title = (meta.title or "untitled").replace(" ", "-").replace("/", "-")[:50]
    return f"{ts}-{safe_title}" if ts else f"{meta.session_id[:8]}-{safe_title}"


def _write_result(result: RenderResult, output: str | None, stem: str, fmt: MarkdownFormat) -> None:
    """Write a RenderResult to disk (single file or directory)."""
    if result.is_single_file:
        path = Path(output) if output else Path(f"{stem}.{fmt.file_extension}")
        path.write_text(result.single_content())
        print(f"Wrote {path}", file=sys.stderr)
    else:
        # Multi-file: write to a directory
        out_dir = Path(output) if output else Path(stem)
        out_dir.mkdir(parents=True, exist_ok=True)
        for rel_path, content in result.files.items():
            dest = out_dir / rel_path
            dest.write_text(content)
        print(f"Wrote {out_dir}/  ({len(result.files)} files)", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert Claude Code or Codex chat sessions to Markdown",
        epilog=(
            "Examples:\n"
            "  cc2md --list\n"
            "  cc2md --latest -o chat.md\n"
            "  cc2md --agent codex --latest -o log\n"
            "  cc2md --all -d ./exports/\n"
            "  cc2md /path/to/session.jsonl"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "session",
        nargs="?",
        help="Session UUID (prefix), index from --list, title substring, or path to a .jsonl file",
    )
    parser.add_argument("--list", "-l", action="store_true", help="List all sessions")
    parser.add_argument("--latest", action="store_true", help="Convert the most recent session")
    parser.add_argument("--all", action="store_true", help="Convert all sessions")
    parser.add_argument(
        "--agent",
        choices=("claude", "codex"),
        default="claude",
        help="Session source backend to read from",
    )
    parser.add_argument("--project", "-p", help="Filter sessions by project path substring")
    parser.add_argument(
        "--dir",
        metavar="PATH",
        help=(
            "Directory to scan instead of the default source directory. "
            "For Claude: project dir or ~/.claude/projects/. "
            "For Codex: ~/.codex/sessions/ or any nested session directory."
        ),
    )
    parser.add_argument(
        "--output", "-o",
        help="Output path. Single file for sessions without subagents; directory for sessions with subagents.",
    )
    parser.add_argument("--output-dir", "-d", help="Output directory (for --all mode)")
    parser.add_argument("--no-subagents", action="store_true", help="Exclude subagent conversations")
    parser.add_argument("--no-tool-results", action="store_true", help="Exclude tool call results")

    args = parser.parse_args()

    source_cls = ClaudeCodeSource if args.agent == "claude" else CodexSource
    source = source_cls(scan_dir=Path(args.dir) if args.dir else None, project_filter=args.project)
    fmt = MarkdownFormat(
        include_subagents=not args.no_subagents,
        include_tool_results=not args.no_tool_results,
    )

    # Direct file path — skip discovery
    if args.session and Path(args.session).is_file():
        path = Path(args.session)
        meta = source.resolve_file(path)
        if meta is not None:
            session = source.load(meta)
            stem = _session_stem(meta)
        else:
            session = source.load_file(path)
            stem = session.session_id[:8]
        result = fmt.render(session)
        if result.is_single_file and not args.output:
            print(result.single_content())
        else:
            _write_result(result, args.output, stem, fmt)
        return

    sessions = source.discover()

    if args.list:
        _print_table(sessions)
        return

    if not args.session and not args.latest and not args.all:
        parser.print_help()
        print("\nUse --list to see available sessions.", file=sys.stderr)
        sys.exit(1)

    if args.all:
        out_dir = Path(args.output_dir) if args.output_dir else Path(".")
        out_dir.mkdir(parents=True, exist_ok=True)
        for meta in sessions:
            session = source.load(meta)
            result = fmt.render(session)
            stem = _session_stem(meta)
            if result.is_single_file:
                dest = out_dir / f"{stem}.{fmt.file_extension}"
                dest.write_text(result.single_content())
                print(f"Wrote {dest}", file=sys.stderr)
            else:
                session_dir = out_dir / stem
                session_dir.mkdir(parents=True, exist_ok=True)
                for rel_path, content in result.files.items():
                    (session_dir / rel_path).write_text(content)
                print(f"Wrote {session_dir}/  ({len(result.files)} files)", file=sys.stderr)
        return

    if args.latest:
        if not sessions:
            print("No sessions found.", file=sys.stderr)
            sys.exit(1)
        meta = sessions[0]
    else:
        meta = source.find(sessions, args.session)
        if not meta:
            print(f"Session not found: {args.session}", file=sys.stderr)
            print("Use --list to see available sessions.", file=sys.stderr)
            sys.exit(1)

    session = source.load(meta)
    result = fmt.render(session)

    if result.is_single_file and not args.output:
        print(result.single_content())
    else:
        _write_result(result, args.output, _session_stem(meta), fmt)


if __name__ == "__main__":
    main()
