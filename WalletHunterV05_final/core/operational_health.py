"""Read-only resource observations, independent of financial policy."""
from pathlib import Path
import shutil
from functools import lru_cache


def storage_health(root,now):
    # Storage is operational telemetry, not execution/account evidence. A fixed
    # one-minute sample avoids an event for every changing free-disk byte.
    return dict(_sample(str(Path(root)),now//60000))


@lru_cache(maxsize=64)
def _sample(root,minute):
    now=minute*60000
    root=Path(root)
    try:
        disk=shutil.disk_usage(root)
        files=[p for p in (root/'data').rglob('*') if p.is_file() and not p.is_symlink() and (p.suffix in {'.sqlite','.sqlite3'} or p.name.endswith(('-wal','-shm')))]
        size=sum(p.stat().st_size for p in files)
        # 5GiB warning / 1GiB critical leaves space for consistent backups and
        # WAL checkpoints; these are operational, not strategy thresholds.
        status='UNHEALTHY' if disk.free<1024**3 else 'DEGRADED' if disk.free<5*1024**3 or size>4*1024**3 else 'READY'
        return dict(status=status,checked_ms=now,free_bytes=disk.free,database_bytes=size,
            warning_free_bytes=5*1024**3,critical_free_bytes=1024**3,database_warning_bytes=4*1024**3)
    except OSError:return dict(status='UNKNOWN',checked_ms=now,reason='STORAGE_METRICS_UNAVAILABLE')
