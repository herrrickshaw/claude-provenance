import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

import provenance_log as pl


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Redirect every path the module binds at import into tmp_path."""
    proj = tmp_path / "home" / ".claude" / "projects"
    proj.mkdir(parents=True)
    monkeypatch.setattr(pl, "PROJECTS_DIR", proj)
    monkeypatch.setattr(pl, "HISTORY_FILE", tmp_path / "home" / ".claude" / "history.jsonl")
    monkeypatch.setattr(pl, "BASE", tmp_path / "base")
    (tmp_path / "base").mkdir()
    monkeypatch.setattr(pl, "DB_PATH", tmp_path / "base" / "provenance.db")
    return tmp_path


@pytest.fixture
def cur():
    con = sqlite3.connect(":memory:")
    con.executescript(pl.SCHEMA)
    yield con.cursor()
    con.close()


def jl(path, *records, raw=()):
    lines = [json.dumps(r) for r in records] + list(raw)
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return Path(path)


def rows(cur, sql="SELECT * FROM messages ORDER BY src_file, src_line"):
    cur.execute(sql)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ---------------- small helpers ----------------
def test_sha256_text():
    assert pl.sha256_text("abc") == hashlib.sha256(b"abc").hexdigest()
    assert pl.sha256_text("café") == hashlib.sha256("café".encode()).hexdigest()
    assert pl.sha256_text("a\ud800b") == hashlib.sha256("a\ud800b".encode("utf-8", "replace")).hexdigest()   # lone surrogate does not raise


def test_now_iso_is_utc_seconds():
    s = pl.now_iso()
    assert s.endswith("+00:00") and "." not in s and len(s) == 25


def test_extract_content_variants():
    assert pl.extract_content("not a dict") == ("", [])
    assert pl.extract_content({"content": "plain"}) == ("plain", [])
    assert pl.extract_content({}) == ("", [])
    msg = {"content": [
        {"type": "text", "text": "hello"}, {"type": "thinking", "thinking": "hmm"},
        {"type": "tool_use", "name": "Bash", "input": {"cmd": "ls"}}, {"type": "tool_use"},
        {"type": "tool_result", "content": "result text"},
        {"type": "tool_result", "content": [{"text": "r1"}, "junk", {"text": "r2"}]},
        {"type": "tool_result", "content": 5}, {"type": "image"}, "stray string", {"type": "text"}]}
    text, tools = pl.extract_content(msg)
    assert text == 'hello\nhmm\n{"cmd": "ls"}\n""\nresult text\nr1\nr2'
    assert tools == ["Bash", "?"]


def test_tool_input_is_truncated_to_2000_chars():
    text, _ = pl.extract_content({"content": [{"type": "tool_use", "name": "X", "input": {"k": "v" * 5000}}]})
    assert len(text) == 2000


def test_non_serialisable_tool_input_uses_default_str():
    text, _ = pl.extract_content({"content": [{"type": "tool_use", "name": "X", "input": {"p": Path("/x")}}]})
    assert text == '{"p": "/x"}'


# ---------------- file_changed ----------------
def test_file_changed(cur, tmp_path):
    p = tmp_path / "f.jsonl"
    p.write_text("x")
    assert pl.file_changed(cur, p) is True                                  # never seen
    st = p.stat()
    cur.execute("INSERT INTO source_files VALUES (?,?,?,?,?,?)", (str(p), st.st_mtime, st.st_size, 1, 1, "t"))
    assert pl.file_changed(cur, p) is False
    cur.execute("UPDATE source_files SET size=?", (st.st_size + 1,))
    assert pl.file_changed(cur, p) is True                                  # size differs
    cur.execute("UPDATE source_files SET size=?, mtime=?", (st.st_size, st.st_mtime + 1))
    assert pl.file_changed(cur, p) is True                                  # mtime differs by 1s
    cur.execute("UPDATE source_files SET mtime=?", (st.st_mtime + 1e-7,))
    assert pl.file_changed(cur, p) is False                                 # < 1e-6 tolerance


# ---------------- ingest_transcript ----------------
def assistant(uuid, text, ts="2026-01-02T10:00:00Z", **kw):
    return {"type": "assistant", "uuid": uuid, "parentUuid": "p", "sessionId": "s1", "timestamp": ts, "cwd": "/w",
            "gitBranch": "main", "version": "2.1", "entrypoint": "cli", "requestId": "req", "promptSource": "typed",
            "message": {"role": "assistant", "model": "claude-x", "content": [{"type": "text", "text": text},
                                                                               {"type": "tool_use", "name": "Bash", "input": {}}]}, **kw}


def test_ingest_transcript_row_fields(cur, tmp_path):
    p = jl(tmp_path / "t.jsonl",
           {"type": "user", "uuid": "u1", "sessionId": "s1", "timestamp": "2026-01-02T09:00:00Z", "message": {"role": "user", "content": "hi there"}},
           assistant("a1", "x" * 400, isSidechain=True),
           {"type": "system", "uuid": "sy1", "sessionId": "s1", "slug": "the-slug", "subtype": "st"},
           {"type": "system", "uuid": "sy2", "sessionId": "s1", "subtype": "only-subtype"},
           {"type": "system", "uuid": "sy3", "sessionId": "s1"},
           {"type": "summary", "uuid": "ignored"},
           {"type": "user", "uuid": "u2", "sessionId": "s1", "message": "not a dict"},
           raw=["", "   ", "{not json"])
    n = pl.ingest_transcript(cur, p, "proj")
    assert n == 6
    r = {x["msg_uuid"]: x for x in rows(cur)}
    u1, a1 = r["u1"], r["a1"]
    assert (u1["source"], u1["project"], u1["session_id"], u1["record_type"], u1["role"]) == ("claude-code", "proj", "s1", "user", "user")
    assert u1["content_chars"] == 8 and u1["content_sha256"] == pl.sha256_text("hi there") and u1["content_preview"] == "hi there"
    assert u1["is_sidechain"] == 0 and u1["tool_names"] is None and u1["ts"] == "2026-01-02T09:00:00Z"
    assert a1["role"] == "assistant" and a1["model"] == "claude-x" and a1["tool_names"] == "Bash" and a1["is_sidechain"] == 1
    assert a1["parent_uuid"] == "p" and a1["request_id"] == "req" and a1["cwd"] == "/w" and a1["git_branch"] == "main"
    assert a1["cli_version"] == "2.1" and a1["entrypoint"] == "cli" and a1["prompt_source"] == "typed"
    assert len(a1["content_preview"]) == pl.PREVIEW_CHARS == 300 and a1["content_chars"] == 400 + 2 + 1       # text + "\\n" + '{}'
    assert r["sy1"]["content_preview"] == "the-slug" and r["sy2"]["content_preview"] == "only-subtype"
    assert r["sy3"]["content_preview"] == "" and r["sy3"]["content_sha256"] is None and r["sy3"]["role"] == "system"
    assert r["u2"]["content_chars"] == 0 and r["u2"]["role"] == "user"
    assert [x["src_line"] for x in rows(cur)] == [1, 2, 3, 4, 5, 7]


def test_source_line_numbers_count_blank_and_bad_lines(cur, tmp_path):
    p = jl(tmp_path / "t.jsonl", {"type": "user", "uuid": "a", "sessionId": "s", "message": {"content": "x"}},
           raw=["", "{bad", json.dumps({"type": "user", "uuid": "b", "sessionId": "s", "message": {"content": "y"}})])
    pl.ingest_transcript(cur, p, "p")
    assert {x["msg_uuid"]: x["src_line"] for x in rows(cur)} == {"a": 1, "b": 4}
    sf = rows(cur, "SELECT * FROM source_files")[0]
    assert sf["lines_parsed"] == 4 and sf["messages_kept"] == 2 and sf["path"] == str(p) and sf["size"] == p.stat().st_size


def test_titles_create_or_update_sessions(cur, tmp_path):
    p = jl(tmp_path / "t.jsonl",
           {"type": "custom-title", "sessionId": "s1", "customTitle": "Custom"},
           {"type": "ai-title", "sessionId": "s2", "aiTitle": "AI one"},
           {"type": "ai-title", "sessionId": "s1", "aiTitle": "AI for s1"},
           {"type": "ai-title", "aiTitle": "no session"},
           {"type": "custom-title", "sessionId": "s3"})
    assert pl.ingest_transcript(cur, p, "proj") == 0
    s = {x["session_id"]: x for x in rows(cur, "SELECT * FROM sessions")}
    assert s["s1"]["title"] == "AI for s1" and s["s2"]["title"] == "AI one" and s["s3"]["title"] is None
    assert s["s1"]["project"] == "proj" and s["s1"]["src_file"] == str(p) and len(s) == 3
    # a re-ingest updates the title but keeps one row per session
    jl(p, {"type": "custom-title", "sessionId": "s1", "customTitle": "Renamed"})
    pl.ingest_transcript(cur, p, "proj")
    assert [x["title"] for x in rows(cur, "SELECT * FROM sessions WHERE session_id='s1'")] == ["Renamed"]


def test_custom_title_wins_over_ai_title_on_same_record(cur, tmp_path):
    p = jl(tmp_path / "t.jsonl", {"type": "ai-title", "sessionId": "s", "customTitle": "C", "aiTitle": "A"})
    pl.ingest_transcript(cur, p, "p")
    assert rows(cur, "SELECT title FROM sessions")[0]["title"] == "C"


def test_reingest_replaces_rows_for_that_file_only(cur, tmp_path):
    a = jl(tmp_path / "a.jsonl", {"type": "user", "uuid": "a1", "sessionId": "s", "message": {"content": "1"}},
           {"type": "user", "uuid": "a2", "sessionId": "s", "message": {"content": "2"}})
    b = jl(tmp_path / "b.jsonl", {"type": "user", "uuid": "b1", "sessionId": "s", "message": {"content": "3"}})
    pl.ingest_transcript(cur, a, "p")
    pl.ingest_transcript(cur, b, "p")
    jl(a, {"type": "user", "uuid": "a3", "sessionId": "s", "message": {"content": "4"}})
    pl.ingest_transcript(cur, a, "p")
    assert sorted(x["msg_uuid"] for x in rows(cur)) == ["a3", "b1"]


def test_subagent_source_label_is_kept(cur, tmp_path):
    p = jl(tmp_path / "t.jsonl", {"type": "user", "uuid": "x", "sessionId": "s", "message": {"content": "y"}})
    pl.ingest_transcript(cur, p, "p", source="claude-code-subagent")
    assert rows(cur)[0]["source"] == "claude-code-subagent"


# ---------------- ingest_history ----------------
def test_ingest_history(cur, tmp_path):
    p = jl(tmp_path / "history.jsonl",
           {"display": "first prompt", "timestamp": 1_767_000_000_000, "project": "/proj", "sessionId": "s1"},
           {"display": "seconds", "timestamp": 1_767_000_000, "project": "/proj"},
           {"display": "", "timestamp": "2026-01-01T00:00:00Z"},
           {"timestamp": None}, raw=["{bad"])
    assert pl.ingest_history(cur, p) == 4
    r = rows(cur)
    assert [x["src_line"] for x in r] == [1, 2, 3, 4]
    assert r[0]["ts"] == "2025-12-29T09:20:00+00:00" and r[1]["ts"] == "2025-12-29T09:20:00+00:00"       # ms and s agree
    assert r[2]["ts"] == "2026-01-01T00:00:00Z" and r[3]["ts"] is None
    assert (r[0]["source"], r[0]["record_type"], r[0]["role"], r[0]["project"], r[0]["cwd"]) == ("prompt-history", "prompt", "user", "/proj", "/proj")
    assert r[0]["content_sha256"] == pl.sha256_text("first prompt") and r[2]["content_sha256"] is None and r[2]["content_chars"] == 0
    sf = rows(cur, "SELECT * FROM source_files")[0]
    assert sf["lines_parsed"] == 5 and sf["messages_kept"] == 4


def test_history_timestamp_just_above_1e12_is_milliseconds(cur, tmp_path):
    p = jl(tmp_path / "h.jsonl", {"display": "b", "timestamp": 1_000_000_000_001})
    pl.ingest_history(cur, p)
    assert rows(cur)[0]["ts"] == "2001-09-09T01:46:40+00:00"


# ---------------- claude.ai export ----------------
def test_ingest_claude_ai_export(cur, tmp_path):
    data = [{"uuid": "c1", "name": "Chat one", "chat_messages": [
                {"uuid": "m1", "sender": "human", "text": "hello", "created_at": "2026-01-01T00:00:00Z"},
                {"uuid": "m2", "parent_message_uuid": "m1", "sender": "assistant", "text": "", "created_at": "2026-01-01T00:00:05Z"}]},
            {"uuid": "c2", "name": None}]
    p = tmp_path / "conversations.json"
    p.write_text(json.dumps(data))
    assert pl.ingest_claude_ai_export(cur, p) == 2
    r = rows(cur)
    assert [(x["src_line"], x["msg_uuid"], x["role"], x["parent_uuid"]) for x in r] == [(1, "m1", "human", None), (2, "m2", "assistant", "m1")]
    assert r[0]["source"] == "claude-ai-export" and r[0]["record_type"] == "chat" and r[0]["project"] == "claude.ai"
    assert r[1]["content_sha256"] is None and r[0]["content_sha256"] == pl.sha256_text("hello")
    assert {x["session_id"]: (x["project"], x["title"]) for x in rows(cur, "SELECT * FROM sessions")} == {"c1": ("claude.ai", "Chat one"), "c2": ("claude.ai", None)}
    sf = rows(cur, "SELECT * FROM source_files")[0]
    assert sf["lines_parsed"] == 2 and sf["messages_kept"] == 2          # conversations parsed, messages kept


def test_claude_ai_export_title_upsert_keeps_project(cur, tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps([{"uuid": "c1", "name": "One"}]))
    pl.ingest_claude_ai_export(cur, p)
    p.write_text(json.dumps([{"uuid": "c1", "name": "Two"}]))
    pl.ingest_claude_ai_export(cur, p)
    assert [(x["title"], x["project"]) for x in rows(cur, "SELECT * FROM sessions")] == [("Two", "claude.ai")]


def test_ingest_design_chat(cur, tmp_path):
    d = {"uuid": "d1", "title": "Design", "messages": [
        {"uuid": "m1", "role": "user", "content": [{"text": "part one"}, "junk", {"text": "part two"}], "created_at": "t1"},
        {"uuid": "m2", "role": "assistant", "content": "plain string"},
        {"uuid": "m3", "role": "assistant", "content": {"k": 1}},
        {"uuid": "m4", "role": "assistant", "content": None}]}
    p = tmp_path / "d1.json"
    p.write_text(json.dumps(d))
    assert pl.ingest_design_chat(cur, p) == 4
    r = rows(cur)
    assert r[0]["content_preview"] == "part one\npart two" and r[1]["content_preview"] == "plain string"
    assert r[2]["content_preview"] == '{"k": 1}' and r[3]["content_preview"] == "null"
    assert r[0]["project"] == "claude.ai-design" and r[0]["ts"] == "t1" and [x["src_line"] for x in r] == [1, 2, 3, 4]
    s = rows(cur, "SELECT * FROM sessions")[0]
    assert (s["session_id"], s["project"], s["title"]) == ("d1", "claude.ai-design", "Design")
    sf = rows(cur, "SELECT * FROM source_files")[0]
    assert sf["lines_parsed"] == 1 and sf["messages_kept"] == 4


# ---------------- session stats ----------------
def test_rebuild_session_stats(cur, tmp_path):
    p = jl(tmp_path / "t.jsonl",
           {"type": "user", "uuid": "1", "sessionId": "s1", "timestamp": "2026-01-01T00:00:00Z", "message": {"role": "user", "content": "a"}},
           {"type": "assistant", "uuid": "2", "sessionId": "s1", "timestamp": "2026-01-01T00:05:00Z", "message": {"role": "assistant", "content": "b"}},
           {"type": "assistant", "uuid": "3", "sessionId": "s1", "timestamp": "2026-01-01T00:09:00Z", "message": {"role": "assistant", "content": "c"}},
           {"type": "system", "uuid": "4", "sessionId": "s1", "timestamp": "2026-01-01T00:10:00Z", "slug": "x"},
           {"type": "user", "uuid": "5", "message": {"content": "no session id"}})
    pl.ingest_transcript(cur, p, "proj")
    cur.execute("INSERT INTO sessions(session_id, project, title) VALUES ('s1', 'keep-me', 'T')")
    pl.rebuild_session_stats(cur)
    s = {x["session_id"]: x for x in rows(cur, "SELECT * FROM sessions")}
    assert set(s) == {"s1"}
    s1 = s["s1"]
    assert (s1["first_ts"], s1["last_ts"]) == ("2026-01-01T00:00:00Z", "2026-01-01T00:10:00Z")
    assert s1["n_user"] == 1 and s1["n_assistant"] == 2        # system rows are counted on neither side
    assert s1["project"] == "keep-me" and s1["title"] == "T"    # an existing project is not overwritten


def test_rebuild_inserts_missing_session_with_project(cur, tmp_path):
    p = jl(tmp_path / "h.jsonl", {"display": "x", "timestamp": 1_767_000_000, "project": "/p", "sessionId": "hs"})
    pl.ingest_history(cur, p)
    pl.rebuild_session_stats(cur)
    s = rows(cur, "SELECT * FROM sessions")[0]
    assert (s["session_id"], s["project"], s["n_user"], s["n_assistant"]) == ("hs", "/p", 1, 0)


# ---------------- summary ----------------
def test_write_summary(env, cur, tmp_path):
    jl(env / "t.jsonl", {"type": "user", "uuid": "1", "sessionId": "s1", "timestamp": "2026-01-01T00:00:00Z", "message": {"content": "a"}},
       assistant("2", "b", ts="2026-01-02T00:00:00Z"))
    pl.ingest_transcript(cur, env / "t.jsonl", "projA")
    cur.execute("UPDATE sessions SET title=NULL")
    pl.rebuild_session_stats(cur)
    cur.execute("UPDATE sessions SET title='A title that is much longer than sixty characters in total length so it must be cut' WHERE session_id='s1'")
    out = pl.write_summary(cur)
    text = out.read_text()
    assert out == env / "base" / "PROVENANCE_SUMMARY.md"
    assert "**2 messages** across **1 sessions**" in text
    assert "| claude-code | 2 | 2026-01-01T00:00:00Z | 2026-01-02T00:00:00Z |" in text
    assert "| projA | 2 |" in text and "| claude-x | 1 |" in text
    assert "| 2026-01-02T00:00:00Z | `s1` | A title that is much longer than sixty characters in total l | 1 | 1 |" in text
    assert text.index("## By source") < text.index("## By project") < text.index("## By model") < text.index("## 20 most recent sessions")


def test_summary_limits_projects_to_15_and_sessions_to_20(env, cur):
    for i in range(25):
        cur.execute("INSERT INTO messages (src_file, src_line, source, project, session_id, ts, ingested_at) VALUES (?,?,?,?,?,?,?)",
                    (f"f{i}", 1, "claude-code", f"proj{i:02d}", f"sess{i:02d}xxxxxxxx", f"2026-01-{i + 1:02d}T00:00:00Z", "now"))
    pl.rebuild_session_stats(cur)
    text = pl.write_summary(cur).read_text()
    assert text.count("| proj") == 15
    recent = text.split("## 20 most recent sessions")[1]
    assert recent.count("| `sess") == 20 and "`sess24xx`" in recent and "`sess04xx`" not in recent
    assert recent.index("`sess24xx`") < recent.index("`sess23xx`")           # newest first


# ---------------- main ----------------
def run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["provenance_log.py", *argv])
    pl.main()


def seed_projects(env):
    d = env / "home" / ".claude" / "projects"
    (d / "projA").mkdir()
    (d / "projA" / "sub").mkdir()
    (d / "projB" / "subagents").mkdir(parents=True)
    jl(d / "projA" / "one.jsonl", {"type": "user", "uuid": "u1", "sessionId": "s1", "message": {"content": "hi"}})
    jl(d / "projA" / "sub" / "two.jsonl", {"type": "user", "uuid": "u2", "sessionId": "s2", "message": {"content": "yo"}})
    jl(d / "projB" / "subagents" / "agent.jsonl", {"type": "assistant", "uuid": "u3", "sessionId": "s3", "message": {"content": "x"}})
    (d / "projA" / "notes.txt").write_text("ignored")
    return d


def db(env):
    con = sqlite3.connect(env / "base" / "provenance.db")
    con.row_factory = sqlite3.Row
    return con


def test_main_ingests_projects_and_is_incremental(env, monkeypatch, capsys):
    seed_projects(env)
    run(monkeypatch)
    out = capsys.readouterr().out
    assert "Parsed 3 files (0 unchanged, skipped). Ingested 3 messages this run." in out
    assert "Database now holds 3 messages across 3 sessions" in out
    con = db(env)
    src = {r["msg_uuid"]: (r["project"], r["source"]) for r in con.execute("SELECT * FROM messages")}
    assert src == {"u1": ("projA", "claude-code"), "u2": ("projA", "claude-code"), "u3": ("projB", "claude-code-subagent")}
    run(monkeypatch)
    assert "Parsed 0 files (3 unchanged, skipped). Ingested 0 messages this run." in capsys.readouterr().out
    run(monkeypatch, "--full")
    assert "Parsed 3 files (0 unchanged, skipped)." in capsys.readouterr().out


def test_main_reparses_changed_file_only(env, monkeypatch, capsys):
    d = seed_projects(env)
    run(monkeypatch)
    capsys.readouterr()
    jl(d / "projA" / "one.jsonl", {"type": "user", "uuid": "u1", "sessionId": "s1", "message": {"content": "hi"}},
       {"type": "user", "uuid": "u9", "sessionId": "s1", "message": {"content": "more"}})
    run(monkeypatch)
    assert "Parsed 1 files (2 unchanged, skipped). Ingested 2 messages this run." in capsys.readouterr().out


def test_main_progress_every_50_files(env, monkeypatch, capsys):
    d = env / "home" / ".claude" / "projects" / "bulk"
    d.mkdir()
    for i in range(51):
        jl(d / f"{i:03d}.jsonl", {"type": "user", "uuid": f"u{i}", "sessionId": f"s{i}", "message": {"content": "x"}})
    run(monkeypatch)
    cap = capsys.readouterr()
    assert "...50 files parsed" in cap.err and "...100 files parsed" not in cap.err
    assert "Parsed 51 files" in cap.out


def test_main_history_file(env, monkeypatch, capsys):
    jl(pl.HISTORY_FILE, {"display": "a prompt", "timestamp": 1_767_000_000, "project": "/p", "sessionId": "hs"})
    run(monkeypatch)
    assert "Parsed 1 files (0 unchanged, skipped). Ingested 1 messages this run." in capsys.readouterr().out
    run(monkeypatch)
    assert "Parsed 0 files" in capsys.readouterr().out                       # unchanged history is skipped
    run(monkeypatch, "--full")
    assert "Parsed 1 files" in capsys.readouterr().out


def test_main_without_any_sources(env, monkeypatch, capsys):
    pl.PROJECTS_DIR.rmdir()
    run(monkeypatch)
    assert "Parsed 0 files (0 unchanged, skipped). Ingested 0 messages this run." in capsys.readouterr().out


def test_main_claude_ai_export_file_and_directory(env, monkeypatch, capsys):
    exp = env / "export"
    (exp / "design_chats").mkdir(parents=True)
    (exp / "conversations.json").write_text(json.dumps([{"uuid": "c1", "name": "N", "chat_messages": [{"uuid": "m", "sender": "human", "text": "t"}]}]))
    (exp / "design_chats" / "d1.json").write_text(json.dumps({"uuid": "d1", "title": "D", "messages": [{"uuid": "x", "role": "user", "content": "q"}]}))
    (exp / "design_chats" / "readme.txt").write_text("ignored")
    run(monkeypatch, "--claude-ai-export", str(exp))
    assert "Parsed 2 files (0 unchanged, skipped). Ingested 2 messages this run." in capsys.readouterr().out
    # pointing at the file itself finds design_chats next to it
    run(monkeypatch, "--claude-ai-export", str(exp / "conversations.json"))
    assert "Parsed 2 files" in capsys.readouterr().out


def test_main_claude_ai_export_missing_paths_are_ignored(env, monkeypatch, capsys):
    run(monkeypatch, "--claude-ai-export", str(env / "nothing-here"))
    assert "Parsed 0 files" in capsys.readouterr().out


def test_main_summary_flag(env, monkeypatch, capsys):
    seed_projects(env)
    run(monkeypatch, "--summary")
    out = capsys.readouterr().out
    assert f"Summary written -> {env / 'base' / 'PROVENANCE_SUMMARY.md'}" in out
    assert (env / "base" / "PROVENANCE_SUMMARY.md").exists()
    run(monkeypatch)
    assert "Summary written" not in capsys.readouterr().out


def test_main_session_stats_present_after_run(env, monkeypatch):
    seed_projects(env)
    run(monkeypatch)
    n = {r["session_id"]: r["n_user"] for r in db(env).execute("SELECT * FROM sessions")}
    assert n == {"s1": 1, "s2": 1, "s3": 0}
