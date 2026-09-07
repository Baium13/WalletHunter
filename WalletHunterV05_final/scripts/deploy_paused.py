"""One-off audited deployment. Leaves all copying PAUSED; sends no orders.

Run only from an isolated wh-audit-20260906-* / release directory on this host,
after the staged tests pass. Exact project destination is intentionally fixed.
"""
import hashlib
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import uuid
from datetime import datetime, timezone

DEST=Path('/home/ubuntu/WalletHunterV07/WalletHunterV05_final')
SOURCE=Path(__file__).resolve().parents[1]


def run(preserve_ownership=False):
    if os.name!='posix' or SOURCE.name!='release' or not SOURCE.parent.name.startswith('wh-audit-20260906-'):
        raise RuntimeError('Unexpected staging environment')
    if DEST.resolve()!=DEST or not (DEST/'data'/'state.json').is_file():
        raise RuntimeError('Unexpected production directory')
    if not (SOURCE/'TESTS_PASSED').is_file():
        raise RuntimeError('Staging tests must pass before deployment')
    os.umask(0o077)
    sys.path.insert(0,str(SOURCE))
    from dotenv import load_dotenv
    load_dotenv(DEST/'.env',override=True)
    from core.storage import Storage, _unique_object, _validate_state, _validate_unique_accounts
    from core.legacy_metrics import normalise_legacy_metrics
    from core.settings import load
    state=Storage(str(DEST),load().master_key)
    with open(DEST/'data'/'state.json',encoding='utf-8') as stream:
        candidate,legacy_changes=normalise_legacy_metrics(json.load(stream,object_pairs_hook=_unique_object))
    _validate_state(candidate);_validate_unique_accounts(candidate)
    json.dumps(candidate,allow_nan=False)  # Validate everything BEFORE stop.
    if preserve_ownership and any(p.get('copy_enabled') for p in candidate['profiles'].values()):
        raise RuntimeError('Copying was enabled since preflight; deployment aborted without changing it')
    files=[]
    for folder in ('core','integrations','desktop','webapp','scripts','tests'):
        for path in (SOURCE/folder).rglob('*'):
            if path.is_symlink(): raise RuntimeError('Unexpected staged symlink')
            if not path.is_file() or '__pycache__' in path.parts or path.suffix=='.pyc': continue
            if '.session' in path.name: raise RuntimeError('Session must not be deployed')
            files.append(path)
    files += [SOURCE/'requirements.txt',SOURCE/'requirements-dev.txt']
    manifest={}
    for path in files:
        relative=path.relative_to(SOURCE)
        target=DEST/relative
        if target.is_symlink() or not target.resolve().is_relative_to(DEST):
            raise RuntimeError('Deployment target leaves project')
        manifest[str(relative)]=hashlib.sha256(path.read_bytes()).hexdigest()
    subprocess.run(['sudo','systemctl','stop','wallethunter','wallethunter-web'],check=True)
    if preserve_ownership:
        # A user may have enabled copying between preflight and service stop.
        # Do not silently overwrite that newer decision during a UI release.
        try:
            current = state.load()
            if any(p.get('copy_enabled') for p in current['profiles'].values()):
                raise RuntimeError('Copying changed during preflight; existing services restored')
        except Exception:
            subprocess.run(['sudo','systemctl','start','wallethunter','wallethunter-web'],check=True)
            raise
    stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup=DEST.parent/f'pre-audit-{stamp}-{uuid.uuid4().hex[:8]}.tar.gz'
    with tarfile.open(backup,'x:gz') as archive:
        for path in DEST.iterdir():
            if path.name in ('.venv','backups','__pycache__'): continue
            archive.add(path,arcname=path.name,recursive=True)
    # Pause persists even if any later copy or recovery validation fails.
    with state._file_lock():
        with open(DEST/'data'/'state.json',encoding='utf-8') as stream:
            data,legacy_changes=normalise_legacy_metrics(json.load(stream,object_pairs_hook=_unique_object))
        for profile in data['profiles'].values(): profile['copy_enabled']=False
        state._write(data)
    for path in files:
        target=DEST/path.relative_to(SOURCE)
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(path,target)
    shell=DEST/'scripts'/'backup_state.sh'
    shell.write_bytes(shell.read_bytes().replace(b'\r\n',b'\n'))
    shell.chmod(0o700)
    for relative,expected in manifest.items():
        if relative=='scripts/backup_state.sh': continue  # Explicit LF formatting.
        if hashlib.sha256((DEST/relative).read_bytes()).hexdigest()!=expected:
            raise RuntimeError('Installed file checksum mismatch')
    python=str(DEST/'.venv'/'bin'/'python')
    recovery=None if preserve_ownership else subprocess.run([python,str(DEST/'scripts'/'recover_ownership.py'),'--apply'],cwd=DEST,
                            capture_output=True,text=True)
    if recovery is not None and recovery.returncode:
        print(json.dumps({'deployed':True,'copy_enabled':False,'recovery':'ABORTED','backup':str(backup),
                          'services':'stopped','orders_sent':0}))
        raise RuntimeError('Recovery validation failed; inspect read-only before restarting')
    subprocess.run(['sudo','systemctl','start','wallethunter','wallethunter-web'],check=True)
    print(json.dumps({'deployed':True,'copy_enabled':False,'recovery':'preserved existing execution journal' if preserve_ownership else 'user_confirmed BTC/ETH source4473e0',
                      'backup':str(backup),'files_verified':len(manifest),'legacy_metrics_migrated':len(legacy_changes),'orders_sent':0}))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--preserve-ownership',action='store_true')
    run(parser.parse_args().preserve_ownership)
