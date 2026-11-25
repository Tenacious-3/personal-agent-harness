import json
import asyncio
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

    def test_owner_correction_holds_same_source_paraphrase_and_preserves_unrelated_notes(self):
        primary = self.save()
        related = self.save({**self.body, 'path': 'memory/agent/related.md',
                             'content': 'SQLite is Aurora’s checkpoint store.'})
        unrelated = self.save({**self.body, 'path': 'memory/agent/unrelated.md',
                               'content': 'Aurora ships every Monday.'})
        corrected = self.save({**self.body, 'id': primary['id'], 'expectedRevision': 1,
                               'content': 'Aurora uses Postgres checkpoints.'}, owner=True)
        held = self.docs.read(related['id'])
        self.assertTrue(held['needsReview'])
        self.assertEqual(held['correction'], {'id': primary['id'], 'revision': 2})
        self.assertFalse(self.docs.read(unrelated['id'])['needsReview'])
        self.assertNotIn('SQLite', self.docs.render(held))
        recall = RecallIndex(self.store)
        self.assertEqual(recall.search('Aurora Postgres')['results'][0]['id'], corrected['id'])
        self.assertNotIn(related['id'], [result['id'] for result in recall.search('SQLite')['results']])
        with self.assertRaisesRegex(ValueError, 'needs owner review'):
            recall.read('document', related['id'])
        updated = self.save({**held, 'expectedRevision': held['revision'], 'content': 'Postgres is Aurora’s checkpoint store.'}, owner=True)
        self.assertFalse(updated['needsReview'])
        self.assertIn('Postgres', recall.read('document', related['id'])['content'])


class DocumentRuntimeTests(OfflineAsyncCase):
    async def test_projection_writes_only_changed_or_missing_files_at_scale(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            store = LocalStore(':memory:')
            self.addCleanup(store.close)
            docs = MemoryDocuments(store)
            workspace = WorkspaceService(root)
            task = store.create_task({'prompt': 'Projection fixture task'})
            created = []
            for size in (20, 500, 1000):
                for index in range(len(created), size):
                    created.append(docs.save(dict(path=f'memory/agent/note-{index:04d}.md',
                        content=f'Note {index}', sourceIds=[f"task:{task['id']}"]), privacy_revision=0))
                initial = await sync_memory_files(store, workspace)
                self.assertGreater(initial['writes'], 0)
                self.assertEqual((await sync_memory_files(store, workspace))['writes'], 0, f'{size} unchanged notes')
                path = root / created[-1]['path']
                path.write_text('Tampered test file')
                (root / created[-2]['path']).unlink()
                self.assertEqual((await sync_memory_files(store, workspace))['writes'], 2)
            latest = created[-1]
            docs.save({**latest, 'expectedRevision': 1, 'content': 'Edited one note'}, privacy_revision=0)
            self.assertEqual((await sync_memory_files(store, workspace))['writes'], 1)
            store.update_task(task['id'], {'status': 'running'})
            self.assertEqual((await sync_memory_files(store, workspace))['writes'], 1000)

    async def test_symlink_namespace_cannot_write_outside_workspace(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); outside=root/'outside'; outside.mkdir()
            runtime=Runtime(Config(root=root,data_dir=root/'.assistant',token='document-test-token-at-least-24-characters',encryption_secret='document-test-key-at-least-24-characters'))
            try:
                (runtime.workspace.root/'memory').mkdir()
                try:(runtime.workspace.root/'memory/agent').symlink_to(outside,target_is_directory=True)
                except OSError as error:
                    if getattr(error,'winerror',None)==1314:self.skipTest('Windows symlink privilege unavailable; Linux Docker gate covers this boundary')
                    raise
                message=runtime.store.add_message('user','Keep local notes.')
                with self.assertRaisesRegex(ValueError,'symlink'):
                    await runtime.operate('memory_documents',dict(action='save',path='memory/agent/escape.md',content='Confined note',sourceIds=['message:'+message['id']],privacyRevision=0))
                self.assertEqual(list(outside.iterdir()),[])
                self.assertEqual(runtime.documents.list()['documents'],[])
            finally:await runtime.close()

    async def test_owner_correction_during_model_run_rejects_stale_document_write(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); ready=asyncio.Event(); proceed=asyncio.Event()
            async def runner(config,prompt,context,caps):
                ready.set(); await proceed.wait()
                return await caps.operate('memory_documents',json.dumps(dict(action='save',path='memory/agent/stale.md',content='Old claim',sourceIds=['message:'+message['id']])))
            runtime=Runtime(Config(root=root,data_dir=root/'.assistant',token='document-test-token-at-least-24-characters',encryption_secret='document-test-key-at-least-24-characters',api_key='ci-test-only'),runner=runner)
            try:
                message=runtime.store.add_message('user','Old claim')
                memory=runtime.store.save_memory({'text':'Old claim','source':message['id'],'provenance':'explicit'})
                task=asyncio.create_task(runtime.model('Review retained context','Old claim'))
                await ready.wait()
                runtime.store.update_memory(memory['id'],{'text':'Corrected claim','source':'user-correction','provenance':'explicit'})
                proceed.set()
                with self.assertRaisesRegex(ValueError,'privacy revision changed'):await task
                self.assertEqual(runtime.documents.list()['documents'],[])
            finally:await runtime.close()

    async def test_forget_during_projection_cannot_republish_a_deleted_document(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); ready=asyncio.Event(); proceed=asyncio.Event()
            class PausedWorkspace(WorkspaceService):
                pause=True
                async def write(self,path,content):
                    if path=='memory/agent/private.md' and self.pause:
                        self.pause=False; ready.set(); await proceed.wait()
                    return await super().write(path,content)
            workspace=PausedWorkspace(root/'workspace')
            runtime=Runtime(Config(root=root,data_dir=root/'.assistant',token='document-test-token-at-least-24-characters',encryption_secret='document-test-key-at-least-24-characters'),workspace=workspace)
            try:
                source=runtime.store.add_message('user','A private retained project claim.')
                memory=runtime.store.save_memory({'text':'A private retained project claim.','source':source['id'],'provenance':'explicit'})
                runtime.documents.save(dict(path='memory/agent/private.md',content='A private retained project claim.',sourceIds=['memory:'+memory['id']]),privacy_revision=runtime.store.revision())
                projection=asyncio.create_task(runtime.project_memory())
                await ready.wait()
                runtime.store.forget_memory(memory['id'],True)
                proceed.set(); report=await projection
                self.assertNotIn('error',report)
                self.assertFalse((workspace.root/'memory/agent/private.md').exists())
                self.assertFalse(runtime.store.get_setting('memoryProjectionStale'))
                self.assertEqual(runtime.recall.search('private retained')['results'],[])
            finally:await runtime.close()

    async def test_failed_projection_blocks_files_until_repaired(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            runtime=Runtime(Config(root=root,data_dir=root/'.assistant',token='document-test-token-at-least-24-characters',encryption_secret='document-test-key-at-least-24-characters'))
            try:
                source=runtime.store.add_message('user','Retain the checkpoint design.')
                doc=runtime.documents.save(dict(path='memory/agent/design.md',content='Checkpoint design',sourceIds=['message:'+source['id']]),privacy_revision=0)
                collision=runtime.workspace.root/doc['path']; collision.mkdir(parents=True)
                self.assertIn('error',await runtime.project_memory())
                with self.assertRaisesRegex(ValueError,'stale'):runtime.check_memory_file('memory/agent')
                collision.rmdir()
                self.assertNotIn('error',await runtime.project_memory())
                self.assertIn('Checkpoint design',(await runtime.workspace.read(doc['path']))['content'])
            finally:await runtime.close()

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
