"""Characterisation tests: end-to-end ingest into a temp SQLite DB."""

from __future__ import annotations

import sqlite3
import subprocess
import sys

from conftest import REPO_ROOT, run_cli


def _rows(db, sql, *params):
    con = sqlite3.connect(db)
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def test_cli_help_runs():
    out = subprocess.run(
        [sys.executable, str(REPO_ROOT / "provenance_log.py"), "--help"],
        capture_output=True, text=True, check=True,
    )
    assert "--claude-ai-export" in out.stdout
    assert "--summary" in out.stdout
    assert "--full" in out.stdout


def test_ingest_counts_by_source(patched_env, monkeypatch, capsys):
    run_cli(monkeypatch, "--claude-ai-export", str(patched_env["export"]))
    db = patched_env["db"]
    counts = dict(_rows(db, "SELECT source, COUNT(*) FROM messages GROUP BY source"))
    assert counts == {
        "claude-code": 3,
        "claude-code-subagent": 1,
        "prompt-history": 3,
        "claude-ai-export": 5,
    }
    out = capsys.readouterr().out
    assert "Parsed 5 files (0 unchanged, skipped). Ingested 12 messages this run." in out
    assert "Database now holds 12 messages across 4 sessions" in out


def test_transcript_fields(patched_env, monkeypatch):
    run_cli(monkeypatch)
    db = patched_env["db"]
    (row,) = _rows(db, """
        SELECT src_line, project, session_id, msg_uuid, parent_uuid, record_type, role,
               model, request_id, ts, cwd, git_branch, cli_version, entrypoint,
               is_sidechain, prompt_source, tool_names, content_chars, content_preview
        FROM messages WHERE msg_uuid='a1'""")
    assert row[:9] == (3, "proj-a", "sess-1", "a1", "u1", "assistant", "assistant",
                       "claude-test-1", "req-1")
    assert row[9] == "2026-07-01T10:00:05.000Z"
    assert row[10:16] == (None, None, None, None, 0, None)
    assert row[16] == "Bash,Read"
    assert row[18] == 'hmm\nhi\n{"command": "ls"}\n{"file_path": "/x"}'
    assert row[17] == len(row[18])

    (user,) = _rows(db, "SELECT cwd, git_branch, cli_version, entrypoint, is_sidechain, "
                        "prompt_source, tool_names, content_sha256 FROM messages WHERE msg_uuid='u1'")
    assert user[:7] == ("/w", "main", "2.1.0", "cli", 0, "user", None)
    assert len(user[7]) == 64

    (system,) = _rows(db, "SELECT content_preview, content_chars, content_sha256 "
                          "FROM messages WHERE msg_uuid='s1'")
    assert system[:2] == ("compact_boundary", 16)
    assert len(system[2]) == 64

    # non-kept record types and junk lines are skipped, but line numbers are preserved
    assert _rows(db, "SELECT COUNT(*) FROM messages WHERE msg_uuid='p1'") == [(0,)]
    (sf,) = _rows(db, "SELECT lines_parsed, messages_kept FROM source_files WHERE path LIKE '%sess-1.jsonl'")
    assert sf == (7, 3)


def test_subagent_tool_result_text(patched_env, monkeypatch):
    run_cli(monkeypatch)
    (row,) = _rows(patched_env["db"],
                   "SELECT source, content_preview, role FROM messages WHERE msg_uuid='su1'")
    assert row == ("claude-code-subagent", "result", "user")


def test_history_timestamp_normalisation(patched_env, monkeypatch):
    run_cli(monkeypatch)
    rows = _rows(patched_env["db"],
                 "SELECT src_line, ts, record_type, role, project, cwd, session_id, content_preview "
                 "FROM messages WHERE source='prompt-history' ORDER BY src_line")
    assert rows == [
        (1, "2025-07-01T10:00:00+00:00", "prompt", "user", "/w", "/w", "sess-1", "first prompt"),
        (2, "2025-07-01T10:00:01+00:00", "prompt", "user", "/w", "/w", "sess-2", "second prompt"),
        (3, "2026-07-01T10:00:02+00:00", "prompt", "user", "/w", "/w", None, "string ts"),
    ]


def test_claude_ai_export_rows(patched_env, monkeypatch):
    run_cli(monkeypatch, "--claude-ai-export", str(patched_env["export"]))
    db = patched_env["db"]
    rows = _rows(db, "SELECT src_line, project, session_id, msg_uuid, parent_uuid, record_type, "
                     "role, ts, content_preview FROM messages WHERE source='claude-ai-export' "
                     "ORDER BY project, src_line")
    assert rows == [
        (1, "claude.ai", "conv-1", "m1", None, "chat", "human", "2026-07-02T00:00:00Z", "question"),
        (2, "claude.ai", "conv-1", "m2", "m1", "chat", "assistant", "2026-07-02T00:00:01Z", "answer"),
        (1, "claude.ai-design", "design-1", "dm1", None, "chat", "user", "2026-07-03T00:00:00Z", "make it blue"),
        (2, "claude.ai-design", "design-1", "dm2", None, "chat", "assistant", "2026-07-03T00:00:01Z", "done"),
        (3, "claude.ai-design", "design-1", "dm3", None, "chat", "assistant", "2026-07-03T00:00:02Z", '{"weird": 1}'),
    ]
    titles = dict(_rows(db, "SELECT session_id, title FROM sessions WHERE title IS NOT NULL"))
    assert titles == {"sess-1": "My title", "conv-1": "Web chat", "design-1": "Design chat"}


def test_claude_ai_export_accepts_file_path(patched_env, monkeypatch):
    conv = patched_env["export"] / "conversations.json"
    run_cli(monkeypatch, "--claude-ai-export", str(conv))
    counts = dict(_rows(patched_env["db"], "SELECT project, COUNT(*) FROM messages "
                                           "WHERE source='claude-ai-export' GROUP BY project"))
    # sibling design_chats/ is picked up from the file's parent directory too
    assert counts == {"claude.ai": 2, "claude.ai-design": 3}


def test_session_stats(patched_env, monkeypatch):
    run_cli(monkeypatch, "--claude-ai-export", str(patched_env["export"]))
    rows = _rows(patched_env["db"],
                 "SELECT session_id, project, first_ts, last_ts, n_user, n_assistant "
                 "FROM sessions ORDER BY session_id")
    assert rows == [
        ("conv-1", "claude.ai", "2026-07-02T00:00:00Z", "2026-07-02T00:00:01Z", 1, 1),
        ("design-1", "claude.ai-design", "2026-07-03T00:00:00Z", "2026-07-03T00:00:02Z", 1, 2),
        ("sess-1", "proj-a", "2025-07-01T10:00:00+00:00", "2026-07-01T10:01:00.000Z", 3, 1),
        ("sess-2", "/w", "2025-07-01T10:00:01+00:00", "2025-07-01T10:00:01+00:00", 1, 0),
    ]


def test_incremental_skip_and_full(patched_env, monkeypatch, capsys):
    run_cli(monkeypatch)
    capsys.readouterr()
    run_cli(monkeypatch)
    assert "Parsed 0 files (2 unchanged, skipped). Ingested 0 messages this run." in capsys.readouterr().out
    assert _rows(patched_env["db"], "SELECT COUNT(*) FROM messages") == [(7,)]
    run_cli(monkeypatch, "--full")
    assert "Parsed 3 files (0 unchanged, skipped). Ingested 7 messages this run." in capsys.readouterr().out
    assert _rows(patched_env["db"], "SELECT COUNT(*) FROM messages") == [(7,)]


def test_changed_file_is_reparsed(patched_env, monkeypatch, capsys):
    run_cli(monkeypatch)
    hist = patched_env["history"]
    with open(hist, "a") as fh:
        fh.write('{"display": "third", "timestamp": 1751364002, "project": "/w"}\n')
    capsys.readouterr()
    run_cli(monkeypatch)
    assert "Parsed 1 files (2 unchanged, skipped). Ingested 4 messages this run." in capsys.readouterr().out
    assert _rows(patched_env["db"], "SELECT COUNT(*) FROM messages WHERE source='prompt-history'") == [(4,)]


def test_summary_written(patched_env, monkeypatch, capsys):
    run_cli(monkeypatch, "--summary", "--claude-ai-export", str(patched_env["export"]))
    out = patched_env["base"] / "PROVENANCE_SUMMARY.md"
    assert f"Summary written -> {out}" in capsys.readouterr().out
    text = out.read_text()
    assert text.startswith("# Claude Message Provenance Log\n\nGenerated: ")
    assert "**12 messages** across **4 sessions**" in text
    assert "| claude-code | 3 | 2026-07-01T10:00:00.000Z | 2026-07-01T10:00:06.000Z |" in text
    assert "| proj-a | 4 |" in text
    assert "| claude-test-1 | 1 |" in text
    assert "| 2026-07-03T00:00:02Z | `design-1` | Design chat | 1 | 2 |" in text


def test_missing_sources_are_tolerated(patched_env, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("provenance_log.PROJECTS_DIR", tmp_path / "nope")
    monkeypatch.setattr("provenance_log.HISTORY_FILE", tmp_path / "nope.jsonl")
    run_cli(monkeypatch)
    assert "Parsed 0 files (0 unchanged, skipped). Ingested 0 messages this run." in capsys.readouterr().out
