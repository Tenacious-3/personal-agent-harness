"""Actual SQLite snapshot/migration checks; no running user state is accessed."""
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

from harness.persistence.store import LocalStore
from harness.knowledge.documents import MemoryDocuments
from harness.knowledge.recall import RecallIndex

script=Path(__file__).resolve().parents[2]/'scripts/local-database.py'
spec=importlib.util.spec_from_file_location('local_database_tools',script)
tools=importlib.util.module_from_spec(spec);spec.loader.exec_module(tools)


class LocalDatabaseTests(unittest.TestCase):
    def test_backup_preserves_real_records_fts_documents_and_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); source=root/'source.sqlite'; target=root/'copy.sqlite'
            store=LocalStore(source)
            message=store.add_message('user','Persistent Atlas project')
            doc=MemoryDocuments(store).save(dict(path='memory/agent/atlas.md',content='Atlas persisted project note',sourceIds=['message:'+message['id']]),privacy_revision=0)
            store.close()
            report=tools.copy_database(source,target)
            self.assertEqual(report['integrity'],'ok')
            self.assertTrue(source.is_file())
            restored=LocalStore(target)
            try:
                self.assertEqual(MemoryDocuments(restored).read(doc['id'])['content'],'Atlas persisted project note')
                self.assertIn(doc['id'],[r['id'] for r in RecallIndex(restored).search('Atlas')['results']])
            finally:restored.close()

    def test_existing_destination_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); source=root/'source.sqlite'; target=root/'copy.sqlite'
            LocalStore(source).close(); target.write_text('keep existing file')
            with self.assertRaisesRegex(ValueError,'already exists'):tools.copy_database(source,target)
            self.assertEqual(target.read_text(),'keep existing file')

    def test_migration_rejects_live_owner_but_explicit_snapshot_is_consistent(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); source=root/'source.sqlite'; target=root/'copy.sqlite'
            store=LocalStore(source);store.claim_runtime_ownership()
            try:
                store.add_message('user','Committed while the database is open')
                with self.assertRaisesRegex(ValueError,'Stop the source app'):tools.copy_database(source,target)
                report=tools.copy_database(source,target,allow_live=True)
                self.assertEqual(report['tables']['messages'],1)
                check=sqlite3.connect(target)
                try:self.assertEqual(check.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                finally:check.close()
            finally:store.close()

    def test_corrupt_source_does_not_publish_a_database(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); source=root/'broken.sqlite';target=root/'copy.sqlite'
            source.write_text('not a database')
            with self.assertRaises(sqlite3.DatabaseError):tools.copy_database(source,target)
            self.assertFalse(target.exists())
