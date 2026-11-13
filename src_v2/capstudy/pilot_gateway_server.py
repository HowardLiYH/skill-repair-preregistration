"""Owner-only local launch wrapper for the scoped pilot gateway.

Nothing listens without --serve. Real provider credentials stay on this host.
An external SSH tunnel can forward this loopback port to an isolated solver.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import time
from .connection_pilot import read_private_key
from .contracts import write_json_atomic
from .deepseek_gateway import BudgetedGateway, OfficialTransport, make_server
from .journal import Journal, AdmissionBlocked

ROOT = Path(__file__).resolve().parents[2]

def read_relay_token(path):
    path = Path(path)
    allowed = (ROOT/'private_artifacts/secrets').resolve()
    if path.is_symlink() or not path.is_file() or path.resolve().parent != allowed:
        raise ValueError('Relay token must be a regular file in private secrets directory')
    if path.stat().st_mode & 0o077:
        raise ValueError('Relay token requires owner-only permissions')
    token=path.read_text().strip()
    if not re.fullmatch(r'capstudy-relay-[A-Za-z0-9_-]{32,}',token):
        raise ValueError('Dedicated scoped relay token required; never a provider API key')
    return token

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--trial-id',required=True)
    p.add_argument('--token-file',type=Path,required=True)
    p.add_argument('--port',type=int,default=18081)
    p.add_argument('--lifetime-seconds',type=int,default=3600)
    p.add_argument('--serve',action='store_true')
    a=p.parse_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',a.trial_id):raise ValueError('Invalid trial identity')
    if not 1024 <= a.port <= 65535 or not 60 <= a.lifetime_seconds <= 7200:
        raise ValueError('Bounded nonprivileged listener settings required')
    config=json.loads((ROOT/'configs/pilot-deepseek.proposed.json').read_text())
    if (config.get('approved_incremental_budget_usd') != 5 or
        config.get('approval_status') != 'user_approved_pilot_only_2026-11-06'):
        raise AdmissionBlocked('Existing $5 scoped approval required')
    if not a.serve:
        print(json.dumps({'listening':False,'bind':'127.0.0.1','port':a.port,
            'trial_id':a.trial_id,'budget_usd':5,'model_calls':0}));return 0
    # Reading keys is delayed until the explicit serving path. Neither is output.
    token=read_relay_token(a.token_file)
    key=read_private_key(ROOT/config['secret_file'])
    root=ROOT/'private_artifacts/deepseek-pilot'
    journal=Journal(root/'ledger.sqlite',5,campaign_id=config['campaign_id'])
    server=None
    stopping=False
    def stop(_signal,_frame):
        nonlocal stopping
        stopping=True
    signal.signal(signal.SIGTERM,stop)
    signal.signal(signal.SIGINT,stop)
    state=root/'gateway'/a.trial_id/'server.json'
    status={'trial_id':a.trial_id,'pid':os.getpid(),'bind':'127.0.0.1',
        'port':a.port,'budget_usd':5,'started_at':datetime.now(timezone.utc).isoformat(),
        'lifetime_seconds':a.lifetime_seconds,'provider_key_location':'local_private_file_only'}
    try:
        gateway=BudgetedGateway(journal,root/'gateway'/a.trial_id/'requests',
            OfficialTransport(key,timeout_seconds=300),relay_token=token,trial_id=a.trial_id)
        server=make_server(gateway,('127.0.0.1',a.port));server.timeout=1
        write_json_atomic(state,{**status,'listening':True})
        print(json.dumps({'listening':True,'port':a.port,'trial_id':a.trial_id}),flush=True)
        deadline=time.monotonic()+a.lifetime_seconds
        while not stopping and time.monotonic()<deadline:
            server.handle_request()
    finally:
        if server is not None:server.server_close()
        write_json_atomic(state,{**status,'listening':False,
            'stopped_at':datetime.now(timezone.utc).isoformat()})
        journal.close()
    return 0

if __name__=='__main__':raise SystemExit(main())
