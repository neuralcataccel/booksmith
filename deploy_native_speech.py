"""Prepare/build/start only Booksmith's authenticated loopback CUDA renderer.

Does not restart the family application or change its default renderer.
"""
import argparse
import hashlib
import json
from pathlib import Path
import secrets
import shutil
import subprocess

from settings import CONFIG, MODELS, ROOT, RUNTIME
from speech_engines import NATIVE_CONFIG_SHA, NATIVE_MODEL_SHA

CONTAINER='booksmith-native-speech'
IMAGE='booksmith-native-speech:1'


def run(args):
    result=subprocess.run(args,capture_output=True,text=True,encoding='utf-8',errors='replace',
                          creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    if result.stdout:print(result.stdout[-6000:],flush=True)
    if result.stderr:print(result.stderr[-6000:],flush=True)
    result.check_returncode()
    return result


def verify(path,expected):
    checksum=hashlib.sha256()
    with path.open('rb') as stream:
        for part in iter(lambda:stream.read(1024*1024),b''):checksum.update(part)
    if checksum.hexdigest()!=expected:raise ValueError('Pinned model checksum mismatch: '+path.name)


def write(path,data):
    temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data,indent=2),encoding='utf-8');temporary.replace(path)


def prepare(benchmark):
    root=RUNTIME/'native-speech';models=root/'models';private=RUNTIME/'data'/'native-speech'
    models.mkdir(parents=True,exist_ok=True);private.mkdir(parents=True,exist_ok=True)
    identity=subprocess.check_output(['whoami'],text=True).strip()
    run(['icacls',str(private),'/inheritance:r','/grant:r',identity+':(OI)(CI)F','SYSTEM:(OI)(CI)F','Administrators:(OI)(CI)F'])
    source=json.loads((benchmark/'native-files.json').read_text())
    for name,expected in [('config.json',NATIVE_CONFIG_SHA),('kokoro-v1_0.pth',NATIVE_MODEL_SHA)]:
        original=benchmark/source[name]['path'];verify(original,expected)
        target=models/name
        if not target.exists():shutil.copyfile(original,target)
        verify(target,expected)
    original=MODELS/'voices-v1.0.bin';verify(original,CONFIG['voices_sha256'])
    target=models/original.name
    if not target.exists():shutil.copyfile(original,target)
    verify(target,CONFIG['voices_sha256'])
    client=private/'client.json'
    token=json.loads(client.read_text())['token'] if client.exists() else secrets.token_urlsafe(48)
    write(client,{'url':'http://127.0.0.1:18790','token':token})
    write(private/'server.json',{'token':token,'frontend_model_sha256':CONFIG['model_sha256'],
        'voices_sha256':CONFIG['voices_sha256'],'model_config':'/models/config.json',
        'model_path':'/models/kokoro-v1_0.pth','voices_path':'/models/voices-v1.0.bin','idle_seconds':120})
    print('Private renderer configuration and pinned models prepared.')


def start(replace=False):
    # Never recreate an unexpected existing container or stop any other workload.
    existing=subprocess.run(['docker','inspect',CONTAINER],capture_output=True,text=True)
    if existing.returncode==0:
        row=json.loads(existing.stdout)[0]
        if row['Config'].get('Labels',{}).get('org.booksmith.role')!='native-speech':
            raise ValueError('Container name belongs to another workload.')
        if replace:
            # Only this labeled, stateless container is replaced. All models and
            # credentials are read-only bind mounts and remain on the host.
            write(RUNTIME/'native-speech'/'previous-container.json',
                  {'image':row['Image'],'container':row['Id']})
            run(['docker','stop','--timeout','10',CONTAINER])
            run(['docker','rm',CONTAINER])
        else:
            current=subprocess.check_output(['docker','image','inspect',IMAGE,'--format','{{.Id}}'],text=True).strip()
            if row['Image']!=current:raise ValueError('The image changed; use replace to deploy it explicitly.')
            run(['docker','start',CONTAINER]);return
    private=RUNTIME/'data'/'native-speech';models=RUNTIME/'native-speech'/'models'
    probe="import json,urllib.request;d=json.load(open('/private/server.json'));urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8790/health',headers={'Authorization':'Bearer '+d['token']}),timeout=5).read()"
    run(['docker','run','-d','--name',CONTAINER,'--label','org.booksmith.role=native-speech',
         '--restart','unless-stopped','--gpus','all','--cpus','4','--memory','8g','--pids-limit','256',
         '--read-only','--tmpfs','/tmp:rw,size=256m,nosuid','--cap-drop','ALL',
         '--security-opt','no-new-privileges','--user','1000:1000',
         '-p','127.0.0.1:18790:8790',
         '--mount',f'type=bind,source={models},target=/models,readonly',
         '--mount',f'type=bind,source={private / "server.json"},target=/private/server.json,readonly',
         '--health-cmd','python -c "'+probe+'"','--health-interval','30s','--health-timeout','8s',
         '--health-start-period','40s','--health-retries','3',IMAGE])


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['prepare','build','start','replace'])
    parser.add_argument('--benchmark-root',type=Path)
    args=parser.parse_args()
    if args.command=='prepare':
        if not args.benchmark_root:parser.error('--benchmark-root is required for prepare')
        prepare(args.benchmark_root)
    elif args.command=='build':run(['docker','build','--pull=false','-f',str(ROOT/'Dockerfile.native'),'-t',IMAGE,str(ROOT)])
    else:start(replace=args.command=='replace')
