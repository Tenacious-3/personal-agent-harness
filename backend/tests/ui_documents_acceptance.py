"""Document tools -> real SQLite/files -> Chromium review -> persistent restart."""
import json
from pathlib import Path
import tempfile
import unittest

import httpx
from playwright.sync_api import expect,sync_playwright

from harness.application.runtime import Runtime
from harness.core.config import Config
from tests.ui_acceptance import ROOT,TOKEN,serve


class DocumentUIAcceptance(unittest.TestCase):
    def test_agent_documents_owner_review_and_restart(self):
        # CI controls decisions only. Every operation, file, request and revision
        # below uses the real runtime; live model behavior is verified separately.
        async def runner(config,prompt,context,caps):
            doc_source=context.split('USER [')[-1].split(']')[0]
            refs=['message:'+doc_source]
            first=await caps.operate('memory_documents',json.dumps(dict(action='save',path='memory/agent/project/architecture.md',content='SQLite stores authoritative state.',sourceIds=refs)))
            await caps.operate('memory_documents',json.dumps(dict(action='save',path='memory/agent/procedures/review.md',content='Review actual execution receipts.',sourceIds=refs)))
            await caps.operate('memory_documents',json.dumps(dict(action='save',id=first['id'],expectedRevision=first['revision'],path=first['path'],content='SQLite stores authoritative state; readable files are projections.',sourceIds=refs)))
            caps.on_usage(10,10)
            return 'Created two sourced documents and updated the architecture document.'
        def factory(config,store):
            store.set_setting('nightlyDreamEnabled',False)
            return Runtime(config,store=store,runner=runner)
        with tempfile.TemporaryDirectory(prefix='harness-documents-ui-') as folder:
            root=Path(folder)
            config=Config(root=root,data_dir=root/'.assistant',port=0,token=TOKEN,api_key='ci-test-only',encryption_secret=TOKEN,frontend_dir=ROOT/'frontend/dist',browser_channel='chromium',run_token_reservation=100,daily_token_limit=100000)
            with sync_playwright() as playwright:
                browser=playwright.chromium.launch()
                try:
                    page=browser.new_page(viewport={'width':1440,'height':1000})
                    errors=[]; external=[]
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    def local_only(route):
                        if route.request.url.startswith('http://127.0.0.1:'):route.continue_()
                        else:external.append(route.request.url);route.abort()
                    page.route('**/*',local_only)
                    with serve(config,factory) as origin:
                        page.goto(origin)
                        page.get_by_label('Workspace access token',exact=True).fill(TOKEN)
                        page.get_by_role('button',name='Connect workspace',exact=True).click()
                        page.get_by_label('Message your assistant',exact=True).fill('Remember the verified project architecture and review process.')
                        page.get_by_role('button',name='Send message',exact=True).click()
                        expect(page.get_by_text('Created two sourced documents and updated the architecture document.',exact=True)).to_be_visible()
                        page.get_by_role('button',name='Memory',exact=True).click()
                        page.get_by_role('button',name='Open document memory/agent/project/architecture.md',exact=True).click()
                        expect(page.get_by_label('Memory document content',exact=True)).to_have_value('SQLite stores authoritative state; readable files are projections.')
                        page.get_by_label('Memory document path',exact=True).fill('memory/agent/project/decisions.md')
                        page.get_by_label('Memory document content',exact=True).fill('Owner correction: check SQLite and its real file projections.')
                        page.get_by_role('button',name='Save reviewed document',exact=True).click()
                        expect(page.get_by_text('Reviewed document saved. Your correction is protected.',exact=True)).to_be_visible()
                        self.assertFalse((root/'.assistant/workspace/memory/agent/project/architecture.md').exists())
                        self.assertIn('Owner correction',(root/'.assistant/workspace/memory/agent/project/decisions.md').read_text())
                        page.set_viewport_size({'width':390,'height':844})
                        self.assertTrue(page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'))
                        with httpx.Client(base_url=origin,trust_env=False,headers={'Authorization':'Bearer '+TOKEN}) as api:
                            docs=api.get('/api/memory-documents').json()['documents']
                            reviewed=next(doc for doc in docs if doc['provenance']=='user_reviewed')
                            self.assertEqual(reviewed['revision'],3)
                            found=api.post('/api/recall/search',json={'query':'Owner correction'}).json()['results']
                            self.assertIn(reviewed['id'],[item['id'] for item in found])
                    # Linux may retain the prior connection in TIME_WAIT. A new
                    # listener tests durable state without relying on port reuse.
                    config.port=0
                    with serve(config,factory) as restarted:
                        with httpx.Client(base_url=restarted,trust_env=False,headers={'Authorization':'Bearer '+TOKEN}) as api:
                            self.assertEqual(len(api.get('/api/memory-documents').json()['documents']),2)
                        page.goto(restarted)
                        page.get_by_label('Workspace access token',exact=True).fill(TOKEN)
                        page.get_by_role('button',name='Connect workspace',exact=True).click()
                        page.get_by_role('button',name='Memory',exact=True).click()
                        page.get_by_role('button',name='Open document memory/agent/project/decisions.md',exact=True).click()
                        page.get_by_role('button',name='Remove document',exact=True).click()
                        expect(page.get_by_text('Document and its versions removed. Source conversations are retained.',exact=True)).to_be_visible()
                        self.assertFalse((root/'.assistant/workspace/memory/agent/project/decisions.md').exists())
                    self.assertEqual(errors,[])
                    self.assertEqual(external,[])
                finally:browser.close()


if __name__=='__main__':unittest.main(verbosity=2)
