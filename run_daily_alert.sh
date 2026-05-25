#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

HOME="/lvs0/rccs-nghpcadu/hikaru.inoue"

# SLACK_BOT_TOKEN を読み込み (~/.secrets/slack_tokens.sh, chmod 600 推奨)
if [ -f "$HOME/.secrets/slack_tokens.sh" ]; then
    # shellcheck disable=SC1090
    source "$HOME/.secrets/slack_tokens.sh"
fi

# JST 日付ごとのログファイルに stdout/stderr を追記
LOG_DIR="./logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/daily_alert-$(TZ=Asia/Tokyo date +%Y-%m-%d).log"
{
    echo "===== $(TZ=Asia/Tokyo date +%Y-%m-%dT%H:%M:%S%z) ====="
    rc=0
    python3 bedrock_daily_alert.py --config ./config.yaml "$@" || rc=$?
    echo "----- exit=$rc -----"
    exit "$rc"
} >> "$LOG_FILE" 2>&1
