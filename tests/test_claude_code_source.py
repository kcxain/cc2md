from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
