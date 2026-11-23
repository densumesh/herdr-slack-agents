"""Shared config, state, herdr helpers and Slack message layout for slack-ping and the relay.

Config (CONFIG_DIR/tokens.env): SLACK_BOT_TOKEN, SLACK_APP_TOKEN, OWNER_SLACK_USER, optional OWNER_NAME and
CLAUDE_ARGS. State (STATE_DIR/state.json):
  threads[ts]   -> one Slack thread: card text, agent identity, last shown status
  waiting[ts]   -> a --wait caller blocked on the owner's next reply in that thread
  acks[ts]      -> a delivered reply whose agent turn is still running (👀, narration mirroring)
Replies for --wait land in replies/<ts>.txt; files the owner sends land in inbox/.
Both dirs default to herdr's plugin dirs; SLACK_AGENTS_CONFIG_DIR / SLACK_AGENTS_STATE_DIR override them.
"""
import fcntl
import json
import os
import re
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

PLUGIN_ID = "densumesh.slack-agents"
CONFIG_DIR = Path(os.environ.get("SLACK_AGENTS_CONFIG_DIR") or Path.home() / ".config/herdr/plugins/config" / PLUGIN_ID)
STATE_DIR = Path(os.environ.get("SLACK_AGENTS_STATE_DIR") or Path.home() / ".local/state/herdr/plugins" / PLUGIN_ID)
TOKENS = CONFIG_DIR / "tokens.env"
STATE = STATE_DIR / "state.json"
REPLIES = STATE_DIR / "replies"
INBOX = STATE_DIR / "inbox"

ICONS = {"claude": ":large_orange_circle:", "codex": ":large_blue_circle:"}
STATUS = {
    "working": "🟢 Working",
    "needs_you": "❓ Waiting for your reply",
    "blocked": "🟠 Stuck on an approval prompt in herdr",
    "idle": "⏸ Idle",
    "ended": "⚪ Session ended",
    "oneway": "📣 One-way (script, replies go nowhere)",
}


def load_config():
    cfg = {}
    for line in TOKENS.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip("'\"")
    missing = [k for k in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "OWNER_SLACK_USER") if not cfg.get(k)]
    if missing:
        raise SystemExit(f"{TOKENS}: missing {', '.join(missing)} (run the plugin's bin/setup)")
    cfg.setdefault("OWNER_NAME", "the owner")
    return cfg


@contextmanager
def state():
    """Locked read-modify-write of the whole state file."""
    for d in (STATE_DIR, REPLIES, INBOX):
        d.mkdir(parents=True, exist_ok=True)
    with open(STATE_DIR / "state.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = json.loads(STATE.read_text()) if STATE.exists() else {}
        data.setdefault("threads", {})
        data.setdefault("waiting", {})
        data.setdefault("acks", {})  # owner replies showing 👀 until the agent's turn settles
        yield data
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        os.replace(tmp, STATE)


HERDR = os.environ.get("HERDR_BIN_PATH") or "herdr"


def herdr(*args, timeout=60):
    """Run a herdr CLI command; return (ok, parsed JSON or error text)."""
    try:
        p = subprocess.run([HERDR, *args], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "herdr timed out"
    out = p.stdout if p.returncode == 0 else p.stderr
    try:
        return p.returncode == 0, json.loads(out)
    except json.JSONDecodeError:
        return p.returncode == 0, out.strip()


def herdr_error(res):
    if isinstance(res, dict):
        err = res.get("error", res)
        return err.get("message", err) if isinstance(err, dict) else err
    return res


def agent_panes():
    ok, res = herdr("pane", "list")
    return [p for p in res["result"]["panes"] if p.get("agent")] if ok else None


def match_pane(panes, thread):
    """The pane now hosting this thread's agent; pane ids change on move, terminal and session ids don't."""
    for pane in panes or []:
        if same_session(pane.get("terminal_id"), (pane.get("agent_session") or {}).get("value"), thread):
            return pane
    return None


def same_session(terminal_id, session_id, thread):
    """Session ids win when both sides know one (a /clear in the same pane is a new session); else the terminal."""
    if session_id and thread.get("session_id"):
        return session_id == thread["session_id"]
    return bool(terminal_id) and terminal_id == thread.get("terminal_id")


def thread_for(data, ctx):
    """Existing thread of the calling agent session, if any."""
    for ts, t in data["threads"].items():
        if ctx and same_session(ctx.get("terminal_id"), ctx.get("session_id"), t):
            return ts
    return None


TABLE_SEP = re.compile(r"^\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _cells(line):
    return [c.strip().replace("**", "").replace("`", "") for c in line.strip().strip("|").split("|")]


def _table(lines):
    """GFM table lines -> Slack's native table block (the markdown block mangles tables)."""
    head, sep, body = _cells(lines[0]), _cells(lines[1]), [_cells(l) for l in lines[2:]]
    width = len(head)
    align = [{"align": "right"} if c.endswith(":") and not c.startswith(":") else
             {"align": "center"} if c.startswith(":") and c.endswith(":") else {"align": "left"} for c in sep[:width]]
    rows = [[{"type": "raw_text", "text": (r[i] if i < len(r) else "") or " "} for i in range(width)]
            for r in [head] + body]
    return {"type": "table", "rows": rows[:100], "column_settings": align + [{"align": "left"}] * (width - len(align))}


def md_blocks(text):
    """Markdown -> blocks: prose as markdown blocks, the first table native, later tables as code blocks."""
    blocks, prose, lines, i, tables = [], [], text.splitlines(), 0, 0
    while i < len(lines):
        if lines[i].lstrip().startswith("|") and i + 1 < len(lines) and TABLE_SEP.match(lines[i + 1].strip()):
            j = i + 2
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                j += 1
            if prose and "\n".join(prose).strip():
                blocks.append({"type": "markdown", "text": "\n".join(prose)[:11000]})
            prose = []
            if tables == 0:
                blocks.append(_table(lines[i:j]))
            else:  # Slack allows one table block per message
                blocks.append({"type": "markdown", "text": "```\n" + "\n".join(lines[i:j]) + "\n```"})
            tables += 1
            i = j
        else:
            prose.append(lines[i])
            i += 1
    if "\n".join(prose).strip():
        blocks.append({"type": "markdown", "text": "\n".join(prose)[:11000]})
    return blocks


def card_blocks(t, status):
    """Top-of-thread card: live status, where the agent runs, its first message, and its task list."""
    blocks = [{"type": "context", "elements": [{"type": "mrkdwn", "text": f"{STATUS[status]}  ·  {t['where']}"}]}]
    if t.get("late_name"):  # Slack can't rename a posted message's sender, so the session's title lives here
        blocks.insert(0, {"type": "section", "text": {"type": "mrkdwn", "text": f"*{t['name']}*"}})
    if t.get("text"):
        blocks += md_blocks(t["text"])
    if t.get("chosen"):
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"✔ You chose *{t['chosen']}*"}]})
    if t.get("plan"):
        blocks.append({"type": "plan", "title": "Plan", "tasks": t["plan"]})
    return blocks


def choice_blocks(choices):
    return [{"type": "actions", "block_id": "choices",
             "elements": [{"type": "button", "text": {"type": "plain_text", "text": c[:75]}, "value": c[:2000],
                           "action_id": f"choice{i}"} for i, c in enumerate(choices)]}]


def now():
    return time.time()
