# Claude message provenance log

<!-- how-to-use-with-ai -->
> **New here?** See [How to use this repo with AI (Claude, Gemini, ChatGPT)](HOW_TO_USE_WITH_AI.md) — clone it, paste it into an AI tool, and ask in plain English.

A single-file Python tool that builds an auditable, per-message record of every Claude
interaction stored on this machine — Claude Code session transcripts, the Claude Code
prompt history and (optionally) a claude.ai data export — into one SQLite database,
`provenance.db`, plus an optional Markdown digest, `PROVENANCE_SUMMARY.md`.

> Status: active, personal tooling. The database and summary contain transcript content
> (which can include echoed credentials) and must stay local — see "Keep local" below.

## What it does
- Walks `~/.claude/projects/**/*.jsonl` and records every `user` / `assistant` / `system`
  record; transcripts under a `subagents/` directory are tagged `claude-code-subagent`.
- Records every prompt in `~/.claude/history.jsonl` (epoch timestamps, in seconds or
  milliseconds, are normalised to UTC ISO-8601).
- Optionally ingests a claude.ai export: `conversations.json` and any sibling
  `design_chats/*.json`.
- Stores, per message: source file + line, project, session id, message/parent uuid,
  record type, role, model, CLI version, git branch, cwd, tool names, content length,
  a 300-character preview and the sha256 of the full content (tamper-evidence).
- Re-runs are incremental: a source file whose mtime and size are unchanged is skipped;
  changed files are re-parsed and their rows replaced (`--full` forces a re-parse).
- Rebuilds a `sessions` table (title, first/last timestamp, user/assistant counts) and,
  with `--summary`, writes `PROVENANCE_SUMMARY.md`.

## Quick start
```bash
git clone https://github.com/herrrickshaw/claude-provenance.git
cd claude-provenance
python3 provenance_log.py --summary      # no third-party dependencies (Python >= 3.9)
```

```bash
python3 provenance_log.py            # incremental update (unchanged files skipped)
python3 provenance_log.py --summary  # also regenerate PROVENANCE_SUMMARY.md
python3 provenance_log.py --full     # force re-parse of everything
python3 provenance_log.py --claude-ai-export ~/path/to/export   # folder or conversations.json
```

claude.ai chats are stored server-side only (the desktop app keeps just a cache). To
include them: claude.ai → Settings → Privacy → Export data, then pass either the
`conversations.json` or the whole unzipped export folder; folder mode (or the file's
parent directory) also ingests `design_chats/*.json`. The 2026-07-21 export is already
ingested on the owner's machine; the `design_chats` files are kept in `claude-ai-export/`
so the DB's `src_file` pointers stay resolvable (`conversations.json` itself is not committed).

## How it works
All logic lives in `provenance_log.py`; pure functions build rows, ingesters do the SQL,
`main()` only parses flags and prints counts.

| Stage | Function(s) | Output |
|---|---|---|
| Parse a `.jsonl` line by line, tolerating junk lines | `iter_jsonl` | `(lineno, dict \| None)` |
| Flatten a transcript message (text, thinking, tool_use input, tool_result) | `extract_content` | `(text, tool_names)` |
| Build one `messages` row in `MESSAGE_COLUMNS` order | `message_row`, `transcript_row` | 23-tuple |
| Ingest one source file (delete old rows, insert new, record mtime/size) | `ingest_transcript`, `ingest_history`, `ingest_claude_ai_export`, `ingest_design_chat` → `replace_file_rows` | rows in `messages`, `source_files` |
| Recompute per-session stats | `rebuild_session_stats` | `sessions` |
| Walk all sources, skip unchanged files, commit every 50 transcripts | `ingest_all` | `(files, skipped, messages)` |
| Render / write the digest | `summary_markdown`, `write_summary` | `PROVENANCE_SUMMARY.md` |

Design notes:
- Rows are keyed `(src_file, src_line)` so every message can be traced back to the exact
  line it came from, and a file re-parse replaces exactly its own rows.
- `tool_use` inputs are truncated to 2,000 characters before hashing; previews to 300.
- Paths are module-level constants (`DB_PATH`, `PROJECTS_DIR`, `HISTORY_FILE`) so tests
  (and callers) can point the tool at a different tree without touching the CLI.

Example queries:

```bash
# All messages on a given day
sqlite3 provenance.db "SELECT ts, role, model, content_preview FROM messages
                       WHERE ts LIKE '2026-07-15%' ORDER BY ts"

# Which sessions touched a given git branch
sqlite3 provenance.db "SELECT DISTINCT session_id, git_branch FROM messages
                       WHERE git_branch LIKE '%bear-market%'"

# Tool usage frequency
sqlite3 provenance.db "SELECT tool_names, COUNT(*) FROM messages
                       WHERE tool_names IS NOT NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 20"

# Verify a message hasn't been altered: recompute sha256 of the content at
# src_file:src_line and compare to content_sha256
```

## Repository layout
| Path | Purpose |
|---|---|
| `provenance_log.py` | The whole tool (CLI + ingesters + summary writer) |
| `tests/` | pytest: synthetic `~/.claude` tree + export in `tmp_path`, end-to-end ingest, pure-function unit tests |
| `PROVENANCE_SUMMARY.md` | Digest generated 2026-07-21 (115,132 messages, 272 sessions); regenerated by `--summary` |
| `SOP.md` | Standard operating procedures distilled from the provenance log |
| `HOW_TO_USE_WITH_AI.md` | Plain-language guide for using the repo with an AI assistant |
| `claude-ai-export/design_chats/` | Design-chat files from the 2026-07-21 claude.ai export (tracked so `src_file` resolves) |
| `graphify-out/` | Generated knowledge-graph output; not hand-edited |
| `provenance.db` | Generated, gitignored — never commit |

## Data sources
| Source | What is taken | Access route | Caveat |
|---|---|---|---|
| `claude-code` / `claude-code-subagent` | every user/assistant/system record | local files `~/.claude/projects/**/*.jsonl` | contains full transcript text |
| `prompt-history` | every prompt typed into the CLI | local file `~/.claude/history.jsonl` | — |
| `claude-ai-export` | web/desktop chats and design chats | manual export from claude.ai (Settings → Privacy → Export data) | not available locally without the export |

## Configuration
No environment variables and no secrets. Paths are constants at the top of
`provenance_log.py`: `DB_PATH` (`<repo>/provenance.db`), `PROJECTS_DIR`
(`~/.claude/projects`), `HISTORY_FILE` (`~/.claude/history.jsonl`).

## Testing
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
ruff check . && pytest
```

## Limitations and known issues
- **Keep local.** `provenance.db` and `PROVENANCE_SUMMARY.md` contain transcript content,
  which can include echoed credentials. `provenance.db` is gitignored here (and in the
  owner's `~/.gitignore`); never commit or publish either file.
- Change detection is mtime + size only; a same-size in-place edit with a preserved
  mtime is not detected (use `--full`).
- `ingest_claude_ai_export` numbers rows sequentially across the whole
  `conversations.json`, so `src_line` is a message index there, not a file line.
- Transcript lines that are valid JSON but not an object are not guarded against
  (a JSON array line would raise); not observed in practice.

## Licence
No licence file yet — all rights reserved.
