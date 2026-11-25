"""Explicit runtime diagnostics. Liveness and page loads do not invoke these checks."""
import asyncio
import os
from pathlib import Path
import shutil
import tempfile

from harness.core.utils import now_ms
from harness.integrations.failures import ConnectorFailure
from harness.integrations.google import GMAIL_READ_SCOPE, SHEETS_SCOPE


def item(status, detail):
    return dict(status=status, detail=detail)


async def browser_readiness(config):
    if config.browser_executable:
        available = Path(config.browser_executable).is_file()
    else:
        try:
            from playwright.async_api import async_playwright
            channel = config.browser_channel or ('msedge' if os.name == 'nt' else 'chromium')
            if channel == 'chromium':
                async with asyncio.timeout(5), async_playwright() as playwright:
                    available = Path(playwright.chromium.executable_path).is_file()
            else:
                executable = {'msedge': 'msedge', 'chrome': 'google-chrome'}.get(channel, channel)
                available = bool(shutil.which(executable))
                if os.name == 'nt':
                    product = {'msedge': 'Microsoft/Edge/Application/msedge.exe',
                               'chrome': 'Google/Chrome/Application/chrome.exe'}.get(channel)
                    if product:
                        available |= any((Path(root) / product).is_file() for key in ('PROGRAMFILES', 'PROGRAMFILES(X86)', 'LOCALAPPDATA')
                                         if (root := os.environ.get(key)))
        except (ImportError, OSError, TimeoutError, RuntimeError):
            available = False
    return item('ready' if available else 'unavailable', 'Browser executable found. Use Browser to verify a launch.' if available else
                'Install the configured browser or set BROWSER_EXECUTABLE_PATH, then retry.')


async def check(runtime, check_name):
    if check_name not in ('local', 'google', 'model'):
        raise ValueError('Choose local, google, or model diagnostics')
    checks = {}
    if check_name == 'local':
        try:
            runtime.store._tx(lambda: runtime.store.db.execute('SELECT 1').fetchone())
            checks['database'] = item('ready', 'SQLite accepted a transaction.')
        except Exception:
            checks['database'] = item('unavailable', 'Check that the SQLite directory is writable by the runtime.')
        try:
            with tempfile.TemporaryFile(dir=runtime.workspace.root) as probe:
                probe.write(b'readiness')
            checks['workspace'] = item('ready', 'Workspace files can be written.')
        except OSError:
            checks['workspace'] = item('unavailable', 'Check the workspace mount and its permissions.')
        stale = runtime.store.get_setting('memoryProjectionStale')
        checks['projection'] = item('degraded' if stale else 'ready', 'Refresh readable files from Memory.' if stale else 'Readable memory projection has no recorded pending refresh.')
        checks['browser'] = await browser_readiness(runtime.config)
        container = runtime.config.container_mode
        checks['runtime'] = item('ready', 'Container mode. Browser takeover requires a display.' if container else 'Native local runtime.')
        checks['model'] = item('unverified' if runtime.config.api_key else 'unavailable', 'Run the model check to verify access.' if runtime.config.api_key else 'Configure OPENAI_API_KEY.')
        checks['google'] = item('unverified' if runtime.config.google_client_id else 'unavailable', 'Run the Google read check to verify a grant.' if runtime.config.google_client_id else 'Configure the Google OAuth client.')
    elif check_name == 'google':
        try:
            status = await runtime.google.status()
            scopes = status.get('scopes', [])
            if not status.get('connected'):
                checks['google'] = item('unavailable', 'Connect a Google account first.')
            elif GMAIL_READ_SCOPE in scopes:
                await runtime.google.list_mail(max_results=1)
                checks['google'] = item('ready', 'Google accepted a Gmail read. Mail content was excluded from the report.')
            elif SHEETS_SCOPE in scopes:
                await runtime.google.binding(SHEETS_SCOPE)
                checks['google'] = item('unverified', 'Sheets access is configured. Read a registered sheet to verify it.')
            else:
                checks['google'] = item('unavailable', 'Grant Gmail read access or verify another grant through its normal workflow.')
        except ConnectorFailure as error:
            checks['google'] = item('degraded', str(error))
        except Exception:
            checks['google'] = item('degraded', 'Check the Google connection and its granted scopes.')
    else:
        if not runtime.config.api_key:
            checks['model'] = item('unavailable', 'Configure OPENAI_API_KEY.')
        elif not runtime.store.has_usage_capacity(runtime.config.daily_token_limit, runtime.config.run_token_reservation):
            checks['model'] = item('degraded', 'The daily token budget cannot admit another run.')
        else:
            try:
                await runtime.model('Reply with OK.', '', read_only=True, no_tools=True)
                checks['model'] = item('ready', 'The configured model returned a reply.')
            except Exception:
                checks['model'] = item('degraded', 'The model call failed. Check API access, quota and connectivity.')
    report = dict(check=check_name, checkedAt=now_ms(), checks=checks)
    runtime.store.append_event('runtime.readiness', report)
    return report
