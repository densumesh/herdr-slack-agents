"""Wake-ups from Claude Code hooks: each hook truncates a file in WAKE_DIR (a shell redirect, no process start),
and the relay's inotify watch on that directory turns it into an immediate update instead of a timed poll."""
import ctypes
import logging
import os
import struct
import time
from pathlib import Path

# tmpfs, so inotify sees every write; the Claude hook (hooks/slack-wake.sh) resolves the same path
WAKE_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "slack-relay"
IN_CLOSE_WRITE, IN_IGNORED = 0x00000008, 0x00008000
EVENT = struct.Struct("iIII")
libc = ctypes.CDLL("libc.so.6", use_errno=True)
log = logging.getLogger("slack-relay")


def _open():
    WAKE_DIR.mkdir(mode=0o700, exist_ok=True)
    fd = libc.inotify_init1(os.O_CLOEXEC)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "inotify_init1 failed")
    if libc.inotify_add_watch(fd, str(WAKE_DIR).encode(), IN_CLOSE_WRITE) < 0:
        err = ctypes.get_errno()
        os.close(fd)
        raise OSError(err, f"inotify watch on {WAKE_DIR} failed")
    return os.fdopen(fd, "rb", buffering=0)


def watch():
    """Yield once per burst of hook writes (blocks until the next one). When the directory goes away (logind
    removes the runtime dir at logout), retry every 30 s until it can be recreated, polling meanwhile."""
    while True:
        try:
            f = _open()
        except OSError as e:
            log.warning("hook wake-ups unavailable (%s); retrying in 30 s", e)
            time.sleep(30)
            continue
        with f:
            gone = False
            while not gone:
                buf = f.read(64 * 1024)  # one read drains every queued event, so a burst is a single wake-up
                off = 0
                while off + EVENT.size <= len(buf):
                    _, mask, _, name_len = EVENT.unpack_from(buf, off)
                    gone |= bool(mask & IN_IGNORED)
                    off += EVENT.size + name_len
                if buf:
                    yield
        log.warning("wake dir %s removed; re-watching", WAKE_DIR)
        time.sleep(1)
