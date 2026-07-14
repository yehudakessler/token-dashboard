# AGENTS.md — Token Dashboard

Platform-neutral guidance for any coding agent working in this repository.

## Purpose

This local, stdlib-only dashboard combines Claude Code and Codex usage. It must preserve old Claude history, keep sources distinguishable, and never invent unavailable Codex price or skill data.

## Safety

- Never modify or delete source logs under `~/.claude/` or `~/.codex/`.
- Never overwrite or delete the legacy backup `~/.claude/token-dashboard.db`.
- The combined database belongs under `~/.token-dashboard/`.
- Use SQLite parameter binding for user-reachable values.
- Keep the server bound to `127.0.0.1` by default.

## Data rules

- Prefix logical IDs with `claude:` or `codex:` and store `source` metadata.
- Claude scanning keeps streaming-snapshot deduplication.
- Codex prompts come from visible `event_msg.user_message` events.
- Codex usage counts every `last_token_usage` event once, including aborted work.
- Codex fresh input is `input_tokens - cached_input_tokens`.
- Codex `output_tokens` already contains reasoning; never add reasoning twice.
- Link Codex tools and results by `call_id`.
- Do not estimate Codex prices or skill invocations when the logs do not prove them.

## Verification

Run `python3 -m unittest discover tests` (or `py -m unittest discover tests` on this Windows workspace). The baseline before the dual-source migration was 71 tests; the current dual-source suite discovers 93.
