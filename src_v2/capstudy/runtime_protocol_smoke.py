"""Model-free paired Skill/Read/Bash protocol check on an immutable family image."""
import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from . import cli_mapping_mock as fixture
from .contracts import write_json_atomic


def server_with_bash():
    script=fixture.mock_server()
    needle="const useTool=content.type==='tool_use';"
    added="else if(wantsSkill && prior.length===2)content={type:'tool_use',id:'toolu_fixture_bash',name:'Bash',input:{command:'printf CAPSTUDY_FAMILY_BASH_OK'}};\n"
    if script.count(needle)!=1:raise ValueError('Pinned mock source changed')
    return script.replace(needle,added+needle)


def execute(output,image,family):
    output=Path(output)
    image=json.loads(subprocess.check_output(['docker','image','inspect',image],timeout=30))[0]['Id']
    server=server_with_bash()
    original=fixture.mock_server
    try:
        fixture.mock_server=lambda:server
        result=fixture.run(output,image,'invoke-installed-before-task-v1')
    finally:fixture.mock_server=original
    cases={r['case']:r for r in result['cases']};selected=cases['selected_skill'];empty=cases['no_skill']
    selected_trace=(output/'selected_skill.stdout').read_text()
    events=[json.loads(line) for line in selected_trace.splitlines() if line.startswith('{')]
    bash_seen=any(c.get('type')=='tool_result' and c.get('tool_use_id')=='toolu_fixture_bash' and c.get('is_error',False) is False and c.get('content')=='CAPSTUDY_FAMILY_BASH_OK' for e in events if e.get('type')=='user' for c in e.get('message',{}).get('content',[]) if isinstance(c,dict))
    expected=['Bash','Read','Write','Edit','Glob','Grep','Skill']
    tools_ok=all(set(row['tool_names'])==set(expected) and row['max_tokens']==16384 and row['model']=='deepseek-flash' for c in cases.values() for row in c['requests'])
    result.update(family=family,bash_tool_result_seen=bash_seen,
                  controller_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  fixture_sha256=hashlib.sha256(Path(fixture.__file__).read_bytes()).hexdigest())
    result['passed']=all(c['returncode']==0 and c['fixture_completion_seen'] and not c['tool_error_seen'] for c in cases.values()) and selected['skill_discovery_seen'] and selected['skill_body_seen'] and selected['skill_path_seen'] and selected['init_skills']==['capstudy-fixture'] and selected['init_plugins']==[] and empty['init_skills']==[] and empty['init_plugins']==[] and not any(empty[k] for k in ('skill_discovery_seen','skill_body_seen','skill_path_seen')) and result['bash_tool_result_seen'] and tools_ok
    write_json_atomic(output/'result.json',result)
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--image',required=True);p.add_argument('--family',required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--execute',action='store_true');a=p.parse_args()
    if a.execute:
        r=execute(a.output,a.image,a.family);print(json.dumps({'family':a.family,'passed':r['passed'],'image_id':r['image_id']}))
    else:print(json.dumps({'executed':False,'provider_requests':0}))
