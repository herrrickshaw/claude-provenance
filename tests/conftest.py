"""Shared fixtures: a synthetic ~/.claude tree and a claude.ai export in tmp_path."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import provenance_log as pl  # noqa: E402


def _write_jsonl(path: Path, records: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        for rec in records:
            fh.write(rec if isinstance(rec, str) else json.dumps(rec))
            fh.write("\n")


@pytest.fixture
def claude_tree(tmp_path: Path) -> dict:
    """Build a fake ~/.claude with one project, one main session, one subagent."""
    projects = tmp_path / "projects"
    main_session = [
        {"type": "custom-title", "sessionId": "sess-1", "customTitle": "My title"},
        {
            "type": "user", "sessionId": "sess-1", "uuid": "u1", "parentUuid": None,
            "timestamp": "2026-07-01T10:00:00.000Z", "cwd": "/w", "gitBranch": "main",
            "version": "2.1.0", "entrypoint": "cli", "isSidechain": False,
            "promptSource": "user",
            "message": {"role": "user", "content": "hello world"},
        },
        {
            "type": "assistant", "sessionId": "sess-1", "uuid": "a1", "parentUuid": "u1",
            "timestamp": "2026-07-01T10:00:05.000Z", "requestId": "req-1",
            "message": {
                "role": "assistant", "model": "claude-test-1",
                "content": [
                    {"type": "thinking", "thinking": "hmm"},
                    {"type": "text", "text": "hi"},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                    {"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}},
                ],
            },
        },
        {
            "type": "system", "sessionId": "sess-1", "uuid": "s1", "parentUuid": "a1",
            "timestamp": "2026-07-01T10:00:06.000Z", "subtype": "compact_boundary",
            "message": {},
        },
        {"type": "progress", "sessionId": "sess-1", "uuid": "p1"},
        "this is not json",
        "",
    ]
    _write_jsonl(projects / "proj-a" / "sess-1.jsonl", main_session)
    subagent = [
        {
            "type": "user", "sessionId": "sess-1", "uuid": "su1", "parentUuid": None,
            "timestamp": "2026-07-01T10:01:00.000Z",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "content": [{"type": "text", "text": "result"}]}],
            },
        },
    ]
    _write_jsonl(projects / "proj-a" / "sess-1" / "subagents" / "agent-x.jsonl", subagent)

    history = tmp_path / "history.jsonl"
    _write_jsonl(history, [
        {"display": "first prompt", "timestamp": 1751364000000, "project": "/w", "sessionId": "sess-1"},
        {"display": "second prompt", "timestamp": 1751364001, "project": "/w", "sessionId": "sess-2"},
        {"display": "string ts", "timestamp": "2026-07-01T10:00:02+00:00", "project": "/w"},
        "{bad json",
    ])

    export = tmp_path / "export"
    export.mkdir()
    with open(export / "conversations.json", "w") as fh:
        json.dump([
            {
                "uuid": "conv-1", "name": "Web chat",
                "chat_messages": [
                    {"uuid": "m1", "parent_message_uuid": None, "sender": "human",
                     "text": "question", "created_at": "2026-07-02T00:00:00Z"},
                    {"uuid": "m2", "parent_message_uuid": "m1", "sender": "assistant",
                     "text": "answer", "created_at": "2026-07-02T00:00:01Z"},
                ],
            },
        ], fh)
    (export / "design_chats").mkdir()
    with open(export / "design_chats" / "d1.json", "w") as fh:
        json.dump({
            "uuid": "design-1", "title": "Design chat",
            "messages": [
                {"uuid": "dm1", "role": "user", "created_at": "2026-07-03T00:00:00Z",
                 "content": [{"type": "text", "text": "make it blue"}]},
                {"uuid": "dm2", "role": "assistant", "created_at": "2026-07-03T00:00:01Z",
                 "content": "done"},
                {"uuid": "dm3", "role": "assistant", "created_at": "2026-07-03T00:00:02Z",
                 "content": {"weird": 1}},
            ],
        }, fh)

    return {
        "base": tmp_path,
        "projects": projects,
        "history": history,
        "export": export,
        "db": tmp_path / "provenance.db",
    }


@pytest.fixture
def patched_env(monkeypatch, claude_tree):
    """Point the module's path constants at the synthetic tree."""
    monkeypatch.setattr(pl, "BASE", claude_tree["base"])
    monkeypatch.setattr(pl, "DB_PATH", claude_tree["db"])
    monkeypatch.setattr(pl, "PROJECTS_DIR", claude_tree["projects"])
    monkeypatch.setattr(pl, "HISTORY_FILE", claude_tree["history"])
    return claude_tree


def run_cli(monkeypatch, *argv: str) -> None:
    monkeypatch.setattr(sys, "argv", ["provenance_log.py", *argv])
    pl.main()
