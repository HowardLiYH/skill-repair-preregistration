"""Credential-free reachability fixture for the prepared GDP network/tunnel."""
import argparse
import json
from pathlib import Path
import subprocess
import uuid
from .contracts import write_json_atomic
from .pilot_network import POLICY,policy_hash,verify_installed

CODE=r'''
const http=require('http'),https=require('https'),net=require('net'),fs=require('fs'),os=require('os');
function tcp(host,port){return new Promise(resolve=>{const s=net.createConnection({host,port});const t=setTimeout(()=>s.destroy(new Error('timeout')),2500);s.on('connect',()=>{clearTimeout(t);s.destroy();resolve({host,port,connected:true})});s.on('error',e=>{clearTimeout(t);resolve({host,port,connected:false,error:e.code||e.message})})})}
function external(url){return new Promise(resolve=>{const r=https.get(url,{signal:AbortSignal.timeout(3000)},s=>{s.resume();resolve({url,response:true,status:s.statusCode})});r.on('error',e=>resolve({url,response:false,error:e.code||e.message}))})}
function relay(){return new Promise(resolve=>{const r=http.request('http://172.30.53.1:18080/v1/messages?beta=true',{method:'POST',headers:{'content-type':'application/json','content-length':'2'},signal:AbortSignal.timeout(10000)},s=>{s.resume();resolve({status:s.statusCode,authorization_sent:false})});r.on('error',e=>resolve({error:e.code||e.message,authorization_sent:false}));r.end('{}')})}
Promise.all([relay(),external('https://api.deepseek.com/'),external('https://1.1.1.1/'),tcp('172.30.53.1',22),tcp('172.30.53.1',18081),tcp('::ffff:172.30.53.1',22)]).then(checks=>console.log(JSON.stringify({checks,ipv6_all_disabled:fs.readFileSync('/proc/sys/net/ipv6/conf/all/disable_ipv6','utf8').trim(),ipv6_default_disabled:fs.readFileSync('/proc/sys/net/ipv6/conf/default/disable_ipv6','utf8').trim(),interfaces:os.networkInterfaces()})));
'''

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--network-state',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--image',default='capstudy-runtime-smoke:20261106')
    args=parser.parse_args(argv)
    verified=verify_installed(args.network_state)
    name='capstudy-network-probe-'+uuid.uuid4().hex[:10]
    command=['docker','run','--rm','--name',name,'--network',POLICY['network'],
        '--cpus','1','--memory','256m','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges',
        '--sysctl','net.ipv6.conf.all.disable_ipv6=1','--sysctl','net.ipv6.conf.default.disable_ipv6=1',
        args.image,'node','-e',CODE]
    try:
        completed=subprocess.run(command,capture_output=True,text=True,timeout=20)
        result=json.loads(completed.stdout) if completed.returncode==0 else {'error':'probe_process_failed'}
        checks=result.get('checks',[])
        passed=(completed.returncode==0 and len(checks)==6 and checks[0].get('status')==400
            and all(x.get('response') is False for x in checks[1:3])
            and all(x.get('connected') is False for x in checks[3:])
            and result.get('ipv6_all_disabled')=='1' and result.get('ipv6_default_disabled')=='1'
            and not any(a['family']=='IPv6' for group in result.get('interfaces',{}).values() for a in group))
        report={'schema_version':1,'evidence_kind':'fixture','executed_model':False,
            'credentials_sent':False,'passed':passed,'policy_sha256':policy_hash(),
            'network_verification':verified,'result':result,'command':command,'stderr':completed.stderr,
            'limitations':'Probes establish these exact targets and current scoped firewall rules; not a claim about all future host configuration.'}
        write_json_atomic(args.output,report);print(json.dumps(report,indent=2));return 0 if passed else 2
    finally:subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=15)

if __name__=='__main__':raise SystemExit(main())
