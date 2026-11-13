"""Scoped Linux Docker network/firewall and credential-free TCP forwarder.

Plan is the default. Only task-owned named chains/network may be created or
removed. The solver can reach one bridge-bound relay port; other host services
and forwarded destinations are rejected. No API credentials or model calls.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import selectors
import socket
import socketserver
import subprocess
import time
from .contracts import canonical_hash, write_json_atomic

POLICY = {'schema_version':1,'network':'capstudy-gdp-pilot','bridge':'br-capstudy5300',
          'subnet':'172.30.53.0/24','gateway':'172.30.53.1','relay_port':18080,
          'upstream_host':'127.0.0.1','upstream_port':18080,
          'input_chain':'CAPSTUDY5300IN','forward_chain':'CAPSTUDY5300FWD',
          'internal':True,'ipv6':False,'label':'capstudy.owner=gdp-pilot'}


def policy_hash():return canonical_hash(POLICY)


def firewall_rules():
    p=POLICY
    return [
        ['-N',p['input_chain']],
        ['-A',p['input_chain'],'-s',p['subnet'],'-d',p['gateway'],'-p','tcp','--dport',str(p['relay_port']),'-j','ACCEPT'],
        ['-A',p['input_chain'],'-j','REJECT'],
        ['-I','INPUT','1','-i',p['bridge'],'-j',p['input_chain']],
        ['-N',p['forward_chain']],
        ['-A',p['forward_chain'],'-j','REJECT'],
        ['-I','FORWARD','1','-i',p['bridge'],'-j',p['forward_chain']],
    ]


def _run(args,*,check=True):
    return subprocess.run(args,check=check,capture_output=True,text=True,timeout=30)


def install(state_path):
    state_path=Path(state_path)
    if state_path.exists():raise ValueError('Existing state requires inspection; never overwrite ownership')
    state_path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    p=POLICY
    if _run(['docker','network','inspect',p['network']],check=False).returncode==0:
        raise ValueError('Network name already exists; refuse to assume ownership')
    for chain in (p['input_chain'],p['forward_chain']):
        if _run(['iptables','-w','5','-S',chain],check=False).returncode==0:
            raise ValueError('Firewall chain already exists; refuse to modify')
    state={'policy':p,'policy_sha256':policy_hash(),'network_id':None,'rules_applied':[],'status':'installing'}
    write_json_atomic(state_path,state)
    try:
        result=_run(['docker','network','create','--driver','bridge','--internal',
            '--subnet',p['subnet'],'--gateway',p['gateway'],'--opt','com.docker.network.bridge.name='+p['bridge'],
            '--label',p['label'],p['network']])
        state['network_id']=result.stdout.strip();write_json_atomic(state_path,state)
        for rule in firewall_rules():
            _run(['iptables','-w','5',*rule]);state['rules_applied'].append(rule);write_json_atomic(state_path,state)
        state['network_inspect']=json.loads(_run(['docker','network','inspect',p['network']]).stdout)
        info=state['network_inspect'][0]
        if not info['Internal'] or info['EnableIPv6'] or info.get('Containers'):
            raise RuntimeError('Unexpected network state')
        state['iptables_input']=_run(['iptables','-w','5','-S',p['input_chain']]).stdout
        state['iptables_forward']=_run(['iptables','-w','5','-S',p['forward_chain']]).stdout
        state['status']='installed';write_json_atomic(state_path,state)
        return state
    except BaseException:
        state['status']='failed_install_preserved_for_cleanup';write_json_atomic(state_path,state)
        raise


def remove(state_path):
    state_path=Path(state_path);state=json.loads(state_path.read_text())
    if state.get('policy_sha256')!=policy_hash() or state.get('policy')!=POLICY:
        raise ValueError('Network ownership policy differs')
    if state.get('status')=='removed':return state
    p=POLICY
    if state.get('network_id'):
        current=json.loads(_run(['docker','network','inspect',p['network']]).stdout)[0]
        if current['Id']!=state['network_id'] or current['Labels'].get('capstudy.owner')!='gdp-pilot':
            raise ValueError('Network identity/ownership changed')
        if current.get('Containers'):raise RuntimeError('Stop task solver containers before removing firewall')
    # Remove only exact rules recorded as successfully installed; never flush
    # built-in chains or unrelated Docker/UFW rules.
    for rule in reversed(state['rules_applied']):
        if rule[0]=='-N':inverse=['-X',rule[1]]
        elif rule[0]=='-I':inverse=['-D',rule[1],*rule[3:]]
        else:inverse=['-D',*rule[1:]]
        _run(['iptables','-w','5',*inverse])
        state['rules_applied'].pop();write_json_atomic(state_path,state)
    if state.get('network_id'):_run(['docker','network','rm',state['network_id']])
    state['status']='removed';write_json_atomic(state_path,state);return state


def verify_installed(state_path):
    state=json.loads(Path(state_path).read_text())
    if state.get('status')!='installed' or state.get('policy_sha256')!=policy_hash() or state.get('policy')!=POLICY:
        raise ValueError('Installed matching network policy required')
    actual=json.loads(_run(['docker','network','inspect',POLICY['network']]).stdout)[0]
    if actual['Id']!=state['network_id'] or actual['Internal'] is not True or actual['EnableIPv6']:
        raise ValueError('Network identity/isolation changed')
    if actual.get('Containers'):raise ValueError('Concurrency one requires empty solver network before launch')
    for rule in firewall_rules():
        if rule[0]=='-N':continue
        check=['-C',rule[1],*(rule[3:] if rule[0]=='-I' else rule[2:])]
        _run(['iptables','-w','5',*check])
    for chain,key in [(POLICY['input_chain'],'iptables_input'),(POLICY['forward_chain'],'iptables_forward')]:
        if _run(['iptables','-w','5','-S',chain]).stdout!=state[key]:
            raise ValueError('Task-owned firewall chain was modified')
    for builtin,chain in [('INPUT',POLICY['input_chain']),('FORWARD',POLICY['forward_chain'])]:
        first=next((line for line in _run(['iptables','-w','5','-S',builtin]).stdout.splitlines() if line.startswith('-A ')),None)
        if first!=f'-A {builtin} -i {POLICY["bridge"]} -j {chain}':
            raise ValueError('Task-owned bridge firewall jump is no longer first')
    return {'verified':True,'policy_sha256':policy_hash(),'network_id':actual['Id']}


class _Forward(socketserver.BaseRequestHandler):
    def handle(self):
        # This server never reads authentication, parses HTTP, or logs payloads.
        try:upstream=socket.create_connection((POLICY['upstream_host'],POLICY['upstream_port']),timeout=5)
        except OSError:return
        with upstream, selectors.DefaultSelector() as poll:
            client=self.request
            for connection in (client,upstream):
                connection.setblocking(False);poll.register(connection,selectors.EVENT_READ)
            began=time.monotonic()
            while time.monotonic()-began<360:
                events=poll.select(1)
                if not events:continue
                for key,_ in events:
                    source=key.fileobj;target=upstream if source is client else client
                    try:
                        chunk=source.recv(65536)
                        if not chunk:return
                        # Bounded, blocking send is simpler than unbounded queues.
                        target.settimeout(10);target.sendall(chunk);target.setblocking(False)
                    except OSError:return


class _Server(socketserver.TCPServer):
    allow_reuse_address=True
    request_queue_size=2


def forward(state_path):
    state=json.loads(Path(state_path).read_text())
    if state.get('status')!='installed' or state.get('policy_sha256')!=policy_hash():
        raise ValueError('Installed matching policy required')
    # Single-thread forwarding matches campaign concurrency one. The source is
    # a fixed literal bridge IP; no public bind and no dynamic destination.
    with _Server((POLICY['gateway'],POLICY['relay_port']),_Forward) as server:
        server.serve_forever(poll_interval=.5)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['plan','install','remove','forward'],nargs='?',default='plan')
    parser.add_argument('--state',type=Path)
    args=parser.parse_args(argv)
    if args.action=='plan':print(json.dumps({'policy':POLICY,'policy_sha256':policy_hash(),'iptables_rules':firewall_rules()},indent=2));return 0
    if args.state is None:parser.error('--state is required for mutations or forwarding')
    result={'install':install,'remove':remove,'forward':forward}[args.action](args.state)
    if result:print(json.dumps(result,indent=2))
    return 0


if __name__=='__main__':raise SystemExit(main())
