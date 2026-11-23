"""slack-ping: message the owner on Slack from an agent or script; their replies come back.

  slack-ping "message"                    post in this agent session's thread (first ping opens it)
  slack-ping --file plot.png "message"    attach files; images show inline (repeatable)
  slack-ping --choices "Push,Hold" "q"    add quick-reply buttons; a click arrives like a typed reply
  slack-ping --wait [--timeout S] "q"     block until the owner replies; print it (exit 3 on timeout)
  slack-ping --thread TS "message"        post in a specific thread
  slack-ping --new "message"              open a fresh thread for this session
  echo text | slack-ping -                message from stdin
Messages are Markdown: **bold**, `code`, ``` blocks, tables, lists, links.
Prints the thread ts (with --wait: the reply).
"""
import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from common import ICONS, REPLIES, card_blocks, md_blocks, choice_blocks, herdr, load_config, now, state, thread_for

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def label(kind, ident, key):
    ok, res = herdr(kind, "list", *(["--workspace", ident.split(":")[0]] if kind == "tab" else []))
    for item in res["result"][key] if ok else []:
        if item[f"{kind}_id"] == ident:
            return item.get("label") or ""
    return ""


def where_label(pane):
    """'workspace › tab · repo ⎇ branch', skipping auto-numbered tab labels and a bare home dir."""
    parts = [label("workspace", pane["workspace_id"], "workspaces")]
    tab = label("tab", pane["tab_id"], "tabs")
    if tab and not tab.isdigit():
        parts.append(tab)
    loc = " › ".join(p for p in parts if p)
    cwd = (pane.get("foreground_cwd") or pane.get("cwd") or "").rstrip("/")
    if cwd and cwd != str(Path.home()):
        try:
            branch = subprocess.run(["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True,
                                    text=True, timeout=5).stdout.strip()
        except subprocess.TimeoutExpired:
            branch = ""
        repo = os.path.basename(cwd)
        loc += f" · {repo} ⎇ {branch}" if branch else f" · {repo}"  # no "@": Slack turns @name into a mention
    return loc.strip(" ·")


def herdr_context():
    if os.environ.get("HERDR_ENV") != "1":
        return {}
    ok, res = herdr("pane", "current", "--current")
    if not ok or not res["result"]["pane"].get("agent"):
        return {}
    pane = res["result"]["pane"]
    return {
        "pane_id": pane["pane_id"],
        "terminal_id": pane.get("terminal_id"),
        "session_id": (pane.get("agent_session") or {}).get("value"),
        "agent": pane["agent"],
        "name": pane.get("terminal_title_stripped") or f"{pane['agent']} agent",
        "where": where_label(pane),
    }


def upload_images(client, paths):
    """Upload privately and return image blocks that reference the files."""
    blocks = []
    for p in paths:
        f = client.files_upload_v2(file=str(p), filename=p.name)["file"]
        blocks.append({"type": "image", "slack_file": {"id": f["id"]}, "alt_text": p.name})
    return blocks


def post(client, **kw):
    """chat.postMessage, retrying while freshly uploaded images are still processing (invalid_blocks)."""
    for i in range(10):
        try:
            return client.chat_postMessage(**kw)
        except SlackApiError as e:
            if e.response.get("error") != "invalid_blocks" or i == 9:
                raise
            time.sleep(1.5)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("message", help="Markdown text, or - for stdin")
    ap.add_argument("--file", action="append", default=[], type=Path, help="attach a file (repeatable)")
    ap.add_argument("--choices", help="comma-separated quick-reply buttons")
    ap.add_argument("--wait", action="store_true", help="block until the owner replies and print the reply")
    ap.add_argument("--timeout", type=float, default=3600, help="--wait timeout in seconds (default 3600)")
    ap.add_argument("--thread", help="post in this thread (ts)")
    ap.add_argument("--new", action="store_true", help="open a fresh thread for this session")
    a = ap.parse_args()
    text = (sys.stdin.read() if a.message == "-" else a.message).strip()
    for f in a.file:
        if not f.is_file():
            ap.error(f"no such file: {f}")
    choices = [c.strip() for c in a.choices.split(",") if c.strip()] if a.choices else []

    cfg = load_config()
    client = WebClient(token=cfg["SLACK_BOT_TOKEN"])
    channel = client.conversations_open(users=cfg["OWNER_SLACK_USER"])["channel"]["id"]
    ctx = herdr_context()
    host = socket.gethostname().split(".")[0]
    with state() as s:
        ts = a.thread or (None if a.new else thread_for(s, ctx))
        existing = s["threads"].get(ts, {}) if ts else {}
    name = (existing.get("name") or ctx.get("name") or f"script on {host}")[:80]
    where = existing.get("where") or ctx.get("where") or f"`{host}` · {os.getcwd()}"
    icon = ICONS.get(ctx.get("agent"), ":gear:")
    asks = a.wait or bool(choices)

    images = [f for f in a.file if f.suffix.lower() in IMAGE_EXT]
    others = [f for f in a.file if f not in images]
    extra = upload_images(client, images) + (choice_blocks(choices) if choices else [])
    summary = f"{name}: {text[:150]}"

    if ts:
        msg = post(client, channel=channel, thread_ts=ts, text=summary,
                   blocks=md_blocks(text) + extra, username=name, icon_emoji=icon)
        status = existing.get("status", "working")
    else:
        status = "needs_you" if asks else ("working" if ctx else "oneway")
        msg = post(client, channel=channel, text=summary, username=name, icon_emoji=icon,
                   blocks=card_blocks({"where": where, "text": text}, status) + extra)
        ts = msg["ts"]
    for f in others:
        client.files_upload_v2(channel=channel, thread_ts=ts, file=str(f), filename=f.name)

    with state() as s:
        # A --thread ts we never recorded isn't our card, so the relay must not redraw it.
        card_ts = None if a.thread and not existing else ts
        t = s["threads"].setdefault(ts, {"card_ts": card_ts, "name": name, "where": where, "text": text,
                                         "status": status, "created": now()})
        t.update({k: v for k, v in ctx.items() if k in ("pane_id", "terminal_id", "session_id", "agent")})
        t.update(channel=channel, mode="pane" if ctx else "none", needs_you=asks or t.get("needs_you", False),
                 last_ping=now())
        if a.wait:
            s["waiting"][ts] = {"since": now()}

    if not a.wait:
        print(ts)
        return
    reply = REPLIES / f"{ts}.txt"
    deadline = time.time() + a.timeout
    while time.time() < deadline:
        if reply.exists():
            print(reply.read_text())
            reply.unlink()
            return
        time.sleep(2)
    with state() as s:
        s["waiting"].pop(ts, None)
        s["threads"][ts]["needs_you"] = False
    print(f"slack-ping: no reply in {a.timeout:.0f}s (thread {ts})", file=sys.stderr)
    sys.exit(3)


if __name__ == "__main__":
    main()
