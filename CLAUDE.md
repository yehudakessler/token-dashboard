# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project overview

**Token Dashboard** — a local dashboard for tracking Claude Code and Codex token usage and session history. It reads both agents' local logs, preserves historical Claude rows, and exposes source-filtered analytics.

Inspired by [phuryn/claude-usage](https://github.com/phuryn/claude-usage) but diverges in UI (vanilla JS + ECharts, dark theme, hash router, SSE refresh) and scope (expensive-prompt drill-down, skills view, tips engine, streaming-snapshot dedup). See `docs/inspiration.md` for the original's feature set and known limitations.

## Status

Working codebase. The pre-migration baseline was 71 Python unit tests; the dual-source suite currently discovers 93 (`python3 -m unittest discover tests`). Seven UI tabs are wired up. Runs on macOS, Windows, and Linux.

## Architecture

- `cli.py` → shared scan coordinator → Claude and Codex scanners → `~/.token-dashboard/token-dashboard.db` (SQLite)
- `token_dashboard/server.py` exposes JSON APIs (`/api/*`) + SSE stream (`/api/stream`) + static frontend (`web/`)
- `web/` is vanilla JS, no build step — hash router + ECharts

## Data source

Claude Code writes JSONLs under `~/.claude/projects/`; Codex writes rollout JSONLs under `~/.codex/sessions/`. Claude scans are incremental by byte offset. Changed Codex rollouts are atomically reparsed. All logical IDs are source-prefixed.

## Conventions

- **Fully local.** No telemetry, no remote calls for user data. Tests run offline.
- **Stdlib only.** No `pip install`. If a new feature needs a third-party library, argue for it first — we're willing to pay ergonomics cost to keep install friction at zero.
- **SQLite parameter binding always.** Any f-string in a SQL statement must interpolate only internal, caller-controlled values (column names, placeholder lists). User-reachable values go through `?`.
- **Small files with clear responsibilities.** If a file grows past ~400 lines or accretes three distinct concerns, split it.
- **Streaming-snapshot dedup.** Claude uses `(session_id, message_id)`; Codex counts each `last_token_usage` event once and does not add reasoning tokens to output a second time.
- **Do not invent data.** Unknown Codex pricing and unproven Codex skill invocations remain unavailable.
- **Preserve legacy history.** Never delete or overwrite `~/.claude/token-dashboard.db`.

## Customizing

Env vars: `PORT`, `HOST`, `CLAUDE_PROJECTS_DIR`, `CODEX_SESSIONS_DIR`, `TOKEN_DASHBOARD_SOURCE`, and `TOKEN_DASHBOARD_DB`. Pricing lives in `pricing.json`.

## Known limitations

See `docs/KNOWN_LIMITATIONS.md`. Current summary: Skills `tokens_per_call` is populated only for skills installed under the three scanned roots (`~/.claude/skills/`, `~/.claude/scheduled-tasks/`, `~/.claude/plugins/`); project-local skills and subagent-dispatched skills show invocation counts but blank token counts.

## Verifying changes

```bash
python3 -m unittest discover tests        # all tests
python3 cli.py dashboard --no-open        # start the server
curl http://127.0.0.1:8080/api/overview   # sanity-check an endpoint
```
