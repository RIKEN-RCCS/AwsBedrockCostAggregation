#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

# SLACK_BOT_TOKEN を読み込み (~/.secrets/slack_tokens.sh, chmod 600 推奨)
if [ -f "$HOME/.secrets/slack_tokens.sh" ]; then
    # shellcheck disable=SC1090
    source "$HOME/.secrets/slack_tokens.sh"
fi

exec python3 bedrock_daily_alert.py --config ./config.yaml "$@"
