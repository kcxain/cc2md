# cc2md

Convert [Claude Code](https://docs.anthropic.com/en/docs/claude-code) or Codex chat sessions to Markdown.

## Install

```bash
pip install cc2md
```

## Quick Start

```bash
# Extract the most recent session
cc2md --latest -o log

# Extract the most recent Codex session
cc2md --agent codex --latest -o log
```

If the session spawned subagents, output is a directory:

```
log/
  index.md                        # main conversation, with links to subagents
  reduce-min-host-层实现-a46d2ef.md
  reduce-min-device-层实现-a95f57b.md
  ...
```

Otherwise, a single `log.md` is written.

## Usage

```bash
cc2md --list                        # list all sessions
cc2md --latest -o log               # most recent session
cc2md --agent codex --latest -o log # most recent Codex session
cc2md 1 -o log                      # by index from --list
cc2md a1b2c3 -o log                 # by UUID prefix
cc2md "auth middleware" -o log      # by title substring
cc2md --all -d ./exports/           # export everything
cc2md --latest -p myapp -o log      # filter by project
```

### Options

| Flag | Description |
|---|---|
| `--list`, `-l` | List available sessions |
| `--latest` | Most recent session |
| `--all` | Convert all sessions |
| `--agent` | Source backend: `claude` or `codex` |
| `--project`, `-p` | Filter by project path substring |
| `--dir` | Scan a custom directory instead of the default source directory |
| `--output`, `-o` | Output path |
| `--output-dir`, `-d` | Output directory for `--all` |
| `--no-subagents` | Exclude subagent conversations |
| `--no-tool-results` | Exclude tool call results |

## Requirements

Python 3.10+, no external dependencies.
