# Slack Agents — herdr plugin

Drive your herdr Claude/Codex agents from a Slack DM with your own bot:

- every herdr agent session gets its own Slack thread with a live status card (working / waiting / stuck on an
  approval / idle / ended) and the Claude task list;
- each Claude turn is mirrored into its thread (live "Working" message, then the final reply), including prompts
  typed in the terminal;
- your thread reply is typed into that agent's pane (Ctrl+B first if it is blocked on a foreground tool); buttons
  work the same way; Slack's stop button sends Esc;
- a top-level DM starts a new Claude in a new herdr tab (first word may be a directory); `status` lists agents;
- agents and scripts reach you with `slack-ping` (Markdown, tables, images/files, buttons, `--wait`).

Linux only (inotify, `setsid`, `flock`). Needs herdr >= 0.9.0, python3 with venv, and herdr's Claude
integration (`herdr integration install claude`).

## Install

1. Slack app: api.slack.com/apps -> Create New App -> From a manifest -> paste `slack-app-manifest.yaml`.
   Install it to the workspace. Copy the **Bot User OAuth Token** (`xoxb-...`), and create an **app-level token**
   with `connections:write` (`xapp-...`). Your member id: Slack profile -> ⋯ -> Copy member ID.
2. Plugin, from GitHub: `herdr plugin install densumesh/herdr-slack-agents --yes` (herdr builds the venv),
   or from any git host: `git clone https://github.com/densumesh/herdr-slack-agents ~/herdr-slack-agents && herdr plugin link ~/herdr-slack-agents`.
3. Setup: prompts for the tokens and checks them with Slack, asks whether Claudes started from a DM may skip
   permission prompts, adds the Claude hook, links `~/.local/bin/slack-ping`, installs the `slack-ping` skill,
   then starts the relay and waits until it is connected:

   ```
   herdr plugin pane open --plugin densumesh.slack-agents --entrypoint setup     # popup in herdr
   ~/herdr-slack-agents/bin/setup                                               # or from a shell (linked copy)
   ```

   Non-interactive: put the tokens in the environment (`SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`; never on the command
   line) and pass `--owner U012ABC [--owner-name Sam] [--claude-args=--dangerously-skip-permissions]`.

4. DM the bot `help`.

Use one Slack app per person and host: two relays on one app token split Slack's events between them. On a
home directory shared by several hosts, install and run setup on one host; the others leave its relay alone.

## Operate

```
herdr plugin action invoke densumesh.slack-agents.status     # connected? last log lines; also .restart, .stop
```

- The relay is a detached process, one per user (`relay.lock`; on a shared NFS home the first host to start it
  wins and `status` names that host). It is restarted after a crash (10 s, backing off to 5 min while it keeps
  failing); `status` says "NOT connected" while it fails.
- herdr's startup hook (`bin/relay-ctl ensure`) starts it with the herdr server and restarts it when the plugin
  code changed or its herdr socket is gone. After `herdr plugin update` or a `git pull` of a linked copy, run the
  `restart` action (or restart herdr).
- One herdr server per user: the relay talks to the server that started it.
- `stop` lasts until the next herdr server start.
- Config: `~/.config/slack-agents/tokens.env` (0600; `CLAUDE_ARGS`, `OWNER_NAME` = the name in the
  `[Slack from NAME, thread TS]` prompt prefix). Rerun setup with `--reset-tokens` to change tokens.
- State and logs (0700): `~/.local/state/slack-agents/` (`relay.log`, `relay.stderr`, `state.json`, `inbox/`).
- Wake-ups ride on `$XDG_RUNTIME_DIR/slack-relay`; without it the relay falls back to a 30 s poll.

Only the owner can steer agents: messages, button taps and stop requests are acted on only when they come from
`OWNER_SLACK_USER` in that user's own DM with the bot; anything else (other people DMing the bot, channels, other
DMs) is dropped and logged. Config and state are readable only by you. Anyone holding the bot token can post as the bot;
anyone who can send Slack messages as the owner can run Claude on this machine with `CLAUDE_ARGS` permissions.

## Uninstall

`bin/setup --uninstall` (stops the relay, removes the Claude hooks and `slack-ping`), then
`herdr plugin uninstall densumesh.slack-agents` (or `unlink`). Config and state dirs are left for you to delete.
