#!/usr/bin/env python3
"""Isolated JSON-RPC hook discovery fixture, never a native trust attestation."""
import json
import os
from pathlib import Path
import shlex
import sys
import time
mode = os.environ.get("CPD_TEST_MODE", "normal")
if mode == 'exit':
    Path(os.environ['CPD_TEST_PID']).write_text(str(os.getpid()))
    sys.exit(3)
if mode == "timeout":
    if os.environ.get("CPD_TEST_PID"):
        Path(os.environ["CPD_TEST_PID"]).write_text(str(os.getpid()))
    time.sleep(60)
supervisor = Path(__file__).resolve().parents[1] / 'skills/codex-pro-dispatch/scripts/resident-supervision.mjs'
for line in sys.stdin:
    message = json.loads(line)
    if message.get('id') == 1:
        if mode == 'notify':
            print(json.dumps({'method': 'notice', 'params': {}}), flush=True)
            print(json.dumps({'id': 1, 'method': 'server/request'}), flush=True)
        if mode == 'garbage':
            print('not json', flush=True)
        if mode == 'oversized':
            print('x' * 1048577, flush=True)
        print(json.dumps({'id': 1, 'result': {'userAgent': 'fixture-codex 1'}}), flush=True)
    if message.get('id') == 2:
        hook = {'key': 'fixture', 'eventName': 'stop', 'async': False, 'enabled': True,
                'trustStatus': 'trusted', 'command': shlex.join(['node', str(supervisor), 'stop'])}
        hook.update(json.loads(os.environ.get('CPD_TEST_HOOK', '{}')))
        hooks = [] if mode == 'empty' else [hook, dict(hook, key='second', eventName='other')] if mode == 'duplicate' else [hook]
        reply = {'id': 2, 'result': {'data': [{'hooks': hooks, 'errors': ['fixture discovery error'] if mode == 'errors' else []}]}}
        if mode == 'rpc_error':
            reply = {'id': 2, 'error': {'code': -32601, 'message': 'fixture method unavailable'}}
        if mode == 'not_object':
            reply = []
        print(json.dumps(reply), flush=True)
