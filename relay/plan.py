"""Follow a Claude Code transcript: task list (TaskCreate/TaskUpdate), narration, session title and typed prompts."""
import base64
import html
import binascii
import json
import os
import re
from pathlib import Path

PROJECTS = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
WANTED = (b'"assistant"', b'"user"', b'-title"', b'"turn_duration"', b'<task-notification>')  # cheap line prefilter
SLACK_STATUS = {"pending": "pending", "in_progress": "in_progress", "completed": "complete"}


def is_narration(block):
    """Mid-turn narration shown in the terminal is stored as a thinking block whose signature is tagged
    'narration'; private reasoning is a thinking block with empty text."""
    sig = block.get("signature", "")[:48]
    try:
        return b"narration" in base64.b64decode(sig + "=" * (-len(sig) % 4))
    except (binascii.Error, ValueError):
        return False


class PlanTracker:
    """Incremental per-transcript reader: only bytes appended since the last call are parsed."""

    def __init__(self):
        # session id -> {"path", "offset", "tasks": {id: {...}}, "texts": [str], "n_texts": int, "title": str,
        #                "n_prompts": int, "prompt": str, "prompt_base": n_texts when the last prompt was typed,
        #                "end_texts": [n_texts at the end of each finished turn], "prompt_turn": its index,
        #                "trigger": label of the last background event (task/Monitor notification), "trigger_turn"}
        self.files = {}

    def _update(self, session_id):
        f = self.files.get(session_id)
        if f is None:
            hits = list(PROJECTS.glob(f"*/{session_id}.jsonl"))
            if not hits:
                return None
            f = self.files[session_id] = {"path": hits[0], "offset": 0, "tasks": {}, "texts": [], "n_texts": 0,
                                          "title": "", "n_prompts": 0, "prompt": "", "prompt_base": 0,
                                          "end_texts": [], "prompt_turn": 0, "trigger": "", "trigger_turn": -1}
        with open(f["path"], "rb") as fh:
            fh.seek(f["offset"])
            chunk = fh.read()
        end = chunk.rfind(b"\n") + 1  # leave a half-written last line for next time
        f["offset"] += end
        for line in chunk[:end].splitlines():
            if any(k in line for k in WANTED):
                self._apply(f, line)
        return f

    def tasks(self, session_id):
        f = self._update(session_id)
        return [{"task_id": k, "title": v["title"][:150], "status": SLACK_STATUS[v["status"]]}
                for k, v in (f["tasks"] if f else {}).items() if v["status"] in SLACK_STATUS]

    def texts_since(self, session_id, n):
        """(total narration count, texts after the first n); only the last 20 are kept in memory."""
        f = self._update(session_id)
        if f is None:
            return 0, []
        new = f["n_texts"] - n
        return f["n_texts"], f["texts"][-new:] if new > 0 else []

    def texts_between(self, session_id, start, stop=None):
        """Narration texts with index in [start, stop); only the last 20 are kept in memory."""
        f = self._update(session_id)
        if f is None:
            return []
        first = f["n_texts"] - len(f["texts"])
        stop = f["n_texts"] if stop is None else min(stop, f["n_texts"])
        return f["texts"][max(start, first) - first:max(stop, first) - first]

    def turns_ended(self, session_id):
        """Finished turns so far (Claude writes a turn_duration record when a turn ends)."""
        f = self._update(session_id)
        return len(f["end_texts"]) if f else 0

    def turn_end(self, session_id, k):
        """Narration count when turn number k (0-based, counting finished turns) ended, or None if it hasn't."""
        f = self._update(session_id)
        return f["end_texts"][k] if f and k < len(f["end_texts"]) else None

    def turn_start(self, session_id, k):
        """Narration count when turn k started (the end of turn k-1)."""
        f = self._update(session_id)
        return f["end_texts"][k - 1] if f and 0 < k <= len(f["end_texts"]) else 0

    def trigger(self, session_id, k):
        """What woke turn k if it was a background event, else ''."""
        f = self._update(session_id)
        return f["trigger"] if f and f["trigger_turn"] == k else ""

    def turn_of_prompt(self, session_id):
        """Index of the turn the last typed prompt started (finished turns before it)."""
        f = self._update(session_id)
        return f["prompt_turn"] if f else 0

    def title(self, session_id):
        """The session's own title (Claude's auto-title or /rename), which the terminal title lags behind."""
        f = self._update(session_id)
        return f["title"] if f else ""

    def last_prompt(self, session_id):
        """(prompts typed so far, the last one, narration count when it was typed)."""
        f = self._update(session_id)
        return (f["n_prompts"], f["prompt"], f["prompt_base"]) if f else (0, "", 0)

    @staticmethod
    def _apply(f, line):
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            return
        if d.get("type") == "system" and d.get("subtype") == "turn_duration" and not d.get("isSidechain"):
            f["end_texts"].append(f["n_texts"])
            return
        if d.get("type") == "queue-operation" and str(d.get("content", "")).startswith("<task-notification>"):
            c = d["content"]
            summary = re.search(r"<summary>(.*?)</summary>", c, re.S)
            event = re.search(r"<event>(.*?)</event>", c, re.S)
            label = " · ".join(html.unescape(x.group(1).strip()) for x in (summary, event) if x)
            f["trigger"] = label or "background event"
            f["trigger_turn"] = len(f["end_texts"])  # wakes the next turn (or joins the running one)
            return
        if d.get("type") in ("ai-title", "custom-title"):
            f["title"] = d.get("customTitle") or d.get("aiTitle") or f["title"]
            return
        if d.get("type") == "user" and not d.get("isMeta") and not d.get("isSidechain"):
            content = d.get("message", {}).get("content")
            if isinstance(content, list):
                content = " ".join(c.get("text", "") for c in content if c.get("type") == "text")
            if isinstance(content, str) and content.startswith("[Request interrupted"):
                f["end_texts"].append(f["n_texts"])  # Esc ends the turn without a turn_duration record
            # tool results carry no text; command echoes start with '<'
            elif isinstance(content, str) and content.strip() and not content.startswith("<"):
                f["n_prompts"] += 1
                f["prompt"], f["prompt_base"], f["prompt_turn"] = content.strip(), f["n_texts"], len(f["end_texts"])
            return
        if d.get("type") != "assistant" or d.get("isSidechain"):
            return
        content = d.get("message", {}).get("content")
        for item in content if isinstance(content, list) else []:
            text = item.get("text") if item.get("type") == "text" else \
                item.get("thinking") if item.get("type") == "thinking" and is_narration(item) else None
            if text and text.strip():
                f["texts"] = (f["texts"] + [text.strip()])[-20:]
                f["n_texts"] += 1
            if item.get("type") != "tool_use":
                continue
            inp, tasks = item.get("input", {}), f["tasks"]
            if item.get("name") == "TaskCreate":  # ids are handed out sequentially per session
                tasks[str(len(tasks) + 1)] = {"title": inp.get("subject", ""), "status": "pending"}
            elif item.get("name") == "TaskUpdate" and str(inp.get("taskId")) in tasks:
                t = tasks[str(inp["taskId"])]
                t["status"] = inp.get("status", t["status"])
                t["title"] = inp.get("subject", t["title"])
