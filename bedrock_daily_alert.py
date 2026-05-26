#!/usr/bin/env python3
"""
Bedrock 日次コストアラート (Slack 通知)

JST 当日のユーザー別コストを段階しきい値で判定し、超過した段階ごとに Slack に通知する。
毎時 cron 起動を想定し、同一 (date, identity_arn, tier) には 1 日 1 通までに抑制する。
($10, $100, $1000 と段階を上昇すれば 1 日に最大 3 通通知される)

設定は config.yaml に集中管理。Slack Bot Token は env SLACK_BOT_TOKEN で渡す。

使い方:
    python bedrock_daily_alert.py --config config.yaml [--date YYYY-MM-DD]
                                  [--dry-run] [--force-notify]
"""

import argparse
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from bedrock_cost_report import (
    calculate_cost,
    ingest_logs,
    init_db,
)
from resolve_owner import build_owner_map, resolve_username


JST = ZoneInfo("Asia/Tokyo")
SLACK_SECTION_LIMIT = 2900  # Block Kit section の安全上限 (3000 字)


# ============================================================
# 設定読込
# ============================================================
def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"❌ config not found: {path}")
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    required_top = ["bucket", "region", "db_path"]
    missing = [k for k in required_top if k not in cfg]
    if "slack" not in cfg or "channel_id" not in (cfg.get("slack") or {}):
        missing.append("slack.channel_id")
    if "daily_threshold_tiers_usd" not in cfg and "daily_threshold_usd" not in cfg:
        missing.append("daily_threshold_tiers_usd")
    if missing:
        sys.exit(f"❌ config missing required keys: {missing}")

    if not os.environ.get("SLACK_BOT_TOKEN"):
        sys.exit("❌ env SLACK_BOT_TOKEN is required")

    return cfg


# ============================================================
# 段階別しきい値
# ============================================================
# tier 1 = :warning: 注意, tier 2 = :rotating_light: 警戒, tier 3 = :fire: 重大
TIER_STYLES: dict[int, dict[str, str]] = {
    1: {"emoji": ":warning:", "label": "注意"},
    2: {"emoji": ":rotating_light:", "label": "警戒"},
    3: {"emoji": ":fire:", "label": "重大"},
}


def load_thresholds(cfg: dict) -> list[float]:
    """config から段階しきい値を昇順 list で取得。後方互換のため旧キーも許容。"""
    tiers = cfg.get("daily_threshold_tiers_usd")
    if tiers is None:
        tiers = [cfg["daily_threshold_usd"]]
    if not isinstance(tiers, list) or not tiers:
        sys.exit("❌ daily_threshold_tiers_usd must be a non-empty list")
    try:
        vals = sorted(float(x) for x in tiers)
    except (TypeError, ValueError):
        sys.exit(f"❌ daily_threshold_tiers_usd must be numeric: {tiers}")
    if any(v <= 0 for v in vals):
        sys.exit(f"❌ daily_threshold_tiers_usd must be positive: {vals}")
    return vals


def tier_style(tier: int) -> dict[str, str]:
    return TIER_STYLES.get(tier, {"emoji": ":warning:", "label": f"tier{tier}"})


# ============================================================
# JST 日付 → UTC 範囲・対象月
# ============================================================
def jst_today() -> date:
    return datetime.now(JST).date()


def parse_jst_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def jst_date_to_utc_range(d: date) -> tuple[str, str]:
    """JST [d 00:00, d+1 00:00) を ISO8601 UTC 文字列で返す (Z 終端)。"""
    start_utc = datetime.combine(d, time(0, 0), tzinfo=JST).astimezone(timezone.utc)
    end_utc = datetime.combine(d + timedelta(days=1), time(0, 0), tzinfo=JST).astimezone(timezone.utc)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return start_utc.strftime(fmt), end_utc.strftime(fmt)


def months_to_ingest(d: date) -> list[tuple[int, int]]:
    """JST 当日を覆う UTC 範囲がまたぐ (year, month) のユニーク集合を返す。"""
    start_str, end_str = jst_date_to_utc_range(d)
    start_dt = datetime.strptime(start_str, "%Y-%m-%dT%H:%M:%SZ")
    # end は半開区間なので 1 秒前を見る
    end_dt = datetime.strptime(end_str, "%Y-%m-%dT%H:%M:%SZ") - timedelta(seconds=1)
    months = {(start_dt.year, start_dt.month), (end_dt.year, end_dt.month)}
    return sorted(months)


# ============================================================
# 日次集計
# ============================================================
def load_daily_stats(conn: sqlite3.Connection, jst_date: date,
                     owner_map: dict | None) -> list[dict]:
    """JST 当日のユーザー×モデル別 SUM を取得し、コスト計算してユーザー単位で集約。"""
    start_utc, end_utc = jst_date_to_utc_range(jst_date)

    # ユーザー × モデル × cache_ttl_type で SUM
    cur = conn.execute(
        """
        SELECT identity_arn, username, model_id, cache_ttl_type,
               SUM(input_tokens), SUM(output_tokens),
               SUM(cache_read_input_tokens),
               SUM(cache_write_input_tokens),
               COUNT(*)
        FROM invocations
        WHERE timestamp >= ? AND timestamp < ?
        GROUP BY identity_arn, username, model_id, cache_ttl_type
        """,
        (start_utc, end_utc),
    )

    # まずユーザー × モデルでまとめ直す (5min/1h cache を分別)
    per_user_model = defaultdict(lambda: {
        "input": 0, "output": 0, "cache_read": 0,
        "cache_write_5min": 0, "cache_write_1h": 0, "calls": 0,
    })
    user_meta: dict[str, dict] = {}  # identity_arn -> {username}

    for arn, uname, model_id, cache_ttl, inp, out, cread, cwrite, calls in cur:
        key = (arn or "", model_id or "")
        b = per_user_model[key]
        b["input"] += inp or 0
        b["output"] += out or 0
        b["cache_read"] += cread or 0
        if cache_ttl == "1h":
            b["cache_write_1h"] += cwrite or 0
        else:
            b["cache_write_5min"] += cwrite or 0
        b["calls"] += calls or 0
        user_meta.setdefault(arn or "", {"username_raw": uname or "unknown"})

    # ユーザー単位に集約
    per_user: dict[str, dict] = {}
    for (arn, model_id), m in per_user_model.items():
        cost = calculate_cost(
            m["input"], m["output"], model_id,
            m["cache_read"], m["cache_write_5min"], m["cache_write_1h"],
        )
        rec = per_user.setdefault(arn, {
            "identity_arn": arn,
            "username_raw": user_meta.get(arn, {}).get("username_raw", "unknown"),
            "total_usd": Decimal("0"),
            "total_calls": 0,
            "total_input": 0,
            "total_output": 0,
            "breakdown": [],
        })
        rec["total_usd"] += cost
        rec["total_calls"] += m["calls"]
        rec["total_input"] += m["input"]
        rec["total_output"] += m["output"]
        rec["breakdown"].append({
            "model_id": model_id, "usd": cost, "calls": m["calls"],
            "input": m["input"], "output": m["output"],
        })

    # display_name を解決
    rows = []
    for arn, rec in per_user.items():
        if owner_map is not None and arn:
            display, _ = resolve_username(arn, owner_map)
        else:
            display = rec["username_raw"]
        rec["username"] = display
        rec["breakdown"].sort(key=lambda x: -x["usd"])
        rows.append(rec)

    rows.sort(key=lambda r: -r["total_usd"])
    return rows


# ============================================================
# 通知履歴
# ============================================================
def already_notified(conn: sqlite3.Connection, date_str: str,
                      identity_arn: str, tier: int) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM notified_alerts WHERE date = ? AND identity_arn = ? AND tier = ?",
        (date_str, identity_arn, tier),
    )
    return cur.fetchone() is not None


def record_notification(conn: sqlite3.Connection, date_str: str,
                         identity_arn: str, tier: int, username: str,
                         daily_cost_usd: Decimal, threshold_usd: float) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO notified_alerts
            (date, identity_arn, tier, username, daily_cost_usd, threshold_usd, notified_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            date_str, identity_arn, tier, username,
            float(daily_cost_usd), float(threshold_usd),
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        ),
    )
    conn.commit()


# ============================================================
# Slack メッセージ整形
# ============================================================
def _short_model(model_id: str) -> str:
    return (model_id or "").replace("anthropic.", "").replace("apac.anthropic.", "[apac]")


def format_slack_message(user_row: dict, threshold: float, jst_date: date,
                          usd_to_jpy: float | None,
                          tier: int, tier_count: int) -> tuple[str, list[dict]]:
    total = user_row["total_usd"]
    username = user_row["username"] or "unknown"
    style = tier_style(tier)

    # ヘッダー (mrkdwn: 太字は *bold*)
    header_lines = [
        f"*{style['emoji']} Bedrock 日次コスト超過 "
        f"[Tier {tier}/{tier_count} {style['label']}] ({jst_date.isoformat()})*",
        f"ユーザー: *{username}*",
        f"本日コスト: *${total:.2f} USD*  (閾値: ${threshold:.2f})",
    ]
    if usd_to_jpy:
        jpy = total * Decimal(str(usd_to_jpy))
        header_lines.append(f"概算: ¥{jpy:,.0f}")
    header_lines.append(
        f"呼び出し: {user_row['total_calls']:,} 回 / "
        f"入力 {user_row['total_input']:,} tok / 出力 {user_row['total_output']:,} tok"
    )

    detail_lines = ["*モデル別内訳:*"]
    for b in user_row["breakdown"]:
        detail_lines.append(
            f"• `{_short_model(b['model_id'])}`  "
            f"${b['usd']:.4f}  ({b['calls']:,} calls, in={b['input']:,}, out={b['output']:,})"
        )

    text = "\n".join(header_lines + [""] + detail_lines)

    # Block Kit に分割 (3000 字制限)
    blocks: list[dict] = []
    buf = ""
    for line in text.split("\n"):
        while len(line) > SLACK_SECTION_LIMIT:
            if buf:
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": buf}})
                buf = ""
            blocks.append({"type": "section",
                           "text": {"type": "mrkdwn", "text": line[:SLACK_SECTION_LIMIT]}})
            line = line[SLACK_SECTION_LIMIT:]
        candidate = (buf + "\n" + line) if buf else line
        if len(candidate) > SLACK_SECTION_LIMIT:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": buf}})
            buf = line
        else:
            buf = candidate
    if buf:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": buf}})

    fallback = (
        f"Bedrock 日次コスト超過 [Tier {tier}/{tier_count} {style['label']}] "
        f"{jst_date.isoformat()}: {username} ${total:.2f} (閾値 ${threshold:.2f})"
    )
    return fallback, blocks


# ============================================================
# Slack 投稿
# ============================================================
def post_slack(client: WebClient, channel: str, fallback: str, blocks: list[dict],
               username: str | None, icon_emoji: str | None) -> None:
    kwargs = dict(channel=channel, text=fallback, blocks=blocks)
    if username:
        kwargs["username"] = username
    if icon_emoji:
        kwargs["icon_emoji"] = icon_emoji
    client.chat_postMessage(**kwargs)


# ============================================================
# main
# ============================================================
def main() -> int:
    parser = argparse.ArgumentParser(description="Bedrock 日次コストアラート")
    parser.add_argument("--config", default="config.yaml", help="YAML 設定ファイル")
    parser.add_argument("--date", default=None,
                        help="集計対象 JST 日付 (YYYY-MM-DD)。省略時は本日 (JST)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Slack 投稿せず stderr に出力。履歴も書き込まない")
    parser.add_argument("--force-notify", action="store_true",
                        help="dedup を無視して再通知")
    parser.add_argument("--skip-ingest", action="store_true",
                        help="S3 取り込みをスキップして DB 内容のみで判定")
    args = parser.parse_args()

    cfg_path = Path(args.config)
    cfg = load_config(cfg_path)

    jst_d = parse_jst_date(args.date) if args.date else jst_today()
    thresholds = load_thresholds(cfg)

    db_path = Path(cfg["db_path"])
    if not db_path.is_absolute():
        db_path = (cfg_path.parent / db_path).resolve()

    tier_desc = ", ".join(f"T{i+1}=${t:.2f}" for i, t in enumerate(thresholds))
    print(f"🗄️  DB: {db_path}", file=sys.stderr)
    print(f"📅 対象日 (JST): {jst_d}  段階しきい値: {tier_desc}", file=sys.stderr)

    conn = init_db(db_path)
    try:
        if not args.skip_ingest:
            for (y, m) in months_to_ingest(jst_d):
                ingest_logs(conn, cfg["bucket"], y, m, cfg["region"])

        owner_map = None
        if cfg.get("resolve_owner", True):
            try:
                owner_map = build_owner_map()
                print(f"👥 IAM タグから {len(owner_map)} 件取得", file=sys.stderr)
            except Exception as e:
                print(f"⚠️  所有者解決をスキップ: {e}", file=sys.stderr)

        rows = load_daily_stats(conn, jst_d, owner_map)

        date_str = jst_d.isoformat()
        client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
        sent = 0
        skipped_dedup = 0
        over_users = 0
        tier_count = len(thresholds)

        # ユーザー × 段階で個別判定。低い段階から順に投稿することで時系列の読みやすさを保つ。
        for r in rows:
            arn = r["identity_arn"] or ""
            user_over = False
            for idx, threshold in enumerate(thresholds):
                tier = idx + 1
                if r["total_usd"] < Decimal(str(threshold)):
                    break  # 昇順なのでこれ以上の段階も未達
                user_over = True

                if not args.force_notify and already_notified(conn, date_str, arn, tier):
                    skipped_dedup += 1
                    continue

                fallback, blocks = format_slack_message(
                    r, threshold, jst_d, cfg.get("usd_to_jpy"),
                    tier, tier_count,
                )

                if args.dry_run:
                    print(
                        f"--- DRY-RUN ALERT[T{tier}/{tier_count}]: "
                        f"{r['username']} (${r['total_usd']:.2f} >= ${threshold:.2f}) ---",
                        file=sys.stderr,
                    )
                    print(fallback, file=sys.stderr)
                    print(
                        f"  blocks={len(blocks)} "
                        f"text_chars={sum(len(b['text']['text']) for b in blocks)}",
                        file=sys.stderr,
                    )
                    continue

                try:
                    post_slack(
                        client, cfg["slack"]["channel_id"],
                        fallback, blocks,
                        cfg["slack"].get("username"),
                        cfg["slack"].get("icon_emoji"),
                    )
                except SlackApiError as e:
                    print(
                        f"❌ Slack API error for {r['username']} (tier {tier}): "
                        f"{e.response['error']}",
                        file=sys.stderr,
                    )
                    raise

                record_notification(conn, date_str, arn, tier, r["username"],
                                    r["total_usd"], threshold)
                sent += 1

            if user_over:
                over_users += 1

        print(
            f"✅ checked={len(rows)} over_users={over_users} "
            f"notified={sent} skipped_dedup={skipped_dedup}"
            f"{' [dry-run]' if args.dry_run else ''}",
            file=sys.stderr,
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
