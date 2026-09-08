"""Consistent per-database backup of state, ownership, AI history and keys.

SQLite backup API supports a running bot. Each database is a consistent snapshot;
the execution journal resolves a possible JSON/SQLite boundary on recovery.
Archives contain plaintext secrets (tar.gz is NOT encryption). POSIX permissions
are owner-only; Windows deployments require an operator-managed private ACL.
Retention is KEEP ALL: this script never deletes previous backups.
"""
import io
import json
import os
import sqlite3
import tarfile
import tempfile
import uuid
import hashlib
import shutil
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path,PurePosixPath

ROOT=Path(__file__).resolve().parents[1]


def _sha256(path):
    with Path(path).open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()


def _archive_name(value):
    path=PurePosixPath(value)
    if not value or path.is_absolute() or '..' in path.parts or '\\' in value or ':' in value:
        raise ValueError('Unsafe manifest archive name')
    return path.as_posix()


def runtime_manifest(path=None):
    """Explicit configured paths support stores outside ROOT and any extension.

    Legacy inventory is supplementary, never silently omits registered stores.
    Operators must register external/custom stores and external key material.
    """
    configured=Path(path) if path else ROOT/'runtime-state-manifest.json'
    explicit=configured.exists()
    if path and not explicit:raise ValueError('Required runtime manifest missing')
    document=json.loads(configured.read_text()) if explicit else {'version':1,'artifacts':[]}
    if document.get('version')!=1 or not isinstance(document.get('artifacts'),list):raise ValueError('Invalid runtime manifest')
    entries=document['artifacts'];known={}
    for folder in ('data','research','autonomous','paper'):
        base=ROOT/folder
        if base.exists():
            for p in base.rglob('*'):
                if p.is_symlink():raise ValueError('Runtime symlink not supported')
                if not p.is_file() or p.name.endswith(('-wal','-shm','-journal')):continue
                with p.open('rb') as stream:header=stream.read(16)
                if header==b'SQLite format 3\x00' or p.suffix in {'.db','.sqlite','.sqlite3'}:
                    known[p.resolve()]=dict(path=p.relative_to(ROOT).as_posix(),archive=p.relative_to(ROOT).as_posix(),kind='sqlite',required=True)
    for name,kind,required in [('data/state.json','state',True),('.env','file',False),('desktop/bot.session','sqlite',False)]:
        p=ROOT/name
        if p.exists() or required:known[p.resolve()]=dict(path=name,archive=name,kind=kind,required=required)
    if not explicit:entries=list(known.values())
    resolved=[];names=set();paths=set()
    for item in entries:
        source=Path(item['path']);source=source if source.is_absolute() else ROOT/source
        if source.is_symlink() or any(parent.is_symlink() for parent in source.parents):raise ValueError('Runtime symlink not supported')
        source=source.resolve();name=_archive_name(item['archive'])
        if name=='backup-manifest.json' or name in names or source in paths:raise ValueError('Duplicate runtime artifact')
        if item['kind'] not in {'sqlite','state','file'}:raise ValueError('Unknown runtime artifact kind')
        names.add(name);paths.add(source)
        if not source.exists():
            if item.get('required',True):raise ValueError('Required runtime artifact missing')
            continue
        if not source.is_file():raise ValueError('Runtime artifact is not a file')
        resolved.append(dict(source=source,archive=name,kind=item['kind']))
    if explicit and set(known)-paths:raise ValueError('Runtime manifest omits detected required state')
    return resolved,document,explicit


def run(manifest_path=None):
    os.umask(0o077)
    target=ROOT/"backups"
    if target.is_symlink(): raise ValueError("Backup directory must not be a symlink")
    target.mkdir(exist_ok=True)
    if os.name == "posix": target.chmod(0o700)
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output=target/f"runtime-{stamp}-{uuid.uuid4().hex}.tar.gz"
    temporary=output.with_suffix(".partial")
    artifacts,configuration,explicit=runtime_manifest(manifest_path)
    budget=configuration.get('capacity_warning_bytes',5*1024**3)
    if type(budget) is not int or budget<=0:raise ValueError('Invalid backup capacity warning threshold')
    databases=[a for a in artifacts if a['kind']=='sqlite']
    input_bytes=sum(a['source'].stat().st_size for a in artifacts)
    free=shutil.disk_usage(target).free
    if free<input_bytes*2:raise RuntimeError('Insufficient backup capacity')
    try:
        with tempfile.TemporaryDirectory(prefix="wh-backup-") as folder:
            snapshots=[]
            for index,artifact in enumerate(databases):
                source=artifact['source']
                destination=Path(folder)/f"database-{index}.sqlite3"
                with closing(sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True)) as db:
                    with closing(sqlite3.connect(destination)) as copy:
                        db.backup(copy)
                        if copy.execute("PRAGMA integrity_check").fetchone()[0]!="ok":
                            raise RuntimeError("Backup SQLite integrity failed")
                snapshots.append((destination,artifact['archive'],'sqlite'))
            with tarfile.open(temporary,"w:gz") as archive:
                # state.json uses atomic replacement, so this read cannot
                # observe half a JSON document even while the watcher saves.
                manifest=[]
                def owner_only(member):
                    member.mode=0o600
                    member.uid=member.gid=0
                    member.uname=member.gname=""
                    return member
                for artifact in artifacts:
                    if artifact['kind']=='sqlite':continue
                    path=artifact['source'];data=path.read_bytes()
                    if artifact['kind']=='state':
                        document=json.loads(data)
                        if not isinstance(document,dict) or not isinstance(document.get('profiles'),dict) or any(not isinstance(p,dict) for p in document['profiles'].values()):raise ValueError('Invalid state document in backup')
                    snapshot=Path(folder)/('file-'+str(len(snapshots)))
                    snapshot.write_bytes(data);snapshots.append((snapshot,artifact['archive'],artifact['kind']))
                for path,name,kind in snapshots:
                    archive.add(path,arcname=name,recursive=False,filter=owner_only)
                    manifest.append(dict(archive=name,kind=kind,size=path.stat().st_size,sha256=_sha256(path)))
                body=json.dumps({'version':1,'artifacts':manifest,'encryption':'NONE','retention':'KEEP_ALL',
                    'explicit_runtime_manifest':explicit,'consistency':'PER_DATABASE_SNAPSHOT_RECOVER_UNRESOLVED_BEFORE_EXECUTION'},sort_keys=True).encode()
                info=tarfile.TarInfo('backup-manifest.json');info.size=len(body);info.mode=0o600
                archive.addfile(info,io.BytesIO(body))
        os.replace(temporary,output)
        if os.name == "posix": output.chmod(0o600)
        total=sum(p.stat().st_size for p in target.glob('*.tar.gz'))
        print(json.dumps({"backup":output.name,"databases":len(databases),"integrity":"ok","old_backups_deleted":0,
            'retention':'KEEP_ALL','encrypted':False,'archive_bytes':output.stat().st_size,'retained_bytes':total,'free_bytes':free,
            'warnings':(['EXPLICIT_MANIFEST_REQUIRED_FOR_EXTERNAL_STORES'] if not explicit else [])+
                (['BACKUP_CAPACITY_WARNING'] if total>budget else [])+['PERMISSIONS_ARE_NOT_ENCRYPTION']}))
    finally:
        if temporary.exists(): temporary.unlink()


def restore(archive_path,destination,*,max_bytes=10*1024**3):
    """Verify and restore into an EMPTY isolated directory. Never starts workers."""
    destination=Path(destination)
    if destination.is_symlink() or any(p.is_symlink() for p in destination.parents):raise ValueError('Unsafe restore path')
    if destination.exists() and any(destination.iterdir()):raise ValueError('Restore destination must be empty')
    destination.mkdir(parents=True,exist_ok=True,mode=0o700)
    with tarfile.open(archive_path,'r:gz') as archive:
        entries=archive.getmembers()
        if type(max_bytes) is not int or max_bytes<=0 or sum(m.size for m in entries)>max_bytes:raise ValueError('Restore capacity bound exceeded')
        if len({m.name for m in entries})!=len(entries) or any(not m.isfile() for m in entries):raise ValueError('Unsafe archive members')
        for member in entries:_archive_name(member.name)
        manifest=json.load(archive.extractfile('backup-manifest.json'))
        if manifest['version']!=1:raise ValueError('Unsupported backup schema')
        if {m.name for m in entries}!={'backup-manifest.json',*(a['archive'] for a in manifest['artifacts'])}:raise ValueError('Manifest mismatch')
        for artifact in manifest['artifacts']:
            name=_archive_name(artifact['archive'])
            if archive.getmember(name).size!=artifact['size']:raise ValueError('Backup size mismatch')
            path=destination/name;path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
            with archive.extractfile(name) as source,path.open('xb') as target:shutil.copyfileobj(source,target,1024*1024)
            path.chmod(0o600)
            if _sha256(path)!=artifact['sha256']:raise ValueError('Backup hash mismatch')
            if artifact['kind']=='sqlite':
                with closing(sqlite3.connect(path)) as db:
                    if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('Restored SQLite integrity failure')
    return manifest


if __name__=="__main__":
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--manifest')
    args=parser.parse_args();run(args.manifest)
