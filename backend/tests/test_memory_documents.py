import json
from pathlib import Path
import tempfile
import unittest

from harness.persistence.store import LocalStore
from harness.knowledge.documents import MemoryDocuments,document_path
from harness.knowledge.recall import RecallIndex
from harness.knowledge.memory import sync_memory_files,build_context_packet
from harness.workspace.files import WorkspaceService
from harness.application.runtime import Runtime
from harness.core.config import Config
from tests.offline_loop import OfflineAsyncCase


class DocumentTests(unittest.TestCase):
    def setUp(self):
        self.store=LocalStore(':memory:'); self.docs=MemoryDocuments(self.store)
        self.source=self.store.add_message('user','The Aurora project uses SQLite checkpoints.')
        self.body=dict(path='memory/agent/projects/aurora.md',content='Aurora uses SQLite checkpoints.',sourceIds=['message:'+self.source['id']])
    def tearDown(self): self.store.close()
    def save(self,body=None,**kwargs): return self.docs.save(body or self.body,privacy_revision=self.store.revision(),**kwargs)

    def test_sourced_documents_updates_deduplication_and_bounded_recall(self):
        doc=self.save()
        self.assertEqual(doc['provenance'],'inferred')
        duplicate=self.save({**self.body,'path':'memory/agent/another.md'})
        self.assertEqual(duplicate['id'],doc['id'])
        self.assertEqual(self.save({**self.body,'id':doc['id'],'expectedRevision':1})['revision'],1)
        updated=self.save({**self.body,'id':doc['id'],'expectedRevision':1,'content':'Aurora checkpoint migration is pending.'})
        self.assertEqual(updated['revision'],2)
        second=self.save({**self.body,'path':'memory/agent/procedures/checkpoints.md','content':'Inspect the checkpoint before migration.'})
        self.assertNotEqual(second['id'],doc['id'])
        matches=RecallIndex(self.store).search('Aurora')['results']
        self.assertIn(doc['id'],[r['id'] for r in matches])
        packet=build_context_packet(self.store,'Aurora')
        self.assertLessEqual(packet['characters'],12000)
        self.assertEqual(packet['modelCalls'],0)
        self.assertNotIn('content',self.docs.list()['documents'][0])

    def test_path_source_revision_and_provenance_boundaries(self):
        for path in ('../x.md','memory/MEMORY.md','memory/agent/../x.md','memory/agent/a.py','memory/agent/CON.md','memory/agent/a\\b.md','memory/agent/a./b.md'):
            with self.subTest(path=path),self.assertRaises(ValueError): document_path(path)
        with self.assertRaises(ValueError): self.save({**self.body,'sourceIds':['message:missing']})
        with self.assertRaises(ValueError): self.docs.save(self.body,privacy_revision=-1)
        doc=self.save()
        with self.assertRaisesRegex(ValueError,'revision conflict'): self.save({**self.body,'id':doc['id'],'expectedRevision':0})
        owned=self.save({**self.body,'id':doc['id'],'expectedRevision':1,'content':'Owner corrected Aurora.'},owner=True)
        with self.assertRaisesRegex(ValueError,'protected'): self.save({**self.body,'id':doc['id'],'expectedRevision':owned['revision']})
        snapshots=''.join(r[0] for r in self.store.db.execute('SELECT snapshot FROM memory_document_versions'))
        self.assertNotIn(self.body['content'],snapshots)

    def test_correction_forget_and_versions_clear_known_copies(self):
        memory=self.store.save_memory({'text':'Private Aurora claim','source':self.source['id'],'provenance':'explicit'})
        self.body['sourceIds']=['memory:'+memory['id']]
        doc=self.save()
        self.store.update_memory(memory['id'],{'text':'Corrected claim','source':'user-correction','provenance':'explicit'})
        with self.assertRaises(ValueError): self.docs.read(doc['id'])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM memory_document_versions').fetchone()[0],0)
        doc=self.save({**self.body,'content':'Paraphrased derived note'})
        self.store.forget_memory(memory['id'],True)
        self.assertEqual(self.docs.list()['documents'],[])
        self.assertEqual(RecallIndex(self.store).search('Paraphrased')['results'],[])

    def test_event_provenance_preserves_requested_failed_completed_states(self):
        for state in ('action.requested','run.failed','action.completed'):
            event=self.store.append_event(state,{'result':'Actual recorded outcome'})
            doc=self.save({**self.body,'path':f'memory/agent/{state}.md','content':state,'sourceIds':['event:'+event['id']]})
            self.assertIn(state,self.docs.render(doc))


class DocumentRuntimeTests(OfflineAsyncCase):
    async def test_real_tools_projection_restart_rename_remove_and_read_only(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); store=LocalStore(root/'state.sqlite'); workspace=WorkspaceService(root/'workspace')
            docs=MemoryDocuments(store); message=store.add_message('user','Keep the Atlas design decisions together.')
            body=dict(path='memory/agent/atlas/design.md',content='Atlas stores durable state in SQLite.',sourceIds=['message:'+message['id']],privacyRevision=store.revision())
            runtime=Runtime(Config(root=root,data_dir=root/'.assistant',token='document-test-token-at-least-24-characters',encryption_secret='document-test-key-at-least-24-characters'),store=store,workspace=workspace)
            doc=await runtime.operate('memory_documents',{'action':'save',**body})
            await runtime.operate('memory_documents',{'action':'save',**body,'path':'memory/agent/decisions/checkpoints.md','content':'Checkpoint design needs review.'})
            self.assertIn('Atlas',(await workspace.read(doc['path']))['content'])
            self.assertEqual((await runtime.operate('memory_documents',{'action':'read','id':doc['id']},read_only=True))['content'],doc['content'])
            with self.assertRaisesRegex(ValueError,'read-only'): await runtime.operate('memory_documents',{'action':'save',**body},read_only=True)
            with self.assertRaisesRegex(ValueError,'Specialists'): await runtime.operate('memory_documents',{'action':'save',**body},task={'parentId':'parent'})
            await runtime.close()
            store=LocalStore(root/'state.sqlite'); docs=MemoryDocuments(store)
            renamed=docs.save({**docs.read(doc['id']),'path':'memory/agent/atlas/decisions.md','expectedRevision':1},owner=True,privacy_revision=store.revision())
            await sync_memory_files(store,workspace)
            self.assertFalse((workspace.root/doc['path']).exists())
            self.assertTrue((workspace.root/renamed['path']).is_file())
            docs.remove(doc['id'],renamed['revision']); await sync_memory_files(store,workspace)
            self.assertFalse((workspace.root/renamed['path']).exists())
            store.close()

    async def test_projection_failure_remembers_paths_and_forget_blocks_stale_run(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); store=LocalStore(':memory:'); docs=MemoryDocuments(store); workspace=WorkspaceService(root)
            message=store.add_message('user','Keep source-linked context.')
            body=dict(path='memory/agent/private.md',content='Private retained note',sourceIds=['message:'+message['id']])
            doc=docs.save(body,privacy_revision=0)
            # Simulate a crash after a real file write and before completing projection.
            await workspace.write(doc['path'],doc['content'])
            docs.remove(doc['id'],1)
            await sync_memory_files(store,workspace)
            self.assertFalse((root/doc['path']).exists())
            with self.assertRaisesRegex(ValueError,'privacy revision'): docs.save(body,privacy_revision=0)
            store.close()


if __name__=='__main__': unittest.main()
