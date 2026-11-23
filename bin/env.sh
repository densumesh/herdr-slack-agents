# Sourced by the plugin's scripts: plugin root and the config/state dirs (same defaults as relay/common.py).
ROOT=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
CONFIG=${SLACK_AGENTS_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/slack-agents}
STATE=${SLACK_AGENTS_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/slack-agents}
export SLACK_AGENTS_CONFIG_DIR="$CONFIG" SLACK_AGENTS_STATE_DIR="$STATE"
PY="$ROOT/.venv/bin/python"
umask 077
