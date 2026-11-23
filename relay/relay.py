"""Socket Mode relay between the owner's Slack DM with the bot and their herdr agents.

- reply in a ping thread   -> typed into that agent's pane (or handed to a `slack-ping --wait` caller); a Claude
                              blocked on a foreground command/subagent gets Ctrl+B first so it reads it right away
- tap a quick-reply button -> same, with the button's text
- top-level DM             -> starts a new Claude in a new herdr tab; the DM's thread becomes its thread
- Claude hook / every POLL s -> status cards (and Slack's native session status/title) follow herdr agent state
                              and the Claude transcript's task list; 👀 on a reply turns ✅ once the agent settles
- herdr agent session with no thread (new tab or /clear) -> gets its own session card within one poll, titled
                              "New Claude session" until its auto-title lands
- every Claude turn         -> its narration is mirrored into the thread, then its final message (turns typed in the
                              terminal too, headed by the prompt)
- Slack's stop button      -> Esc in the agent's pane; top-level `status` / `help` are answered by the relay
"""
import fcntl
import functools
import html
import logging
import logging.handlers
import os
import re
import socket
import threading
import time
import urllib.request
from pathlib import Path

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

from common import (ICONS, INBOX, REPLIES, STATE_DIR, STATUS, agent_panes, card_blocks, herdr, herdr_error, load_config,
                    match_pane, md_blocks, now, state)
import wake
from plan import PlanTracker
from slack_ping import where_label

POLL = 30          # fallback only: Claude hooks wake the relay (wake.py); Codex and herdr-only changes wait for this
# extra looks (s after a hook): Claude writes the prompt / turn-end record only once its hooks have finished
FOLLOW_UPS = (0.2, 0.5, 1.0, 2.0)
MIRROR_EVERY = 5   # min s between edits of one Working message (chat.update is rate-limited workspace-wide)
NATIVE = {"working": "processing", "needs_you": "suspended", "blocked": "suspended", "idle": "active",
          "ended": "active", "oneway": "active"}
log = logging.getLogger("slack-relay")
cfg = load_config()
web = WebClient(token=cfg["SLACK_BOT_TOKEN"])
seen = set()
plans = PlanTracker()
PROMPT_TAG = f"[Slack from {cfg['OWNER_NAME']}"
dm_channel = web.conversations_open(users=cfg["OWNER_SLACK_USER"])["channel"]["id"]
UNTITLED = {"", "claude", "claude code", "codex"}
native_ok = True
wakeup = threading.Event()
mirror_deferred = False  # a Working message has news it was too soon to show


def react(channel, ts, name, remove=None):
    try:
        if remove:
            web.reactions_remove(channel=channel, timestamp=ts, name=remove)
        if name:
            web.reactions_add(channel=channel, timestamp=ts, name=name)
    except Exception as e:  # already_reacted / no_reaction must not drop a delivery
        log.warning("reaction %s/%s failed: %s", name, remove, e)


def note(channel, thread_ts, text):
    web.chat_postMessage(channel=channel, thread_ts=thread_ts, text=text)


@functools.lru_cache(maxsize=256)
def user_name(uid):
    try:
        u = web.users_info(user=uid)["user"]
        return u.get("real_name") or u["name"]
    except Exception:
        return uid


def clean(text):
    """Slack mrkdwn -> plain text for the agent: links, mentions, entities."""
    text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2 (\1)", text)
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
    text = re.sub(r"<@(U[A-Z0-9]+)>", lambda m: "@" + user_name(m.group(1)), text)
    return html.unescape(text).strip()


def shares(ev):
    """Forwarded/shared messages arrive as attachments, not in ev["text"]."""
    return [a for a in ev.get("attachments", []) if a.get("is_share") or a.get("is_msg_unfurl")]


def forwarded(ev):
    parts = []
    for a in shares(ev):
        who = a.get("author_name") or a.get("author_subname") or "someone"
        body = clean(a.get("text") or a.get("fallback") or "")
        src = f" ({a['from_url']})" if a.get("from_url") else ""
        parts.append(f"\n[Forwarded message from {who}{src}]\n{body}")
    return "".join(parts)


def download(ev):
    paths = []
    for f in ev.get("files", []) + [f for a in shares(ev) for f in a.get("files", [])]:
        url = f.get("url_private_download")
        if not url:
            continue
        dest = INBOX / f"{ev['ts']}_{Path(f.get('name') or f['id']).name}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {cfg['SLACK_BOT_TOKEN']}"})
        with urllib.request.urlopen(req, timeout=60) as r:
            dest.write_bytes(r.read())
        paths.append(str(dest))
    return paths


def deliver(channel, thread_ts, text, react_ts):
    """Route the owner's reply (typed or button) to whoever owns the thread."""
    with state() as s:
        t = s["threads"].get(thread_ts)
        waiting = s["waiting"].pop(thread_ts, None)
        if t:
            t["needs_you"] = False
    if t is None:
        note(channel, thread_ts, "This thread isn't linked to an agent. Send a new top-level message to start one.")
        return
    if waiting:
        tmp = REPLIES / f"{thread_ts}.tmp"
        tmp.write_text(text)
        os.replace(tmp, REPLIES / f"{thread_ts}.txt")
        react(channel, react_ts, "white_check_mark")
        return
    if t.get("mode") != "pane":
        note(channel, thread_ts, "Not delivered: that message came from a script, which can't take replies.")
        react(channel, react_ts, "warning")
        return
    pane = match_pane(agent_panes(), t)
    if pane is None:
        note(channel, thread_ts, "Not delivered: that agent session has ended.")
        react(channel, react_ts, "warning")
        return
    sid = t.get("session_id") if t.get("agent") == "claude" else None
    base = plans.texts_since(sid, 0)[0] if sid else 0  # narration before this turn isn't mirrored
    prompt = (f"{PROMPT_TAG}, thread {thread_ts}] {text}\n(Your replies this turn appear in the Slack thread "
              f"automatically; use slack-ping only for files, buttons or a later update.)")
    if t.get("agent") == "claude":
        background_foreground_tool(pane["pane_id"])
    ok, res = herdr("agent", "prompt", pane["pane_id"], prompt)
    if not ok:
        note(channel, thread_ts, f"Not delivered: {herdr_error(res)}")
        react(channel, react_ts, "warning")
        return
    react(channel, react_ts, "eyes")
    with state() as s:
        # a message while the agent's turn is still running joins that turn: one mirror, ✅ on both
        prev = pending_turn(s, thread_ts)
        if prev:
            prev.setdefault("also", []).append(react_ts)
            prev["moves"] = prev.get("moves", 0) + 1  # re-post the Working message below the owner's new message
            wakeup.set()
            return
        turn = plans.turns_ended(sid) if sid else None
        s["acks"][react_ts] = {"channel": channel, "ts": react_ts, "thread": thread_ts, "since": now(),
                               "seen_working": False, "sid": sid, "base": base, "progress_ts": None, "last_edit": 0,
                               "turn": turn}
        if turn is not None and thread_ts in s["threads"]:
            th = s["threads"][thread_ts]
            th["next_turn"] = max(th.get("next_turn", 0), turn + 1)


BG_HINT = re.compile(r"(^|[(·]\s*)ctrl\+b to run in background\)?$")


def background_foreground_tool(pane_id):
    """Ctrl+B a running foreground Bash/subagent so the queued Slack message is read now, not when it finishes.

    Only when Claude's own hint line is on screen, so no stray key lands in an idle prompt."""
    ok, res = herdr("pane", "read", pane_id, "--source", "visible")
    lines = res.splitlines()[-30:] if ok and isinstance(res, str) else []
    if any(BG_HINT.search(x.strip()) and "`" not in x for x in lines):
        ok, res = herdr("agent", "send-keys", pane_id, "ctrl+b")
        log.info("backgrounded the foreground tool in %s: %s", pane_id, "ok" if ok else herdr_error(res))


def spawn(ev, text):
    """Top-level DM: new herdr tab running Claude, prompted with the message; the DM becomes its thread."""
    channel, ts = ev["channel"], ev["ts"]
    first, _, rest = text.partition(" ")
    cwd = Path(os.path.expanduser(first))
    if first and cwd.is_dir() and rest.strip():
        text = rest.strip()
    else:
        cwd = Path.home()
    ok, res = herdr("workspace", "list")
    spaces = res["result"]["workspaces"] if ok else []
    ws = next((w for w in spaces if w.get("focused")), spaces[0] if spaces else None)
    if ws is None:
        note(channel, ts, f"Couldn't start an agent: no herdr workspace ({herdr_error(res)}).")
        react(channel, ts, "warning")
        return
    ok, res = herdr("tab", "create", "--workspace", ws["workspace_id"], "--cwd", str(cwd), "--label", "slack",
                    "--no-focus")
    if not ok:
        note(channel, ts, f"Couldn't open a herdr tab: {herdr_error(res)}")
        react(channel, ts, "warning")
        return
    pane_id = res["result"]["root_pane"]["pane_id"]
    pane = herdr("pane", "get", pane_id)[1]["result"]["pane"]
    name = "Claude (from Slack)"
    t = {"card_ts": None, "name": name, "where": where_label(pane), "text": None, "status": "working",
         "created": now(), "channel": channel, "mode": "pane", "needs_you": False, "agent": "claude",
         "pane_id": pane_id, "terminal_id": pane.get("terminal_id"), "session_id": None,
         "starting": True}
    with state() as s:  # claim the pane now, or the poller opens a second session for it while Claude starts
        s["threads"][ts] = t
    agent = "slack-" + ts.replace(".", "")[-10:]
    for attempt in range(3):  # a brand-new shell may not be at its prompt yet
        extra = ["--", *cfg["CLAUDE_ARGV"]] if cfg["CLAUDE_ARGV"] else []
        ok, res = herdr("agent", "start", agent, "--kind", "claude", "--pane", pane_id, "--timeout", "60000",
                        *extra, timeout=90)
        if ok or attempt == 2:
            break
        time.sleep(3)
    if not ok:
        with state() as s:
            s["threads"].pop(ts, None)
        note(channel, ts, f"Couldn't start Claude in `{pane_id}`: {herdr_error(res)}")
        react(channel, ts, "warning")
        return
    try:
        pane = herdr("pane", "get", pane_id)[1]["result"]["pane"]
        card = web.chat_postMessage(channel=channel, thread_ts=ts, text=f"{name} started",
                                    blocks=card_blocks(t, "working"), username=name, icon_emoji=ICONS["claude"])
    finally:  # Claude is running either way; let the poller track (and eventually end) the thread
        with state() as s:
            s["threads"][ts]["starting"] = False
    with state() as s:
        t = s["threads"][ts]
        t["card_ts"] = card["ts"]
        t["session_id"] = t.get("session_id") or (pane.get("agent_session") or {}).get("value")
    deliver(channel, ts, text, ts)


def on_message(ev):
    text = clean(ev.get("text", "")) + forwarded(ev)
    files = download(ev)
    if files:
        text += f"\nAttached files (saved on {socket.gethostname()}): " + ", ".join(files)
    if ev.get("thread_ts") and ev["thread_ts"] != ev["ts"]:
        deliver(ev["channel"], ev["thread_ts"], text, ev["ts"])
    elif text.lower() in ("status", "help"):
        if text.lower() == "status":
            status_report(ev["channel"], ev["ts"])
        else:
            web.chat_postMessage(channel=ev["channel"], thread_ts=ev["ts"], text="help",
                                 blocks=[{"type": "markdown", "text": HELP}])
    else:
        spawn(ev, text)


def on_button(payload):
    if payload["user"]["id"] != cfg["OWNER_SLACK_USER"]:
        return
    action, msg, channel = payload["actions"][0], payload["message"], payload["channel"]["id"]
    choice = action["value"]
    blocks = [b for b in msg.get("blocks", []) if b.get("type") != "actions"]
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"✔ You chose *{choice}*"}]})
    web.chat_update(channel=channel, ts=msg["ts"], blocks=blocks, text=msg.get("text", choice))
    thread_ts = msg.get("thread_ts") or msg["ts"]
    with state() as s:
        t = s["threads"].get(thread_ts)
        if t and t.get("card_ts") == msg["ts"]:
            t["chosen"] = choice  # keeps the choice visible when the poller redraws the card
    deliver(channel, thread_ts, choice, msg["ts"])


def status_of(ts, t, panes, waiting):
    if ts in waiting or t.get("needs_you"):
        return "needs_you"
    if t.get("mode") != "pane":
        return "oneway"
    pane = match_pane(panes, t)
    if pane is None:
        return "working" if t.get("starting") else "ended"
    return {"working": "working", "blocked": "blocked"}.get(pane.get("agent_status"), "idle")


def native(method, **payload):
    """Call Slack's agent-session API (spinner + stop button, session titles) when the app has it."""
    global native_ok
    if not native_ok:
        return
    try:
        web.api_call(method, json=payload)
    except SlackApiError as e:
        if e.response.get("error") in ("not_authorized", "missing_scope", "unknown_method"):
            native_ok = False  # agent feature or assistant:write missing; restart the relay after adding it
            log.info("Slack agent sessions unavailable (%s); using the card only", e.response.get("error"))
        else:
            log.warning("%s %s failed: %s", method, payload.get("thread_ts"), e.response.get("error"))


def native_status(ts, t, status):
    native("agents.sessions.setStatus", channel_id=t["channel"], thread_ts=ts, status=NATIVE[status])


def native_title(ts, t, title):
    native("agents.sessions.rename", channel_id=t["channel"], thread_ts=ts, title=title[:100])


herdr_down = False


def poll_once():
    global herdr_down
    panes = agent_panes()
    if (panes is None) != herdr_down:
        herdr_down = panes is None
        if herdr_down:
            log.warning("herdr unreachable (%s); agent tracking paused", os.environ.get("HERDR_SOCKET_PATH", "herdr"))
        else:
            log.info("herdr reachable again")
    if panes is None:
        return
    redraw, statuses, titles = [], [], []
    with state() as s:
        for ts, t in s["threads"].items():
            if t.get("status") in ("ended", "oneway") and not t.get("needs_you") and ts not in s["waiting"]:
                continue
            new = status_of(ts, t, panes, s["waiting"])
            pane = match_pane(panes, t)
            plan = plans.tasks(t["session_id"]) if t.get("agent") == "claude" and t.get("session_id") else []
            changed = new != t.get("status") or plan != t.get("plan", [])
            t["status"], t["plan"] = new, plan
            if changed:
                redraw.append(dict(t))
            # "processing" times out after an hour on Slack's side, so refresh it while the agent works
            if changed or (new == "working" and now() - t.get("native_at", 0) > 3000):
                t["native_at"] = now()
                statuses.append((ts, dict(t), new))
            sess = (pane.get("agent_session") or {}).get("value") if pane else None
            if sess and not t.get("session_id"):
                t["session_id"] = sess  # pin it, so a later /clear in this pane opens a new thread
            claude = t.get("agent") == "claude" and t.get("session_id") and pane
            title = plans.title(t["session_id"]) if claude else None
            title = title or (pane.get("terminal_title_stripped") if pane and not t.get("placeholder") else None)
            if title and title.lower() not in UNTITLED and title != t.get("title"):
                t["title"] = title
                if t.get("placeholder"):
                    t["name"], t["placeholder"] = title[:80], False
                    redraw.append(dict(t))
                if native_ok:
                    titles.append((ts, dict(t), title))
            if claude:
                track_prompt(s, ts, t)
                track_turns(s, ts, t)
        threads = dict(s["threads"])
        acks = {k: dict(v) for k, v in s["acks"].items()}
    for t in redraw:
        if not t.get("card_ts"):
            continue
        try:
            web.chat_update(channel=t["channel"], ts=t["card_ts"], blocks=card_blocks(t, t["status"]),
                            text=f"{t['name']}: {STATUS[t['status']]}")
        except Exception as e:
            log.warning("card update %s failed: %s", t["card_ts"], e)
    for args in statuses:
        native_status(*args)
    for args in titles:
        native_title(*args)
    open_sessions(panes)
    done = []
    for key, a in acks.items():  # Slack calls happen outside the state lock; results are merged back below
        pane = match_pane(panes, threads.get(a["thread"], {}))
        st = pane.get("agent_status") if pane else None
        t = threads.get(a["thread"], {})
        ended = plans.turn_end(a["sid"], a["turn"]) is not None if a.get("turn") is not None else None
        if ended is None:  # no transcript to read (Codex): fall back to the pane going idle
            ended = st in ("idle", "done") and (a["seen_working"] or now() - a["since"] > 4 * POLL)
        if pane is not None and not ended:
            a["seen_working"] = True
            mirror(a, t, final=False)
        elif pane is None:
            for ts in filter(None, [a["ts"], *a.get("also", [])]):  # typed-in-terminal turns have no Slack message
                react(a["channel"], ts, None, remove="eyes")
            done.append(key)
        elif ended:
            mirror(a, t, final=True)
            for ts in filter(None, [a["ts"], *a.get("also", [])]):
                react(a["channel"], ts, "white_check_mark", remove="eyes")
            done.append(key)
    with state() as s:
        for ts, a in acks.items():
            if ts in s["acks"]:
                # deliver() may have added a message meanwhile
                a["also"], a["moves"] = s["acks"][ts].get("also", []), s["acks"][ts].get("moves", 0)
                s["acks"][ts] = a
        for ts in done:
            s["acks"].pop(ts, None)


def track_prompt(s, ts, t):
    """A prompt typed in the terminal starts a mirrored turn, like a Slack reply does (caller holds the state)."""
    n, prompt, base = plans.last_prompt(t["session_id"])
    if n <= t.setdefault("seen_prompts", n):  # first sight: history isn't replayed
        return
    t["seen_prompts"] = n
    if prompt.startswith(PROMPT_TAG) or pending_turn(s, ts):
        return  # deliver() already opened this turn, or it was typed while the turn was still running
    turn = plans.turn_of_prompt(t["session_id"])
    if any(a["thread"] == ts and a.get("turn") == turn for a in s["acks"].values()):
        return  # track_turns() already picked this turn up from its narration
    s["acks"][f"local:{ts}:{n}"] = {"channel": t["channel"], "ts": None, "thread": ts, "since": now(),
                                    "seen_working": False, "sid": t["session_id"], "base": base,
                                    "progress_ts": None, "last_edit": 0, "prompt": prompt, "turn": turn}
    t["next_turn"] = max(t.get("next_turn", 0), turn + 1)


def track_turns(s, ts, t):
    """Mirror every turn, not only prompted ones: background agents, Monitor events and task notifications wake
    Claude too. A turn not yet covered gets a mirror once it has narration or has ended (caller holds the state)."""
    sid = t["session_id"]
    ended = plans.turns_ended(sid)
    k = t.setdefault("next_turn", ended)  # first sight: earlier turns aren't replayed
    while k <= ended:
        start = plans.turn_start(sid, k)
        if k == ended and plans.texts_since(sid, 0)[0] <= start:
            break  # nothing running, or the running turn hasn't said anything yet
        if not any(a["thread"] == ts and a.get("turn") == k for a in s["acks"].values()):
            label = plans.trigger(sid, k)
            s["acks"][f"turn:{ts}:{k}"] = {"channel": t["channel"], "ts": None, "thread": ts, "since": now(),
                                           "seen_working": False, "sid": sid, "base": start, "progress_ts": None,
                                           "last_edit": 0, "turn": k, "label": label}
        k += 1
    t["next_turn"] = k


def pending_turn(s, ts):
    """The thread's mirror whose Claude turn hasn't ended yet (one whose turn ended is only waiting to post)."""
    for a in s["acks"].values():
        if a["thread"] == ts and (a.get("turn") is None or plans.turn_end(a["sid"], a["turn"]) is None):
            return a
    return None


def mirror(a, t, final):
    """Show the agent's narration for a turn in the thread, Devin style.

    While it works: one progress message, edited at most every MIRROR_EVERY s, with its latest updates.
    When it settles: its last message as a fresh reply (so Slack notifies), replacing the progress message,
    unless the agent already answered with slack-ping during the turn."""
    if not a.get("sid"):
        return
    stop = plans.turn_end(a["sid"], a["turn"]) if a.get("turn") is not None else None
    new = plans.texts_between(a["sid"], a["base"], stop)  # never the next turn's narration
    kw = {"channel": a["channel"], "username": t.get("name", "agent"), "icon_emoji": ICONS.get(t.get("agent"))}
    head = f"💬 {a['prompt'][:280]}" if a.get("prompt") else f"⚙️ {a['label'][:280]}" if a.get("label") else None
    asked = [{"type": "context", "elements": [{"type": "mrkdwn", "text": head}]}] if head else []
    try:
        if final:
            if a["progress_ts"]:
                try:
                    web.chat_delete(channel=a["channel"], ts=a["progress_ts"])
                except SlackApiError as e:  # already gone must not cost the owner the reply
                    log.warning("progress delete %s failed: %s", a["progress_ts"], e.response.get("error"))
            if new and t.get("last_ping", 0) <= a["since"]:
                web.chat_postMessage(thread_ts=a["thread"], text=new[-1][:300], blocks=asked + md_blocks(new[-1]),
                                     **kw)
            return
        if a.get("moves", 0) > a.get("moved", 0):  # the owner wrote after it: keep the live message at the bottom
            a["moved"] = a["moves"]
            if a["progress_ts"]:
                try:
                    web.chat_delete(channel=a["channel"], ts=a["progress_ts"])
                except SlackApiError as e:  # already gone is fine; a fresh one is posted either way
                    log.warning("progress delete %s failed: %s", a["progress_ts"], e.response.get("error"))
                a["progress_ts"], a["shown"], a["last_edit"] = None, None, 0
        if not new or len(new) == a.get("shown"):
            return  # nothing new to show: no Slack call
        if now() - a["last_edit"] < MIRROR_EVERY:
            global mirror_deferred
            mirror_deferred = True
            return
        steps = "\n".join(f"• {x.splitlines()[0][:300]}" for x in new[-5:])
        working = f"⏳ Working · {len(new)} updates"
        blocks = asked + [{"type": "context", "elements": [{"type": "mrkdwn", "text": working}]},
                          {"type": "markdown", "text": steps}]
        if a["progress_ts"]:
            web.chat_update(ts=a["progress_ts"], text="Working…", blocks=blocks, channel=a["channel"])
        else:
            a["progress_ts"] = web.chat_postMessage(thread_ts=a["thread"], text="Working…", blocks=blocks, **kw)["ts"]
        a["last_edit"], a["shown"] = now(), len(new)
    except SlackApiError as e:
        log.warning("mirror %s failed: %s", a["thread"], e.response.get("error"))


def open_sessions(panes):
    """Give every herdr agent session without a thread its own Slack session as soon as it starts."""
    with state() as s:
        threads = list(s["threads"].values())
    for pane in panes:
        title = pane.get("terminal_title_stripped") or ""
        if pane["agent"] not in ICONS or any(match_pane([pane], t) for t in threads):
            continue
        st = {"working": "working", "blocked": "blocked"}.get(pane.get("agent_status"), "idle")
        sess = (pane.get("agent_session") or {}).get("value")
        if pane["agent"] == "claude":  # its transcript title; the terminal's may still be the pre-/clear one
            title = plans.title(sess) if sess else ""
        elif any(t.get("terminal_id") == pane.get("terminal_id") for t in threads):
            title = ""
        placeholder = title.lower() in UNTITLED
        name = f"New {pane['agent'].capitalize()} session" if placeholder else title[:80]
        t = {"name": name, "placeholder": placeholder, "late_name": placeholder,
             "title": None if placeholder else title, "where": where_label(pane), "text": None, "status": st,
             "created": now(), "channel": dm_channel, "mode": "pane", "needs_you": False, "agent": pane["agent"],
             "pane_id": pane["pane_id"], "terminal_id": pane.get("terminal_id"), "session_id": sess}
        t["plan"] = plans.tasks(sess) if pane["agent"] == "claude" and sess else []
        msg = web.chat_postMessage(channel=dm_channel, text=f"{t['name']}: {STATUS[st]}", blocks=card_blocks(t, st),
                                   username=t["name"], icon_emoji=ICONS[pane["agent"]])
        t["card_ts"] = msg["ts"]
        with state() as s:
            s["threads"][msg["ts"]] = t
        threads.append(t)
        native_status(msg["ts"], t, st)
        log.info("opened session %s for %s (%s)", msg["ts"], name, sess)


def on_stop(ev):
    """Slack's stop button on a session: interrupt the agent like pressing Esc in its pane."""
    ts = ev.get("thread_ts") or (ev.get("session") or {}).get("thread_ts")
    with state() as s:
        t = s["threads"].get(ts)
    pane = match_pane(agent_panes(), t) if t else None
    if pane is None:
        log.warning("stop for unknown or ended session: %s", ev)
        return
    ok, res = herdr("agent", "send-keys", pane["pane_id"], "esc")
    note(t["channel"], ts, "⏹ Interrupted (sent Esc)." if ok else f"Couldn't interrupt: {herdr_error(res)}")


def status_report(channel, ts):
    """Reply to 'status': every live herdr agent, whether or not it has a Slack thread."""
    rows = ["| agent | state | where |", "|---|---|---|"]
    for p in agent_panes() or []:
        title = (p.get("terminal_title_stripped") or p["agent"]).replace("|", "/")
        rows.append(f"| {title} | {p.get('agent_status', '?')} | {p['pane_id']} · {p.get('agent')} |")
    web.chat_postMessage(channel=channel, thread_ts=ts, text="Agent status",
                         blocks=[{"type": "markdown", "text": "\n".join(rows)}])


HELP = """**herdr agents**
- Any new message starts a Claude in a new herdr tab (first word may be a directory: `~/src/app fix the lint`).
- Reply in a thread to talk to that agent; ⏹ stop interrupts it.
- `status` lists every live agent. Agents reach you with `slack-ping`."""


def hook_listener():
    try:
        for _ in wake.watch():
            wakeup.set()
    except OSError:
        log.exception("hook wake-ups unavailable; polling every %s s only", POLL)


def poller():
    global mirror_deferred
    looks = []  # pending follow-up times after the last hook
    while True:
        mirror_deferred = False
        try:
            poll_once()
        except Exception:
            log.exception("poll failed")
        timeout = MIRROR_EVERY if mirror_deferred else POLL
        if looks:
            timeout = min(timeout, max(0.0, looks[0] - now()))
        if wakeup.wait(timeout):
            wakeup.clear()
            looks = [now() + d for d in FOLLOW_UPS]
        looks = [x for x in looks if x > now() + 0.01]


def guarded(fn, *args, channel=None, ts=None):
    try:
        fn(*args)
    except Exception:
        log.exception("%s failed", fn.__name__)
        if channel and ts:
            react(channel, ts, "x")


def on_request(client: SocketModeClient, req: SocketModeRequest):
    client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
    if req.type == "interactive" and req.payload.get("type") == "block_actions":
        threading.Thread(target=guarded, args=(on_button, req.payload), daemon=True).start()
        return
    if req.type != "events_api":
        return
    ev = req.payload.get("event", {})
    if ev.get("type") == "agent_session_stopped":
        threading.Thread(target=guarded, args=(on_stop, ev), daemon=True).start()
        return
    if (ev.get("type") != "message" or ev.get("channel_type") != "im" or ev.get("subtype") not in (None, "file_share")
            or ev.get("user") != cfg["OWNER_SLACK_USER"] or ev["ts"] in seen):
        return
    seen.add(ev["ts"])
    threading.Thread(target=guarded, args=(on_message, ev), kwargs={"channel": ev["channel"], "ts": ev["ts"]},
                     daemon=True).start()


if __name__ == "__main__":
    own = open(STATE_DIR / "relay.py.lock", "a")  # an orphan relay whose supervisor died keeps this, so no twin
    try:
        fcntl.flock(own, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("another relay.py is already connected (orphaned?); stop it first")
    log_file = STATE_DIR / "relay.log"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.handlers.RotatingFileHandler(log_file, maxBytes=5 << 20, backupCount=2)])
    sm = SocketModeClient(app_token=cfg["SLACK_APP_TOKEN"], web_client=web)
    sm.socket_mode_request_listeners.append(on_request)
    sm.connect()
    log.info("connected")
    (STATE_DIR / "relay.connected").write_text(f"{os.getpid()} {time.strftime('%F %T')}\n")
    threading.Thread(target=hook_listener, daemon=True).start()
    threading.Thread(target=poller, daemon=True).start()
    threading.Event().wait()
