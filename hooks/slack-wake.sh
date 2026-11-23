#!/bin/sh
# Claude Code hook: wake the Slack relay (its inotify watch is on this dir). Must never fail or block: the relay
# may be down, and exit 2 from a PreToolUse/Stop hook blocks the tool / the turn end.
d="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/slack-relay"
[ -d "$d" ] && : > "$d/wake" 2>/dev/null
exit 0
