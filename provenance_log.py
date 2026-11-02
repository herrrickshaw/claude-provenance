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

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

# --- paths and tunables ------------------------------------------------------
# Module-level so callers (and tests) can override them before calling main().

HOME = Path.home()
BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "provenance.db"
PROJECTS_DIR = HOME / ".claude" / "projects"
HISTORY_FILE = HOME / ".claude" / "history.jsonl"
SUMMARY_NAME = "PROVENANCE_SUMMARY.md"
PREVIEW_CHARS = 300
TOOL_INPUT_CHARS = 2000  # tool_use input JSON is truncated to this many chars
COMMIT_EVERY_FILES = 50  # commit + progress line after this many transcripts
MTIME_TOLERANCE_S = 1e-6  # mtime drift below this counts as "unchanged"
MS_EPOCH_THRESHOLD = 1e12  # numeric timestamps above this are milliseconds

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

# Column order of the `messages` table; every row tuple follows it.
MESSAGE_COLUMNS: tuple[str, ...] = (
    "src_file", "src_line", "source", "project", "session_id",
    "msg_uuid", "parent_uuid", "record_type", "role", "model",
    "request_id", "ts", "cwd", "git_branch", "cli_version",
    "entrypoint", "is_sidechain", "prompt_source", "tool_names",
    "content_chars", "content_sha256", "content_preview", "ingested_at",
)
_INSERT_MESSAGE = (
    f"INSERT OR REPLACE INTO messages ({', '.join(MESSAGE_COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(MESSAGE_COLUMNS))})"
)

KEEP_TYPES = {"user", "assistant", "system"}
TITLE_TYPES = {"custom-title", "ai-title"}

SOURCE_CLAUDE_CODE = "claude-code"
SOURCE_SUBAGENT = "claude-code-subagent"
SOURCE_HISTORY = "prompt-history"
SOURCE_EXPORT = "claude-ai-export"

Row = tuple  # one `messages` row, ordered as MESSAGE_COLUMNS


# --- pure helpers -------------------------------------------------------------


def now_iso() -> str:
    """Current UTC time as ISO-8601 with second precision."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_text(text: str) -> str:
    """Hex sha256 of `text` encoded as UTF-8 (unencodable chars replaced)."""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def extract_content(message: Any) -> tuple[str, list[str]]:
    """Return (text, tool_names) from a transcript 'message' payload.

    Text, thinking, tool_use input (truncated) and tool_result blocks are
    joined with newlines; empty pieces are dropped.
    """
    if not isinstance(message, dict):
        return "", []
    content = message.get("content")
    if isinstance(content, str):
        return content, []
    texts: list[str] = []
    tools: list[str] = []
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
                texts.append(
                    json.dumps(block.get("input", ""), default=str)[:TOOL_INPUT_CHARS]
                )
            elif btype == "tool_result":
                c = block.get("content")
                if isinstance(c, str):
                    texts.append(c)
                elif isinstance(c, list):
                    texts.extend(b.get("text", "") for b in c if isinstance(b, dict))
    return "\n".join(t for t in texts if t), tools


def history_timestamp(ts: Any) -> Any:
    """Normalise a history.jsonl timestamp.

    Numeric values (seconds or milliseconds since the epoch) become UTC ISO
    strings with second precision; anything else is returned unchanged.
    """
    if isinstance(ts, (int, float)):
        seconds = ts / 1000 if ts > MS_EPOCH_THRESHOLD else ts
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat(timespec="seconds")
    return ts


def design_chat_text(content: Any) -> str:
    """Flatten a design_chats message `content` field to plain text."""
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    if isinstance(content, str):
        return content
    return json.dumps(content, default=str)


def message_row(
    src_file: Path,
    src_line: int,
    source: str,
    text: str,
    *,
    project: Optional[str] = None,
    session_id: Optional[str] = None,
    msg_uuid: Optional[str] = None,
    parent_uuid: Optional[str] = None,
    record_type: Optional[str] = None,
    role: Optional[str] = None,
    model: Optional[str] = None,
    request_id: Optional[str] = None,
    ts: Any = None,
    cwd: Optional[str] = None,
    git_branch: Optional[str] = None,
    cli_version: Optional[str] = None,
    entrypoint: Optional[str] = None,
    is_sidechain: int = 0,
    prompt_source: Optional[str] = None,
    tools: Optional[Sequence[str]] = None,
) -> Row:
    """Build one `messages` row (ordered as MESSAGE_COLUMNS) from a message's text + metadata."""
    return (
        str(src_file), src_line, source, project, session_id,
        msg_uuid, parent_uuid, record_type, role, model,
        request_id, ts, cwd, git_branch, cli_version,
        entrypoint, is_sidechain, prompt_source,
        ",".join(tools) if tools else None,
        len(text), sha256_text(text) if text else None,
        text[:PREVIEW_CHARS], now_iso(),
    )


def transcript_row(d: dict, path: Path, lineno: int, project: str, source: str) -> Row:
    """Convert one kept Claude Code transcript record (dict) into a `messages` row."""
    rtype = d.get("type")
    msg = d.get("message") if isinstance(d.get("message"), dict) else {}
    text, tools = extract_content(msg)
    if rtype == "system":
        text = text or d.get("slug") or d.get("subtype") or ""
    return message_row(
        path, lineno, source, text,
        project=project, session_id=d.get("sessionId"),
        msg_uuid=d.get("uuid"), parent_uuid=d.get("parentUuid"),
        record_type=rtype, role=msg.get("role") or rtype, model=msg.get("model"),
        request_id=d.get("requestId"), ts=d.get("timestamp"),
        cwd=d.get("cwd"), git_branch=d.get("gitBranch"), cli_version=d.get("version"),
        entrypoint=d.get("entrypoint"), is_sidechain=1 if d.get("isSidechain") else 0,
        prompt_source=d.get("promptSource"), tools=tools,
    )


def iter_jsonl(path: Path, *, strip: bool = False) -> Iterable[tuple[int, Optional[dict]]]:
    """Yield (lineno, parsed-or-None) for every line of a .jsonl file.

    Undecodable lines yield None so callers can keep counting lines. With
    `strip=True`, blank lines also yield None.
    """
    with open(path, errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            if strip:
                line = line.strip()
                if not line:
                    yield lineno, None
                    continue
            try:
                yield lineno, json.loads(line)
            except json.JSONDecodeError:
                yield lineno, None


# --- database helpers ---------------------------------------------------------


def file_changed(cur: sqlite3.Cursor, path: Path) -> bool:
    """True unless `path`'s mtime and size match the last recorded ingest."""
    st = path.stat()
    row = cur.execute(
        "SELECT mtime, size FROM source_files WHERE path=?", (str(path),)
    ).fetchone()
    if row and abs(row[0] - st.st_mtime) < MTIME_TOLERANCE_S and row[1] == st.st_size:
        return False
    return True


def replace_file_rows(cur: sqlite3.Cursor, path: Path, rows: list[Row], lines_parsed: int) -> int:
    """Replace every `messages` row for `path` and record the file in `source_files`."""
    cur.execute("DELETE FROM messages WHERE src_file=?", (str(path),))
    cur.executemany(_INSERT_MESSAGE, rows)
    st = path.stat()
    cur.execute(
        "INSERT OR REPLACE INTO source_files VALUES (?,?,?,?,?,?)",
        (str(path), st.st_mtime, st.st_size, lines_parsed, len(rows), now_iso()),
    )
    return len(rows)


def upsert_session_title(
    cur: sqlite3.Cursor,
    session_id: Optional[str],
    project: str,
    title: Optional[str],
    src_file: Path,
    *,
    update_src_file: bool = False,
) -> None:
    """Insert or update a session's title (and, optionally, its src_file)."""
    extra = ", src_file=excluded.src_file" if update_src_file else ""
    cur.execute(
        "INSERT INTO sessions(session_id, project, title, src_file) VALUES (?,?,?,?) "
        f"ON CONFLICT(session_id) DO UPDATE SET title=excluded.title{extra}",
        (session_id, project, title, str(src_file)),
    )


# --- ingesters ----------------------------------------------------------------


def ingest_transcript(
    cur: sqlite3.Cursor, path: Path, project: str, source: str = SOURCE_CLAUDE_CODE
) -> int:
    """Ingest one Claude Code session transcript (.jsonl); return rows kept."""
    rows: list[Row] = []
    titles: dict[str, Optional[str]] = {}
    n_lines = 0
    for lineno, d in iter_jsonl(path, strip=True):
        n_lines = lineno
        if d is None:
            continue
        rtype = d.get("type")
        sid = d.get("sessionId")
        if rtype in TITLE_TYPES and sid:
            titles[sid] = d.get("customTitle") or d.get("aiTitle") or titles.get(sid)
            continue
        if rtype not in KEEP_TYPES:
            continue
        rows.append(transcript_row(d, path, lineno, project, source))
    n = replace_file_rows(cur, path, rows, n_lines)
    for sid, title in titles.items():
        upsert_session_title(cur, sid, project, title, path, update_src_file=True)
    return n


def ingest_history(cur: sqlite3.Cursor, path: Path) -> int:
    """Ingest ~/.claude/history.jsonl (one prompt per line); return rows kept."""
    rows: list[Row] = []
    n_lines = 0
    for lineno, d in iter_jsonl(path):
        n_lines = lineno
        if d is None:
            continue
        rows.append(message_row(
            path, lineno, SOURCE_HISTORY, d.get("display", ""),
            project=d.get("project"), session_id=d.get("sessionId"),
            record_type="prompt", role="user",
            ts=history_timestamp(d.get("timestamp")), cwd=d.get("project"),
        ))
    return replace_file_rows(cur, path, rows, n_lines)


def ingest_claude_ai_export(cur: sqlite3.Cursor, path: Path) -> int:
    """Ingest conversations.json from claude.ai Settings -> Privacy -> Export data."""
    with open(path) as fh:
        data = json.load(fh)
    rows: list[Row] = []
    n = 0
    for conv in data:
        cid = conv.get("uuid")
        upsert_session_title(cur, cid, "claude.ai", conv.get("name"), path)
        for msg in conv.get("chat_messages", []):
            n += 1
            rows.append(message_row(
                path, n, SOURCE_EXPORT, msg.get("text", ""),
                project="claude.ai", session_id=cid,
                msg_uuid=msg.get("uuid"), parent_uuid=msg.get("parent_message_uuid"),
                record_type="chat", role=msg.get("sender"), ts=msg.get("created_at"),
            ))
    return replace_file_rows(cur, path, rows, len(data))


def ingest_design_chat(cur: sqlite3.Cursor, path: Path) -> int:
    """Ingest one design_chats/<uuid>.json file from a claude.ai data export."""
    with open(path) as fh:
        d = json.load(fh)
    cid = d.get("uuid")
    upsert_session_title(cur, cid, "claude.ai-design", d.get("title"), path)
    rows = [
        message_row(
            path, n, SOURCE_EXPORT, design_chat_text(msg.get("content")),
            project="claude.ai-design", session_id=cid,
            msg_uuid=msg.get("uuid"), record_type="chat", role=msg.get("role"),
            ts=msg.get("created_at"),
        )
        for n, msg in enumerate(d.get("messages", []), 1)
    ]
    return replace_file_rows(cur, path, rows, 1)


def rebuild_session_stats(cur: sqlite3.Cursor) -> None:
    """Recompute first/last timestamp and user/assistant counts per session."""
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


# --- orchestration ------------------------------------------------------------


def ingest_all(
    con: sqlite3.Connection,
    *,
    projects_dir: Path,
    history_file: Path,
    claude_ai_export: Optional[Path] = None,
    full: bool = False,
    progress: Optional[Any] = None,
) -> tuple[int, int, int]:
    """Ingest every source; return (files_parsed, files_skipped, messages_ingested).

    Progress lines go to `progress` (default: sys.stderr at call time).
    """
    progress = sys.stderr if progress is None else progress
    cur = con.cursor()
    n_files = n_skipped = n_msgs = 0
    if projects_dir.is_dir():
        for path in sorted(projects_dir.rglob("*.jsonl")):
            rel = path.relative_to(projects_dir)
            project = rel.parts[0]
            source = SOURCE_SUBAGENT if "subagents" in rel.parts else SOURCE_CLAUDE_CODE
            if not full and not file_changed(cur, path):
                n_skipped += 1
                continue
            n_msgs += ingest_transcript(cur, path, project, source)
            n_files += 1
            if n_files % COMMIT_EVERY_FILES == 0:
                con.commit()
                print(f"  ...{n_files} files parsed", file=progress)
    if history_file.is_file() and (full or file_changed(cur, history_file)):
        n_msgs += ingest_history(cur, history_file)
        n_files += 1
    if claude_ai_export is not None:
        exp = Path(claude_ai_export)
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
    return n_files, n_skipped, n_msgs


def summary_markdown(cur: sqlite3.Cursor) -> str:
    """Render the PROVENANCE_SUMMARY.md body from the current database."""
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
    lines = [
        f"# Claude Message Provenance Log\n\nGenerated: {now_iso()}\n\n",
        f"**{total:,} messages** across **{n_sessions:,} sessions**\n\n",
        "## By source\n\n| Source | Messages | First | Last |\n|---|---|---|---|\n",
    ]
    lines += [f"| {s} | {c:,} | {lo or ''} | {hi or ''} |\n" for s, c, lo, hi in by_source]
    lines.append("\n## By project\n\n| Project | Messages |\n|---|---|\n")
    lines += [f"| {p} | {c:,} |\n" for p, c in by_project]
    lines.append("\n## By model\n\n| Model | Assistant messages |\n|---|---|\n")
    lines += [f"| {m} | {c:,} |\n" for m, c in top_models]
    lines.append(
        "\n## 20 most recent sessions\n\n"
        "| Last activity | Session | Title | User msgs | Assistant msgs |\n"
        "|---|---|---|---|---|\n"
    )
    lines += [
        f"| {ts} | `{sid[:8]}` | {title[:60]} | {nu or 0} | {na or 0} |\n"
        for ts, sid, title, nu, na in recent
    ]
    return "".join(lines)


def write_summary(cur: sqlite3.Cursor, out: Optional[Path] = None) -> Path:
    """Write the summary markdown to `out` (default: BASE/PROVENANCE_SUMMARY.md)."""
    out = out or BASE / SUMMARY_NAME
    with open(out, "w") as fh:
        fh.write(summary_markdown(cur))
    return out


def build_parser() -> argparse.ArgumentParser:
    """Argument parser for the CLI (flags unchanged: --claude-ai-export, --summary, --full)."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--claude-ai-export", help="path to claude.ai conversations.json export")
    ap.add_argument("--summary", action="store_true", help="write PROVENANCE_SUMMARY.md")
    ap.add_argument("--full", action="store_true", help="re-parse all files even if unchanged")
    return ap


def main(argv: Optional[Sequence[str]] = None) -> None:
    """CLI entry point: ingest, print counts, optionally write the summary."""
    args = build_parser().parse_args(argv)

    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    try:
        n_files, n_skipped, n_msgs = ingest_all(
            con,
            projects_dir=PROJECTS_DIR,
            history_file=HISTORY_FILE,
            claude_ai_export=Path(args.claude_ai_export) if args.claude_ai_export else None,
            full=args.full,
        )
        cur = con.cursor()
        total, = cur.execute("SELECT COUNT(*) FROM messages").fetchone()
        n_sessions, = cur.execute("SELECT COUNT(*) FROM sessions").fetchone()
        print(f"Parsed {n_files} files ({n_skipped} unchanged, skipped). "
              f"Ingested {n_msgs:,} messages this run.")
        print(f"Database now holds {total:,} messages across {n_sessions:,} sessions -> {DB_PATH}")

        if args.summary:
            out = write_summary(cur)
            print(f"Summary written -> {out}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
