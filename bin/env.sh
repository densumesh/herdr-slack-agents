# Sourced by the plugin's scripts: plugin root and the config/state dirs (herdr's plugin dirs unless overridden).
ROOT=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
PLUGIN_ID=densumesh.slack-agents
CONFIG=${SLACK_AGENTS_CONFIG_DIR:-${HERDR_PLUGIN_CONFIG_DIR:-$HOME/.config/herdr/plugins/config/$PLUGIN_ID}}
STATE=${SLACK_AGENTS_STATE_DIR:-${HERDR_PLUGIN_STATE_DIR:-$HOME/.local/state/herdr/plugins/$PLUGIN_ID}}
export SLACK_AGENTS_CONFIG_DIR="$CONFIG" SLACK_AGENTS_STATE_DIR="$STATE"
PY="$ROOT/.venv/bin/python"
