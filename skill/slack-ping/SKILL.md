---
name: slack-ping
description: Message the user on Slack (phone push) from any agent or script on this host and get their reply back - one thread per agent session under a live status card, Markdown (tables, code), inline images/files (--file), quick-reply buttons (--choices), blocking questions for scripts (--wait). Their thread reply arrives as your next prompt. Use when long work they are waiting on finishes, you are blocked, you need their OK, or they ask you to ping/notify/message them; also explains prompts that start "[Slack from NAME, thread ...]".
---

# slack-ping

Sent by the user's Slack bot from the herdr plugin `densumesh.slack-agents`; works on the host where its relay runs.

```
slack-ping "**bench done**: 7.23 ms, matches the reference"        # Markdown; prints thread ts
slack-ping --file /tmp/plot.png "latency vs batch"                 # images inline, other files attached
slack-ping --choices "Push,Hold" "CI is green. Push?"              # buttons; the tap arrives as a reply
ans=$(slack-ping --wait --timeout 1800 "Delete the old cache? (y/n)")   # scripts: blocks, exit 3 on timeout
```

- Every herdr agent session (Claude, Codex) gets its own Slack thread with a live status card within seconds of
  starting (new tab or `/clear`). Every Claude turn is mirrored there, including prompts typed in the terminal, so
  your pings from that session land in the same thread.
- Their reply comes in as a prompt `[Slack from NAME, thread TS] ...` (attached files arrive as local paths). Treat it
  as their instruction and just answer normally: the relay mirrors your turn into the thread. Use `slack-ping` during
  such a turn only for files, buttons or a separate later update. After pinging, end your turn instead of polling;
  the reply wakes you.
- A top-level DM to the bot starts a new Claude in a new herdr tab (first word may be a directory). `status` and
  `help` are answered by the relay; Slack's stop button sends Esc to the agent.
- Keep pings phone-sized: outcome first, numbers with how they were checked, then the one question. Finished work,
  blockers and OK-requests only, no progress chatter. Never put secrets in a ping.
- Relay: `herdr plugin action invoke densumesh.slack-agents.status` (or `.restart`); log in
  `~/.local/state/herdr/plugins/densumesh.slack-agents/relay.log`.
