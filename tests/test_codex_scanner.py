import json
import os
import tempfile
import unittest
from pathlib import Path

from token_dashboard.codex_scanner import scan_codex_dir
from token_dashboard.db import connect, expensive_prompts, init_db, overview_totals
from token_dashboard.scanner import scan_sources


class CodexScannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "sessions"
        self.root.mkdir()
        self.rollout = self.root / "rollout-test.jsonl"
        self.db = str(self.tmp / "db.sqlite")
        init_db(self.db)

    def _write(self, extra=None):
        records = [
            {"timestamp":"2026-01-01T00:00:00Z","type":"session_meta","payload":{"id":"s1","cwd":"C:/work/demo","thread_source":"user","cli_version":"1"}},
            {"timestamp":"2026-01-01T00:00:01Z","type":"turn_context","payload":{"turn_id":"t1","model":"gpt-5.6-sol","cwd":"C:/work/demo"}},
            {"timestamp":"2026-01-01T00:00:02Z","type":"event_msg","payload":{"type":"user_message","message":"hello"}},
            {"timestamp":"2026-01-01T00:00:03Z","type":"response_item","payload":{"type":"function_call","name":"exec_command","arguments":"{\"cmd\":\"rg x\"}","call_id":"c1"}},
            {"timestamp":"2026-01-01T00:00:04Z","type":"response_item","payload":{"type":"function_call_output","call_id":"c1","output":"ok"}},
            {"timestamp":"2026-01-01T00:00:05Z","type":"event_msg","payload":{"type":"token_count","info":{"last_token_usage":{"input_tokens":100,"cached_input_tokens":40,"output_tokens":20,"reasoning_output_tokens":7}}}},
            {"timestamp":"2026-01-01T00:00:06Z","type":"response_item","payload":{"type":"reasoning","content":"must not be stored"}},
            {"timestamp":"2026-01-01T00:00:07Z","type":"event_msg","payload":{"type":"agent_message","message":"visible reply"}},
        ]
        records.extend(extra or [])
        self.rollout.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

    def test_usage_math_and_visible_prompt(self):
        self._write()
        scan_codex_dir(self.root, self.db)
        totals = overview_totals(self.db, source="codex")
        self.assertEqual(totals["turns"], 1)
        self.assertEqual(totals["input_tokens"], 60)
        self.assertEqual(totals["cache_read_tokens"], 40)
        self.assertEqual(totals["output_tokens"], 20)
        with connect(self.db) as c:
            user = c.execute("SELECT * FROM messages WHERE type='user'").fetchone()
            self.assertEqual(user["prompt_text"], "hello")
            self.assertTrue(user["uuid"].startswith("codex:"))
            self.assertEqual(user["source"], "codex")
            self.assertEqual(user["turn_key"], "codex:t1")
            self.assertEqual(c.execute("SELECT COUNT(*) FROM messages WHERE prompt_text LIKE '%must not%'").fetchone()[0], 0)

    def test_tools_link_by_call_id(self):
        self._write()
        scan_codex_dir(self.root, self.db)
        with connect(self.db) as c:
            tool = c.execute("SELECT * FROM tool_calls").fetchone()
            self.assertEqual(tool["call_id"], "codex:c1")
            self.assertEqual(tool["tool_name"], "exec_command")
            self.assertIsNotNone(tool["result_tokens"])

    def test_rescan_is_idempotent_and_aborted_usage_counts(self):
        self._write()
        scan_codex_dir(self.root, self.db)
        self.assertEqual(scan_codex_dir(self.root, self.db)["files"], 0)
        extra = [{"timestamp":"2026-01-01T00:00:08Z","type":"event_msg","payload":{"type":"token_count","info":{"last_token_usage":{"input_tokens":30,"cached_input_tokens":10,"output_tokens":5,"reasoning_output_tokens":2}}}}]
        self._write(extra)
        scan_codex_dir(self.root, self.db)
        totals = overview_totals(self.db, source="codex")
        self.assertEqual(totals["input_tokens"], 80)
        self.assertEqual(totals["output_tokens"], 25)
        prompts = expensive_prompts(self.db, source="codex")
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0]["billable_tokens"], 105)

    def test_subagent_metadata_is_retained_when_available(self):
        records = [
            {"timestamp":"2026-01-01T00:00:00Z","type":"session_meta","payload":{"id":"sub1","cwd":"C:/work/demo","thread_source":"subagent","agent_nickname":"reviewer","agent_path":"/root/reviewer","parent_thread_id":"parent1","forked_from_id":"fork1"}},
            {"timestamp":"2026-01-01T00:00:00Z","type":"turn_context","payload":{"turn_id":"st1","model":"gpt-5.6-sol"}},
            {"timestamp":"2026-01-01T00:00:01Z","type":"event_msg","payload":{"type":"user_message","message":"audit"}},
        ]
        self.rollout.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        scan_codex_dir(self.root, self.db)
        with connect(self.db) as c:
            row = c.execute("SELECT is_sidechain,agent_id,parent_session_id,agent_name,agent_path,forked_from_id FROM messages").fetchone()
        self.assertEqual(row["is_sidechain"], 1)
        self.assertEqual(row["agent_id"], "reviewer")
        self.assertEqual(row["parent_session_id"], "codex:parent1")
        self.assertEqual(row["agent_name"], "reviewer")
        self.assertEqual(row["agent_path"], "/root/reviewer")
        self.assertEqual(row["forked_from_id"], "codex:fork1")

    def test_forked_history_before_first_context_is_ignored(self):
        records = [
            {"timestamp":"2026-01-01T00:00:00Z","type":"session_meta","payload":{"id":"fork1","cwd":"C:/work/demo","parent_thread_id":"parent1"}},
            {"timestamp":"2026-01-01T00:00:01Z","type":"event_msg","payload":{"type":"user_message","message":"copied parent prompt"}},
            {"timestamp":"2026-01-01T00:00:02Z","type":"event_msg","payload":{"type":"token_count","info":{"last_token_usage":{"input_tokens":1000,"cached_input_tokens":0,"output_tokens":100}}}},
            {"timestamp":"2026-01-01T00:00:03Z","type":"turn_context","payload":{"turn_id":"child-turn","model":"gpt-5.6-sol"}},
            {"timestamp":"2026-01-01T00:00:04Z","type":"event_msg","payload":{"type":"user_message","message":"child prompt"}},
            {"timestamp":"2026-01-01T00:00:05Z","type":"event_msg","payload":{"type":"token_count","info":{"last_token_usage":{"input_tokens":50,"cached_input_tokens":20,"output_tokens":10}}}},
        ]
        self.rollout.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        scan_codex_dir(self.root, self.db)
        totals = overview_totals(self.db, source="codex")
        self.assertEqual(totals["input_tokens"], 30)
        self.assertEqual(totals["output_tokens"], 10)
        with connect(self.db) as c:
            prompts = [r[0] for r in c.execute("SELECT prompt_text FROM messages WHERE type='user'")]
        self.assertEqual(prompts, ["child prompt"])

    def test_new_turn_without_visible_user_does_not_link_old_prompt(self):
        records = [
            {"timestamp":"2026-01-01T00:00:00Z","type":"session_meta","payload":{"id":"multi1","cwd":"C:/work/demo"}},
            {"timestamp":"2026-01-01T00:00:01Z","type":"turn_context","payload":{"turn_id":"t1","model":"gpt-5.6-sol"}},
            {"timestamp":"2026-01-01T00:00:02Z","type":"event_msg","payload":{"type":"user_message","message":"first"}},
            {"timestamp":"2026-01-01T00:00:03Z","type":"event_msg","payload":{"type":"token_count","info":{"last_token_usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":2}}}},
            {"timestamp":"2026-01-01T00:00:04Z","type":"turn_context","payload":{"turn_id":"t2","model":"gpt-5.6-sol"}},
            {"timestamp":"2026-01-01T00:00:05Z","type":"event_msg","payload":{"type":"token_count","info":{"last_token_usage":{"input_tokens":20,"cached_input_tokens":0,"output_tokens":3}}}},
        ]
        self.rollout.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        scan_codex_dir(self.root, self.db)
        with connect(self.db) as c:
            rows = c.execute("SELECT turn_key,parent_uuid FROM messages WHERE type='assistant' ORDER BY timestamp").fetchall()
        self.assertIsNotNone(rows[0]["parent_uuid"])
        self.assertIsNone(rows[1]["parent_uuid"])
        self.assertEqual(rows[1]["turn_key"], "codex:t2")

    def test_aborted_turn_usage_is_counted_without_agent_message(self):
        records = [
            {"timestamp":"2026-01-01T00:00:00Z","type":"session_meta","payload":{"id":"aborted1","cwd":"C:/work/demo"}},
            {"timestamp":"2026-01-01T00:00:01Z","type":"turn_context","payload":{"turn_id":"t1","model":"gpt-5.6-sol"}},
            {"timestamp":"2026-01-01T00:00:02Z","type":"event_msg","payload":{"type":"user_message","message":"start"}},
            {"timestamp":"2026-01-01T00:00:03Z","type":"event_msg","payload":{"type":"token_count","info":{"last_token_usage":{"input_tokens":40,"cached_input_tokens":15,"output_tokens":7}}}},
        ]
        self.rollout.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        scan_codex_dir(self.root, self.db)
        totals = overview_totals(self.db, source="codex")
        self.assertEqual(totals["input_tokens"], 25)
        self.assertEqual(totals["output_tokens"], 7)

    def test_overlapping_roots_keep_separate_file_checkpoints(self):
        self._write()
        scan_sources(self.root, self.root, self.db, "all")
        with connect(self.db) as c:
            sources = {r[0] for r in c.execute("SELECT source FROM files WHERE path=?", (str(self.rollout),))}
        self.assertEqual(sources, {"claude", "codex"})


if __name__ == "__main__":
    unittest.main()
