"""Token Dashboard CLI entrypoint."""
from __future__ import annotations

import argparse
import os
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

from token_dashboard.db import (
    init_db, default_db_path, legacy_db_path, migrate_legacy_db, overview_totals,
)
from token_dashboard.scanner import scan_sources
from token_dashboard.tips import all_tips


def _db_path(args) -> str:
    return args.db or os.environ.get("TOKEN_DASHBOARD_DB") or str(default_db_path())


def _claude_dir(args) -> str:
    return (
        args.claude_dir
        or args.projects_dir
        or os.environ.get("CLAUDE_PROJECTS_DIR")
        or str(Path.home() / ".claude" / "projects")
    )


def _codex_dir(args) -> str:
    return (
        args.codex_dir
        or os.environ.get("CODEX_SESSIONS_DIR")
        or str(Path.home() / ".codex" / "sessions")
    )


def _source(args) -> str:
    explicit = args.source or os.environ.get("TOKEN_DASHBOARD_SOURCE")
    if explicit:
        if explicit not in {"all", "claude", "codex"}:
            raise SystemExit("TOKEN_DASHBOARD_SOURCE must be all, claude, or codex")
        return explicit
    # Old scripts that pass only --projects-dir retain Claude-only behavior.
    if args.projects_dir:
        return "claude"
    return "all"


def _ensure_db(args) -> str:
    db = Path(_db_path(args))
    if not db.exists() and db.resolve() == default_db_path().resolve() and legacy_db_path().is_file():
        result = migrate_legacy_db(legacy_db_path(), db)
        print(f"Token Dashboard: legacy migration copied {result['rows']} rows")
    else:
        init_db(db)
    return str(db)


def _today_range():
    now = datetime.now(timezone.utc)
    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc).isoformat()
    end = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    return start, end


def cmd_scan(args):
    db = _ensure_db(args)
    n = scan_sources(_claude_dir(args), _codex_dir(args), db, _source(args))
    print(f"Token Dashboard: scanned {n['files']} files, {n['messages']} messages, {n['tools']} tool calls")


def cmd_today(args):
    db = _ensure_db(args)
    s, e = _today_range()
    t = overview_totals(db, since=s, until=e, source=_source(args))
    print("Token Dashboard — today")
    print(f"  sessions: {t['sessions']}    turns: {t['turns']}")
    print(f"  input:    {t['input_tokens']:>12,}    output: {t['output_tokens']:>12,}")
    print(f"  cache rd: {t['cache_read_tokens']:>12,}    cache cr: {t['cache_create_5m_tokens']+t['cache_create_1h_tokens']:>12,}")


def cmd_stats(args):
    db = _ensure_db(args)
    t = overview_totals(db, source=_source(args))
    print("Token Dashboard — all time")
    print(f"  sessions: {t['sessions']}    turns: {t['turns']}")
    print(f"  input:    {t['input_tokens']:>12,}    output: {t['output_tokens']:>12,}")


def cmd_tips(args):
    db = _ensure_db(args)
    tips = all_tips(db, source=_source(args))
    if not tips:
        print("Token Dashboard: no suggestions")
        return
    for tip in tips:
        print(f"[{tip['category']}] {tip['title']}")
        print(f"  {tip['body']}\n")


def cmd_dashboard(args):
    db = _ensure_db(args)
    if not args.no_scan:
        scan_sources(_claude_dir(args), _codex_dir(args), db, _source(args))
    from token_dashboard.server import run

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8080"))
    url = f"http://{host}:{port}/"
    if not args.no_open:
        webbrowser.open(url)
    print(f"Token Dashboard listening on {url}")
    run(host, port, db, _claude_dir(args), _codex_dir(args), _source(args))


def cmd_migrate_legacy(args):
    target = _db_path(args)
    result = migrate_legacy_db(args.legacy_db or legacy_db_path(), target)
    print(f"Token Dashboard: {result['reason']}; {result['rows']} rows copied")


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", help="SQLite path (default ~/.token-dashboard/token-dashboard.db)")
    common.add_argument("--claude-dir", help="Claude JSONL root (default ~/.claude/projects)")
    common.add_argument("--projects-dir", help="Backward-compatible alias for --claude-dir")
    common.add_argument("--codex-dir", help="Codex rollout root (default ~/.codex/sessions)")
    common.add_argument("--source", choices=("all", "claude", "codex"), default=None)

    p = argparse.ArgumentParser(prog="token-dashboard", description="Local Claude and Codex usage dashboard", parents=[common])
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan",  parents=[common]).set_defaults(func=cmd_scan)
    sub.add_parser("today", parents=[common]).set_defaults(func=cmd_today)
    sub.add_parser("stats", parents=[common]).set_defaults(func=cmd_stats)
    sub.add_parser("tips",  parents=[common]).set_defaults(func=cmd_tips)
    d = sub.add_parser("dashboard", parents=[common])
    d.add_argument("--no-scan", action="store_true")
    d.add_argument("--no-open", action="store_true")
    d.set_defaults(func=cmd_dashboard)
    m = sub.add_parser("migrate-legacy", parents=[common])
    m.add_argument("--legacy-db", help="Legacy Claude database (default ~/.claude/token-dashboard.db)")
    m.set_defaults(func=cmd_migrate_legacy)
    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
