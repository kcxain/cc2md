# cc2md

[![PyPI](https://img.shields.io/pypi/v/cc2md)](https://pypi.org/project/cc2md/)
[![License](https://img.shields.io/github/license/kcxain/cc2md)](LICENSE)

Convert [Claude Code](https://docs.anthropic.com/en/docs/claude-code) or Codex chat sessions to Markdown.

## Install

```bash
pip install .
```

## Quick Start

```bash
# Most recent Claude Code session
cc2md --agent claude --latest -o log

# Most recent Codex session
cc2md --agent codex --latest -o log
```

## Usage

```bash
cc2md --list                         # list all sessions
cc2md --latest -o log                # most recent session → log.md (or log/)
cc2md --agent codex --latest -o log  # most recent Codex session
cc2md 1 -o log                       # by index from --list
cc2md a1b2c3 -o log                  # by UUID prefix
cc2md "auth middleware" -o log       # by title substring
cc2md --all -d ./exports/            # export all sessions
cc2md --latest -p myapp -o log       # filter by project
```

If a session spawned subagents, output is a directory:

```
log/
  index.md                          # main conversation, links to subagents
  reduce-min-host-层实现-a46d2ef.md
  reduce-min-device-层实现-a95f57b.md
  ...
```

### Options

| Flag | Description |
|---|---|
| `--list`, `-l` | List available sessions |
| `--latest` | Most recent session |
| `--all` | Convert all sessions |
| `--agent` | Source backend: `claude` (default) or `codex` |
| `--project`, `-p` | Filter by project path substring |
| `--dir` | Custom source directory |
| `--output`, `-o` | Output path |
| `--output-dir`, `-d` | Output directory for `--all` |
| `--no-subagents` | Exclude subagent conversations |
| `--no-tool-results` | Exclude tool call results |

## Output Format

- Tool calls are expanded inline; results are collapsed in `<details>` blocks
- Code edits (`Write`, `Edit`, `MultiEdit`, `apply_patch`, `Delete`, …) render as `diff` blocks

````md
**Tool: exec_command**
```bash
git status --short
```

<details><summary>Result: exec_command</summary>

```
 M README.md
```

</details>
````

````md
**Tool: apply_patch**
Applying patch
```diff
*** Begin Patch
*** Update File: README.md
@@
-old line
+new line
*** End Patch
```

**Result: apply_patch**
```
Success. Updated the following files:
M README.md
```
````

## Requirements

Python 3.10+, no external dependencies.
