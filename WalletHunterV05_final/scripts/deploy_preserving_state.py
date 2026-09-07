"""Code-only release: preserve all runtime, ownership, credentials and flags.

Only the isolated, tested WalletHunter staging directory is accepted. This
script has no exchange client and never changes a trading preference.
"""
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid

DEST=Path('/home/ubuntu/WalletHunterV07/WalletHunterV05_final')
SOURCE=Path(__file__).resolve().parents[1]
SERVICES=('wallethunter-web','wallethunter')


def command(*args):
    subprocess.run(args,check=True)


def read_state():
    with (DEST/'data/state.json').open(encoding='utf-8') as source:
        data=json.load(source,parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Nonfinite state')))
    if not isinstance(data.get('profiles'),dict):raise ValueError('Invalid state')
    return data


def run():
    if os.name!='posix' or SOURCE.name!='release' or not SOURCE.parent.name.startswith('wh-audit-20260906-'):
        raise RuntimeError('Unexpected stage')
    if not (SOURCE/'TESTS_PASSED').is_file() or DEST.resolve()!=DEST or not (DEST/'data/state.json').is_file():
        raise RuntimeError('Tested stage and exact production directory required')
    os.umask(0o077)
    sys.path.insert(0,str(SOURCE))
    from core.ai_review import account_guard
    manifest={}
    for directory in ('core','integrations','desktop','webapp','scripts','tests'):
        for path in (SOURCE/directory).rglob('*'):
            if path.is_symlink():raise RuntimeError('Staged symlink')
            if not path.is_file() or '__pycache__' in path.parts or path.suffix=='.pyc':continue
            if '.session' in path.name:raise RuntimeError('Session is not code')
            manifest[path.relative_to(SOURCE)]=path
    for filename in ('requirements.txt','requirements-dev.txt'):manifest[Path(filename)]=SOURCE/filename
    for relative in manifest:
        target=DEST/relative
        if target.is_symlink() or not target.resolve().is_relative_to(DEST):raise RuntimeError('Unsafe code destination')
    active={name:subprocess.run(['systemctl','is-active','--quiet',name]).returncode==0 for name in SERVICES}
    if not all(active.values()):raise RuntimeError('Inspect stopped services before deploying')
    command('sudo','systemctl','stop','wallethunter-web')
    stopped_bot=False
    backup=None
    try:
        with ExitStack() as locks:
            initial=read_state()
            # Wait-free guards ensure no financial action is in flight when
            # the existing bot stops. A busy account aborts this deployment.
            for uid in sorted(initial['profiles']):locks.enter_context(account_guard(str(DEST),f'telegram-profile:{uid}'))
            addresses=sorted({p['account']['address'].lower() for p in initial['profiles'].values() if p.get('account')})
            for address in addresses:locks.enter_context(account_guard(str(DEST),address))
            fresh=read_state()
            if {uid:p.get('account') for uid,p in initial['profiles'].items()}!={uid:p.get('account') for uid,p in fresh['profiles'].items()}:
                raise RuntimeError('Accounts changed; retry preflight')
            command('sudo','systemctl','stop','wallethunter');stopped_bot=True
            before={path.relative_to(DEST):hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in [DEST/'data/state.json',DEST/'.env',DEST/'desktop/bot.session'] if path.exists()}
            stamp=datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            backup=DEST.parent/f'pre-position-actions-{stamp}-{uuid.uuid4().hex[:8]}.tar.gz'
            with tarfile.open(backup,'x:gz') as archive:
                for path in DEST.iterdir():
                    if path.name not in ('.venv','backups','__pycache__'):archive.add(path,arcname=path.name,recursive=True)
            with tempfile.TemporaryDirectory(prefix='wh-old-code-') as temporary:
                originals=[];created=[]
                try:
                    for relative,source in manifest.items():
                        target=DEST/relative
                        if target.exists():
                            saved=Path(temporary)/relative;saved.parent.mkdir(parents=True,exist_ok=True)
                            shutil.copy2(target,saved);originals.append((target,saved))
                        else:created.append(target)
                        target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
                    shell=DEST/'scripts/backup_state.sh'
                    shell.write_bytes(shell.read_bytes().replace(b'\r\n',b'\n'));shell.chmod(0o700)
                    for relative,source in manifest.items():
                        if str(relative)=='scripts/backup_state.sh':continue
                        if hashlib.sha256(source.read_bytes()).digest()!=hashlib.sha256((DEST/relative).read_bytes()).digest():
                            raise RuntimeError('Installed checksum mismatch')
                    if any(hashlib.sha256((DEST/relative).read_bytes()).hexdigest()!=digest for relative,digest in before.items()):
                        raise RuntimeError('Runtime unexpectedly changed while stopped')
                except Exception:
                    # Restore original code only; never restore stale runtime
                    # over the user's funds/ownership/settings.
                    for target,saved in originals:shutil.copy2(saved,target)
                    for target in created:
                        if target.resolve().is_relative_to(DEST) and target.is_file():target.unlink()
                    raise
    finally:
        try:
            if stopped_bot:command('sudo','systemctl','start','wallethunter')
        finally:
            command('sudo','systemctl','start','wallethunter-web')
    print(json.dumps({'deployed':True,'files_verified':len(manifest),'runtime_preserved':True,
        'copy_flags_preserved':True,'backup':str(backup),'exchange_actions_invoked':False}))


if __name__=='__main__':run()
