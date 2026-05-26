#!/usr/bin/env python3
"""AWS Cost Explorer から当日 (JST) の Bedrock コストを取得し Slack へ投稿する。

既存の S3 ログ集計 (bedrock_daily_alert.py) とは独立し、Cost Explorer API
(SERVICE = Amazon Bedrock) の集計値をそのまま投稿する。usage type 別の内訳も
併記する。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import boto3
import yaml
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

JST = ZoneInfo("Asia/Tokyo")
# Cost Explorer の SERVICE ディメンションでは、Bedrock 利用は
# "Claude Opus 4.7 (Amazon Bedrock Edition)" のようにモデル別の独立サービスとして現れる。
# そのため "Amazon Bedrock" 単体ではマッチせず、この文字列を含む SERVICE を全て拾う。
BEDROCK_SERVICE_MARKER = "Amazon Bedrock"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default="config.yaml", help="YAML 設定 (Slack 投稿先)")
    p.add_argument("--date", help="集計対象 JST 日付 (YYYY-MM-DD, 既定: 前日)")
    p.add_argument("--dry-run", action="store_true", help="Slack 投稿せず stderr に出力")
    return p.parse_args()


def jst_yesterday() -> datetime:
    """JST の前日 0:00。月またぎは timedelta が吸収する。"""
    today = datetime.now(JST).replace(hour=0, minute=0, second=0, microsecond=0)
    return today - timedelta(days=1)


def jst_day_to_utc_range(jst_day: datetime) -> tuple[datetime, datetime]:
    start = jst_day.astimezone(timezone.utc)
    end = (jst_day + timedelta(days=1)).astimezone(timezone.utc)
    return start, end


def fetch_bedrock_cost(jst_day: datetime) -> dict:
    """Cost Explorer から当日 Bedrock のコストを取得する。

    Cost Explorer は UTC 日次粒度しか取れないため、JST 日付に対応する 2 つの UTC 日
    (例: JST 2026-05-26 → UTC 2026-05-25 と 2026-05-26) を取得し、JST 範囲に重なる
    時間比率で按分はせず、両 UTC 日の合計を返す近似運用とする。

    SERVICE 名で `Amazon Bedrock` を含むものを全てモデル別に集計する
    (例: "Claude Opus 4.7 (Amazon Bedrock Edition)")。
    """
    utc_start, utc_end = jst_day_to_utc_range(jst_day)
    start_date = utc_start.date().isoformat()
    end_date = (utc_end.date() + timedelta(days=1)).isoformat()

    ce = boto3.client("ce", region_name="us-east-1")
    resp = ce.get_cost_and_usage(
        TimePeriod={"Start": start_date, "End": end_date},
        Granularity="DAILY",
        Metrics=["UnblendedCost"],
        GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
    )

    target_utc_dates = {utc_start.date().isoformat(), utc_end.date().isoformat()}
    by_service: dict[str, float] = {}
    total = 0.0
    currency = "USD"
    for result in resp.get("ResultsByTime", []):
        if result["TimePeriod"]["Start"] not in target_utc_dates:
            continue
        for g in result.get("Groups", []):
            service = g["Keys"][0]
            if BEDROCK_SERVICE_MARKER not in service:
                continue
            amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
            currency = g["Metrics"]["UnblendedCost"]["Unit"]
            by_service[service] = by_service.get(service, 0.0) + amount
            total += amount

    return {
        "currency": currency,
        "total": total,
        "by_service": dict(sorted(by_service.items(), key=lambda x: -x[1])),
        "utc_dates": sorted(target_utc_dates),
    }


def format_message(jst_day: datetime, cost: dict, usd_to_jpy: float | None) -> str:
    date_str = jst_day.strftime("%Y-%m-%d")
    total = cost["total"]
    cur = cost["currency"]
    head = f"*AWS Bedrock 日次コスト ({date_str} JST)*"
    jpy_suffix = f" / ¥{total * usd_to_jpy:,.0f}" if usd_to_jpy else ""
    summary = f"合計: ${total:,.4f} {cur}{jpy_suffix}"
    src = f"出典: AWS Cost Explorer (UTC {', '.join(cost['utc_dates'])} の合計)"

    if not cost["by_service"]:
        return "\n".join([head, summary, "_当日該当の Bedrock 利用は未集計です_", src])

    lines = ["内訳 (モデル別):"]
    for service, amount in cost["by_service"].items():
        if amount < 0.0001:
            continue
        lines.append(f"  • `{service}`: ${amount:,.4f}")
    return "\n".join([head, summary, "", *lines, "", src])


def post_to_slack(token: str, channel: str, text: str, username: str | None,
                  icon_emoji: str | None) -> None:
    client = WebClient(token=token)
    kwargs: dict = {"channel": channel, "text": text}
    if username:
        kwargs["username"] = username
    if icon_emoji:
        kwargs["icon_emoji"] = icon_emoji
    try:
        client.chat_postMessage(**kwargs)
    except SlackApiError as e:
        print(f"❌ Slack 投稿失敗: {e.response['error']}", file=sys.stderr)
        raise


def main() -> int:
    args = parse_args()

    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    slack_cfg = cfg.get("slack", {})
    channel = slack_cfg.get("channel_id")
    if not channel:
        print("❌ config.yaml に slack.channel_id が必要です", file=sys.stderr)
        return 1

    if args.date:
        jst_day = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=JST)
    else:
        jst_day = jst_yesterday()

    print(f"📅 集計対象 (JST): {jst_day.date().isoformat()}", file=sys.stderr)
    cost = fetch_bedrock_cost(jst_day)
    print(f"💰 total=${cost['total']:.4f} services={len(cost['by_service'])} "
          f"utc_dates={cost['utc_dates']}", file=sys.stderr)

    msg = format_message(jst_day, cost, cfg.get("usd_to_jpy"))

    if args.dry_run:
        print("--- DRY RUN ---", file=sys.stderr)
        print(msg, file=sys.stderr)
        return 0

    token = os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        print("❌ env SLACK_BOT_TOKEN is required", file=sys.stderr)
        return 1

    post_to_slack(token, channel, msg, slack_cfg.get("username"),
                  slack_cfg.get("icon_emoji"))
    print("✅ Slack 投稿完了", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
