#!/usr/bin/env python3
"""Provenance log for all Claude messages on this machine.

Ingests three sources into a single SQLite database (provenance.db):

1. Claude Code session transcripts  ~/.claude/projects/**/*.jsonl
   (every user / assistant / system record, with uuid, parent uuid,
    timestamp, cwd, git branch, model, CLI version, tool calls)
2. Claude Code prompt history       ~/.claude/history.jsonl
3. claude.ai data export (optional) pass the path to conversations.json
   from claude.ai Settings -> Privacy -> Export data:
       python3 provenance_log.py --claude-ai-export ~/Downloads/conversations.json

Each message row records WHERE it came from (source file + line number),
WHEN (original timestamp + ingest timestamp), WHO (role/model), and a
sha256 of the content for tamper-evidence. Re-runs are incremental:
unchanged source files are skipped; changed files are re-parsed.

Usage:
    python3 provenance_log.py            # ingest / update
    python3 provenance_log.py --summary  # ingest, then write PROVENANCE_SUMMARY.md
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "provenance.db"
PROJECTS_DIR = HOME / ".claude" / "projects"
HISTORY_FILE = HOME / ".claude" / "history.jsonl"
PREVIEW_CHARS = 300

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_files (
    path            TEXT PRIMARY KEY,
    mtime           REAL,
    size            INTEGER,
    lines_parsed    INTEGER,
    messages_kept   INTEGER,
    last_ingested_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    src_file        TEXT NOT NULL,
    src_line        INTEGER NOT NULL,
    source          TEXT NOT NULL,      -- claude-code | prompt-history | claude-ai-export
    project         TEXT,
    session_id      TEXT,
    msg_uuid        TEXT,
    parent_uuid     TEXT,
    record_type     TEXT,               -- user | assistant | system | prompt | chat
    role            TEXT,
    model           TEXT,
    request_id      TEXT,
    ts              TEXT,               -- original message timestamp (ISO)
    cwd             TEXT,
    git_branch      TEXT,
    cli_version     TEXT,
    entrypoint      TEXT,
    is_sidechain    INTEGER,
    prompt_source   TEXT,
    tool_names      TEXT,               -- comma-separated tool_use names
    content_chars   INTEGER,
    content_sha256  TEXT,
    content_preview TEXT,
    ingested_at     TEXT NOT NULL,
    PRIMARY KEY (src_file, src_line)
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_ts      ON messages(ts);
CREATE INDEX IF NOT EXISTS idx_messages_type    ON messages(record_type);
CREATE TABLE IF NOT EXISTS sessions (
    session_id  TEXT PRIMARY KEY,
    project     TEXT,
    title       TEXT,
    first_ts    TEXT,
    last_ts     TEXT,
    n_user      INTEGER,
    n_assistant INTEGER,
    src_file    TEXT
);
"""

KEEP_TYPES = {"user", "assistant", "system"}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def extract_content(message):
    """Return (text, tool_names) from a transcript 'message' payload."""
    if not isinstance(message, dict):
        return "", []
    content = message.get("content")
    if isinstance(content, str):
        return content, []
    texts, tools = [], []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                texts.append(block.get("text", ""))
            elif btype == "thinking":
                texts.append(block.get("thinking", ""))
            elif btype == "tool_use":
                tools.append(block.get("name", "?"))
                texts.append(json.dumps(block.get("input", ""), default=str)[:2000])
            elif btype == "tool_result":
                c = block.get("content")
                if isinstance(c, str):
                    texts.append(c)
                elif isinstance(c, list):
                    texts.extend(b.get("text", "") for b in c if isinstance(b, dict))
    return "\n".join(t for t in texts if t), tools


def file_changed(cur, path):
    st = path.stat()
    row = cur.execute(
        "SELECT mtime, size FROM source_files WHERE path=?", (str(path),)
    ).fetchone()
    if row and abs(row[0] - st.st_mtime) < 1e-6 and row[1] == st.st_size:
        return False
    return True


def ingest_transcript(cur, path, project, source="claude-code"):
    rows, titles = [], {}
    n_lines = 0
    with open(path, errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            n_lines = lineno
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(d, dict):
                continue
            rtype = d.get("type")
            sid = d.get("sessionId")
            if rtype in ("custom-title", "ai-title") and sid:
                titles[sid] = d.get("customTitle") or d.get("aiTitle") or titles.get(sid)
                continue
            if rtype not in KEEP_TYPES:
                continue
            msg = d.get("message") if isinstance(d.get("message"), dict) else {}
            text, tools = extract_content(msg)
            if rtype == "system":
                text = text or d.get("slug") or d.get("subtype") or ""
            rows.append((
                str(path), lineno, source, project, sid,
                d.get("uuid"), d.get("parentUuid"), rtype,
                msg.get("role") or rtype, msg.get("model"),
                d.get("requestId"), d.get("timestamp"),
                d.get("cwd"), d.get("gitBranch"), d.get("version"),
                d.get("entrypoint"), 1 if d.get("isSidechain") else 0,
                d.get("promptSource"),
                ",".join(tools) if tools else None,
                len(text), sha256_text(text) if text else None,
                text[:PREVIEW_CHARS], now_iso(),
            ))
    cur.execute("DELETE FROM messages WHERE src_file=?", (str(path),))
    cur.executemany(
        "INSERT OR REPLACE INTO messages VALUES (" + ",".join(["?"] * 23) + ")", rows
    )
    for sid, title in titles.items():
        cur.execute(
            "INSERT INTO sessions(session_id, project, title, src_file) VALUES (?,?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET title=excluded.title, src_file=excluded.src_file",
            (sid, project, title, str(path)),
        )
    st = path.stat()
    cur.execute(
        "INSERT OR REPLACE INTO source_files VALUES (?,?,?,?,?,?)",
        (str(path), st.st_mtime, st.st_size, n_lines, len(rows), now_iso()),
    )
    return len(rows)


def ingest_history(cur, path):
    rows = []
    n_lines = 0
    with open(path, errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            n_lines = lineno
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(d, dict):
                continue
            text = d.get("display", "")
            ts = d.get("timestamp")
            if isinstance(ts, (int, float)):
                ts = datetime.fromtimestamp(
                    ts / 1000 if ts > 1e12 else ts, tz=timezone.utc
                ).isoformat(timespec="seconds")
            rows.append((
                str(path), lineno, "prompt-history", d.get("project"),
                d.get("sessionId"), None, None, "prompt", "user", None,
                None, ts, d.get("project"), None, None, None, 0, None, None,
                len(text), sha256_text(text) if text else None,
                text[:PREVIEW_CHARS], now_iso(),
            ))
    cur.execute("DELETE FROM messages WHERE src_file=?", (str(path),))
    cur.executemany(
        "INSERT OR REPLACE INTO messages VALUES (" + ",".join(["?"] * 23) + ")", rows
    )
    st = path.stat()
    cur.execute(
        "INSERT OR REPLACE INTO source_files VALUES (?,?,?,?,?,?)",
        (str(path), st.st_mtime, st.st_size, n_lines, len(rows), now_iso()),
    )
    return len(rows)


def ingest_claude_ai_export(cur, path):
    """conversations.json from claude.ai Settings -> Privacy -> Export data."""
    data = json.load(open(path))
    rows, n = [], 0
    for conv in data:
        cid = conv.get("uuid")
        title = conv.get("name")
        cur.execute(
            "INSERT INTO sessions(session_id, project, title, src_file) VALUES (?,?,?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET title=excluded.title",
            (cid, "claude.ai", title, str(path)),
        )
        for msg in conv.get("chat_messages", []):
            n += 1
            text = msg.get("text", "")
            rows.append((
                str(path), n, "claude-ai-export", "claude.ai", cid,
                msg.get("uuid"), msg.get("parent_message_uuid"), "chat", msg.get("sender"), None,
                None, msg.get("created_at"), None, None, None, None, 0, None,
                None, len(text), sha256_text(text) if text else None,
                text[:PREVIEW_CHARS], now_iso(),
            ))
    cur.execute("DELETE FROM messages WHERE src_file=?", (str(path),))
    cur.executemany(
        "INSERT OR REPLACE INTO messages VALUES (" + ",".join(["?"] * 23) + ")", rows
    )
    st = Path(path).stat()
    cur.execute(
        "INSERT OR REPLACE INTO source_files VALUES (?,?,?,?,?,?)",
        (str(path), st.st_mtime, st.st_size, len(data), len(rows), now_iso()),
    )
    return len(rows)


def ingest_design_chat(cur, path):
    """One design_chats/<uuid>.json file from a claude.ai data export."""
    d = json.load(open(path))
    cid = d.get("uuid")
    cur.execute(
        "INSERT INTO sessions(session_id, project, title, src_file) VALUES (?,?,?,?) "
        "ON CONFLICT(session_id) DO UPDATE SET title=excluded.title",
        (cid, "claude.ai-design", d.get("title"), str(path)),
    )
    rows = []
    for n, msg in enumerate(d.get("messages", []), 1):
        content = msg.get("content")
        if isinstance(content, list):
            text = "\n".join(
                b.get("text", "") for b in content if isinstance(b, dict)
            )
        else:
            text = content if isinstance(content, str) else json.dumps(content, default=str)
        rows.append((
            str(path), n, "claude-ai-export", "claude.ai-design", cid,
            msg.get("uuid"), None, "chat", msg.get("role"), None,
            None, msg.get("created_at"), None, None, None, None, 0, None,
            None, len(text), sha256_text(text) if text else None,
            text[:PREVIEW_CHARS], now_iso(),
        ))
    cur.execute("DELETE FROM messages WHERE src_file=?", (str(path),))
    cur.executemany(
        "INSERT OR REPLACE INTO messages VALUES (" + ",".join(["?"] * 23) + ")", rows
    )
    st = Path(path).stat()
    cur.execute(
        "INSERT OR REPLACE INTO source_files VALUES (?,?,?,?,?,?)",
        (str(path), st.st_mtime, st.st_size, 1, len(rows), now_iso()),
    )
    return len(rows)


def rebuild_session_stats(cur):
    cur.execute("""
        INSERT INTO sessions(session_id, project, first_ts, last_ts, n_user, n_assistant)
        SELECT session_id, MAX(project), MIN(ts), MAX(ts),
               SUM(record_type IN ('user','prompt','chat') AND role != 'assistant'),
               SUM(role = 'assistant')
        FROM messages WHERE session_id IS NOT NULL
        GROUP BY session_id
        ON CONFLICT(session_id) DO UPDATE SET
            first_ts=excluded.first_ts, last_ts=excluded.last_ts,
            n_user=excluded.n_user, n_assistant=excluded.n_assistant,
            project=COALESCE(sessions.project, excluded.project)
    """)


def write_summary(cur):
    out = BASE / "PROVENANCE_SUMMARY.md"
    total, = cur.execute("SELECT COUNT(*) FROM messages").fetchone()
    by_source = cur.execute(
        "SELECT source, COUNT(*), MIN(ts), MAX(ts) FROM messages GROUP BY source"
    ).fetchall()
    by_project = cur.execute(
        "SELECT COALESCE(project,'?'), COUNT(*) FROM messages GROUP BY 1 ORDER BY 2 DESC LIMIT 15"
    ).fetchall()
    top_models = cur.execute(
        "SELECT model, COUNT(*) FROM messages WHERE model IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()
    n_sessions, = cur.execute("SELECT COUNT(*) FROM sessions").fetchone()
    recent = cur.execute("""
        SELECT s.last_ts, s.session_id, COALESCE(s.title,''), s.n_user, s.n_assistant
        FROM sessions s WHERE s.last_ts IS NOT NULL
        ORDER BY s.last_ts DESC LIMIT 20
    """).fetchall()
    with open(out, "w") as fh:
        fh.write(f"# Claude Message Provenance Log\n\nGenerated: {now_iso()}\n\n")
        fh.write(f"**{total:,} messages** across **{n_sessions:,} sessions**\n\n")
        fh.write("## By source\n\n| Source | Messages | First | Last |\n|---|---|---|---|\n")
        for s, c, lo, hi in by_source:
            fh.write(f"| {s} | {c:,} | {lo or ''} | {hi or ''} |\n")
        fh.write("\n## By project\n\n| Project | Messages |\n|---|---|\n")
        for p, c in by_project:
            fh.write(f"| {p} | {c:,} |\n")
        fh.write("\n## By model\n\n| Model | Assistant messages |\n|---|---|\n")
        for m, c in top_models:
            fh.write(f"| {m} | {c:,} |\n")
        fh.write("\n## 20 most recent sessions\n\n| Last activity | Session | Title | User msgs | Assistant msgs |\n|---|---|---|---|---|\n")
        for ts, sid, title, nu, na in recent:
            fh.write(f"| {ts} | `{sid[:8]}` | {title[:60]} | {nu or 0} | {na or 0} |\n")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--claude-ai-export", help="path to claude.ai conversations.json export")
    ap.add_argument("--summary", action="store_true", help="write PROVENANCE_SUMMARY.md")
    ap.add_argument("--full", action="store_true", help="re-parse all files even if unchanged")
    args = ap.parse_args()

    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    cur = con.cursor()

    n_files = n_skipped = n_msgs = 0
    if PROJECTS_DIR.is_dir():
        for path in sorted(PROJECTS_DIR.rglob("*.jsonl")):
            rel = path.relative_to(PROJECTS_DIR)
            project = rel.parts[0]
            source = "claude-code-subagent" if "subagents" in rel.parts else "claude-code"
            if not args.full and not file_changed(cur, path):
                n_skipped += 1
                continue
            n_msgs += ingest_transcript(cur, path, project, source)
            n_files += 1
            if n_files % 50 == 0:
                con.commit()
                print(f"  ...{n_files} files parsed", file=sys.stderr)
    if HISTORY_FILE.is_file() and (args.full or file_changed(cur, HISTORY_FILE)):
        n_msgs += ingest_history(cur, HISTORY_FILE)
        n_files += 1
    if args.claude_ai_export:
        exp = Path(args.claude_ai_export)
        conv = exp / "conversations.json" if exp.is_dir() else exp
        if conv.is_file():
            n_msgs += ingest_claude_ai_export(cur, conv)
            n_files += 1
        design_dir = (exp if exp.is_dir() else exp.parent) / "design_chats"
        if design_dir.is_dir():
            for dc in sorted(design_dir.glob("*.json")):
                n_msgs += ingest_design_chat(cur, dc)
                n_files += 1

    rebuild_session_stats(cur)
    con.commit()

    total, = cur.execute("SELECT COUNT(*) FROM messages").fetchone()
    n_sessions, = cur.execute("SELECT COUNT(*) FROM sessions").fetchone()
    print(f"Parsed {n_files} files ({n_skipped} unchanged, skipped). "
          f"Ingested {n_msgs:,} messages this run.")
    print(f"Database now holds {total:,} messages across {n_sessions:,} sessions -> {DB_PATH}")

    if args.summary:
        out = write_summary(cur)
        print(f"Summary written -> {out}")
    con.close()


if __name__ == "__main__":
    main()
