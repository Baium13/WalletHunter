"""Defense-in-depth guards for copied baseline tests, not a hostile-code sandbox.

Loaded only in the runner's isolated child. CI must additionally deny networking
at the OS/container level. No production module is imported by this file.
"""
import os
from pathlib import Path
import socket
import sys
import threading
from urllib.parse import unquote, urlsplit

ACTIVE = False
DENIED = {"network": 0, "process": 0, "filesystem": 0}
EVENTS = []
CURRENT_TEST = None
_local_pair = threading.local()


def _deny(kind, event=None):
    DENIED[kind] += 1
    frame = sys._getframe(1)
    stack = []
    while frame is not None and len(stack) < 7:
        stack.append({"file": os.path.basename(frame.f_code.co_filename),
                      "line": frame.f_lineno, "function": frame.f_code.co_name})
        frame = frame.f_back
    if len(EVENTS) < 100:
        EVENTS.append({"kind":kind,"event":event,"test":CURRENT_TEST,"stack":stack})
    raise PermissionError("BASELINE_DENIED_" + kind.upper())


def _within(path, parent):
    try:
        return os.path.commonpath((path, parent)) == parent
    except ValueError:
        return False


def _normalized(path, dir_fd=None):
    if isinstance(path, int) or path is None:
        return None
    path = os.fsdecode(path)
    if dir_fd not in (None, -1) and not os.path.isabs(path) and os.name != "nt":
        path = os.path.join(os.readlink(f"/proc/self/fd/{dir_fd}"), path)
    return os.path.normcase(os.path.abspath(path))


if os.environ.get("WALLETHUNTER_BASELINE_CHILD") == "1":
    sandbox = _normalized(os.environ["WALLETHUNTER_BASELINE_SANDBOX"])
    original = _normalized(os.environ["WALLETHUNTER_BASELINE_SOURCE"])
    environment = _normalized(sys.prefix)
    # Runtime dependencies may live in the existing venv under the source tree;
    # permit that environment, never the original application/data beside it.
    def check_path(path, writing=False, dir_fd=None):
        value = _normalized(path, dir_fd)
        if value is None:
            return
        if _within(value, original) and not _within(value, environment):
            _deny("filesystem")
        if writing and not _within(value, sandbox):
            _deny("filesystem")

    def audit(event, args):
        if event.startswith("socket.") and event in {
            "socket.connect", "socket.bind", "socket.getaddrinfo", "socket.gethostbyname",
            "socket.gethostbyaddr", "socket.getnameinfo", "socket.sendto", "socket.sendmsg",
        }:
            if not getattr(_local_pair, "allowed", False):
                _deny("network", event)
        if event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.spawn",
                     "os.exec", "os.fork", "os.forkpty", "pty.spawn", "os.startfile",
                     "os.startfile/2", "_winapi.CreateProcess"}:
            _deny("process", event)
        if event == "open":
            path, mode, flags = args
            writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            check_path(path, writing)
        elif event == "sqlite3.connect":
            if args[0] != ":memory:":
                database = args[0]
                if isinstance(database, str) and database.startswith("file:"):
                    parsed = urlsplit(database)
                    if parsed.netloc:
                        _deny("filesystem")
                    database = unquote(parsed.path)
                    if os.name == "nt" and len(database) > 2 and database[0] == "/" and database[2] == ":":
                        database = database[1:]
                check_path(database, True)
        elif event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.truncate"}:
            # mkdir/chmod include mode before dir_fd, remove/rmdir do not.
            index = 2 if event in {"os.mkdir", "os.chmod"} else 1
            fd = args[index] if len(args) > index else None
            check_path(args[0], True, fd)
        elif event == "os.rename":
            check_path(args[0], True, args[2] if len(args) > 2 else None)
            check_path(args[1], True, args[3] if len(args) > 3 else None)

    sys.addaudithook(audit)
    # asyncio requires a local self-pipe. Windows builds implement socketpair
    # through localhost; permit ONLY the standard library's synchronous pair.
    _socketpair = socket.socketpair

    def local_socketpair(*args, **kwargs):
        previous = getattr(_local_pair, "allowed", False)
        _local_pair.allowed = True
        try:
            return _socketpair(*args, **kwargs)
        finally:
            _local_pair.allowed = previous

    socket.socketpair = local_socketpair
    ACTIVE = True
