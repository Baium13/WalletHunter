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
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def run():
    os.umask(0o077)
    target=ROOT/"backups"
    if target.is_symlink(): raise ValueError("Backup directory must not be a symlink")
    target.mkdir(exist_ok=True)
    if os.name == "posix": target.chmod(0o700)
    stamp=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output=target/f"runtime-{stamp}-{uuid.uuid4().hex}.tar.gz"
    temporary=output.with_suffix(".partial")
    databases=list((ROOT/"data").glob("*.sqlite3"))
    session=ROOT/"desktop"/"bot.session"
    if session.exists(): databases.append(session)
    for source in [*databases, ROOT/".env", ROOT/"data"/"state.json"]:
        if source.is_symlink() or not source.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError("Backup input must be a local regular artifact")
    try:
        with tempfile.TemporaryDirectory(prefix="wh-backup-") as folder:
            snapshots=[]
            for index,source in enumerate(databases):
                destination=Path(folder)/f"database-{index}.sqlite3"
                with closing(sqlite3.connect(f"file:{source.as_posix()}?mode=ro",uri=True)) as db:
                    with closing(sqlite3.connect(destination)) as copy:
                        db.backup(copy)
                        if copy.execute("PRAGMA integrity_check").fetchone()[0]!="ok":
                            raise RuntimeError("Backup SQLite integrity failed")
                snapshots.append((destination,str(source.relative_to(ROOT))))
            with tarfile.open(temporary,"w:gz") as archive:
                # state.json uses atomic replacement, so this read cannot
                # observe half a JSON document even while the watcher saves.
                state=(ROOT/"data"/"state.json").read_bytes()
                document=json.loads(state)
                if not isinstance(document,dict) or not isinstance(document.get("profiles"),dict) or any(not isinstance(p,dict) for p in document["profiles"].values()):
                    raise ValueError("Invalid state document in backup")
                info=tarfile.TarInfo("data/state.json");info.size=len(state);info.mode=0o600
                archive.addfile(info,io.BytesIO(state))
                def owner_only(member):
                    member.mode=0o600
                    member.uid=member.gid=0
                    member.uname=member.gname=""
                    return member
                for path in (ROOT/".env",):
                    if path.exists(): archive.add(path,arcname=path.name,recursive=False,filter=owner_only)
                for path,name in snapshots: archive.add(path,arcname=name,recursive=False,filter=owner_only)
        os.replace(temporary,output)
        if os.name == "posix": output.chmod(0o600)
        print(json.dumps({"backup":output.name,"databases":len(databases),"integrity":"ok","old_backups_deleted":0}))
    finally:
        if temporary.exists(): temporary.unlink()


if __name__=="__main__": run()
