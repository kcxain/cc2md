from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from .sources.base import SessionMeta
from .sources.claude_code import ClaudeCodeSource
from .formats.markdown import MarkdownFormat


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


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert Claude Code chat sessions to Markdown",
        epilog="Examples:\n  cc2md --list\n  cc2md --latest -o chat.md\n  cc2md --all -d ./exports/\n  cc2md /path/to/session.jsonl",
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
    parser.add_argument("--project", "-p", help="Filter sessions by project path substring")
    parser.add_argument(
        "--dir",
        metavar="PATH",
        help=(
            "Directory to scan instead of ~/.claude/projects/. "
            "Auto-detected: contains *.jsonl → project dir; "
            "otherwise → projects dir (subdirs are project dirs)."
        ),
    )
    parser.add_argument("--output", "-o", help="Output file path (default: stdout)")
    parser.add_argument("--output-dir", "-d", help="Output directory (for --all mode)")
    parser.add_argument("--no-subagents", action="store_true", help="Exclude subagent conversations")
    parser.add_argument("--no-tool-results", action="store_true", help="Exclude tool call results")

    args = parser.parse_args()

    source = ClaudeCodeSource(
        scan_dir=Path(args.dir) if args.dir else None,
        project_filter=args.project,
    )
    fmt = MarkdownFormat(
        include_subagents=not args.no_subagents,
        include_tool_results=not args.no_tool_results,
    )

    # Direct file path — skip discovery entirely
    if args.session and Path(args.session).is_file():
        session = source.load_file(Path(args.session))
        md = fmt.render(session)
        if args.output:
            Path(args.output).write_text(md)
            print(f"Wrote {args.output}", file=sys.stderr)
        else:
            print(md)
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
            md = fmt.render(session)
            ts = ""
            if meta.timestamp:
                try:
                    dt = datetime.fromisoformat(meta.timestamp.replace("Z", "+00:00"))
                    ts = dt.strftime("%Y-%m-%d")
                except ValueError:
                    pass
            safe_title = (meta.title or "untitled").replace(" ", "-").replace("/", "-")[:50]
            ext = fmt.file_extension
            filename = f"{ts}-{safe_title}.{ext}" if ts else f"{meta.session_id[:8]}-{safe_title}.{ext}"
            out_path = out_dir / filename
            out_path.write_text(md)
            print(f"Wrote {out_path}", file=sys.stderr)
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
    md = fmt.render(session)

    if args.output:
        Path(args.output).write_text(md)
        print(f"Wrote {args.output}", file=sys.stderr)
    else:
        print(md)


if __name__ == "__main__":
    main()
