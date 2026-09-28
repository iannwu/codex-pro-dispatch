"""Configuration diagnostics only; native Stop qualification remains mandatory."""
import json
import os
from pathlib import Path
import selectors
import shlex
import signal
import subprocess
import time

BUNDLED = Path('/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex')
TIMEOUT = 15
LIMIT = 1048576


def classify(hooks, command):
    """Structural errors precede trust advice so approval cannot hide a bad hook."""
    if not isinstance(hooks, list) or any(not isinstance(h, dict) for h in hooks):
        raise ValueError('Unsupported hooks list schema')
    resident = [h for h in hooks if isinstance(h.get('command'), str)
                and 'resident-supervision.mjs' in h['command']]
    # source/pluginId/sourcePath show which installation each hook belongs to.
    fields = ('key', 'eventName', 'command', 'async', 'enabled', 'trustStatus', 'source', 'pluginId', 'sourcePath')
    details = {'hooks': [{k: h.get(k) for k in fields} for h in resident]}
    if not resident:
        return dict(details, state='blocked', reason='missing', actions=['Configure this installation\'s resident Stop hook: enable the plugin, or run the source checkout\'s install.sh.'])
    if len(resident) != 1:
        return dict(details, state='blocked', reason='duplicate', actions=['Repair duplicate resident hook definitions; keep one synchronous Stop hook.'])
    hook = resident[0]
    if not isinstance(hook.get('eventName'), str) or type(hook.get('async')) is not bool:
        raise ValueError('Unsupported resident hook definition schema: ' + json.dumps(details))
    try:
        argv = shlex.split(hook['command'])
    except ValueError:
        argv = []
    if argv != command or hook['eventName'] != 'stop' or hook['async']:
        return dict(details, state='blocked', reason='wrong_definition', actions=['Repair the resident hook definition to match this installation\'s synchronous Stop command; keep one installation.'], expected_command=shlex.join(command))
    if type(hook.get('enabled')) is not bool or hook.get('trustStatus') not in ('trusted', 'managed', 'untrusted', 'modified'):
        return dict(details, state='unverified', reason='unsupported_hook_state', actions=['Continue only through the mandatory native Stop qualification; do not guess another Codex binary.'])
    reasons, actions = [], []
    if not hook['enabled']:
        reasons.append('disabled'); actions.append('Enable the resident Stop hook in /hooks.')
    if hook['trustStatus'] in ('untrusted', 'modified'):
        reasons.append(hook['trustStatus']); actions.append('Review and trust the current resident Stop hook definition in /hooks.')
    return dict(details, state='blocked' if reasons else 'ready', reason='+'.join(reasons) or 'configured', actions=actions)


def discover(binary, cwd):
    """Bounded fresh-process RPC, never a query of the running desktop host."""
    p = subprocess.Popen([binary, 'app-server', '--listen', 'stdio://'], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        def send(value):
            p.stdin.write((json.dumps(value) + '\n').encode()); p.stdin.flush()
        send({'id': 1, 'method': 'initialize', 'params': {
            'clientInfo': {'name': 'pro-dispatch-hook-check', 'version': '1'},
            'capabilities': {'experimentalApi': True}}})
        end, buffer, total, initialized = time.monotonic() + TIMEOUT, b'', 0, None
        with selectors.DefaultSelector() as selector:
            selector.register(p.stdout, selectors.EVENT_READ)
            while time.monotonic() < end:
                if not selector.select(max(0, end-time.monotonic())):
                    break
                chunk = os.read(p.stdout.fileno(), 65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > LIMIT:
                    raise ValueError('Hook response exceeds limit')
                buffer += chunk
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError('Unsupported RPC response schema')
                    if 'method' in value:
                        continue
                    if value.get('id') == 1 and initialized is None:
                        if 'error' in value or not isinstance(value.get('result'), dict):
                            raise ValueError('Codex initialization rejected: ' + json.dumps(value))
                        initialized = value['result']
                        send({'method': 'initialized'})
                        send({'id': 2, 'method': 'hooks/list', 'params': {'cwds': [cwd]}})
                    elif value.get('id') == 2 and initialized is not None:
                        if 'error' in value:
                            raise ValueError('Hook discovery rejected: ' + json.dumps(value))
                        data = value.get('result', {}).get('data')
                        if ('error' in value or not isinstance(data, list) or len(data) != 1
                                or not isinstance(data[0], dict) or data[0].get('errors')):
                            raise ValueError('Hook discovery rejected: ' + json.dumps(value))
                        if data[0].get('cwd', cwd) != cwd:
                            raise ValueError('Hook discovery returned a different cwd')
                        return data[0].get('hooks'), initialized
        raise ValueError('Codex hook discovery timed out or ended; exit=' + str(p.poll()))
    finally:
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        finally:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            p.wait()
            for pipe in (p.stdin, p.stdout):
                try:
                    pipe.close()
                except OSError:
                    pass


def check(codex=None):
    binary = codex or (str(BUNDLED) if BUNDLED.is_file() else None)
    cwd = str(Path.cwd())
    context = {'codex': binary, 'cwd': cwd,
               'codex_home': str(Path(os.environ.get('CODEX_HOME', str(Path.home()/'.codex'))).expanduser()),
               'scope': 'fresh_process_configuration_only', 'admissionObserved': False}
    # The selected installation is this CLI's own tree; its hook must be the one.
    supervisor = Path(__file__).resolve().parents[2] / 'skills/codex-pro-dispatch/scripts/resident-supervision.mjs'
    command = ['node', str(supervisor.resolve()), 'stop']
    context['expected_command'] = shlex.join(command)
    try:
        if binary and not Path(binary).is_absolute():
            result = {'state': 'unverified', 'reason': 'invalid_host_path', 'actions': ['Use only an explicitly verified absolute --codex path; continue only through mandatory native qualification.']}
        elif not binary:
            result = {'state': 'unverified', 'reason': 'no_known_host', 'actions': ['Continue only through native Stop qualification, or supply an explicitly verified host with --codex. Never guess a binary.']}
        else:
            hooks, identity = discover(binary, cwd)
            context['server_identity'] = {k: str(identity[k])[:512] for k in ('userAgent', 'version') if k in identity}
            result = classify(hooks, command)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        result = {'state': 'unverified', 'reason': 'discovery_unavailable', 'error': str(exc)[:2048],
                  'actions': ['Preserve this diagnostic and continue only through mandatory native Stop qualification. Never guess another Codex binary or write a trust approval.']}
    return dict(context, **result, ok=result['state'] == 'ready', kind='listener_hook_preflight')
