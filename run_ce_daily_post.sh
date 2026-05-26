#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

HOME="/home/users/hikaru.inoue"
VENV_PYTHON="$HOME/.venv_$(uname -m)/bin/python3"

cd /lvs0/dne1/rccs-nghpcadu/hikaru.inoue/AwsBedrockCostAggregation

if [ -f "$HOME/.secrets/slack_tokens.sh" ]; then
    # shellcheck disable=SC1090
    source "$HOME/.secrets/slack_tokens.sh"
fi

LOG_DIR="./logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/ce_daily_post-$(TZ=Asia/Tokyo date +%Y-%m-%d).log"
{
    echo "===== $(TZ=Asia/Tokyo date +%Y-%m-%dT%H:%M:%S%z) ====="
    rc=0
    $VENV_PYTHON ./bedrock_ce_daily_post.py --config ./config.yaml "$@" || rc=$?
    echo "----- exit=$rc -----"
    exit "$rc"
} >> "$LOG_FILE" 2>&1
