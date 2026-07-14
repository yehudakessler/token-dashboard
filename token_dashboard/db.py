"""SQLite schema, safe legacy migration, and shared query helpers."""
from __future__ import annotations

import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Union


SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
  path        TEXT    NOT NULL,
  mtime       REAL    NOT NULL,
  bytes_read  INTEGER NOT NULL,
  scanned_at  REAL    NOT NULL,
  source      TEXT    NOT NULL DEFAULT 'claude',
  PRIMARY KEY (source, path)
);

CREATE TABLE IF NOT EXISTS messages (
  uuid                    TEXT PRIMARY KEY,
  parent_uuid             TEXT,
  session_id              TEXT NOT NULL,
  project_slug            TEXT NOT NULL,
  cwd                     TEXT,
  git_branch              TEXT,
  cc_version              TEXT,
  entrypoint              TEXT,
  type                    TEXT NOT NULL,
  is_sidechain            INTEGER NOT NULL DEFAULT 0,
  agent_id                TEXT,
  timestamp               TEXT NOT NULL,
  model                   TEXT,
  stop_reason             TEXT,
  prompt_id               TEXT,
  message_id              TEXT,
  input_tokens            INTEGER NOT NULL DEFAULT 0,
  output_tokens           INTEGER NOT NULL DEFAULT 0,
  cache_read_tokens       INTEGER NOT NULL DEFAULT 0,
  cache_create_5m_tokens  INTEGER NOT NULL DEFAULT 0,
  cache_create_1h_tokens  INTEGER NOT NULL DEFAULT 0,
  prompt_text             TEXT,
  prompt_chars            INTEGER,
  tool_calls_json         TEXT,
  source                  TEXT NOT NULL DEFAULT 'claude',
  turn_key                TEXT,
  parent_session_id       TEXT,
  agent_name              TEXT,
  agent_path              TEXT,
  forked_from_id          TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_session   ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_project   ON messages(project_slug);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_model     ON messages(model);
CREATE INDEX IF NOT EXISTS idx_messages_msgid     ON messages(session_id, message_id);
CREATE INDEX IF NOT EXISTS idx_messages_source    ON messages(source);

CREATE TABLE IF NOT EXISTS tool_calls (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  message_uuid  TEXT    NOT NULL,
  session_id    TEXT    NOT NULL,
  project_slug  TEXT    NOT NULL,
  tool_name     TEXT    NOT NULL,
  target        TEXT,
  result_tokens INTEGER,
  is_error      INTEGER NOT NULL DEFAULT 0,
  timestamp     TEXT    NOT NULL,
  source        TEXT    NOT NULL DEFAULT 'claude',
  call_id       TEXT
);
CREATE INDEX IF NOT EXISTS idx_tools_session ON tool_calls(session_id);
CREATE INDEX IF NOT EXISTS idx_tools_name    ON tool_calls(tool_name);
CREATE INDEX IF NOT EXISTS idx_tools_target  ON tool_calls(target);
CREATE INDEX IF NOT EXISTS idx_tools_source  ON tool_calls(source);

CREATE TABLE IF NOT EXISTS plan (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS dismissed_tips (
  tip_key TEXT PRIMARY KEY,
  dismissed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS legacy_migrations (
  name TEXT PRIMARY KEY,
  completed_at REAL NOT NULL,
  row_count INTEGER NOT NULL DEFAULT 0
);
"""


def default_db_path() -> Path:
    return Path.home() / ".token-dashboard" / "token-dashboard.db"


def legacy_db_path() -> Path:
    return Path.home() / ".claude" / "token-dashboard.db"


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn, table: str, definition: str) -> None:
    name = definition.split()[0]
    if name not in _columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def _migrate_files_primary_key(conn) -> None:
    info = list(conn.execute("PRAGMA table_info(files)"))
    primary = [row[1] for row in sorted((r for r in info if r[5]), key=lambda r: r[5])]
    if primary == ["source", "path"]:
        return
    conn.execute("ALTER TABLE files RENAME TO files_path_only")
    conn.execute("""
      CREATE TABLE files (
        path TEXT NOT NULL,mtime REAL NOT NULL,bytes_read INTEGER NOT NULL,scanned_at REAL NOT NULL,
        source TEXT NOT NULL DEFAULT 'claude',PRIMARY KEY(source,path)
      )
    """)
    conn.execute("""
      INSERT OR REPLACE INTO files(path,mtime,bytes_read,scanned_at,source)
      SELECT path,mtime,bytes_read,scanned_at,COALESCE(source,'claude') FROM files_path_only
    """)
    conn.execute("DROP TABLE files_path_only")


def init_db(path: Union[str, Path]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as c:
        # Existing installations predate source/turn/call metadata. Add those
        # columns before creating indexes that reference them; never erase rows.
        c.executescript("""
        CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, mtime REAL NOT NULL, bytes_read INTEGER NOT NULL, scanned_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS messages (uuid TEXT PRIMARY KEY, parent_uuid TEXT, session_id TEXT NOT NULL, project_slug TEXT NOT NULL, cwd TEXT, git_branch TEXT, cc_version TEXT, entrypoint TEXT, type TEXT NOT NULL, is_sidechain INTEGER NOT NULL DEFAULT 0, agent_id TEXT, timestamp TEXT NOT NULL, model TEXT, stop_reason TEXT, prompt_id TEXT, input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0, cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_create_5m_tokens INTEGER NOT NULL DEFAULT 0, cache_create_1h_tokens INTEGER NOT NULL DEFAULT 0, prompt_text TEXT, prompt_chars INTEGER, tool_calls_json TEXT);
        CREATE TABLE IF NOT EXISTS tool_calls (id INTEGER PRIMARY KEY AUTOINCREMENT, message_uuid TEXT NOT NULL, session_id TEXT NOT NULL, project_slug TEXT NOT NULL, tool_name TEXT NOT NULL, target TEXT, result_tokens INTEGER, is_error INTEGER NOT NULL DEFAULT 0, timestamp TEXT NOT NULL);
        """)
        _add_column(c, "files", "source TEXT NOT NULL DEFAULT 'claude'")
        _add_column(c, "messages", "message_id TEXT")
        _add_column(c, "messages", "source TEXT NOT NULL DEFAULT 'claude'")
        _add_column(c, "messages", "turn_key TEXT")
        _add_column(c, "messages", "parent_session_id TEXT")
        _add_column(c, "messages", "agent_name TEXT")
        _add_column(c, "messages", "agent_path TEXT")
        _add_column(c, "messages", "forked_from_id TEXT")
        _add_column(c, "tool_calls", "source TEXT NOT NULL DEFAULT 'claude'")
        _add_column(c, "tool_calls", "call_id TEXT")
        _migrate_files_primary_key(c)
        c.executescript(SCHEMA)
    c.close()


@contextmanager
def connect(path: Union[str, Path]):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def _prefixed(source: str, value):
    if value is None:
        return None
    value = str(value)
    return value if value.startswith(f"{source}:") else f"{source}:{value}"


def migrate_legacy_db(legacy_path: Union[str, Path], target_path: Union[str, Path]) -> dict:
    """Copy the old Claude-only DB into the combined DB without touching it."""
    legacy_path, target_path = Path(legacy_path), Path(target_path)
    if not legacy_path.is_file():
        return {"migrated": False, "reason": "legacy database not found", "rows": 0}
    if legacy_path.resolve() == target_path.resolve():
        raise ValueError("legacy and target database paths must be different")
    target_existed = target_path.exists()
    working_path = target_path if target_existed else target_path.with_name(target_path.name + ".migrating")
    if not target_existed and working_path.exists():
        working_path.unlink()
    init_db(working_path)
    marker = f"claude-db:{legacy_path.resolve()}"
    with connect(working_path) as dst:
        if dst.execute("SELECT 1 FROM legacy_migrations WHERE name=?", (marker,)).fetchone():
            return {"migrated": False, "reason": "already migrated", "rows": 0}
        src = sqlite3.connect(f"file:{legacy_path.as_posix()}?mode=ro", uri=True)
        src.row_factory = sqlite3.Row
        copied = 0
        try:
            dst.execute("BEGIN")
            if "messages" in {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
                src_cols = _columns(src, "messages")
                dst_cols = _columns(dst, "messages")
                cols = [c for c in dst_cols if c in src_cols and c not in {"source", "turn_key"}]
                for row in src.execute(f"SELECT {','.join(cols)} FROM messages"):
                    data = dict(row)
                    for key in ("uuid", "parent_uuid", "session_id", "prompt_id", "message_id"):
                        if key in data:
                            data[key] = _prefixed("claude", data[key])
                    data["source"] = "claude"
                    names = list(data)
                    cursor = dst.execute(
                        f"INSERT OR IGNORE INTO messages ({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
                        [data[n] for n in names],
                    )
                    copied += max(cursor.rowcount, 0)
            if "tool_calls" in {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
                src_cols = _columns(src, "tool_calls")
                cols = [c for c in _columns(dst, "tool_calls") if c in src_cols and c not in {"id", "source", "call_id"}]
                for row in src.execute(f"SELECT {','.join(cols)} FROM tool_calls"):
                    data = dict(row)
                    for key in ("message_uuid", "session_id"):
                        data[key] = _prefixed("claude", data.get(key))
                    data["source"] = "claude"
                    names = list(data)
                    duplicate = dst.execute("""
                      SELECT 1 FROM tool_calls WHERE source='claude'
                       AND message_uuid=? AND session_id=? AND project_slug=? AND tool_name=?
                       AND target IS ? AND result_tokens IS ? AND is_error=? AND timestamp=? LIMIT 1
                    """, (
                        data.get("message_uuid"),data.get("session_id"),data.get("project_slug"),
                        data.get("tool_name"),data.get("target"),data.get("result_tokens"),
                        data.get("is_error"),data.get("timestamp"),
                    )).fetchone()
                    if not duplicate:
                        dst.execute(
                            f"INSERT INTO tool_calls ({','.join(names)}) VALUES ({','.join('?' for _ in names)})",
                            [data[n] for n in names],
                        )
                        copied += 1
            if "files" in {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
                for row in src.execute("SELECT path,mtime,bytes_read,scanned_at FROM files"):
                    dst.execute(
                        "INSERT OR IGNORE INTO files(path,mtime,bytes_read,scanned_at,source) VALUES(?,?,?,?, 'claude')",
                        tuple(row),
                    )
            for table, keys in (("plan", ("k", "v")), ("dismissed_tips", ("tip_key", "dismissed_at"))):
                if table in {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}:
                    for row in src.execute(f"SELECT {','.join(keys)} FROM {table}"):
                        dst.execute(
                            f"INSERT OR IGNORE INTO {table} ({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})",
                            tuple(row),
                        )
            dst.execute(
                "INSERT INTO legacy_migrations(name,completed_at,row_count) VALUES(?,?,?)",
                (marker, time.time(), copied),
            )
            dst.commit()
        except Exception:
            dst.rollback()
            raise
        finally:
            src.close()
    if not target_existed:
        working_path.replace(target_path)
    return {"migrated": True, "reason": "copied", "rows": copied}


def _source_value(source: str | None) -> str:
    return source if source in {"claude", "codex"} else "all"


def _where(since=None, until=None, source="all", col="timestamp", prefix=""):
    where, args = [], []
    p = f"{prefix}." if prefix else ""
    if since:
        where.append(f"{p}{col} >= ?"); args.append(since)
    if until:
        where.append(f"{p}{col} < ?"); args.append(until)
    source = _source_value(source)
    if source != "all":
        where.append(f"{p}source = ?"); args.append(source)
    return ((" AND " + " AND ".join(where)) if where else "", args)


def _encode_slug(path: str) -> str:
    return re.sub(r"[:\\/ ]", "-", path)


def _walk_to_root(cwd: str, slug: str) -> Optional[str]:
    if not cwd or not slug:
        return None
    trimmed = cwd.rstrip("/\\")
    sep = "\\" if "\\" in trimmed else "/"
    parts = trimmed.split(sep)
    for i in range(len(parts), 0, -1):
        if _encode_slug(sep.join(parts[:i])) == slug:
            return parts[i - 1] or None
    return None


def project_name_for(cwd: Optional[str], fallback_slug: str) -> str:
    name = _walk_to_root(cwd or "", fallback_slug or "")
    if name:
        return name
    if cwd:
        tail = cwd.rstrip("/\\").split("\\" if "\\" in cwd else "/")[-1]
        if tail:
            return tail
    parts = [p for p in re.split(r"-+", fallback_slug or "") if p]
    return parts[-1] if parts else (fallback_slug or "")


def best_project_name(cwds, slug: str) -> str:
    cwds = [c for c in (cwds or []) if c]
    for cwd in cwds:
        name = _walk_to_root(cwd, slug)
        if name:
            return name
    return project_name_for(cwds[0] if cwds else None, slug)


def overview_totals(db_path, since=None, until=None, source="all") -> dict:
    rng, args = _where(since, until, source)
    sql = f"""SELECT COUNT(DISTINCT session_id) sessions,
      SUM(CASE WHEN type='user' THEN 1 ELSE 0 END) turns,
      COALESCE(SUM(input_tokens),0) input_tokens, COALESCE(SUM(output_tokens),0) output_tokens,
      COALESCE(SUM(cache_read_tokens),0) cache_read_tokens,
      COALESCE(SUM(cache_create_5m_tokens),0) cache_create_5m_tokens,
      COALESCE(SUM(cache_create_1h_tokens),0) cache_create_1h_tokens
      FROM messages WHERE 1=1 {rng}"""
    with connect(db_path) as c:
        return dict(c.execute(sql, args).fetchone())


def expensive_prompts(db_path, limit=50, sort="tokens", source="all") -> list:
    order = "u.timestamp DESC" if sort == "recent" else "billable_tokens DESC"
    src, args = _where(source=source, prefix="u")
    sql = f"""SELECT u.uuid user_uuid,u.session_id,u.project_slug,u.timestamp,u.prompt_text,u.prompt_chars,
      u.source,MIN(a.uuid) assistant_uuid,MAX(a.model) model,
      SUM(COALESCE(a.input_tokens,0)+COALESCE(a.output_tokens,0)+COALESCE(a.cache_create_5m_tokens,0)+COALESCE(a.cache_create_1h_tokens,0)) billable_tokens,
      SUM(COALESCE(a.cache_read_tokens,0)) cache_read_tokens
      FROM messages u JOIN messages a ON a.parent_uuid=u.uuid AND a.type='assistant' AND a.source=u.source
      WHERE u.type='user' AND u.prompt_text IS NOT NULL {src}
      GROUP BY u.uuid,u.session_id,u.project_slug,u.timestamp,u.prompt_text,u.prompt_chars,u.source
      ORDER BY {order} LIMIT ?"""
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, (*args, limit))]


def project_summary(db_path, since=None, until=None, source="all") -> list:
    rng, args = _where(since, until, source, prefix="m")
    sql = f"""SELECT project_slug,source,COUNT(DISTINCT session_id) sessions,
      SUM(CASE WHEN type='user' THEN 1 ELSE 0 END) turns,COALESCE(SUM(input_tokens),0) input_tokens,
      COALESCE(SUM(output_tokens),0) output_tokens,COALESCE(SUM(input_tokens),0)+COALESCE(SUM(output_tokens),0)+COALESCE(SUM(cache_create_5m_tokens),0)+COALESCE(SUM(cache_create_1h_tokens),0) billable_tokens,
      COALESCE(SUM(cache_read_tokens),0) cache_read_tokens FROM messages m WHERE 1=1 {rng}
      GROUP BY source,project_slug ORDER BY billable_tokens DESC"""
    with connect(db_path) as c:
        rows = [dict(r) for r in c.execute(sql, args)]
        for row in rows:
            cwds = [x["cwd"] for x in c.execute("SELECT DISTINCT cwd FROM messages WHERE project_slug=? AND source=? AND cwd IS NOT NULL", (row["project_slug"], row["source"]))]
            row["project_name"] = best_project_name(cwds, row["project_slug"])
        return rows


def tool_token_breakdown(db_path, since=None, until=None, source="all") -> list:
    rng, args = _where(since, until, source)
    sql = f"SELECT tool_name,source,COUNT(*) calls,COALESCE(SUM(result_tokens),0) result_tokens FROM tool_calls WHERE tool_name!='_tool_result' {rng} GROUP BY source,tool_name ORDER BY calls DESC"
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, args)]


def recent_sessions(db_path, limit=20, since=None, until=None, source="all") -> list:
    rng, args = _where(since, until, source, prefix="m")
    sql = f"""SELECT session_id,project_slug,source,MIN(timestamp) started,MAX(timestamp) ended,
      SUM(CASE WHEN type='user' THEN 1 ELSE 0 END) turns,COALESCE(SUM(input_tokens),0)+COALESCE(SUM(output_tokens),0) tokens
      FROM messages m WHERE 1=1 {rng} GROUP BY source,session_id,project_slug ORDER BY ended DESC LIMIT ?"""
    with connect(db_path) as c:
        rows = [dict(r) for r in c.execute(sql, (*args, limit))]
        for row in rows:
            cwds = [x["cwd"] for x in c.execute("SELECT DISTINCT cwd FROM messages WHERE project_slug=? AND source=? AND cwd IS NOT NULL", (row["project_slug"], row["source"]))]
            row["project_name"] = best_project_name(cwds, row["project_slug"])
        return rows


def session_turns(db_path, session_id: str, source="all") -> list:
    rng, args = _where(source=source)
    sql = f"""SELECT uuid,parent_uuid,type,timestamp,model,is_sidechain,agent_id,input_tokens,output_tokens,
      cache_read_tokens,cache_create_5m_tokens,cache_create_1h_tokens,prompt_text,prompt_chars,tool_calls_json,
      project_slug,cwd,source,turn_key,parent_session_id,agent_name,agent_path,forked_from_id
      FROM messages WHERE session_id=? {rng} ORDER BY timestamp,uuid"""
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, (session_id, *args))]


def daily_token_breakdown(db_path, since=None, until=None, source="all") -> list:
    rng, args = _where(since, until, source)
    sql = f"""SELECT substr(timestamp,1,10) day,COALESCE(SUM(input_tokens),0) input_tokens,
      COALESCE(SUM(output_tokens),0) output_tokens,COALESCE(SUM(cache_read_tokens),0) cache_read_tokens,
      COALESCE(SUM(cache_create_5m_tokens),0)+COALESCE(SUM(cache_create_1h_tokens),0) cache_create_tokens
      FROM messages WHERE timestamp IS NOT NULL {rng} GROUP BY day ORDER BY day"""
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, args)]


def skill_breakdown(db_path, since=None, until=None, source="all") -> list:
    rng, args = _where(since, until, source)
    sql = f"""SELECT target skill,COUNT(*) invocations,COUNT(DISTINCT session_id) sessions,MAX(timestamp) last_used
      FROM tool_calls WHERE tool_name='Skill' AND target IS NOT NULL AND target!='' {rng}
      GROUP BY target ORDER BY invocations DESC"""
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, args)]


def model_breakdown(db_path, since=None, until=None, source="all") -> list:
    rng, args = _where(since, until, source)
    sql = f"""SELECT COALESCE(model,'unknown') model,source,COUNT(*) turns,
      COALESCE(SUM(input_tokens),0) input_tokens,COALESCE(SUM(output_tokens),0) output_tokens,
      COALESCE(SUM(cache_read_tokens),0) cache_read_tokens,COALESCE(SUM(cache_create_5m_tokens),0) cache_create_5m_tokens,
      COALESCE(SUM(cache_create_1h_tokens),0) cache_create_1h_tokens FROM messages
      WHERE type='assistant' {rng} GROUP BY source,model ORDER BY input_tokens+output_tokens+cache_create_5m_tokens+cache_create_1h_tokens DESC"""
    with connect(db_path) as c:
        return [dict(r) for r in c.execute(sql, args)]
