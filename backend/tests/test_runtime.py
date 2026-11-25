import asyncio
import json
import subprocess
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import patch

from tests.offline_loop import OfflineAsyncCase
from harness.core.config import Config
from harness.application.runtime import Runtime
from harness.persistence.store import LocalStore


class RuntimeTests(OfflineAsyncCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='python-runtime-', dir=Path.cwd())
        self.root = Path(self.tmp.name)
        self.config = Config(root=self.root, data_dir=self.root, token='test-access-token-at-least-24-characters',
            api_key='test-only-key', encryption_secret='test-credential-key-at-least-24-characters', daily_token_limit=1000000, run_token_reservation=1000)
        self.browser = SimpleNamespace(status=AsyncMock(return_value={'active': True, 'manualControl': False}),
            tabs=AsyncMock(return_value=[{'id': 'tab', 'url': 'https://playwright.dev/'}]),
            click=AsyncMock(return_value={'url': 'https://playwright.dev/docs/intro'}), fill=AsyncMock(return_value={'ok': True}), close=AsyncMock())
        self.google = SimpleNamespace(status=AsyncMock(return_value={'configured': False, 'connected': False}), close=AsyncMock())
        self.runtime = Runtime(self.config, browser=self.browser, google=self.google, runner=self.runner)

    async def runner(self, config, prompt, context, caps):
        self.assertEqual(config.model, 'gpt-6-luna'); caps.on_usage(5, 5)
        return 'Synthetic result'

    async def asyncTearDown(self):
        await self.runtime.close()
        for attempt in range(8):
            try: self.tmp.cleanup(); break
            except PermissionError:
                if attempt == 7: raise
                await asyncio.sleep(0.05 * (attempt + 1))

    async def request(self, path, body=None, method=None):
        response = await self.runtime.dispatch(method or ('GET' if body is None else 'POST'), '/api' + path, body)
        return response.status_code, json.loads(response.body)

    async def pump(self, count=5):
        for _ in range(count):
            await self.runtime.tick()
            for _ in range(12): await asyncio.sleep(0)

    async def test_chat_memory_and_usage(self):
        async def runner(config, prompt, context, caps):
            await caps.remember('Synthetic preference'); caps.on_usage(12, 9); return 'Stored result'
        self.runtime.runner = runner
        status, data = await self.request('/chat', {'message': 'Remember the preference'})
        self.assertEqual(status, 200, data); self.assertEqual(data['message'], 'Stored result')
        self.assertEqual(len(self.runtime.store.list_messages()), 2)
        self.assertEqual(self.runtime.store.list_memories()[0]['provenance'], 'inferred')
        self.assertEqual(self.runtime.store.list_usage()[0]['inputTokens'], 12)
        self.assertTrue((self.root / 'workspace/memory/MEMORY.md').exists())

    async def test_approval_lifecycle_uses_retained_task_approvals(self):
        store = self.runtime.store
        task = store.create_task({'prompt': 'Run task with retained approval'})
        payload = {'name': 'retained', 'content': '# Test procedure'}
        approval = await self.runtime.propose_action('skill.save', payload, 'Review procedure', task['id'])
        for _ in range(1005):
            store.create_approval({'kind': 'skill.save', 'payload': {}, 'summary': 'Unrelated approval'})
        reused = await self.runtime.propose_action('skill.save', dict(reversed(list(payload.items()))),
                                                   'Review procedure again', task['id'])
        self.assertEqual(reused['id'], approval['id'])
        await self.runtime.run_task(task)
        self.assertEqual(store.get_task(task['id'])['status'], 'waiting_approval')
        self.runtime.resume_task(task['id'])
        self.assertEqual(store.get_task(task['id'])['status'], 'waiting_approval')
        self.assertFalse((self.root / 'workspace/skills/retained').exists())
        self.runtime.cancel_task(task['id'])
        self.assertEqual(store.get_approval(approval['id'])['status'], 'rejected')

    async def test_old_uncertain_approval_blocks_failed_task_retry(self):
        store = self.runtime.store
        task = store.create_task({'prompt': 'Failed task with uncertain delivery'})
        approval = store.create_approval({'taskId': task['id'], 'kind': 'skill.save',
                                          'payload': {}, 'summary': 'Prior action'})
        store.decide_approval(approval['id'], 'approved', approval['digest'])
        store.claim_execution(approval['id'])
        store.mark_action_uncertain(approval['id'], 'Interrupted delivery')
        store.update_task(task['id'], {'status': 'running'})
        store.update_task(task['id'], {'status': 'failed'})
        for _ in range(1005):
            store.create_approval({'kind': 'skill.save', 'payload': {}, 'summary': 'Unrelated approval'})
        status, error = await self.request(f"/tasks/{task['id']}/retry", {})
        self.assertEqual(status, 400)
        self.assertIn('Reconcile', error['error'])
        self.assertEqual(store.get_task(task['id'])['status'], 'failed')

    async def test_delegation_and_cancellation_use_retained_graph(self):
        store = self.runtime.store
        parent = store.create_task({'prompt': 'Parent'})
        children = [await self.runtime.operate('delegate', {'prompt': f'Child {i}'}, parent) for i in range(3)]
        grandchild = store.create_task({'prompt': 'Existing nested task', 'parentId': children[1]['id']})
        store.update_task(children[0]['id'], {'status': 'running'})
        store.update_task(children[0]['id'], {'status': 'completed', 'result': 'Retained child result'})
        for _ in range(1005):
            store.create_task({'prompt': 'Unrelated task'})
        with self.assertRaisesRegex(ValueError, 'Subtask limit'):
            await self.runtime.operate('delegate', {'prompt': 'Fourth child'}, parent)

        async def runner(config, prompt, context, caps):
            self.assertIn('Retained child result', prompt)
            caps.on_usage(5, 5)
            return 'Consumed old child result'

        self.runtime.runner = runner
        await self.runtime.run_task(parent)
        self.assertEqual(store.get_task(parent['id'])['result'], 'Consumed old child result')
        active = asyncio.create_task(asyncio.sleep(60))
        self.runtime.active[grandchild['id']] = active
        try:
            self.runtime.cancel_task(parent['id'])
            with self.assertRaises(asyncio.CancelledError):
                await active
        finally:
            self.runtime.active.pop(grandchild['id'], None)
        self.assertEqual(store.get_task(children[0]['id'])['status'], 'completed')
        for task in [*children[1:], grandchild]:
            self.assertEqual(store.get_task(task['id'])['status'], 'cancelled')

    async def test_provider_usage_details_are_recorded_without_changing_budget_counts(self):
        async def runner(config, prompt, context, caps):
            caps.on_usage(120, 9)
            caps.on_usage_details({'inputTokens': 120, 'outputTokens': 9,
                'cachedInputTokens': 80, 'modelRequests': 2, 'contextCharacters': len(context)})
            return 'Measured result'
        self.runtime.runner = runner
        status, _ = await self.request('/chat', {'message': 'Check usage details'})
        self.assertEqual(status, 200)
        usage = self.runtime.store.list_usage()[0]
        self.assertEqual((usage['inputTokens'], usage['outputTokens']), (120, 9))
        event = next(e for e in self.runtime.store.list_events() if e['type'] == 'run.usage')
        self.assertEqual(event['detail']['cachedInputTokens'], 80)
        self.assertEqual(event['detail']['modelRequests'], 2)
        self.assertLessEqual(event['detail']['contextCharacters'], 12000)

    async def test_schedule_executes_once_and_persists(self):
        self.runtime.store.save_schedule({'id': 'due', 'prompt': 'Synthetic reminder', 'runAt': 1})
        await self.pump(); self.assertEqual(len(self.runtime.store.list_tasks()), 1)
        self.assertEqual(self.runtime.store.list_tasks()[0]['status'], 'completed')
        self.assertEqual(len(self.runtime.store.list_notifications()), 1)
        self.assertEqual(self.runtime.store.list_schedules()[0]['status'], 'completed')

    async def test_explicit_readiness_checks_report_local_degradation_and_budget(self):
        self.runtime.config.browser_executable = str(self.root / 'missing-browser')
        self.runtime.store.set_setting('memoryProjectionStale', True)
        before = len(self.runtime.store.list_usage())
        response = await self.runtime.dispatch('GET', '/health')
        self.assertEqual(response.status_code, 200)
        status, report = await self.request('/readiness', {'check': 'local'})
        self.assertEqual(status, 200, report)
        self.assertEqual(report['checks']['database']['status'], 'ready')
        self.assertEqual(report['checks']['browser']['status'], 'unavailable')
        self.assertEqual(report['checks']['projection']['status'], 'degraded')
        self.assertEqual(len(self.runtime.store.list_usage()), before)
        self.runtime.config.daily_token_limit = 1
        _, blocked = await self.request('/readiness', {'check': 'model'})
        self.assertEqual(blocked['checks']['model']['status'], 'degraded')
        self.assertEqual(len(self.runtime.store.list_usage()), before)
        self.runtime.config.daily_token_limit = 100000
        _, verified = await self.request('/readiness', {'check': 'model'})
        self.assertEqual(verified['checks']['model']['status'], 'ready')
        self.assertEqual(len(self.runtime.store.list_usage()), before + 1)
        self.runtime.config.daily_token_limit = 0
        status, chat = await self.request('/chat', {'message': 'Verify uncapped chat'})
        self.assertEqual(status, 200, chat)
        self.assertEqual(len(self.runtime.store.list_usage()), before + 2)

    async def test_old_queued_task_runs_after_more_than_500_completed_records(self):
        old = self.runtime.store.create_task({'prompt': 'Old work must not disappear'})
        for index in range(501):
            newer = self.runtime.store.create_task({'prompt': f'Completed history {index}'})
            self.runtime.store.update_task(newer['id'], {'status': 'running'})
            self.runtime.store.update_task(newer['id'], {'status': 'completed', 'result': 'Done'})
        self.assertNotIn(old['id'], {t['id'] for t in self.runtime.store.list_tasks(500)})
        status, runtime_status = await self.request('/status', method='GET')
        self.assertEqual(status, 200, runtime_status)
        self.assertEqual(runtime_status['activity']['queuedTasks'], 1)
        response = await self.runtime.dispatch('GET', '/api/tasks', query={'status': 'queued'})
        page = json.loads(response.body)
        self.assertEqual(response.status_code, 200, page)
        self.assertEqual([task['id'] for task in page['tasks']], [old['id']])
        self.assertEqual(page['counts']['completed'], 501)
        result = await self.runtime.operate('tasks_read', {'status': 'queued'}, read_only=True)
        self.assertEqual([task['id'] for task in result['tasks']], [old['id']])
        await self.pump(2)
        self.assertEqual(self.runtime.store.get_task(old['id'])['status'], 'completed')
        self.assertEqual(self.runtime.store.get_task(old['id'])['attempts'], 1)
        receipts = [n for n in self.runtime.store.list_notifications() if n['taskId'] == old['id']]
        self.assertEqual(len(receipts), 1)

    async def test_queued_admission_is_oldest_first_and_keeps_two_task_capacity(self):
        calls = []; gate = asyncio.Future()
        async def runner(config, prompt, context, caps):
            calls.append(prompt)
            await gate
            caps.on_usage(5, 5)
            return 'Done'
        self.runtime.runner = runner
        tasks = [self.runtime.store.create_task({'prompt': f'Queued task {index}'}) for index in range(3)]
        await self.pump(1)
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0].startswith('Queued task 0'))
        self.assertTrue(calls[1].startswith('Queued task 1'))
        self.assertEqual(self.runtime.store.get_task(tasks[2]['id'])['status'], 'queued')
        gate.set_result(None)
        await self.pump(3)
        self.assertTrue(all(self.runtime.store.get_task(t['id'])['status'] == 'completed' for t in tasks))

    async def test_approval_digest_rejection_and_exactly_once_execution(self):
        async def proposal(selector):
            status, approval = await self.request('/actions', {'kind': 'browser.click', 'payload': {'tabId': 'tab', 'selector': selector}, 'summary': 'Synthetic public click'})
            self.assertEqual(status, 201); return approval
        approval = await proposal('#first')
        self.assertEqual(self.browser.click.await_count, 0)
        status, _ = await self.request(f"/approvals/{approval['id']}/decision", {'decision': 'approved', 'expectedDigest': 'wrong'})
        self.assertEqual(status, 409); self.assertEqual(self.browser.click.await_count, 0)
        decision = {'decision': 'approved', 'expectedDigest': approval['digest']}
        status, data = await self.request(f"/approvals/{approval['id']}/decision", decision)
        self.assertEqual(status, 200, data); self.assertEqual(data['status'], 'completed')
        self.assertEqual((await self.request(f"/approvals/{approval['id']}/decision", decision))[0], 409)
        self.assertEqual(self.browser.click.await_count, 1)
        rejected = await proposal('#second')
        await self.request(f"/approvals/{rejected['id']}/decision", {'decision': 'rejected', 'expectedDigest': rejected['digest']})
        self.assertEqual(self.browser.click.await_count, 1)

    async def test_browser_preflight_rejects_without_dispatch_and_transport_failure_remains_uncertain(self):
        approval = await self.runtime.propose_action('browser.click', {'tabId': 'tab', 'selector': '#x'}, 'Reviewed click')
        self.browser.tabs.return_value = [{'id': 'tab', 'url': 'https://playwright.dev/changed'}]
        self.runtime.store.decide_approval(approval['id'], 'approved', approval['digest'])
        result = await self.runtime.execute_action(approval)
        self.assertEqual(result['status'], 'failed'); self.assertEqual(self.browser.click.await_count, 0)
        self.assertIn('stopped before delivery', result['error'])
        self.assertEqual(self.runtime.store.list_events()[0]['type'], 'action.preflight-rejected')
        approval = await self.runtime.propose_action('browser.click', {'tabId': 'tab', 'selector': '#x'}, 'Second reviewed click')
        self.runtime.store.decide_approval(approval['id'], 'approved', approval['digest'])
        self.browser.status.return_value = {'manualControl': True}
        result = await self.runtime.execute_action(approval)
        self.assertEqual(result['status'], 'failed'); self.assertEqual(self.browser.click.await_count, 0)
        self.browser.status.return_value = {'manualControl': False}
        approval = await self.runtime.propose_action('browser.click', {'tabId': 'tab', 'selector': '#x'}, 'Third reviewed click')
        self.runtime.store.decide_approval(approval['id'], 'approved', approval['digest'])
        self.browser.click.side_effect = TimeoutError('Response lost')
        result = await self.runtime.execute_action(approval)
        self.assertEqual(result['status'], 'uncertain'); self.assertEqual(self.browser.click.await_count, 1)
        self.assertEqual((await self.request('/tasks', {'prompt': 'Unrelated task'}))[0], 202)

    async def test_workflow_join_parallel_results_and_one_final_receipt(self):
        gates = []; calls = []
        async def runner(config, prompt, context, caps):
            self.assertTrue(caps.read_only); calls.append(prompt)
            if 'Workflow role: Researcher' in prompt or 'Workflow role: Critical reviewer' in prompt:
                number = len(gates) + 1; gate = asyncio.get_running_loop().create_future(); gates.append(gate)
                await gate; result = f'result-{number}'
            elif 'Workflow role: Verifier' in prompt:
                self.assertIn('result-1', prompt); self.assertIn('result-2', prompt); result = 'verified'
            else: self.assertIn('verified', prompt); result = 'final-answer'
            caps.on_usage(5, 5); return result
        self.runtime.runner = runner
        _, workflow = await self.request('/workflows', {'prompt': 'Synthetic comparison'})
        await self.pump(1); self.assertEqual(len(calls), 2)
        gates[0].set_result(None); await self.pump(1); self.assertEqual(len(calls), 2)
        gates[1].set_result(None); await self.pump()
        self.assertEqual(len(calls), 4)
        self.assertEqual(self.runtime.store.get_task(workflow['id'])['result'], 'final-answer')
        self.assertEqual(len(self.runtime.store.list_notifications()), 1)
        await self.pump(); self.assertEqual(len(self.runtime.store.list_notifications()), 1)

    async def test_workflow_retry_reuses_completed_stage_and_limit(self):
        fail = True; roles = []
        async def runner(config, prompt, context, caps):
            role = prompt.splitlines()[0]; roles.append(role); caps.on_usage(5, 5)
            if role.endswith('Researcher') and fail: raise ValueError('Transient failure')
            return 'Stage result'
        self.runtime.runner = runner
        _, workflow = await self.request('/workflows', {'prompt': 'Retry comparison'})
        await self.pump(); self.assertEqual(self.runtime.store.get_task(workflow['id'])['status'], 'failed')
        fail = False
        self.assertEqual((await self.request(f"/workflows/{workflow['id']}/retry", {}))[0], 202)
        await self.pump(); self.assertEqual(self.runtime.store.get_task(workflow['id'])['status'], 'completed')
        self.assertEqual(roles.count('Workflow role: Critical reviewer'), 1)
        self.assertEqual(roles.count('Workflow role: Researcher'), 2)

    async def test_chat_creates_workflow_and_children_cannot_mutate(self):
        created = []
        async def runner(config, prompt, context, caps):
            if not caps.read_only:
                workflow = await caps.operate('workflow', json.dumps({'action': 'create', 'prompt': 'Synthetic team'})); created.append(workflow['id'])
            else:
                for operation in ('workflow', 'delegate', 'propose_action', 'notify', 'schedule_cron'):
                    with self.assertRaisesRegex(ValueError, 'read-only'): await caps.operate(operation, '{}')
                with self.assertRaisesRegex(ValueError, 'read-only'): await caps.remember('Should not save')
            caps.on_usage(5, 5); return 'Stage result'
        self.runtime.runner = runner
        self.assertEqual((await self.request('/chat', {'message': 'Start a team'}))[0], 200)
        await self.pump(); self.assertEqual(self.runtime.store.get_task(created[0])['status'], 'completed')

    async def test_cancellation_stops_running_and_queued_agents(self):
        calls = []
        async def runner(config, prompt, context, caps): calls.append(prompt); await asyncio.Future()
        self.runtime.runner = runner
        _, workflow = await self.request('/workflows', {'prompt': 'Cancelable team'})
        await self.pump(1); self.assertEqual(len(calls), 2)
        await self.request(f"/workflows/{workflow['id']}/cancel", {}); await self.pump()
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(t['status'] == 'cancelled' for t in self.runtime.store.workflow_tasks(workflow['id'])))
        self.assertFalse(self.runtime.store.list_notifications())

    async def test_dream_requires_current_memory_revision(self):
        _, task = await self.request('/tasks', {'kind': 'dream', 'prompt': 'Synthetic proposal'})
        await self.pump()
        await self.request('/memories', {'text': 'New correction'})
        status, _ = await self.request('/dreams/apply', {'taskId': task['id'], 'text': 'Old inference'})
        self.assertEqual(status, 409)

    async def test_model_refuses_other_models(self):
        from harness.agents.runner import run_assistant
        self.config.model = 'other-model'
        with self.assertRaisesRegex(ValueError, 'gpt-6-luna'): await run_assistant(self.config, 'x', 'x')

    async def test_python_sdk_adapter_settles_usage_and_citations(self):
        from harness.agents.capabilities import AgentCapabilities
        from harness.agents.runner import run_assistant
        from agents.items import ModelResponse
        from agents.usage import Usage
        usage = []; evidence = []; details = []
        result = SimpleNamespace(final_output='Synthetic SDK result', context_wrapper=SimpleNamespace(usage=SimpleNamespace(input_tokens=12, output_tokens=8, requests=2, input_tokens_details=SimpleNamespace(cached_tokens=7))),
            raw_responses=[ModelResponse(output=[{'id': 'msg_test', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                'content': [{'type': 'output_text', 'text': 'Synthetic citation', 'annotations': [
                    {'type': 'url_citation', 'url': 'https://playwright.dev/', 'title': 'Playwright', 'start_index': 0, 'end_index': 9}]}]}], usage=Usage(), response_id=None)])
        client = SimpleNamespace(close=AsyncMock())
        call = AsyncMock(return_value=result)
        with patch('agents.Runner.run', call), patch('openai.AsyncOpenAI', return_value=client):
            output = await run_assistant(self.config, 'Synthetic prompt', 'Synthetic context',
                AgentCapabilities(read_only=True, on_usage=lambda i, o: usage.append((i, o)), on_usage_details=details.append, on_evidence=evidence.append))
        self.assertEqual(output, 'Synthetic SDK result')
        self.assertEqual(details, [{'inputTokens': 12, 'outputTokens': 8, 'cachedInputTokens': 7, 'modelRequests': 2, 'contextCharacters': 17}])
        self.assertEqual(usage, [(12, 8)]); self.assertEqual(evidence, [['https://playwright.dev/']])
        self.assertEqual(call.call_args.args[0].model, 'gpt-6-luna'); self.assertEqual(client.close.await_count, 1)

    async def test_workflow_retry_cap_and_budget_capacity(self):
        async def fail(config, prompt, context, caps): caps.on_usage(5, 5); raise ValueError('Persistent failure')
        self.runtime.runner = fail
        _, workflow = await self.request('/workflows', {'prompt': 'Retry cap'})
        for attempt in range(1, 4):
            await self.pump(2)
            self.assertEqual(self.runtime.store.get_task(workflow['id'])['status'], 'failed')
            status, _ = await self.request(f"/workflows/{workflow['id']}/retry", {})
            self.assertEqual(status, 400 if attempt == 3 else 202)

    async def test_parallel_workflow_waits_for_token_reservations(self):
        self.config.daily_token_limit = 1500; calls = []; gate = asyncio.Future()
        async def runner(config, prompt, context, caps):
            calls.append(prompt)
            if len(calls) == 1: await gate
            caps.on_usage(5, 5); return 'Stage result'
        self.runtime.runner = runner
        _, workflow = await self.request('/workflows', {'prompt': 'Budget-aware team'})
        await self.pump(1); self.assertEqual(len(calls), 1)
        self.assertFalse(any(t['status'] == 'failed' for t in self.runtime.store.workflow_tasks(workflow['id'])))
        gate.set_result(None); await self.pump(7)
        self.assertEqual(len(calls), 4); self.assertEqual(self.runtime.store.get_task(workflow['id'])['status'], 'completed')

    async def test_fastapi_real_runtime_handover_and_empty_delete(self):
        import httpx
        from harness.api.server import create_app
        await self.runtime.close()
        def factory(config, store):
            self.runtime = Runtime(config, store=store, browser=self.browser, google=self.google, runner=self.runner)
            return self.runtime
        app = create_app(self.config, runtime_factory=factory)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://127.0.0.1:8787', headers={'Authorization': f'Bearer {self.config.token}'}) as client:
                self.assertEqual((await client.get('/api/status')).json()['backend'], 'python')
                memory = await client.post('/api/memories', json={'text': 'Persisted Python HTTP memory'})
                self.assertEqual(memory.status_code, 201, memory.text)
                self.assertEqual((await client.post('/api/chat', json={'message': 'Synthetic API chat'})).status_code, 200)
                schedule = await client.post('/api/schedules', json={'prompt': 'Future synthetic task', 'runAt': '2099-01-01T00:00:00Z'})
                self.assertEqual(schedule.status_code, 201, schedule.text)
                self.assertEqual((await client.delete('/api/schedules/' + schedule.json()['id'])).status_code, 200)
        second = create_app(self.config, runtime_factory=factory)
        async with second.router.lifespan_context(second):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=second), base_url='http://127.0.0.1:8787', headers={'Authorization': f'Bearer {self.config.token}'}) as client:
                self.assertTrue(any(m['text'] == 'Persisted Python HTTP memory' for m in (await client.get('/api/memories')).json()['memories']))
                self.assertEqual(len((await client.get('/api/history')).json()['messages']), 2)
                self.assertEqual((await client.delete('/api/memories/' + memory.json()['id'])).status_code, 200)

    async def test_python_resumes_existing_node_database_without_replaying_completed_agents(self):
        await self.runtime.close()
        module = (Path(__file__).resolve().parents[2] / 'legacy/typescript/src/local/store.ts').as_uri()
        seed = '''
import { LocalStore } from MODULE;
const store = new LocalStore(process.argv[1]);
const memory = store.saveMemory({text:'Synthetic Node migration memory',source:'user',provenance:'explicit'});
const workflow = store.createWorkflow({prompt:'Synthetic migration workflow',nodes:[
 {id:'a',role:'A',prompt:'Stage A',dependsOn:[]},
 {id:'b',role:'B',prompt:'Stage B',dependsOn:['a']}],output:'b'});
store.updateTask(workflow.id,{status:'running'});
store.updateTask(`${workflow.id}:a`,{status:'running'});
store.updateTask(`${workflow.id}:a`,{status:'completed',result:'Node checkpoint'});
store.updateTask(`${workflow.id}:b`,{status:'running'});
store.addMessage('user','Synthetic retained Node chat');
store.close(); process.stdout.write(JSON.stringify({workflowId:workflow.id,memoryId:memory.id}));
'''.replace('MODULE', json.dumps(module))
        result = subprocess.run(['node', '--experimental-strip-types', '--input-type=module', '-e', seed, str(self.root / 'assistant.sqlite')], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        ids = json.loads(result.stdout); calls = []
        async def runner(config, prompt, context, caps):
            calls.append(prompt); self.assertIn('Node checkpoint', prompt); caps.on_usage(5, 5); return 'Python resumed result'
        # Copy the closed fixture bytes to a Python-created file. The restricted
        # test host applies different access to Node-created SQLite files.
        restored = self.root / 'python-restored.sqlite'
        restored.write_bytes((self.root / 'assistant.sqlite').read_bytes())
        # Antivirus/fixture file inspection may briefly hold the newly copied
        # file on Windows. Failed initialization closes its connection before
        # this bounded retry; persistent permission errors still fail the test.
        import sqlite3
        for attempt in range(8):
            try:
                migrated = LocalStore(restored)
                break
            except sqlite3.OperationalError as error:
                if attempt == 7 or not any(word in str(error).lower() for word in ('readonly', 'locked')): raise
                await asyncio.sleep(0.05 * (attempt + 1))
        self.runtime = Runtime(self.config, store=migrated, browser=self.browser, google=self.google, runner=runner)
        await self.pump()
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.runtime.store.get_task(ids['workflowId'])['result'], 'Python resumed result')
        self.assertTrue(any(m['id'] == ids['memoryId'] for m in self.runtime.store.list_memories()))
        self.assertEqual(self.runtime.store.list_messages()[0]['content'], 'Synthetic retained Node chat')


    async def test_history_semantic_api_expands_ranked_candidates_and_counts_usage(self):
        old=self.runtime.store.add_message('user','The bicycle has a broken derailleur')
        async def runner(config,prompt,context,caps):
            caps.on_usage(5,5)
            return json.dumps(['bicycle','derailleur']) if prompt.startswith('Expand') else json.dumps([old['id'],'invented-id'])
        self.runtime.runner=runner
        status,result=await self.request('/history/search',dict(query='bike repair',semantic=True))
        self.assertEqual(status,200)
        self.assertEqual([m['id'] for m in result['messages']],[old['id']])
        self.assertEqual(len(self.runtime.store.list_usage()),2)

    async def test_people_api_context_repairs_and_read_only_boundaries(self):
        status,person=await self.request('/people',dict(name='Taylor',context='Synthetic colleague'))
        self.assertEqual(status,201)
        from harness.knowledge.memory import build_context
        self.assertIn('Synthetic colleague',build_context(self.runtime.store,'Ask Taylor'))
        self.assertEqual((await self.runtime.operate('people_search',dict(query='Taylor'),read_only=True))['people'][0]['id'],person['id'])
        with self.assertRaisesRegex(ValueError,'read-only'):
            await self.runtime.operate('propose_action',dict(kind='slack.send',payload={}),read_only=True)
        status,_=await self.request('/people/'+person['id'],method='DELETE')
        self.assertEqual(status,200)
        memory=self.runtime.store.save_memory(dict(text='Old claim',source='user',provenance='explicit'))
        status,_=await self.request('/memories/'+memory['id'],dict(text='Corrected claim',expectedRevision=self.runtime.store.revision()),'PATCH')
        self.assertEqual(status,200)
        status,result=await self.request('/repairs')
        self.assertEqual(result['repairs'][0]['kind'],'correction')

    async def test_provider_approval_integrates_with_one_execution_claim(self):
        import httpx
        sends=[]
        def handler(request):
            if request.url.path=='/api/auth.test':return httpx.Response(200,json=dict(ok=True,user_id='U123456',team_id='T123456'))
            sends.append(json.loads(request.content));return httpx.Response(200,json=dict(ok=True,channel='C123456',ts='1.0'))
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.runtime.providers.client=client;self.runtime.providers.owns_client=True
        await self.runtime.providers.connect_token('slack','synthetic-token-123456789')
        status,approval=await self.request('/actions',dict(kind='slack.send',payload=dict(channel='C123456',text='Synthetic only'),summary='Test proposal'))
        self.assertEqual(status,201);self.assertEqual(sends,[])
        status,result=await self.request('/approvals/'+approval['id']+'/decision',dict(decision='approved',expectedDigest=approval['digest']))
        self.assertEqual(status,200);self.assertEqual(result['status'],'completed');self.assertEqual(len(sends),1)
        await self.request('/approvals/'+approval['id']+'/decision',dict(decision='approved',expectedDigest=approval['digest']))
        self.assertEqual(len(sends),1)

    async def test_selective_delivery_holds_unverified_claims_and_keeps_a_repair(self):
        schedule=self.runtime.create_schedule(dict(prompt='Check source',runAt=1,deliveryPolicy='changes'))
        task=self.runtime.store.create_task(dict(id='schedule-'+schedule['id']+'-1',prompt='Synthetic update'))
        self.runtime.freshness.verify=AsyncMock(return_value={'id':'report-id','selected':False})
        await self.runtime.run_task(task)
        self.assertFalse(any(n['taskId']==task['id'] for n in self.runtime.store.list_notifications()))
        self.runtime.freshness.verify.assert_awaited_once()


if __name__ == '__main__':
    import unittest
    unittest.main()
