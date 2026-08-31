# Standard Operating Procedures — Data & Research Workflows

*Derived from the Claude message provenance log (115,132 messages, 2025-07 → 2026-07)
by extracting the workflows that recur across sessions and the corrections that
became standing rules. Generated 2026-07-21.*

These SOPs codify how work on this machine is actually done, so any session (or a
future you) can repeat it identically. Each SOP lists the **trigger**, the
**numbered procedure**, and a **done-checklist**. Rules marked 🔴 are hard
constraints that have caused real incidents when skipped.

---

## SOP-1 · Onboard a new data source

**Trigger:** a source is identified (from the provenance keyword scan, a claude.ai
research thread, or a direct request) and you want it collected on an ongoing basis.
This is the single most common build workflow in the log; the 2026-07-21 session
(underused-sources → agri/trade trackers → FCI) is the reference implementation.

1. **Probe access first, before writing any collector.** `curl -s -o /dev/null -w "%{http_code}"`
   with a real browser User-Agent. Classify: ✅ open API · 🟡 needs form/browser
   automation · 🔴 blocked (Akamai/Cloudflare 403, connection reset). Retry blocked
   hosts once via `WebFetch` and once via real-Chrome before declaring dead.
2. **Find the real endpoint.** For SPA sites (React/Angular/Livewire) the HTML is a
   shell — the data is behind a POST API. Grep the JS bundles for `api/`, or open the
   page in the browser pane and read `read_network_requests`. 🔴 Government SPAs often
   POST (not GET) and may encrypt the payload (see FCI: AES-128-CBC + HMAC, keys are
   public client-side constants in `main.*.js`).
3. **Validate the data against a known truth** before trusting it: a total that
   reconciles with an official figure (EIDB FY24-25 ≈ $438B exp / $721B imp), a
   spot-check vs screener.in, or a record count. Note timing quirks (Agmarknet fills
   through the IST day — pull after 14:00 IST).
4. **Warehouse it in DuckDB, not loose CSVs.** One table per source, schema preserved.
   Idempotent writes (delete-then-insert by natural key/date) so reruns are safe.
   Commit a `*_snapshot.csv` alongside for diff-ability.
5. **Structure as a repo** under `~/repos/<name>`: `collectors/`, `data/` (DB
   gitignored, snapshots tracked), `README.md` (source table + schema + why), pinned
   `requirements.txt`.
6. **Automate** via `crontab` (🔴 not launchd — launchd jobs on this machine have a
   history of silently never firing; verify `runs =` in `launchctl print`, not
   `launchctl list`). Use absolute `cd` + `/usr/bin/python3` (has duckdb + selenium +
   pycryptodome). End every cron line with `scripts/sync_data.sh` (regen snapshots →
   commit if changed → push).
7. **Back up large data via Git LFS** (`git lfs track "data/*.duckdb"`). Verify the
   pointer landed with `gh api repos/<owner>/<repo>/contents/<path> -q .size`.
   🔴 Check account-wide LFS budget before adding — it has been exhausted before.
8. **Record to memory**: a `reference` note with the working key/endpoint/quirk and a
   `project` note for the repo. Correct any prior memory the probe disproved.

**Done-checklist:** ☐ access classified ☐ data validated vs a known truth
☐ DuckDB table + CSV snapshot ☐ README with source+schema ☐ cron installed & interpreter/path verified ☐ LFS pointer confirmed on GitHub ☐ memory written

---

## SOP-2 · Daily market scan & report

**Trigger:** the recurring daily/morning report (26 dedicated sessions in the log —
the highest-frequency operational task).

1. Refresh instrument lists / caches for the target markets (India NSE/BSE via
   Bhavcopy, US via yfinance, etc.). Verify the cache actually updated, not just that
   the job ran.
2. Run the scanners (Darvas/Buffett + Piotroski/F-score; the log's dominant methods).
3. 🔴 **Filter non-equity instruments before scoring.** Indian ISIN prefix is the
   instrument-type key: `INE`=equity, `INF`=fund/ETF, `IN0-3`=G-Secs/SDLs. `SctySrs`/
   `FinInstrmTp` do **not** filter them — an ETF (LIQUIDSBI) once shipped as a
   golden-cross pick.
4. 🔴 **Validate picks against a public source before mailing.** Check the shortlist on
   screener.in (states its close date; moneycontrol/marketwatch are blocked to the
   agent). DMA/Darvas output is a *recommendation*, not a send trigger.
5. Send via the mailer only after steps 3–4 pass. Log what was sent.

**Done-checklist:** ☐ caches confirmed fresh ☐ non-equity ISINs filtered ☐ picks cross-checked on screener.in ☐ mail sent & logged

---

## SOP-3 · Backtest / screener research

**Trigger:** any claim about a strategy's edge (Piotroski, liquidity, mean-reversion, etc.).
Validation terms appear in 1,255 messages — the log shows this discipline is already
the norm; this SOP is to keep it non-optional.

1. Use **point-in-time** fundamentals (filing-dated), never latest-snapshot values
   joined to past prices — that is look-ahead leakage.
2. Use **split/bonus-adjusted** prices. 🔴 Raw Close fakes a +12.2% illiquid premium
   with 248% sd; adjustment collapses it to ~29%.
3. Keep **delisted names** in the universe (survivorship). The 10.5y India panel
   deliberately retains 964/3476 delisted symbols.
4. 🔴 **Audit sample completeness before citing a size.** Two same-named parquet files
   have burned this twice (US panel was an interrupted alphabetical crawl — 597
   letter-biased tickers = 13% of data). Run `data_completeness_audit.py` first.
5. Separate confounded variables explicitly (illiquidity ≠ size; the edge is
   illiquidity — turnover~mcap corr +0.797 conflated them).
6. State the honest scope: a variance claim on a small sample is not a validated
   backtest. Reconcile against the repo's real backtester before headlining a result.

**Done-checklist:** ☐ PIT fundamentals ☐ adjusted prices ☐ survivorship kept ☐ sample completeness audited ☐ confounds separated ☐ scope stated honestly

---

## SOP-4 · Repo, secrets & publishing hygiene

**Trigger:** any commit, push, or new repo.

1. 🔴 **Secrets stay local.** Real creds live in gitignored machine-local files;
   tracked copies ship placeholders. Check `git diff --cached` (staged secrets are
   invisible to plain `git diff`). Transcript logs echo creds in cleartext — never
   paste a live credential into a command.
2. 🔴 **Never enter a real password into a website login via automation**, even if
   authorized — redirect to the user logging in, then reuse the cookie/session.
3. Before making a repo public, create a verified private `-archive-<date>` backup first.
4. 🔴 The auto-mode push classifier blocks pushes to new public repos after *any*
   secret-grep hit, even a verified false positive. Don't loop retries — hand the user
   the push command.
5. Log **decisions** (including rejected alternatives) and corrections in a `CHANGELOG.md`
   on major changes — git already has the diffs; capture the *why*.

**Done-checklist:** ☐ no staged secrets ☐ private archive (if going public) ☐ CHANGELOG updated for major changes ☐ LFS pointers verified

---

## SOP-5 · Memory discipline

**Trigger:** end of any session that discovered something non-obvious.

1. Write **feedback** notes for corrections/approved approaches (with the *why*),
   **reference** notes for working endpoints/keys/quirks, **project** notes for ongoing
   work. Convert relative dates to absolute.
2. Update the existing file rather than duplicating; **delete memory that turns out
   wrong** (the datagovindia "no key" note was corrected this session).
3. Add exactly one index line to `MEMORY.md`; never put content there.
4. 🔴 Don't save what the repo/git already records. Save what was non-obvious.

**Done-checklist:** ☐ correct note type ☐ no duplicate ☐ wrong memory pruned ☐ MEMORY.md index line added

---

## Environment quick-reference

| Thing | Value |
|---|---|
| Cron interpreter | `/usr/bin/python3` (has duckdb, selenium 4.25, pycryptodome) |
| Scheduler | `crontab` — 🔴 not launchd (silent-fail history); machine runs **IST** |
| git-lfs | `/opt/homebrew/bin/git-lfs` (on cron PATH) |
| data.gov.in public key | `579b464db66ec23bdd000001cdd3946e44ce4aad7209ff7b23ac571b` |
| Blocked from this machine | IMF, IEA, SEBI (use World Bank / real-Chrome as fallback) |
| Provenance DB | `~/claude-provenance/provenance.db` (rebuild: `provenance_log.py --summary`) |
