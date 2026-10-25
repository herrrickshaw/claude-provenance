# Claude Message Provenance Log

<!-- how-to-use-with-ai -->
> **New here?** See [How to use this repo with AI (Claude, Gemini, ChatGPT)](HOW_TO_USE_WITH_AI.md) — clone it, paste it into an AI tool, and ask in plain English.


An auditable, per-message provenance record of every Claude interaction stored on
this machine, built into a single SQLite database.

## Sources covered

| Source | Where it lives | What's captured |
|---|---|---|
| `claude-code` | `~/.claude/projects/*/<session>.jsonl` | every user/assistant/system message in main sessions |
| `claude-code-subagent` | `~/.claude/projects/*/<session>/subagents/**.jsonl` | subagent + workflow-agent transcripts |
| `prompt-history` | `~/.claude/history.jsonl` | every prompt typed into the CLI |
| `claude-ai-export` (optional) | claude.ai data export | claude.ai web/desktop chats |

claude.ai chats are stored server-side only (the local desktop app keeps just a
cache). To include them: claude.ai → Settings → Privacy → Export data, then:

```bash
# pass either the conversations.json or the whole unzipped export folder
# (folder mode also ingests design_chats/*.json)
python3 provenance_log.py --claude-ai-export ~/claude-provenance/claude-ai-export
```

The 2026-07-21 export is already ingested; its files are kept in
`claude-ai-export/` (gitignored) so the DB's `src_file` pointers stay resolvable.

## What each message row records

- **Where**: source file + line number, project, session id
- **When**: original message timestamp + ingest timestamp
- **Who**: role, model (e.g. claude-sonnet-5), CLI version, git branch, cwd
- **What**: record type, tool calls made, content length, 300-char preview
- **Integrity**: sha256 of the full content. On re-ingest, a previously recorded line whose
  hash changed (or that disappeared) is logged to the `integrity_violations` table with the
  original hash, instead of being silently overwritten. The DB itself is not signed or
  anchored, so this detects edits to the source transcripts, not edits to `provenance.db`.
- **Lineage**: message uuid + parent uuid (reconstructs the conversation DAG)

## Usage

```bash
python3 provenance_log.py            # incremental update (unchanged files skipped)
python3 provenance_log.py --summary  # also regenerate PROVENANCE_SUMMARY.md
python3 provenance_log.py --full     # force re-parse of everything
```

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

## ⚠️ Keep local

`provenance.db` and `PROVENANCE_SUMMARY.md` contain transcript content, which can
include echoed credentials. Both are gitignored in `~/.gitignore` — never commit
or publish them.
