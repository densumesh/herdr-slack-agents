"""Keep one relay.py running per user: hold STATE/relay.lock, restart the relay 10 s after it exits, and on
SIGTERM stop it and exit. Started detached (setsid) by bin/relay-ctl."""
import fcntl
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from common import STATE_DIR

STATE_DIR.mkdir(parents=True, exist_ok=True)
lock = open(STATE_DIR / "relay.lock", "w")
try:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(0)  # another supervisor owns the relay
(STATE_DIR / "relay.pid").write_text(f"{os.getpid()}\n")
(STATE_DIR / "relay.root").write_text(f"{Path(__file__).resolve().parent.parent}\n")
child = None


def stop(signum, frame):
    if child and child.poll() is None:
        child.terminate()
        try:
            child.wait(10)
        except subprocess.TimeoutExpired:
            child.kill()
    sys.exit(0)


signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGHUP, signal.SIG_IGN)
while True:
    with open(STATE_DIR / "relay.stderr", "a") as err:
        child = subprocess.Popen([sys.executable, "relay.py"], stdin=subprocess.DEVNULL, stdout=err, stderr=err)
        code = child.wait()
        err.write(f"{time.strftime('%F %T')} relay exited ({code}), restarting in 10 s\n")
    time.sleep(10)
