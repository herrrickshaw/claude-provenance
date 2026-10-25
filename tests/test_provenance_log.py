"""Tests for provenance_log.py, using synthetic transcripts in tmp_path only."""

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import provenance_log as pl  # noqa: E402


@pytest.fixture
def cur():
    con = sqlite3.connect(":memory:")
    con.executescript(pl.SCHEMA)
    yield con.cursor()
    con.close()


def _write_jsonl(path, records):
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def _rec(i, text, rtype="user", role="user", sid="s1"):
    return {
        "type": rtype, "sessionId": sid, "uuid": f"u{i}",
        "parentUuid": f"u{i-1}" if i else None,
        "timestamp": f"2026-10-01T00:00:0{i}Z",
        "message": {"role": role, "content": text, "model": "m" if role == "assistant" else None},
    }


def _bump_mtime(path):
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 10))


# ---- extract_content -------------------------------------------------------

def test_extract_content_plain_string():
    assert pl.extract_content({"content": "hello"}) == ("hello", [])


def test_extract_content_blocks_collects_text_thinking_tools_and_results():
    msg = {"content": [
        {"type": "text", "text": "a"},
        {"type": "thinking", "thinking": "b"},
        {"type": "tool_use", "name": "Bash", "input": {"cmd": "ls"}},
        {"type": "tool_result", "content": [{"type": "text", "text": "c"}]},
        {"type": "tool_result", "content": "d"},
        "not-a-dict",
    ]}
    text, tools = pl.extract_content(msg)
    assert tools == ["Bash"]
    assert text.split("\n") == ["a", "b", '{"cmd": "ls"}', "c", "d"]


def test_extract_content_non_dict_is_empty():
    assert pl.extract_content(None) == ("", [])
    assert pl.extract_content({"content": None}) == ("", [])


# ---- ingest_transcript -----------------------------------------------------

def test_ingest_transcript_keeps_only_message_types_and_hashes(cur, tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [
        _rec(0, "hi"),
        {"type": "custom-title", "sessionId": "s1", "customTitle": "My title"},
        {"type": "file-history-snapshot"},
        _rec(1, "yo", rtype="assistant", role="assistant"),
    ])
    with open(p, "a") as fh:
        fh.write("not json\n\n")
    n = pl.ingest_transcript(cur, p, "proj")
    assert n == 2
    rows = cur.execute(
        "SELECT src_line, role, content_sha256, parent_uuid FROM messages ORDER BY src_line"
    ).fetchall()
    assert [r[0] for r in rows] == [1, 4]
    assert rows[0][2] == pl.sha256_text("hi")
    assert rows[1][3] == "u0"
    assert cur.execute("SELECT title FROM sessions WHERE session_id='s1'").fetchone()[0] == "My title"


def test_reingest_unchanged_file_is_idempotent(cur, tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [_rec(0, "hi"), _rec(1, "there")])
    pl.ingest_transcript(cur, p, "proj")
    pl.ingest_transcript(cur, p, "proj")
    assert cur.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2


def test_file_changed_detects_size_and_mtime(cur, tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [_rec(0, "hi")])
    assert pl.file_changed(cur, p) is True
    pl.ingest_transcript(cur, p, "proj")
    assert pl.file_changed(cur, p) is False
    _bump_mtime(p)
    assert pl.file_changed(cur, p) is True


def test_non_object_json_line_does_not_abort_ingest(cur, tmp_path):
    """A valid-JSON line that is not an object (e.g. a bare string) must be skipped,
    not crash the whole ingest with AttributeError."""
    p = tmp_path / "s.jsonl"
    p.write_text(json.dumps(_rec(0, "hi")) + "\n" + '"stray"\n' + "[1, 2]\n")
    assert pl.ingest_transcript(cur, p, "proj") == 1

    h = tmp_path / "history.jsonl"
    h.write_text('{"display": "x", "timestamp": 1700000000000}\n' + "42\n")
    assert pl.ingest_history(cur, h) == 1


# ---- ingest_history --------------------------------------------------------

def test_ingest_history_converts_ms_and_s_epoch(cur, tmp_path):
    h = tmp_path / "history.jsonl"
    h.write_text(
        '{"display": "a", "timestamp": 1700000000000, "project": "/p"}\n'
        '{"display": "b", "timestamp": 1700000000}\n'
    )
    assert pl.ingest_history(cur, h) == 2
    ts = [r[0] for r in cur.execute("SELECT ts FROM messages ORDER BY src_line")]
    assert ts == ["2023-11-14T22:13:20+00:00", "2023-11-14T22:13:20+00:00"]


# ---- session stats ---------------------------------------------------------

def test_rebuild_session_stats_counts_roles(cur, tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [
        _rec(0, "q1"), _rec(1, "a1", "assistant", "assistant"),
        _rec(2, "q2"), _rec(3, "a2", "assistant", "assistant"),
        _rec(4, "q3"),
    ])
    pl.ingest_transcript(cur, p, "proj")
    pl.rebuild_session_stats(cur)
    n_user, n_asst, first, last = cur.execute(
        "SELECT n_user, n_assistant, first_ts, last_ts FROM sessions WHERE session_id='s1'"
    ).fetchone()
    assert (n_user, n_asst) == (3, 2)
    assert first.endswith(":00Z") and last.endswith(":04Z")


# ---- tamper evidence -------------------------------------------------------

def test_tampering_with_an_already_ingested_line_is_detectable(cur, tmp_path):
    """README claims 'sha256 of the full content (tamper-evidence)'. Transcripts are
    append-only, so a previously ingested line whose content changes is tampering.
    Re-ingesting must not silently overwrite the original hash: the change has to be
    recorded somewhere a user can query."""
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [_rec(0, "original answer"), _rec(1, "follow-up")])
    pl.ingest_transcript(cur, p, "proj")
    original = pl.sha256_text("original answer")

    _write_jsonl(p, [_rec(0, "EDITED answer"), _rec(1, "follow-up")])
    _bump_mtime(p)
    assert pl.file_changed(cur, p)
    pl.ingest_transcript(cur, p, "proj")

    violations = cur.execute(
        "SELECT src_line, old_sha256, new_sha256 FROM integrity_violations"
    ).fetchall()
    assert violations == [(1, original, pl.sha256_text("EDITED answer"))]


def test_appending_new_lines_is_not_flagged_as_tampering(cur, tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [_rec(0, "q1")])
    pl.ingest_transcript(cur, p, "proj")
    _write_jsonl(p, [_rec(0, "q1"), _rec(1, "a1", "assistant", "assistant")])
    pl.ingest_transcript(cur, p, "proj")
    assert cur.execute("SELECT COUNT(*) FROM integrity_violations").fetchone()[0] == 0
    assert cur.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2


def test_deleting_an_ingested_line_is_detectable(cur, tmp_path):
    p = tmp_path / "s.jsonl"
    _write_jsonl(p, [_rec(0, "q1"), _rec(1, "a1", "assistant", "assistant")])
    pl.ingest_transcript(cur, p, "proj")
    _write_jsonl(p, [_rec(0, "q1")])
    pl.ingest_transcript(cur, p, "proj")
    rows = cur.execute(
        "SELECT src_line, new_sha256 FROM integrity_violations"
    ).fetchall()
    assert rows == [(2, None)]
