"""Model-free pinned Claude CLI capture on an internal Docker network.

Requires an existing runtime image. Does not install, pull, build, load secrets,
contact a provider, or mutate the scientific protocol. All replies are fixtures.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import time
import uuid
from .contracts import write_json_atomic

MOCK_SERVER = r'''
const http=require('http'), fs=require('fs');let count=0;
const usage={input_tokens:100,output_tokens:20,cache_creation_input_tokens:0,cache_read_input_tokens:0,service_tier:'standard'};
http.createServer((req,res)=>{let chunks=[],size=0;req.on('data',c=>{size+=c.length;if(size>1048576){req.destroy();return}chunks.push(c)});
req.on('end',()=>{let body;try{body=JSON.parse(Buffer.concat(chunks))}catch{res.writeHead(400);res.end('{}');return}
const n=++count;fs.writeFileSync('/evidence/request-'+String(n).padStart(5,'0')+'.json',JSON.stringify({evidence_kind:'fixture',method:req.method,path:req.url,header_names:Object.keys(req.headers).sort(),dummy_bearer_matches:req.headers.authorization==='Bearer capstudy-offline-dummy-token-0000000000000000',body},null,2));
if(n>12){res.writeHead(429);res.end('{"error":"offline request cap"}');return}
const promptText=JSON.stringify(body.messages);
if(promptText.includes('CAPSTUDY_MOCK_503')){res.writeHead(503,{'content-type':'application/json'});res.end('{"type":"error","error":{"type":"overloaded_error","message":"offline fixture 503"}}');return}
if(promptText.includes('CAPSTUDY_CONNECTION_FAILURE')){req.socket.destroy();return}
const resultSeen=body.messages.some(m=>Array.isArray(m.content)&&m.content.some(c=>c.type==='tool_result'));
const useTool=!resultSeen&&(body.tools||[]).some(t=>t.name==='Bash');
const content=useTool?{type:'tool_use',id:'toolu_fixture_1',name:'Bash',input:{command:'printf CAPSTUDY_TOOL_OK'}}:{type:'text',text:'CAPSTUDY_MOCK_READY'};
const message={id:'msg_fixture_'+n,type:'message',role:'assistant',model:'deepseek-flash',content:[content],stop_reason:useTool?'tool_use':'end_turn',stop_sequence:null,usage};
const events=[{type:'message_start',message:{...message,content:[],stop_reason:null,usage:{...usage,output_tokens:0}}},
 {type:'content_block_start',index:0,content_block:useTool?{...content,input:{}}:{type:'text',text:''}},
 {type:'content_block_delta',index:0,delta:useTool?{type:'input_json_delta',partial_json:JSON.stringify(content.input)}:{type:'text_delta',text:content.text}},
 {type:'content_block_stop',index:0},{type:'message_delta',delta:{stop_reason:message.stop_reason,stop_sequence:null},usage},{type:'message_stop'}];
// Deliberately buffer all SSE until completion, as the accounting gateway does.
setTimeout(()=>{res.writeHead(200,{'content-type':body.stream?'text/event-stream':'application/json'});res.end(body.stream?events.map(e=>'event: '+e.type+'\ndata: '+JSON.stringify(e)+'\n\n').join(''):JSON.stringify(message))},promptText.includes('CAPSTUDY_DELAYED_RESPONSE')?6000:1500);
})}).listen(8080,'0.0.0.0',()=>console.log('MOCK_READY'));
'''

EGRESS_CHECK = r'''
const https=require('https');
Promise.all(['https://api.deepseek.com/','https://1.1.1.1/'].map(url=>new Promise(resolve=>{
const req=https.get(url,{timeout:2000,rejectUnauthorized:true},res=>{res.resume();resolve({url,external_response_received:true,status:res.statusCode})});
req.on('timeout',()=>req.destroy(new Error('fixture timeout')));req.on('error',e=>resolve({url,external_response_received:false,error:e.code||e.message}));
}))).then(x=>console.log(JSON.stringify(x)));
'''

def run(output, image, selected_cases=None):
    output=Path(output).resolve()
    if output.exists():raise ValueError('New output directory required; preserve earlier evidence')
    output.mkdir(parents=True,mode=0o700)
    if shutil.disk_usage(output).free < 6*1024**3:raise RuntimeError('Insufficient free disk headroom')
    token='capstudy-offline-dummy-token-0000000000000000'
    suffix=uuid.uuid4().hex[:10]
    network='capstudy-mock-net-'+suffix
    server='capstudy-mock-server-'+suffix
    clients=[]; commands=[]; outcomes=[]
    def command(args, *, timeout=60, check=True, prefix=None):
        started=time.monotonic()
        try:
            p=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
            record={'command':args,'returncode':p.returncode,'duration_seconds':time.monotonic()-started}
        except subprocess.TimeoutExpired as exc:
            record={'command':args,'returncode':None,'timed_out':True,'duration_seconds':time.monotonic()-started}
            commands.append(record)
            if prefix:
                (output/(prefix+'.stdout')).write_bytes(exc.stdout or b'')
                (output/(prefix+'.stderr')).write_bytes(exc.stderr or b'')
            raise
        commands.append(record)
        if prefix:
            (output/(prefix+'.stdout')).write_text(p.stdout)
            (output/(prefix+'.stderr')).write_text(p.stderr)
        if check and p.returncode:raise RuntimeError('Offline command failed: '+args[0])
        return p
    created_network=False;created_server=False
    try:
        info=command(['docker','image','inspect',image],prefix='image')
        image_id=json.loads(info.stdout)[0]['Id']
        help_result=command(['docker','run','--rm','--network','none','--cpus','1','--memory','1g',
                            image,'claude','--help'],prefix='cli-help')
        (output/'server.cjs').write_text(MOCK_SERVER)
        capture=output/'capture';capture.mkdir(mode=0o700)
        command(['docker','network','create','--internal','--label','capstudy.fixture=cli-mock',network])
        created_network=True
        command(['docker','network','inspect',network],prefix='network')
        command(['docker','run','-d','--name',server,'--network',network,'--network-alias','capstudy-mock',
                 '--read-only','--cpus','1','--memory','256m','--cap-drop','ALL','--security-opt','no-new-privileges',
                 '--mount',f'type=bind,src={output / "server.cjs"},dst=/mock/server.cjs,readonly',
                 '--mount',f'type=bind,src={capture},dst=/evidence',image,'node','/mock/server.cjs'])
        created_server=True
        # Docker start returns after process start; bounded readiness check.
        for _ in range(20):
            if 'MOCK_READY' in command(['docker','logs',server]).stdout:break
            time.sleep(.2)
        else:raise RuntimeError('Mock endpoint did not start')
        env={'HOME':'/tmp/capstudy-home','CLAUDE_CONFIG_DIR':'/tmp/capstudy-cli-config',
             'ANTHROPIC_BASE_URL':'http://capstudy-mock:8080','ANTHROPIC_AUTH_TOKEN':token,
             'ANTHROPIC_MODEL':'deepseek-flash','ANTHROPIC_DEFAULT_OPUS_MODEL':'deepseek-flash',
             'ANTHROPIC_DEFAULT_SONNET_MODEL':'deepseek-flash','ANTHROPIC_DEFAULT_HAIKU_MODEL':'deepseek-flash',
             'CLAUDE_CODE_SUBAGENT_MODEL':'deepseek-flash','CLAUDE_CODE_MAX_OUTPUT_TOKENS':'16384',
             'CLAUDE_CODE_MAX_RETRIES':'0','CLAUDE_CODE_DISABLE_TERMINAL_TITLE':'1',
             'DISABLE_AUTOUPDATER':'1','CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1',
             'DISABLE_TELEMETRY':'1','DISABLE_ERROR_REPORTING':'1'}
        base=['docker','run','--rm','--network',network,'--cpus','1','--memory','2g',
              '--read-only','--cap-drop','ALL','--security-opt','no-new-privileges',
              '--tmpfs','/tmp:rw,size=128m','--tmpfs','/workspace:rw,size=64m','--workdir','/workspace']
        for key,value in env.items():base+=['--env',key+'='+value]
        egress=command([*base,'--name','capstudy-mock-egress-'+suffix,image,'node','-e',EGRESS_CHECK],prefix='egress',timeout=15)
        egress_results=json.loads(egress.stdout)
        if any(x['external_response_received'] for x in egress_results):raise RuntimeError('External response received on isolated network')
        local_tools='Bash,Read,Write,Edit,Glob,Grep,Skill,TodoWrite'
        common=['claude','--verbose','--output-format','stream-json','--model','deepseek-flash',
                '--max-turns','3','--allowedTools',local_tools,
                '--disallowedTools','WebSearch,WebFetch,Agent,Task,SlashCommand',
                '--no-session-persistence','--strict-mcp-config','--mcp-config','{"mcpServers":{}}',
                '--setting-sources','','--settings','{"disableAllHooks":true,"alwaysThinkingEnabled":false}',
                '--permission-prompts','none','-p','CAPSTUDY_TOOL_ROUNDTRIP: run printf CAPSTUDY_TOOL_OK using Bash, then return CAPSTUDY_MOCK_READY.']
        cases=[('harness_permissions_only',[],None),('explicit_tools',['--tools',local_tools],None),
               ('restricted_explicit',['--restricted','--tools',local_tools],None),
               ('restricted_dontask',['--restricted','--tools',local_tools,'--permission-mode','dontAsk'],None),
               ('delayed_buffered',['--restricted','--tools',''],'CAPSTUDY_DELAYED_RESPONSE: return CAPSTUDY_MOCK_READY.'),
               ('provider_503',['--restricted','--tools',''],'CAPSTUDY_MOCK_503'),
               ('connection_failure',['--restricted','--tools',''],'CAPSTUDY_CONNECTION_FAILURE')]
        for name,flags,prompt in cases:
            if selected_cases and name not in selected_cases:continue
            client='capstudy-mock-'+name.replace('_','-')+'-'+suffix;clients.append(client)
            before=len(list(capture.glob('request-*.json')))
            invocation=common if prompt is None else [*common[:-1],prompt]
            timed_out=False
            try:
                result=command([*base,'--name',client,image,*invocation,*flags],prefix=name,check=False,timeout=30)
            except subprocess.TimeoutExpired:
                timed_out=True
                command(['docker','rm','-f',client],check=False)
                result=subprocess.CompletedProcess([],None,(output/(name+'.stdout')).read_text(),(output/(name+'.stderr')).read_text())
            requests=sorted(capture.glob('request-*.json'))[before:]
            rows=[]
            for file in requests:
                record=json.loads(file.read_text());body=record['body']
                rows.append({'file':str(file.relative_to(output)),'sha256':hashlib.sha256(file.read_bytes()).hexdigest(),
                  'path':record['path'],'header_names':record['header_names'],
                  'dummy_bearer_matches':record['dummy_bearer_matches'],
                  'body_fields':sorted(body),'model':body.get('model'),'max_tokens':body.get('max_tokens'),
                  'thinking':body.get('thinking'),'stream':body.get('stream'),
                  'tool_names':[t.get('name') for t in body.get('tools',[])],
                  'tool_fields':sorted(set(k for t in body.get('tools',[]) for k in t))})
            outcomes.append({'case':name,'returncode':result.returncode,'timed_out':timed_out,'requests':rows,
                'fixture_completion_seen':'CAPSTUDY_MOCK_READY' in result.stdout,
                'tool_result_seen':'CAPSTUDY_TOOL_OK' in result.stdout})
        return {'schema_version':1,'evidence_kind':'fixture','provider_requests':0,'executed_model':False,
                'image_id':image_id,'internal_network':True,'egress_checks':egress_results,
                'buffered_sse_delay_seconds':1.5,'cases':outcomes}
    finally:
        for name in clients:
            command(['docker','rm','-f',name],check=False)
        if created_server:
            command(['docker','logs',server],check=False,prefix='server')
            command(['docker','rm','-f',server],check=False)
        if created_network:command(['docker','network','rm',network],check=False)
        write_json_atomic(output/'commands.json',commands)


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--execute',action='store_true')
    p.add_argument('--image',default='capstudy-runtime-smoke:20261106')
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--case',action='append',dest='cases')
    args=p.parse_args(argv)
    if not args.execute:
        print(json.dumps({'executed':False,'provider_requests':0,'requires_existing_image':args.image}));return 0
    result=run(args.output,args.image,args.cases)
    write_json_atomic(args.output/'result.json',result)
    print(json.dumps(result,indent=2));return 0

if __name__=='__main__':raise SystemExit(main())
