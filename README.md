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
3. Setup (prompts for the tokens, checks them with Slack, adds the Claude hook, links `~/.local/bin/slack-ping`,
   installs the `slack-ping` skill, starts the relay):

   ```
   herdr plugin pane open --plugin densumesh.slack-agents --entrypoint setup     # popup in herdr
   # or non-interactively:
   ~/herdr-slack-agents/bin/setup --bot-token xoxb-... --app-token xapp-... --owner U012ABC \
       [--owner-name Sam] [--claude-args="--dangerously-skip-permissions"]
   ```

4. DM the bot `help`.

## Operate

```
herdr plugin action invoke densumesh.slack-agents.status     # also .restart, .stop
```

The relay is a detached process (one per user, `STATE/relay.lock`) restarted 10 s after any crash; herdr's
startup hook (`bin/relay-ctl ensure`) starts it with the herdr server and restarts it after a plugin update.
Config: `~/.config/herdr/plugins/config/densumesh.slack-agents/tokens.env` (0600; `CLAUDE_ARGS` = extra args for
Claudes started from a DM, `OWNER_NAME` = name in the `[Slack from NAME, thread TS]` prompt prefix).
State and logs: `~/.local/state/herdr/plugins/densumesh.slack-agents/` (`relay.log`, `relay.stderr`, `state.json`).

Only the configured owner's messages are acted on. Anyone holding the bot token can post as the bot; anyone who can
DM as the owner can run Claude on this machine, with `CLAUDE_ARGS` permissions.

## Uninstall

`herdr plugin action invoke densumesh.slack-agents.stop`, `herdr plugin uninstall densumesh.slack-agents` (or `unlink`),
remove the `slack-wake.sh` entries from `~/.claude/settings.json` and `~/.local/bin/slack-ping`.
