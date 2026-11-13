"""Exact staged mapping + Skill/Read mock smoke; no provider credentials or calls."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid
from .cli_mock_smoke import MOCK_SERVER
from .deepseek_runtime import (make_mapping, solver_environment, agent_command,
                              clean_home_setup, URL_ENV, TOKEN_ENV, SKILL_DIRECTORY)
from .contracts import write_json_atomic

TOKEN = 'capstudy-relay-' + 'offlinefixture0' * 4
SKILL = 'capstudy-fixture'
SKILL_TEXT = '---\nname: capstudy-fixture\ndescription: CAPSTUDY_SELECTED_SKILL_DISCOVERY_ONLY\n---\nCAPSTUDY_SELECTED_SKILL_BODY: fixture-only instructions.\n'


def mock_server():
    original = "const useTool=!resultSeen&&(body.tools||[]).some(t=>t.name==='Bash');\nconst content=useTool?{type:'tool_use',id:'toolu_fixture_1',name:'Bash',input:{command:'printf CAPSTUDY_TOOL_OK'}}:{type:'text',text:'CAPSTUDY_MOCK_READY'};"
    changed = r'''const prior=body.messages.flatMap(m=>Array.isArray(m.content)?m.content:[]).filter(c=>c.type==='tool_use');
const wantsSkill=promptText.includes('CAPSTUDY_WITH_SKILL');
let content={type:'text',text:'CAPSTUDY_MOCK_READY'};
if(wantsSkill && prior.length===0)content={type:'tool_use',id:'toolu_fixture_skill',name:'Skill',input:{skill:'capstudy-fixture'}};
else if(wantsSkill && prior.length===1)content={type:'tool_use',id:'toolu_fixture_read',name:'Read',input:{file_path:'/tmp/capstudy-solver-home/.claude/skills/capstudy-fixture/SKILL.md'}};
const useTool=content.type==='tool_use';'''
    if MOCK_SERVER.count(original) != 1: raise ValueError('Mock server fixture changed')
    return MOCK_SERVER.replace(original, changed).replace('capstudy-offline-dummy-token-0000000000000000', TOKEN)


def run(output, image, skill_loading_policy=None):
    output = Path(output).resolve()
    if output.exists(): raise ValueError('New artifact directory required')
    output.mkdir(parents=True, mode=0o700)
    suffix = uuid.uuid4().hex[:10]
    network = 'capstudy-mapping-' + suffix
    server = network + '-server'
    clients = []; commands = []; outcomes = []
    made_network = made_server = False
    def command(args, timeout=45, check=True, prefix=None):
        process = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        commands.append({'command': args, 'returncode': process.returncode})
        if prefix:
            (output / (prefix + '.stdout')).write_text(process.stdout)
            (output / (prefix + '.stderr')).write_text(process.stderr)
        if check and process.returncode: raise RuntimeError('Fixture command failed: ' + args[0])
        return process
    try:
        info = command(['docker', 'image', 'inspect', image], prefix='image')
        image_id = json.loads(info.stdout)[0]['Id']
        capture = output / 'capture'; capture.mkdir()
        (output / 'server.cjs').write_text(mock_server())
        selected = output / 'selected-skills' / SKILL; selected.mkdir(parents=True)
        (selected / 'SKILL.md').write_text(SKILL_TEXT)
        empty = output / 'empty-skills'; empty.mkdir()
        command(['docker', 'network', 'create', '--internal', '--label', 'capstudy.fixture=exact-mapping', network])
        made_network = True
        net = command(['docker', 'network', 'inspect', network], prefix='network')
        command(['docker', 'run', '-d', '--name', server, '--network', network, '--read-only',
                 '--cpus', '1', '--memory', '256m', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                 '--mount', f'type=bind,src={output / "server.cjs"},dst=/mock/server.cjs,readonly',
                 '--mount', f'type=bind,src={capture},dst=/evidence', image, 'node', '/mock/server.cjs'])
        made_server = True
        for _ in range(20):
            if 'MOCK_READY' in command(['docker', 'logs', server]).stdout: break
            time.sleep(.2)
        else: raise RuntimeError('Fixture endpoint failed readiness')
        inspect = json.loads(command(['docker', 'inspect', server]).stdout)[0]
        url = 'http://' + inspect['NetworkSettings']['Networks'][network]['IPAddress'] + ':8080'
        desc = make_mapping(relay_url=url, cli_path='/usr/local/bin/claude', cli_version='2.1.296',
            allowed_tools=['Bash', 'Read', 'Write', 'Edit', 'Glob', 'Grep', 'Skill'], max_turns=5,
            network_policy_sha256=hashlib.sha256(net.stdout.encode()).hexdigest(),
            skill_loading_policy=skill_loading_policy)
        write_json_atomic(output / 'mapping.json', desc)
        env = solver_environment({URL_ENV: url, TOKEN_ENV: TOKEN}, desc)
        for case, skill_path, prompt in [
            ('selected_skill', selected.parent, 'CAPSTUDY_WITH_SKILL: invoke the capstudy-fixture skill, then Read its SKILL.md.'),
            ('no_skill', empty, 'CAPSTUDY_NO_SKILL: return CAPSTUDY_MOCK_READY.')]:
            instruction = output / (case + '.instruction'); instruction.write_text(prompt)
            client = network + '-' + case.replace('_', '-'); clients.append(client)
            base = ['docker', 'run', '--rm', '--name', client, '--network', network,
                    '--cpus', '1', '--memory', '2g', '--read-only', '--cap-drop', 'ALL',
                    '--security-opt', 'no-new-privileges', '--tmpfs', '/tmp:rw,size=128m',
                    '--tmpfs', '/workspace:rw,size=64m', '--workdir', '/workspace',
                    '--mount', f'type=bind,src={skill_path},dst=/root/.claude/skills,readonly',
                    '--mount', f'type=bind,src={instruction},dst=/fixture_instruction,readonly']
            for key, value in sorted(env.items()): base.extend(['--env', key + '=' + value])
            before = len(list(capture.glob('request-*.json')))
            shell = 'cp /fixture_instruction /tmp/instruction.txt && ' + clean_home_setup() + ' && ' + agent_command(desc, model='deepseek-flash', max_steps=5)
            result = command([*base, image, 'sh', '-c', shell], check=False, timeout=55, prefix=case)
            requests = sorted(capture.glob('request-*.json'))[before:]
            bodies = [json.loads(file.read_text())['body'] for file in requests]
            rows = [{'file': str(file.relative_to(output)), 'sha256': hashlib.sha256(file.read_bytes()).hexdigest(),
                     'message_roles': [m['role'] for m in body['messages']],
                     'tool_names': [t.get('name') for t in body.get('tools', [])],
                     'max_tokens': body.get('max_tokens'), 'model': body.get('model')} for file, body in zip(requests, bodies)]
            serialized = json.dumps(bodies)
            events = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{')]
            init = next((event for event in events if event.get('type') == 'system' and event.get('subtype') == 'init'), {})
            outcomes.append({'case': case, 'returncode': result.returncode, 'requests': rows,
                'init_skills': init.get('skills'), 'init_plugins': init.get('plugins'),
                'fixture_completion_seen': 'CAPSTUDY_MOCK_READY' in result.stdout,
                'skill_discovery_seen': bool(bodies) and 'CAPSTUDY_SELECTED_SKILL_DISCOVERY_ONLY' in json.dumps(bodies[0]),
                'skill_body_seen': 'CAPSTUDY_SELECTED_SKILL_BODY' in serialized,
                'skill_path_seen': SKILL_DIRECTORY + '/' + SKILL + '/SKILL.md' in serialized,
                'tool_error_seen': '"is_error":true' in result.stdout.replace(' ', '')})
        return {'schema_version': 1, 'evidence_kind': 'fixture', 'executed_model': False,
                'provider_requests': 0, 'image_id': image_id, 'internal_network': True,
                'mapping_sha256': hashlib.sha256(json.dumps(desc, sort_keys=True).encode()).hexdigest(), 'cases': outcomes}
    finally:
        for client in clients: command(['docker', 'rm', '-f', client], check=False)
        if made_server: command(['docker', 'rm', '-f', server], check=False)
        if made_network: command(['docker', 'network', 'rm', network], check=False)
        write_json_atomic(output / 'commands.json', commands)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--image', default='capstudy-runtime-smoke:20261106')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--skill-loading-policy', choices=['invoke-installed-before-task-v1'])
    args = parser.parse_args(argv)
    if not args.execute:
        print(json.dumps({'executed': False, 'provider_requests': 0})); return 0
    result = run(args.output, args.image, args.skill_loading_policy)
    write_json_atomic(args.output / 'result.json', result)
    print(json.dumps(result, indent=2)); return 0

if __name__ == '__main__': raise SystemExit(main())
