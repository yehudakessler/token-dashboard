"""Codex rollout parser using only visible prompts, tool links, and usage events."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Union

from .db import connect


INSERT_MESSAGE = """
INSERT OR REPLACE INTO messages (
  uuid,parent_uuid,session_id,project_slug,cwd,git_branch,cc_version,entrypoint,
  type,is_sidechain,agent_id,timestamp,model,stop_reason,prompt_id,message_id,
  input_tokens,output_tokens,cache_read_tokens,cache_create_5m_tokens,cache_create_1h_tokens,
  prompt_text,prompt_chars,tool_calls_json,source,turn_key,parent_session_id,agent_name,agent_path,forked_from_id
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""

INSERT_TOOL = """
INSERT INTO tool_calls (
  message_uuid,session_id,project_slug,tool_name,target,result_tokens,is_error,timestamp,source,call_id
) VALUES (?,?,?,?,?,?,?,?,?,?)
"""


def _id(value) -> str | None:
    if value is None:
        return None
    value = str(value)
    return value if value.startswith("codex:") else f"codex:{value}"


def _slug(cwd: str | None) -> str:
    if not cwd:
        return "codex-unknown"
    return re.sub(r"[:\\/ ]", "-", cwd.rstrip("/\\"))


def _target(name: str, arguments) -> str | None:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return arguments[:500]
    if not isinstance(arguments, dict):
        return None
    for key in ("path", "file_path", "cmd", "command", "query", "url", "target", "session_id"):
        value = arguments.get(key)
        if isinstance(value, str):
            return value[:500]
    return json.dumps(arguments, ensure_ascii=False)[:500]


def _result_tokens(value) -> int:
    if isinstance(value, str):
        return len(value) // 4
    return len(json.dumps(value, ensure_ascii=False)) // 4 if value is not None else 0


def _meta_text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load_records(path: Path) -> list[dict]:
    records = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line_no, line in enumerate(f, 1):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                rec["_line"] = line_no
                records.append(rec)
    return records


def parse_rollout(path: Path, conn) -> dict:
    """Replace one rollout atomically; one token_count event becomes one usage row."""
    records = _load_records(path)
    meta = next((r.get("payload") or {} for r in records if r.get("type") == "session_meta"), {})
    raw_session = meta.get("id") or path.stem
    session_id = _id(raw_session)
    cwd = meta.get("cwd")
    project_slug = _slug(cwd)
    thread_source = meta.get("thread_source") or meta.get("source") or "user"
    agent_name = _meta_text(meta.get("agent_nickname") or meta.get("agent_name"))
    agent_path = _meta_text(meta.get("agent_path"))
    parent_session_id = _id(meta.get("parent_thread_id") or meta.get("parent_session_id"))
    forked_from_id = _id(meta.get("forked_from_id"))
    sidechain = 1 if parent_session_id or agent_name or agent_path or thread_source != "user" else 0
    agent_id = _meta_text(meta.get("agent_id")) or agent_name or agent_path or (_meta_text(thread_source) if sidechain else None)
    version = meta.get("cli_version")
    entrypoint = meta.get("originator") or meta.get("source")

    conn.execute("DELETE FROM tool_calls WHERE session_id=? AND source='codex'", (session_id,))
    conn.execute("DELETE FROM messages WHERE session_id=? AND source='codex'", (session_id,))

    model = None
    turn_id = None
    context_seen = False
    latest_user = None
    messages = tools = 0
    pending_calls: dict[str, int] = {}

    for rec in records:
        timestamp = rec.get("timestamp") or meta.get("timestamp") or ""
        kind = rec.get("type")
        payload = rec.get("payload") or {}
        if kind == "turn_context":
            new_turn_id = payload.get("turn_id") or payload.get("turnId")
            if new_turn_id != turn_id:
                latest_user = None
                pending_calls.clear()
            model = payload.get("model") or model
            turn_id = new_turn_id or turn_id
            cwd = payload.get("cwd") or cwd
            project_slug = _slug(cwd)
            context_seen = True
            continue

        # Forked/subagent rollouts may start with copied parent history. Only
        # events after this rollout's first turn context belong to the child.
        if not context_seen:
            continue

        if kind == "event_msg" and payload.get("type") == "user_message":
            text = payload.get("message")
            if not isinstance(text, str):
                continue
            raw_uuid = f"{raw_session}:user:{rec['_line']}"
            latest_user = _id(raw_uuid)
            conn.execute(INSERT_MESSAGE, (
                latest_user,None,session_id,project_slug,cwd,None,version,entrypoint,
                "user",sidechain,agent_id,timestamp,None,None,_id(turn_id),None,
                0,0,0,0,0,text,len(text),None,"codex",_id(turn_id),
                parent_session_id,agent_name,agent_path,forked_from_id,
            ))
            messages += 1
            continue

        if kind == "response_item" and payload.get("type") in {"function_call", "custom_tool_call"}:
            call_id = _id(payload.get("call_id") or f"{raw_session}:call:{rec['_line']}")
            name = payload.get("name") or "custom_tool"
            target = _target(name, payload.get("arguments") or payload.get("input"))
            message_uuid = latest_user or _id(f"{raw_session}:turn:{turn_id or 'unknown'}")
            cur = conn.execute(INSERT_TOOL, (
                message_uuid,session_id,project_slug,name,target,None,0,timestamp,"codex",call_id,
            ))
            pending_calls[call_id] = cur.lastrowid
            tools += 1
            continue

        if kind == "response_item" and payload.get("type") in {"function_call_output", "custom_tool_call_output"}:
            call_id = _id(payload.get("call_id"))
            output = payload.get("output")
            row_id = pending_calls.get(call_id)
            is_error = 1 if isinstance(output, str) and ("error" in output.lower() or "exit code: 1" in output.lower()) else 0
            if row_id:
                conn.execute(
                    "UPDATE tool_calls SET result_tokens=?,is_error=? WHERE id=?",
                    (_result_tokens(output), is_error, row_id),
                )
            else:
                message_uuid = latest_user or _id(f"{raw_session}:turn:{turn_id or 'unknown'}")
                conn.execute(INSERT_TOOL, (
                    message_uuid,session_id,project_slug,"_tool_result",call_id,_result_tokens(output),is_error,
                    timestamp,"codex",call_id,
                ))
                tools += 1
            continue

        if kind == "event_msg" and payload.get("type") == "token_count":
            usage = ((payload.get("info") or {}).get("last_token_usage") or {})
            if not usage:
                continue
            total_input = int(usage.get("input_tokens") or 0)
            cached = int(usage.get("cached_input_tokens") or 0)
            # Codex output_tokens already includes reasoning_output_tokens.
            output = int(usage.get("output_tokens") or 0)
            raw_uuid = f"{raw_session}:usage:{rec['_line']}"
            conn.execute(INSERT_MESSAGE, (
                _id(raw_uuid),latest_user,session_id,project_slug,cwd,None,version,entrypoint,
                "assistant",sidechain,agent_id,timestamp,model,None,_id(turn_id),None,
                max(total_input-cached,0),output,cached,0,0,None,None,None,"codex",_id(turn_id),
                parent_session_id,agent_name,agent_path,forked_from_id,
            ))
            messages += 1

    return {"messages": messages, "tools": tools, "session_id": session_id}


def scan_codex_dir(sessions_root: Union[str, Path], db_path: Union[str, Path]) -> dict:
    root = Path(sessions_root)
    totals = {"messages": 0, "tools": 0, "files": 0}
    if not root.is_dir():
        return totals
    with connect(db_path) as conn:
        for path in root.rglob("rollout-*.jsonl"):
            try:
                stat = path.stat()
            except OSError:
                continue
            row = conn.execute("SELECT mtime,bytes_read FROM files WHERE path=? AND source='codex'", (str(path),)).fetchone()
            if row and row["mtime"] == stat.st_mtime and row["bytes_read"] == stat.st_size:
                continue
            sub = parse_rollout(path, conn)
            conn.execute(
                "INSERT OR REPLACE INTO files(path,mtime,bytes_read,scanned_at,source) VALUES(?,?,?,?, 'codex')",
                (str(path),stat.st_mtime,stat.st_size,time.time()),
            )
            totals["messages"] += sub["messages"]
            totals["tools"] += sub["tools"]
            totals["files"] += 1
        conn.commit()
    return totals
