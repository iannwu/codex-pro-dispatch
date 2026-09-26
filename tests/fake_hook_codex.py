#!/usr/bin/env python3
"""Isolated JSON-RPC hook discovery fixture, never a native trust attestation."""
import json
import os
from pathlib import Path
import shlex
import sys
supervisor = Path(__file__).resolve().parents[1] / 'skills/codex-pro-dispatch/scripts/resident-supervision.mjs'
for line in sys.stdin:
    message = json.loads(line)
    if message.get('id') == 1:
        print(json.dumps({'id': 1, 'result': {}}), flush=True)
    if message.get('id') == 2:
        hook = {'key': 'fixture', 'eventName': 'stop', 'async': False, 'enabled': True,
                'trustStatus': 'trusted', 'command': shlex.join(['node', str(supervisor), 'stop'])}
        hook.update(json.loads(os.environ.get('CPD_TEST_HOOK', '{}')))
        print(json.dumps({'id': 2, 'result': {'data': [{'hooks': [hook], 'errors': []}]}}), flush=True)
