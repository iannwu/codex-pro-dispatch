"""Read Codex's own hook trust decision without changing approval state."""
import json
import os
from pathlib import Path
import selectors
import shlex
import signal
import shutil
import subprocess
import time

from .core import StateError


def check(codex=None):
    bundled = Path('/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex')
    binary = codex or (str(bundled) if bundled.is_file() else shutil.which('codex'))
    if not binary:
        raise StateError('hook_preflight_unavailable', details={'next_action': 'Install Codex or supply --codex with the Listener host binary'})
    supervisor = Path(__file__).resolve().parents[2] / 'skills/codex-pro-dispatch/scripts/resident-supervision.mjs'
    command = ['node', str(supervisor.resolve()), 'stop']
    try:
        p = subprocess.Popen([binary, 'app-server', '--listen', 'stdio://'], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            def send(value):
                p.stdin.write((json.dumps(value) + '\n').encode()); p.stdin.flush()
            send({'id': 1, 'method': 'initialize', 'params': {
                'clientInfo': {'name': 'pro-dispatch-hook-check', 'version': '1'},
                'capabilities': {'experimentalApi': True}}})
            end = time.monotonic() + 15
            buffer = b''
            total = 0
            with selectors.DefaultSelector() as selector:
                selector.register(p.stdout, selectors.EVENT_READ)
                while time.monotonic() < end:
                    if not selector.select(max(0, end-time.monotonic())):
                        break
                    chunk = os.read(p.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > 1048576:
                        raise ValueError('Hook response exceeds limit')
                    buffer += chunk
                    while b'\n' in buffer:
                        line, buffer = buffer.split(b'\n', 1)
                        value = json.loads(line)
                        if value.get('id') == 1 and 'method' not in value:
                            if 'error' in value:
                                raise ValueError('Codex initialization rejected')
                            send({'method': 'initialized'})
                            send({'id': 2, 'method': 'hooks/list', 'params': {'cwds': [str(Path.cwd())]}})
                        elif value.get('id') == 2 and 'method' not in value:
                            data = value.get('result', {}).get('data')
                            if not isinstance(data, list) or len(data) != 1 or data[0].get('errors'):
                                raise ValueError('Hook discovery failed')
                            matches = []
                            for hook in data[0].get('hooks', []):
                                try:
                                    same = shlex.split(hook.get('command') or '') == command
                                except ValueError:
                                    same = False
                                if same and hook.get('eventName') == 'stop' and hook.get('async') is False:
                                    matches.append(hook)
                            ready = [h for h in matches if h.get('enabled') is True and h.get('trustStatus') in ('trusted', 'managed')]
                            if len(matches) != 1 or not ready:
                                raise StateError('listener_hook_not_ready', details={
                                    'codex': binary, 'hooks': [{'key': h.get('key'), 'trust': h.get('trustStatus'),
                                    'enabled': h.get('enabled')} for h in matches],
                                    'next_action': 'Review and trust the resident-supervision.mjs Stop hook in /hooks, then retry. No owner was acquired.'})
                            return {'ok': True, 'kind': 'listener_hook_preflight', 'codex': binary,
                                    'hook': ready[0]['key'], 'admissionObserved': False}
            raise ValueError('Codex hook discovery timed out or ended')
        finally:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL); p.wait()
            p.stdin.close()
            p.stdout.close()
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise StateError('hook_preflight_unavailable', details={'error': str(exc), 'codex': binary}) from exc
