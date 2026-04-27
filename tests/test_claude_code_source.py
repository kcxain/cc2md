from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cc2md.formats.markdown import MarkdownFormat
from cc2md.models import Message, Session, ToolResultBlock, ToolUseBlock
from cc2md.sources.claude_code import ClaudeCodeSource
from cc2md.sources.codex import CodexSource


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


class ClaudeCodeSourceResolveFileTests(unittest.TestCase):
    def test_resolve_file_returns_main_session_for_top_level_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "demo-project"
            main_jsonl = project_dir / "rollout-main.jsonl"
            _write_jsonl(
                main_jsonl,
                [
                    {"type": "user", "timestamp": "2026-04-23T10:00:00Z", "message": {"content": "hi"}},
                    {"type": "ai-title", "aiTitle": "Main Session"},
                ],
            )

            source = ClaudeCodeSource(scan_dir=project_dir)
            meta = source.resolve_file(main_jsonl)

            self.assertIsNotNone(meta)
            assert meta is not None
            self.assertEqual(meta.session_id, "rollout-main")
            self.assertEqual(meta.ref["jsonl"].resolve(), main_jsonl.resolve())

    def test_resolve_file_returns_main_session_for_subagent_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_dir = Path(tmp) / "demo-project"
            main_jsonl = project_dir / "rollout-main.jsonl"
            subagent_jsonl = project_dir / "rollout-main" / "subagents" / "agent-sub-123.jsonl"

            _write_jsonl(
                main_jsonl,
                [
                    {"type": "user", "timestamp": "2026-04-23T10:00:00Z", "message": {"content": "hi"}},
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "toolu_1",
                                    "name": "Task",
                                    "input": {"description": "Investigate", "subagent_type": "general-purpose"},
                                }
                            ]
                        },
                    },
                    {
                        "type": "progress",
                        "parentToolUseID": "toolu_1",
                        "data": {"agentId": "sub-123"},
                    },
                    {"type": "ai-title", "aiTitle": "Main Session"},
                ],
            )
            _write_jsonl(
                subagent_jsonl,
                [
                    {"type": "user", "timestamp": "2026-04-23T10:01:00Z", "message": {"content": "work on it"}},
                    {"type": "assistant", "message": {"content": "done"}},
                ],
            )

            source = ClaudeCodeSource(scan_dir=project_dir)
            meta = source.resolve_file(subagent_jsonl)
            session = source.load(meta) if meta is not None else None

            self.assertIsNotNone(meta)
            assert meta is not None
            self.assertEqual(meta.session_id, "rollout-main")
            self.assertEqual(meta.ref["jsonl"].resolve(), main_jsonl.resolve())
            self.assertIsNotNone(session)
            assert session is not None
            self.assertEqual(set(session.subconversations), {"toolu_1"})

class CodexSourceResolveFileTests(unittest.TestCase):
    def test_resolve_codex_subagent_rollout_returns_main_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp)
            main_path = session_dir / "rollout-2026-04-23T16-53-25-019db98b-4563-75b0-9cab-92b560524710.jsonl"
            sub_path = session_dir / "rollout-2026-04-23T16-54-32-019db98c-49b6-7590-a3c9-ce0cf1ad88c7.jsonl"

            _write_jsonl(
                main_path,
                [
                    {
                        "timestamp": "2026-04-23T08:53:25Z",
                        "type": "session_meta",
                        "payload": {
                            "id": "019db98b-4563-75b0-9cab-92b560524710",
                            "timestamp": "2026-04-23T08:53:25Z",
                            "cwd": "/tmp/demo",
                            "source": "cli",
                        },
                    },
                    {
                        "timestamp": "2026-04-23T08:54:32Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "spawn_agent",
                            "arguments": "{\"message\":\"任务1\"}",
                            "call_id": "call_spawn_1",
                        },
                    },
                    {
                        "timestamp": "2026-04-23T08:54:32Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "collab_agent_spawn_end",
                            "call_id": "call_spawn_1",
                            "new_thread_id": "019db98c-49b6-7590-a3c9-ce0cf1ad88c7",
                            "new_agent_role": "explorer",
                            "prompt": "任务1",
                        },
                    },
                    {
                        "timestamp": "2026-04-23T08:54:33Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "主线程内容"}],
                        },
                    },
                ],
            )
            _write_jsonl(
                sub_path,
                [
                    {
                        "timestamp": "2026-04-23T08:54:33Z",
                        "type": "session_meta",
                        "payload": {
                            "id": "019db98c-49b6-7590-a3c9-ce0cf1ad88c7",
                            "timestamp": "2026-04-23T08:54:32Z",
                            "cwd": "/tmp/demo",
                            "source": {
                                "subagent": {
                                    "thread_spawn": {
                                        "parent_thread_id": "019db98b-4563-75b0-9cab-92b560524710"
                                    }
                                }
                            },
                        },
                    },
                    {
                        "timestamp": "2026-04-23T08:54:34Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "子线程内容"}],
                        },
                    },
                ],
            )

            source = CodexSource(scan_dir=session_dir)
            meta = source.resolve_file(sub_path)
            session = source.load(meta) if meta is not None else None

            self.assertIsNotNone(meta)
            assert meta is not None
            self.assertEqual(meta.session_id, "019db98b-4563-75b0-9cab-92b560524710")
            self.assertEqual(meta.ref["rollout"].resolve(), main_path.resolve())
            self.assertIsNotNone(session)
            assert session is not None
            self.assertEqual(set(session.subconversations), {"call_spawn_1"})
            self.assertEqual(
                session.subconversations["call_spawn_1"].messages[0].blocks[0].text,
                "子线程内容",
            )

    def test_load_file_parses_multiline_function_call_output_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp)
            rollout = session_dir / "rollout-2026-04-23T16-53-25-019db98b-4563-75b0-9cab-92b560524710.jsonl"

            rollout.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "timestamp": "2026-04-23T08:53:25Z",
                                "type": "session_meta",
                                "payload": {
                                    "id": "019db98b-4563-75b0-9cab-92b560524710",
                                    "timestamp": "2026-04-23T08:53:25Z",
                                    "cwd": "/tmp/demo",
                                    "source": "cli",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-04-23T08:53:26Z",
                                "type": "response_item",
                                "payload": {
                                    "type": "function_call",
                                    "name": "exec_command",
                                    "arguments": json.dumps({"cmd": "echo hi"}),
                                    "call_id": "call_1",
                                },
                            }
                        ),
                        # Simulate a malformed "jsonl" producer that writes a raw
                        # multi-line tool output into a single JSON object.
                        (
                            '{"timestamp":"2026-04-23T08:53:27Z","type":"response_item",'
                            '"payload":{"type":"function_call_output","call_id":"call_1",'
                            '"output":"Chunk ID: abc123\n'
                            'Wall time: 0.0000 seconds\n'
                            'Output:\n'
                            'first line\n'
                            'second line\n'
                            '"}}'
                        ),
                        json.dumps(
                            {
                                "timestamp": "2026-04-23T08:53:28Z",
                                "type": "response_item",
                                "payload": {
                                    "type": "message",
                                    "role": "assistant",
                                    "content": [{"type": "output_text", "text": "done"}],
                                },
                            }
                        ),
                    ]
                )
                + "\n"
            )

            source = CodexSource(scan_dir=session_dir)
            session = source.load_file(rollout)

            tool_result_messages = [
                msg for msg in session.messages if msg.role == "user" and msg.is_tool_result_only
            ]
            self.assertEqual(len(tool_result_messages), 1)
            self.assertIn("first line", tool_result_messages[0].blocks[0].content)
            self.assertIn("second line", tool_result_messages[0].blocks[0].content)

    def test_load_file_preserves_process_running_status_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            session_dir = Path(tmp)
            rollout = session_dir / "rollout-2026-04-23T16-53-25-019db98b-4563-75b0-9cab-92b560524710.jsonl"

            _write_jsonl(
                rollout,
                [
                    {
                        "timestamp": "2026-04-23T08:53:25Z",
                        "type": "session_meta",
                        "payload": {
                            "id": "019db98b-4563-75b0-9cab-92b560524710",
                            "timestamp": "2026-04-23T08:53:25Z",
                            "cwd": "/tmp/demo",
                            "source": "cli",
                        },
                    },
                    {
                        "timestamp": "2026-04-23T08:53:26Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "name": "exec_command",
                            "arguments": json.dumps({"cmd": "sleep 1"}),
                            "call_id": "call_1",
                        },
                    },
                    {
                        "timestamp": "2026-04-23T08:53:27Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "call_1",
                            "output": (
                                "Chunk ID: abc123\n"
                                "Wall time: 1.0014 seconds\n"
                                "Process running with session ID 40962\n"
                                "Original token count: 0\n"
                                "Output:\n"
                            ),
                        },
                    },
                ],
            )

            source = CodexSource(scan_dir=session_dir)
            session = source.load_file(rollout)

            tool_result_messages = [
                msg for msg in session.messages if msg.role == "user" and msg.is_tool_result_only
            ]
            self.assertEqual(len(tool_result_messages), 1)
            self.assertEqual(
                tool_result_messages[0].blocks[0].content,
                "Process running with session ID 40962",
            )


class MarkdownFormatTests(unittest.TestCase):
    def test_render_preserves_full_tool_result_content(self) -> None:
        tool_output = "\n".join(f"line {index}" for index in range(80))
        session = Session(
            session_id="session-1",
            project="/tmp/demo",
            title="Demo",
            timestamp="2026-04-23T08:53:25Z",
            messages=[
                Message(
                    role="assistant",
                    blocks=[ToolUseBlock(id="call_1", name="exec_command", input={"cmd": "echo hi"})],
                ),
                Message(
                    role="user",
                    blocks=[ToolResultBlock(tool_use_id="call_1", content=tool_output)],
                ),
            ],
        )

        rendered = MarkdownFormat().render(session).files["index.md"]

        self.assertIn("line 0", rendered)
        self.assertIn("line 79", rendered)
        self.assertNotIn("lines omitted", rendered)

    def test_render_preserves_full_patch_diff_content(self) -> None:
        patch = "\n".join(f"+added line {index}" for index in range(80))
        session = Session(
            session_id="session-1",
            project="/tmp/demo",
            title="Demo",
            timestamp="2026-04-23T08:53:25Z",
            messages=[
                Message(
                    role="assistant",
                    blocks=[
                        ToolUseBlock(
                            id="call_1",
                            name="apply_patch",
                            input={"patch": patch},
                        )
                    ],
                )
            ],
        )

        rendered = MarkdownFormat().render(session).files["index.md"]

        self.assertIn("+added line 0", rendered)
        self.assertIn("+added line 79", rendered)
        self.assertNotIn("lines omitted", rendered)


if __name__ == "__main__":
    unittest.main()
