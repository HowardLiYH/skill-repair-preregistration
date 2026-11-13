"""Prepare an explicitly authorized isolated CPU LLMLingua pilot on Linux only.

Downloads official pinned source/checkpoint, installs CPU-only wheels, records
versions and archive hashes. Does not run inference, read keys or call model APIs.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import urllib.request

CODE_REVISION='5a4c78ae18ab17a98cf997e8259354e546081d64'
MODEL='microsoft/llmlingua-2-bert-base-multilingual-cased-meetingbank'
MODEL_REVISION='5f0c82792b7ea14c6484e015b6a072009496b7f2'
PINS=['transformers==4.56.2','tokenizers==0.22.2','accelerate==1.10.1',
      'tiktoken==0.11.0','nltk==3.9.2','numpy==1.26.4','safetensors==0.6.2',
      'huggingface-hub==0.34.4']


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path,data):Path(path).write_text(json.dumps(data,indent=2,sort_keys=True)+'\n')


def download(url,path,maximum_bytes,expected_sha256=None,expected_git_blob=None):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.partial')
    size=0
    with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'capstudy-source-only-preflight'}),timeout=60) as response,temporary.open('wb') as output:
        while True:
            block=response.read(1024*1024)
            if not block:break
            size+=len(block)
            if size>maximum_bytes:raise ValueError('Download exceeds declared size ceiling')
            output.write(block)
    actual=sha(temporary)
    if expected_sha256 and actual!=expected_sha256:raise ValueError('Official LFS SHA256 mismatch')
    if expected_git_blob:
        blob=hashlib.sha1(('blob '+str(size)+'\0').encode()+temporary.read_bytes()).hexdigest()
        if blob!=expected_git_blob:raise ValueError('Official source Git blob mismatch')
    temporary.rename(path)
    return {'url':url,'bytes':size,'sha256':actual,'official_lfs_sha256':expected_sha256,'official_git_blob':expected_git_blob}


def setup(root, resume=False):
    root=Path(root).resolve()
    if platform.system()!='Linux' or platform.machine() not in ('x86_64','AMD64'):
        raise ValueError('This explicitly reviewed pilot environment is remote Linux amd64 only')
    if root.exists() and (not resume or (root/'manifest.json').exists()):
        raise ValueError('New environment required, or explicit resume of an incomplete preparation')
    if resume and not (root/'dependencies-install.json').is_file():
        raise ValueError('Resume requires the saved completed dependency installation')
    root.mkdir(parents=True,mode=0o700,exist_ok=resume)
    home=root/'home';home.mkdir(exist_ok=resume)
    env={'HOME':str(home),'PATH':'/usr/local/bin:/usr/bin:/bin','LANG':'C.UTF-8',
         'PIP_CONFIG_FILE':'/dev/null','PIP_DISABLE_PIP_VERSION_CHECK':'1','CUDA_VISIBLE_DEVICES':'',
         'HF_HUB_DISABLE_TELEMETRY':'1','HF_HUB_DISABLE_XET':'1','DO_NOT_TRACK':'1',
         'TIKTOKEN_CACHE_DIR':str(root/'tiktoken-cache'),'HF_HOME':str(root/'hf-home')}
    commands=json.loads((root/'commands.json').read_text()) if resume else []
    def command(args,name,timeout=600):
        with (root/(name+'.stdout')).open('w') as stdout,(root/(name+'.stderr')).open('w') as stderr:
            result=subprocess.run(args,env=env,stdout=stdout,stderr=stderr,timeout=timeout)
        commands.append({'args':args,'returncode':result.returncode});save(root/'commands.json',commands)
        if result.returncode:raise RuntimeError('Preparation failed: '+name+'; inspect retained logs')
    python=str(root/'venv/bin/python')
    if not resume:
        command([sys.executable,'-m','venv',str(root/'venv')],'venv')
        command([python,'-m','pip','install','--only-binary=:all:','--report',str(root/'bootstrap-install.json'),
            'pip==25.2','setuptools==80.9.0','wheel==0.45.1'],'bootstrap')
        command([python,'-m','pip','install','--only-binary=:all:','--index-url','https://download.pytorch.org/whl/cpu',
            '--report',str(root/'torch-install.json'),'torch==2.6.0+cpu'],'torch-cpu')
        command([python,'-m','pip','install','--only-binary=:all:','--report',str(root/'dependencies-install.json'),*PINS],'dependencies')
    download('https://api.github.com/repos/microsoft/LLMLingua/git/commits/'+CODE_REVISION,root/'code-commit.json',1024**2)
    commit=json.loads((root/'code-commit.json').read_text())
    tree_url='https://api.github.com/repos/microsoft/LLMLingua/git/trees/'+commit['tree']['sha']+'?recursive=1'
    download(tree_url,root/'code-resolved-tree.json',10*1024**2)
    tree=json.loads((root/'code-resolved-tree.json').read_text())
    if commit.get('sha')!=CODE_REVISION or tree.get('sha')!=commit.get('tree',{}).get('sha') or tree.get('truncated'):
        raise ValueError('Pinned source tree not fully resolved')
    files={}
    for row in tree['tree']:
        name=row['path']
        if row['type']=='blob' and (name in ('setup.py','README.md','LICENSE') or name.startswith('llmlingua/') and name.endswith('.py')):
            files[name]=download('https://raw.githubusercontent.com/microsoft/LLMLingua/'+CODE_REVISION+'/'+name,
                root/'source'/name,10*1024**2,expected_git_blob=row['sha'])
    command([python,'-m','pip','install','--no-deps','--no-build-isolation','--report',str(root/'llmlingua-install.json'),str(root/'source')],'llmlingua-source')
    metadata_url='https://huggingface.co/api/models/'+MODEL+'/revision/'+MODEL_REVISION+'?blobs=true'
    download(metadata_url,root/'checkpoint-metadata.json',10*1024**2)
    meta=json.loads((root/'checkpoint-metadata.json').read_text())
    if meta.get('sha')!=MODEL_REVISION:raise ValueError('Checkpoint revision not resolved exactly')
    wanted={'config.json','tokenizer.json','tokenizer_config.json','special_tokens_map.json','vocab.txt','model.safetensors','README.md'}
    checkpoint=root/MODEL.split('/')[-1];model_files={}
    for row in meta['siblings']:
        name=row['rfilename']
        if name not in wanted:continue
        lfs=row.get('lfs',{})
        model_files[name]=download('https://huggingface.co/'+MODEL+'/resolve/'+MODEL_REVISION+'/'+name,
            checkpoint/name,800*1024**2 if name.endswith('.safetensors') else 20*1024**2,
            expected_sha256=lfs.get('sha256'),expected_git_blob=None if lfs else row.get('blobId'))
    if not {'config.json','tokenizer_config.json','vocab.txt','model.safetensors'}.issubset(model_files):
        raise ValueError('Required safetensors/BERT artifacts unavailable')
    probe="""import importlib.metadata as m,json,torch,tiktoken
packages={d.metadata['Name']:d.version for d in m.distributions()}
assert torch.version.cuda is None and '+cpu' in torch.__version__
assert not any(k.lower().startswith(('nvidia-','cuda-')) for k in packages)
tiktoken.encoding_for_model('gpt-3.5-turbo').encode('offline cache readiness')
print(json.dumps({'packages':dict(sorted(packages.items())),'torch_version':torch.__version__,'cuda_compiled_version':torch.version.cuda,'torch_cuda_available':torch.cuda.is_available()},sort_keys=True,indent=2))
"""
    command([python,'-c',probe],'runtime-probe')
    command([python,'-m','pip','check'],'pip-check')
    command([python,'-m','pip','freeze','--all'],'frozen-requirements')
    manifest={'schema_version':1,'purpose':'CPU-only local compressor pilot; no formal candidate selection',
        'executed_inference':False,'provider_calls':0,'code_revision':CODE_REVISION,'source_files':files,
        'checkpoint':MODEL,'checkpoint_revision':MODEL_REVISION,'checkpoint_files':model_files,
        'checkpoint_path':str(checkpoint),'venv_python':python,'runtime':json.loads((root/'runtime-probe.stdout').read_text()),
        'tiktoken_cache_files':{str(p.relative_to(root)):sha(p) for p in sorted((root/'tiktoken-cache').rglob('*')) if p.is_file()},
        'installation_reports':{name:sha(root/name) for name in ['bootstrap-install.json','torch-install.json','dependencies-install.json','llmlingua-install.json']},
        'environment_policy':env,'direct_dependency_pins':['torch==2.6.0+cpu',*PINS]}
    save(root/'manifest.json',manifest)
    print(json.dumps({'prepared':True,'inference_executed':False,'manifest':str(root/'manifest.json'),'manifest_sha256':sha(root/'manifest.json')},indent=2))
    return manifest


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--resume-preparation',action='store_true')
    parser.add_argument('--execute',action='store_true');args=parser.parse_args(argv)
    if args.execute:setup(args.output,args.resume_preparation)
    else:print(json.dumps({'executed':False,'checkpoint':MODEL,'revision':MODEL_REVISION,'cpu_pins':PINS}))

if __name__=='__main__':main()
