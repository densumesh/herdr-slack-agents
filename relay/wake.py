"""Wake-ups from Claude Code hooks: each hook truncates a file in WAKE_DIR (a shell redirect, no process start),
and the relay's inotify watch on that directory turns it into an immediate update instead of a timed poll."""
import ctypes
import os
import struct
from pathlib import Path

# tmpfs, so inotify sees every write; the Claude hook (hooks/slack-wake.sh) resolves the same path
WAKE_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "slack-relay"
IN_CLOSE_WRITE = 0x00000008
libc = ctypes.CDLL("libc.so.6", use_errno=True)


def watch():
    """Yield once per burst of hook writes (blocks until the next one)."""
    WAKE_DIR.mkdir(mode=0o700, exist_ok=True)
    fd = libc.inotify_init1(os.O_CLOEXEC)
    if fd < 0 or libc.inotify_add_watch(fd, str(WAKE_DIR).encode(), IN_CLOSE_WRITE) < 0:
        raise OSError(ctypes.get_errno(), "inotify setup failed")
    with os.fdopen(fd, "rb", buffering=0) as f:
        while True:
            buf = f.read(64 * 1024)  # one read drains every queued event, so a burst is a single wake-up
            if len(buf) >= struct.calcsize("iIII"):
                yield
