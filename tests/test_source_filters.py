import os
import tempfile
import unittest

from token_dashboard.db import (
    connect, daily_token_breakdown, expensive_prompts, init_db, model_breakdown,
    overview_totals, project_summary, recent_sessions, session_turns,
    skill_breakdown, tool_token_breakdown,
)


class SourceFilterTests(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "db.sqlite")
        init_db(self.db)
        with connect(self.db) as c:
            c.executescript("""
            INSERT INTO messages(uuid,session_id,project_slug,type,timestamp,prompt_text,source)
              VALUES('c-u','s1','p','user','2026-01-01T00:00:00Z','claude prompt','claude');
            INSERT INTO messages(uuid,session_id,project_slug,type,timestamp,input_tokens,source)
              VALUES('c1','s1','p','assistant','2026-01-01T00:00:01Z',10,'claude');
            UPDATE messages SET parent_uuid='c-u',model='claude-opus-4-7' WHERE uuid='c1';
            INSERT INTO messages(uuid,session_id,project_slug,type,timestamp,prompt_text,source)
              VALUES('x-u','s2','p','user','2026-01-01T00:00:00Z','codex prompt','codex');
            INSERT INTO messages(uuid,session_id,project_slug,type,timestamp,input_tokens,source)
              VALUES('x1','s2','p','assistant','2026-01-01T00:00:01Z',20,'codex');
            UPDATE messages SET parent_uuid='x-u',model='gpt-5.6-sol' WHERE uuid='x1';
            INSERT INTO tool_calls(message_uuid,session_id,project_slug,tool_name,target,is_error,timestamp,source)
              VALUES('c1','s1','p','Read','a',0,'2026-01-01T00:00:01Z','claude');
            INSERT INTO tool_calls(message_uuid,session_id,project_slug,tool_name,target,is_error,timestamp,source)
              VALUES('x1','s2','p','exec_command','b',0,'2026-01-01T00:00:01Z','codex');
            INSERT INTO tool_calls(message_uuid,session_id,project_slug,tool_name,target,is_error,timestamp,source)
              VALUES('c1','s1','p','Skill','done',0,'2026-01-01T00:00:01Z','claude');
            """)
            c.commit()

    def test_overview_filters_each_source(self):
        self.assertEqual(overview_totals(self.db, source="all")["input_tokens"], 30)
        self.assertEqual(overview_totals(self.db, source="claude")["input_tokens"], 10)
        self.assertEqual(overview_totals(self.db, source="codex")["input_tokens"], 20)
        self.assertEqual(expensive_prompts(self.db, source="claude")[0]["prompt_text"], "claude prompt")
        self.assertEqual(expensive_prompts(self.db, source="codex")[0]["prompt_text"], "codex prompt")
        self.assertEqual(recent_sessions(self.db, source="codex")[0]["source"], "codex")
        self.assertTrue(all(r["source"] == "claude" for r in session_turns(self.db, "s1", "claude")))
        self.assertEqual(daily_token_breakdown(self.db, source="codex")[0]["input_tokens"], 20)
        self.assertEqual(model_breakdown(self.db, source="codex")[0]["model"], "gpt-5.6-sol")
        self.assertEqual(tool_token_breakdown(self.db, source="claude")[0]["source"], "claude")
        self.assertEqual(skill_breakdown(self.db, source="claude")[0]["skill"], "done")
        self.assertEqual(skill_breakdown(self.db, source="codex"), [])

    def test_project_rows_keep_sources_separate(self):
        rows = project_summary(self.db)
        self.assertEqual({r["source"] for r in rows}, {"claude", "codex"})


if __name__ == "__main__":
    unittest.main()
