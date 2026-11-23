"""Keep one relay.py running per user: hold STATE/relay.lock, restart the relay after it exits (backing off from
10 s to 5 min while it keeps failing), and on SIGTERM stop it and exit. Started detached (setsid) by bin/relay-ctl,
which reads relay.pid ("PID HOST") and relay.run (root, herdr socket, code fingerprint) to decide on restarts."""
import ctypes
import fcntl
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from common import STATE_DIR

STATE_DIR.mkdir(parents=True, exist_ok=True)
os.chmod(STATE_DIR, 0o700)
lock = open(STATE_DIR / "relay.lock", "a")
for _ in range(20):  # relay-ctl's running() probe holds the lock for an instant
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        break
    except BlockingIOError:
        time.sleep(0.1)
else:
    sys.exit("another supervisor owns the relay")
(STATE_DIR / "relay.pid").write_text(f"{os.getpid()} {socket.gethostname()}\n")
(STATE_DIR / "relay.run").write_text(sys.argv[1] if len(sys.argv) > 1 else "")
STDERR = STATE_DIR / "relay.stderr"
libc = ctypes.CDLL("libc.so.6", use_errno=True)
child = None


def die_with_parent():
    libc.prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG: a SIGKILLed supervisor takes its relay with it


def stop(signum, frame):
    """Only flag and forward: the main loop is inside child.wait(), which holds Popen's lock, so waiting here would
    stall until the timeout. The loop sees the flag once the relay has exited."""
    global stopping
    stopping = True
    if child and child.poll() is None:
        os.kill(child.pid, signal.SIGTERM)


stopping = False
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGHUP, signal.SIG_IGN)
(STATE_DIR / "relay.connected").unlink(missing_ok=True)  # left by a SIGKILLed predecessor
delay = 5  # doubled before the first wait: 10 s, then up to 5 min while it keeps failing fast
while not stopping:
    if STDERR.exists() and STDERR.stat().st_size > 1 << 20:
        STDERR.replace(STDERR.with_suffix(".stderr.1"))
    started = time.time()
    with open(STDERR, "a") as err:
        child = subprocess.Popen([sys.executable, "relay.py"], stdin=subprocess.DEVNULL, stdout=err, stderr=err,
                                 preexec_fn=die_with_parent)
        try:
            code = child.wait(timeout=None)
        finally:
            (STATE_DIR / "relay.connected").unlink(missing_ok=True)
        if stopping:
            break
        delay = 10 if time.time() - started > 300 else min(delay * 2, 300)
        err.write(f"{time.strftime('%F %T')} relay exited ({code}), restarting in {delay} s\n")
    for _ in range(delay * 10):  # sleep, but leave promptly on SIGTERM
        if stopping:
            break
        time.sleep(0.1)
