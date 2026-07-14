import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from token_dashboard.db import connect, init_db, migrate_legacy_db, overview_totals
from token_dashboard.scanner import scan_dir


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.path.join(self.tmp, "old.db")
        self.new = os.path.join(self.tmp, "new.db")
        with sqlite3.connect(self.old) as c:
            c.executescript("""
            CREATE TABLE messages (
              uuid TEXT PRIMARY KEY,parent_uuid TEXT,session_id TEXT NOT NULL,project_slug TEXT NOT NULL,
              cwd TEXT,git_branch TEXT,cc_version TEXT,entrypoint TEXT,type TEXT NOT NULL,is_sidechain INTEGER DEFAULT 0,
              agent_id TEXT,timestamp TEXT NOT NULL,model TEXT,stop_reason TEXT,prompt_id TEXT,message_id TEXT,
              input_tokens INTEGER DEFAULT 0,output_tokens INTEGER DEFAULT 0,cache_read_tokens INTEGER DEFAULT 0,
              cache_create_5m_tokens INTEGER DEFAULT 0,cache_create_1h_tokens INTEGER DEFAULT 0,
              prompt_text TEXT,prompt_chars INTEGER,tool_calls_json TEXT
            );
            CREATE TABLE tool_calls (id INTEGER PRIMARY KEY,message_uuid TEXT,session_id TEXT,project_slug TEXT,tool_name TEXT,target TEXT,result_tokens INTEGER,is_error INTEGER,timestamp TEXT);
            CREATE TABLE files (path TEXT PRIMARY KEY,mtime REAL,bytes_read INTEGER,scanned_at REAL);
            CREATE TABLE plan (k TEXT PRIMARY KEY,v TEXT);
            CREATE TABLE dismissed_tips (tip_key TEXT PRIMARY KEY,dismissed_at REAL);
            INSERT INTO messages(uuid,session_id,project_slug,type,timestamp,input_tokens,output_tokens)
              VALUES('a1','s1','p','assistant','2026-01-01T00:00:00Z',10,20);
            INSERT INTO tool_calls(message_uuid,session_id,project_slug,tool_name,is_error,timestamp)
              VALUES('a1','s1','p','Read',0,'2026-01-01T00:00:00Z');
            INSERT INTO files VALUES('old.jsonl',1,100,2);
            INSERT INTO plan VALUES('k','v');
            """)

    def test_copy_is_namespaced_and_source_tagged(self):
        result = migrate_legacy_db(self.old, self.new)
        self.assertTrue(result["migrated"])
        with connect(self.new) as c:
            msg = c.execute("SELECT * FROM messages").fetchone()
            tool = c.execute("SELECT * FROM tool_calls").fetchone()
            self.assertEqual(msg["uuid"], "claude:a1")
            self.assertEqual(msg["session_id"], "claude:s1")
            self.assertEqual(msg["source"], "claude")
            self.assertEqual(tool["message_uuid"], "claude:a1")
            self.assertEqual(tool["source"], "claude")

    def test_is_idempotent(self):
        migrate_legacy_db(self.old, self.new)
        second = migrate_legacy_db(self.old, self.new)
        self.assertFalse(second["migrated"])
        self.assertEqual(overview_totals(self.new)["input_tokens"], 10)

    def test_legacy_database_is_not_modified(self):
        hot = Path(self.tmp) / "hot.db"
        connection = sqlite3.connect(self.old)
        try:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("UPDATE messages SET input_tokens=999 WHERE uuid='a1'")
            journal = Path(f"{self.old}-journal")
            self.assertTrue(journal.is_file())
            shutil.copy2(self.old, hot)
            shutil.copy2(journal, Path(f"{hot}-journal"))
        finally:
            connection.rollback()
            connection.close()

        before_db = hot.read_bytes()
        before_journal = Path(f"{hot}-journal").read_bytes()
        result = migrate_legacy_db(hot, self.new)
        self.assertTrue(result["migrated"])
        self.assertEqual(hot.read_bytes(), before_db)
        self.assertEqual(Path(f"{hot}-journal").read_bytes(), before_journal)
        self.assertEqual(overview_totals(self.new)["input_tokens"], 10)

    def test_history_survives_when_jsonls_are_absent(self):
        migrate_legacy_db(self.old, self.new)
        scan_dir(Path(self.tmp) / "no-jsonls", self.new)
        self.assertEqual(overview_totals(self.new)["output_tokens"], 20)

    def test_rejects_identical_legacy_and_target_paths(self):
        before = Path(self.old).read_bytes()
        with self.assertRaises(ValueError):
            migrate_legacy_db(self.old, self.old)
        self.assertEqual(Path(self.old).read_bytes(), before)

    def test_populated_target_keeps_newer_rows_and_dedupes_tools(self):
        init_db(self.new)
        with connect(self.new) as c:
            c.execute("INSERT INTO messages(uuid,session_id,project_slug,type,timestamp,input_tokens,source) VALUES('claude:a1','claude:s1','p','assistant','2026-01-01T00:00:00Z',999,'claude')")
            c.execute("INSERT INTO tool_calls(message_uuid,session_id,project_slug,tool_name,target,result_tokens,is_error,timestamp,source) VALUES('claude:a1','claude:s1','p','Read',NULL,NULL,0,'2026-01-01T00:00:00Z','claude')")
            c.commit()
        migrate_legacy_db(self.old, self.new)
        with connect(self.new) as c:
            self.assertEqual(c.execute("SELECT input_tokens FROM messages WHERE uuid='claude:a1'").fetchone()[0], 999)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM tool_calls").fetchone()[0], 1)

    def test_old_schema_without_message_id_migrates(self):
        c = sqlite3.connect(self.old)
        c.execute("ALTER TABLE messages DROP COLUMN message_id")
        c.commit()
        c.close()
        migrate_legacy_db(self.old, self.new)
        self.assertEqual(overview_totals(self.new)["input_tokens"], 10)

    def test_stale_migrating_file_is_recovered(self):
        stale = Path(self.new + ".migrating")
        stale.write_text("interrupted", encoding="utf-8")
        migrate_legacy_db(self.old, self.new)
        self.assertTrue(Path(self.new).is_file())
        self.assertFalse(stale.exists())


if __name__ == "__main__":
    unittest.main()
