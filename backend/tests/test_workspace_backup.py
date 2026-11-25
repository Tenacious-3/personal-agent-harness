import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

from harness.cli.workspace_backup import export_workspace, restore_workspace
from harness.integrations.mcp import MCPRegistry
from harness.integrations.providers import CredentialVault
from harness.knowledge.documents import MemoryDocuments
from harness.knowledge.recall import RecallIndex
from harness.persistence.store import LocalStore


KEY = 'fixture-credential-key-for-backup'


class WorkspaceBackupTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state = self.root / 'original'
        (self.state / 'workspace').mkdir(parents=True)
        (self.state / 'browser-profile').mkdir()
        (self.state / 'browser-profile/cookie-fixture').write_text('Excluded login')
        (self.state / 'workspace/notes.md').write_text('Retained artifact')
        self.database = self.root / 'native/assistant.sqlite'
        self.archive = self.root / 'recovery.zip'
        self.target = self.root / 'restored'
        self.vault = CredentialVault(self.state / 'credentials/providers', KEY)
        self.vault.save('slack', {'token': 'synthetic-token'})
        with closing(LocalStore(self.database)) as store:
            message = store.add_message('user', 'Backup recall marker')
            self.document = MemoryDocuments(store).save({
                'path': 'memory/agent/backup.md', 'content': 'Restorable sourced document',
                'sourceIds': [f"message:{message['id']}"], 'privacyRevision': 0,
            }, owner=True, privacy_revision=store.revision())
            self.schedule = store.save_schedule({'prompt': 'Retained schedule', 'runAt': 2400000000000})
            self.pending = store.create_approval({'kind': 'skill.save', 'payload': {}, 'summary': 'Pending approval'})
            approval = store.create_approval({'kind': 'skill.save', 'payload': {}, 'summary': 'Uncertain approval'})
            store.decide_approval(approval['id'], 'approved', approval['digest'])
            store.claim_execution(approval['id'])
            self.uncertain = store.mark_action_uncertain(approval['id'], 'Synthetic interruption')
            MCPRegistry(store, KEY)._save({'id': 'fixture', 'token': 'synthetic-mcp-token'})

    def backup(self):
        return export_workspace(self.state, self.database, self.archive, 'original-build')

    def test_restore_to_new_workspace_retains_data_and_checks_encrypted_state(self):
        manifest = self.backup()
        self.assertEqual(manifest['browserProfile'], 'excluded')
        self.assertFalse(any('browser-profile' in path for path in manifest['files']))
        report = restore_workspace(self.archive, self.target, 'original-build', KEY)
        self.assertEqual(report['integrity'], 'ok')
        self.assertEqual(report['verifiedCredentials'], 2)
        with closing(LocalStore(self.target / 'database/assistant.sqlite')) as store:
            self.assertEqual(MemoryDocuments(store).read(self.document['id'])['content'], 'Restorable sourced document')
            self.assertTrue(RecallIndex(store).search('Backup recall marker')['results'])
            self.assertEqual(store.list_schedules()[0]['id'], self.schedule['id'])
            self.assertEqual(store.get_approval(self.pending['id'])['status'], 'pending')
            self.assertEqual(store.get_approval(self.uncertain['id'])['status'], 'uncertain')
        self.assertEqual((self.target / '.assistant/workspace/notes.md').read_text(), 'Retained artifact')
        with self.assertRaises(FileExistsError):
            restore_workspace(self.archive, self.target, 'original-build', KEY)

    def test_missing_key_wrong_build_and_corrupt_archive_leave_no_partial_restore(self):
        self.backup()
        for build, key, error in [('original-build', None, 'original credential key'),
                                  ('original-build', 'wrong-credential-key-for-backup', 'Could not unlock'),
                                  ('new-build', KEY, 'incompatible')]:
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                restore_workspace(self.archive, self.target, build, key)
            self.assertFalse(self.target.exists())
        bad = self.root / 'corrupt.zip'
        with ZipFile(self.archive) as source, ZipFile(bad, 'w') as destination:
            for entry in source.namelist():
                destination.writestr(entry, 'changed' if entry.endswith('notes.md') else source.read(entry))
        with self.assertRaisesRegex(ValueError, 'checksum'):
            restore_workspace(bad, self.target, 'original-build', KEY)
        self.assertFalse(self.target.exists())

    def test_live_owner_or_interrupted_backup_cannot_publish_an_archive(self):
        with closing(LocalStore(self.database)) as store:
            store.claim_runtime_ownership()
            with self.assertRaisesRegex(ValueError, 'Stop the source'):
                self.backup()
        with patch('harness.cli.workspace_backup._copy', side_effect=OSError('Interrupted write')):
            with self.assertRaisesRegex(OSError, 'Interrupted write'):
                self.backup()
        self.assertFalse(self.archive.exists())
        self.assertEqual(list(self.root.glob('.workspace-backup-*')), [])
        self.backup()
