#!/usr/bin/env python3
"""
Bedrock Model Invocation Log Aggregator

S3に出力されたBedrockのログを集計し、IAMユーザー別・モデル別の
トークン消費量と推計コストを月次レポートとして出力する。

ダウンロード済みのS3オブジェクトと展開済みレコードはローカルSQLite DBに
保存され、再実行時はDBに無いオブジェクトのみダウンロードする。

使い方:
    python bedrock_cost_report.py --config config.yaml \
                                  --year 2026 --month 5 \
                                  --output report_202605.md

事前準備:
    pip install boto3
    AWS認証情報を設定（管理者権限が必要）
"""

import argparse
import gzip
import json
import re
import sqlite3
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from datetime import time as dt_time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import boto3
import yaml

from resolve_owner import build_owner_map, resolve_username


# ============================================================
# 料金単価（USD per 1,000 tokens）
# Claude API公式料金を参照: https://platform.claude.com/docs/ja/about-claude/pricing
# AWS Bedrock料金: https://aws.amazon.com/bedrock/pricing/
# Regional endpoint（地域別エンドポイント）: base × 1.1（+10%プレミアム）
# キャッシュ: 5分デフォルト（1時間オプション指定可能）
# ============================================================
REGIONAL_ENDPOINT_MULTIPLIER = Decimal("1.1")  # Regional endpoint +10%
PRICING = {
    # Claude Opus 4.8
    "anthropic.claude-opus-4-8": {
        "input": Decimal("0.005"),    # $5 / 1M tokens
        "output": Decimal("0.025"),   # $25 / 1M tokens
    },
    "apac.anthropic.claude-opus-4-8": {
        "input": Decimal("0.005"),
        "output": Decimal("0.025"),
    },
    # Claude Opus 4.7
    "anthropic.claude-opus-4-7-20250805-v1:0": {
        "input": Decimal("0.005"),
        "output": Decimal("0.025"),
    },
    "apac.anthropic.claude-opus-4-7-20250805-v1:0": {
        "input": Decimal("0.005"),
        "output": Decimal("0.025"),
    },
    # Claude Opus 4.6
    "anthropic.claude-opus-4-6-20250514-v1:0": {
        "input": Decimal("0.005"),
        "output": Decimal("0.025"),
    },
    "apac.anthropic.claude-opus-4-6-20250514-v1:0": {
        "input": Decimal("0.005"),
        "output": Decimal("0.025"),
    },
    # Claude Opus 4.5
    "anthropic.claude-opus-4-5-20251101-v1:0": {
        "input": Decimal("0.005"),
        "output": Decimal("0.025"),
    },
    # Claude Opus 4.1 (deprecated)
    "anthropic.claude-opus-4-1-20250805-v1:0": {
        "input": Decimal("0.015"),    # $15 / 1M tokens
        "output": Decimal("0.075"),   # $75 / 1M tokens
    },
    # Claude Opus 4 (deprecated)
    "anthropic.claude-opus-4-20250514-v1:0": {
        "input": Decimal("0.015"),
        "output": Decimal("0.075"),
    },
    # Claude Sonnet 4.6
    "anthropic.claude-sonnet-4-6-20250514-v1:0": {
        "input": Decimal("0.003"),    # $3 / 1M tokens
        "output": Decimal("0.015"),   # $15 / 1M tokens
    },
    "apac.anthropic.claude-sonnet-4-6-20250514-v1:0": {
        "input": Decimal("0.003"),
        "output": Decimal("0.015"),
    },
    # Claude Sonnet 4.5
    "anthropic.claude-sonnet-4-5-20250929-v1:0": {
        "input": Decimal("0.003"),
        "output": Decimal("0.015"),
    },
    "apac.anthropic.claude-sonnet-4-5-20250929-v1:0": {
        "input": Decimal("0.003"),
        "output": Decimal("0.015"),
    },
    # Claude Sonnet 4 (deprecated)
    "anthropic.claude-sonnet-4-20250514-v1:0": {
        "input": Decimal("0.003"),
        "output": Decimal("0.015"),
    },
    # Claude 3.5 Sonnet v2
    "anthropic.claude-3-5-sonnet-20241022-v2:0": {
        "input": Decimal("0.003"),
        "output": Decimal("0.015"),
    },
    # Claude Haiku 4.5
    "anthropic.claude-haiku-4-5-20250929-v1:0": {
        "input": Decimal("0.001"),    # $1 / 1M tokens
        "output": Decimal("0.005"),   # $5 / 1M tokens
    },
    "apac.anthropic.claude-haiku-4-5-20250929-v1:0": {
        "input": Decimal("0.001"),
        "output": Decimal("0.005"),
    },
    # Claude 3.5 Haiku (retired)
    "anthropic.claude-3-5-haiku-20241022-v1:0": {
        "input": Decimal("0.0008"),   # $0.80 / 1M tokens
        "output": Decimal("0.004"),   # $4 / 1M tokens
    },
}

# 未知のモデル（PRICING に未登録）を集計時に収集する
UNKNOWN_MODELS: set = set()

# プロンプトキャッシング料金（USD per 1,000 tokens）
# Claude API公式: https://platform.claude.com/docs/ja/about-claude/pricing#prompt-caching
CACHE_PRICING = {
    # 乗数: 基本入力価格 × 乗数
    "5min_write": Decimal("1.25"),    # 5分キャッシュ書き込み = 1.25倍の基本入力価格
    "1h_write": Decimal("2.0"),       # 1時間キャッシュ書き込み = 2倍の基本入力価格
    "read": Decimal("0.1"),           # キャッシュ読み取り = 0.1倍の基本入力価格（10%）
}

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "bedrock_logs.db"


# ============================================================
# 共通設定読込 (bedrock_daily_alert.py と共有)
# ============================================================
def load_base_config(path: Path) -> dict:
    """config.yaml を読み込み、共通必須キーを検証して返す。"""
    if not path.exists():
        sys.exit(f"❌ config not found: {path}")
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    required = ["bucket", "region", "db_path"]
    missing = [k for k in required if k not in cfg]
    if missing:
        sys.exit(f"❌ config missing required keys: {missing}")
    return cfg


# ============================================================
# 共通タイムゾーン処理 (bedrock_daily_alert.py と共有)
# ============================================================
def tz_to_utc_iso(dt: datetime) -> str:
    """datetime を UTC ISO8601 文字列 (Z 終端) に変換。"""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def date_to_utc_range(d: date, tz: ZoneInfo) -> tuple[str, str]:
    """日 [d 00:00, d+1 00:00) in tz → UTC ISO8601 文字列。"""
    start = datetime.combine(d, dt_time(0, 0), tzinfo=tz)
    end = datetime.combine(d + timedelta(days=1), dt_time(0, 0), tzinfo=tz)
    return tz_to_utc_iso(start), tz_to_utc_iso(end)


def month_to_utc_range(year: int, month: int, tz: ZoneInfo) -> tuple[str, str]:
    """月 [year-month-1 00:00, next month 1st 00:00) in tz → UTC ISO8601 文字列。"""
    first_day = date(year, month, 1)
    if month == 12:
        next_first = date(year + 1, 1, 1)
    else:
        next_first = date(year, month + 1, 1)
    start = datetime.combine(first_day, dt_time(0, 0), tzinfo=tz)
    end = datetime.combine(next_first, dt_time(0, 0), tzinfo=tz)
    return tz_to_utc_iso(start), tz_to_utc_iso(end)


def months_to_ingest(start_utc: str, end_utc: str) -> list[tuple[int, int]]:
    """UTC ISO8601 範囲を覆う (year, month) のユニーク集合を返す。"""
    start_dt = datetime.strptime(start_utc, "%Y-%m-%dT%H:%M:%SZ")
    # end は半開区間なので 1 秒前を見る
    end_dt = datetime.strptime(end_utc, "%Y-%m-%dT%H:%M:%SZ") - timedelta(seconds=1)
    months = {(start_dt.year, start_dt.month), (end_dt.year, end_dt.month)}
    return sorted(months)


def parse_tz(tz_name: str) -> ZoneInfo:
    """タイムゾーン名を ZoneInfo に変換。UTC は大文字小文字問わず受理。"""
    if tz_name.upper() == "UTC":
        return ZoneInfo("UTC")
    return ZoneInfo(tz_name)


def extract_username(arn: str) -> str:
    """IAM ARN から user/role 名を抽出"""
    if not arn:
        return "unknown"
    # arn:aws:iam::123456789012:user/sample-user
    # arn:aws:sts::123456789012:assumed-role/RoleName/SessionName
    if "/" in arn:
        return arn.split("/")[-1] if "user/" in arn else arn.split("/")[-2]
    return arn


def init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS processed_objects (
            key          TEXT PRIMARY KEY,
            etag         TEXT,
            bucket       TEXT,
            region       TEXT,
            year         INTEGER,
            month        INTEGER,
            record_count INTEGER,
            processed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS invocations (
            id                          INTEGER PRIMARY KEY AUTOINCREMENT,
            key                         TEXT NOT NULL,
            timestamp                   TEXT,
            year                        INTEGER,
            month                       INTEGER,
            username                    TEXT,
            identity_arn                TEXT,
            model_id                    TEXT,
            input_tokens                INTEGER,
            output_tokens               INTEGER,
            cache_read_input_tokens     INTEGER DEFAULT 0,
            cache_write_input_tokens    INTEGER DEFAULT 0,
            cache_ttl_type              TEXT DEFAULT 'unknown',
            FOREIGN KEY(key) REFERENCES processed_objects(key)
        );

        CREATE INDEX IF NOT EXISTS idx_invocations_ym
            ON invocations(year, month);
        CREATE INDEX IF NOT EXISTS idx_invocations_key
            ON invocations(key);
        CREATE INDEX IF NOT EXISTS idx_invocations_timestamp
            ON invocations(timestamp);

        CREATE TABLE IF NOT EXISTS notified_alerts (
            date           TEXT NOT NULL,
            identity_arn   TEXT NOT NULL,
            tier           INTEGER NOT NULL DEFAULT 0,
            username       TEXT,
            daily_cost_usd REAL NOT NULL,
            threshold_usd  REAL NOT NULL,
            notified_at    TEXT NOT NULL,
            PRIMARY KEY (date, identity_arn, tier)
        );
        """
    )
    _migrate_notified_alerts_tier(conn)
    conn.commit()
    return conn


def _migrate_notified_alerts_tier(conn: sqlite3.Connection) -> None:
    """旧スキーマ ((date, identity_arn) PK, tier 列なし) を tier 付きへ移行。

    旧データは tier=1 として登録する。旧通知が新しい上位 tier 超過の妨げにならないよう
    最下位段階のみへ移すことで、$1 → $10 → $30 のような閾値見直し後でも上位通知が出る。
    """
    cols = [r[1] for r in conn.execute("PRAGMA table_info(notified_alerts)")]
    if "tier" in cols:
        return
    conn.executescript(
        """
        ALTER TABLE notified_alerts RENAME TO notified_alerts_old;
        CREATE TABLE notified_alerts (
            date           TEXT NOT NULL,
            identity_arn   TEXT NOT NULL,
            tier           INTEGER NOT NULL DEFAULT 0,
            username       TEXT,
            daily_cost_usd REAL NOT NULL,
            threshold_usd  REAL NOT NULL,
            notified_at    TEXT NOT NULL,
            PRIMARY KEY (date, identity_arn, tier)
        );
        INSERT INTO notified_alerts
            (date, identity_arn, tier, username, daily_cost_usd, threshold_usd, notified_at)
            SELECT date, identity_arn, 1, username, daily_cost_usd, threshold_usd, notified_at
              FROM notified_alerts_old;
        DROP TABLE notified_alerts_old;
        """
    )


def _classify_cache_control(cc: dict) -> str:
    """cache_control オブジェクトから TTL を判定

    Anthropic API 仕様:
      type は常に "ephemeral"。TTL は ttl 属性で指定 ("5m" or "1h")。
      ttl 省略時のデフォルトは 5分。
    """
    if not isinstance(cc, dict):
        return "unknown"
    ttl = cc.get("ttl", "")
    if ttl == "1h":
        return "1h"
    # "5m" or 省略 (デフォルト5分)
    return "5min"


def _extract_cache_ttl_from_payload(payload: dict) -> str:
    """パース済みペイロードから cache_control を探して TTL を判定。
    複数箇所に異なる TTL が混在する場合は 1h を優先（料金が高い方を選ぶ＝過大評価より過小評価を避ける）。
    """
    found_5min = False
    for item in payload.get("system", []):
        if isinstance(item, dict) and "cache_control" in item:
            ttl = _classify_cache_control(item["cache_control"])
            if ttl == "1h":
                return "1h"
            if ttl == "5min":
                found_5min = True
    for msg in payload.get("messages", []):
        if isinstance(msg, dict):
            for item in msg.get("content", []):
                if isinstance(item, dict) and "cache_control" in item:
                    ttl = _classify_cache_control(item["cache_control"])
                    if ttl == "1h":
                        return "1h"
                    if ttl == "5min":
                        found_5min = True
    return "5min" if found_5min else "unknown"


def get_cache_ttl_from_s3(s3_path: str) -> str:
    """ペイロード S3 ファイルから cache_control.type を判定

    Returns:
        "5min" (ephemeral), "1h" (static), "unknown" (取得失敗 or 見つからない)
    """
    if not s3_path:
        return "unknown"

    try:
        import boto3
        from botocore.exceptions import ClientError

        # S3 パスを parse: s3://bucket/key/to/file.gz
        parts = s3_path.replace("s3://", "").split("/", 1)
        if len(parts) != 2:
            return "unknown"

        bucket, key = parts
        s3 = boto3.client("s3", region_name="us-east-1")

        # Gzip ペイロード取得
        response = s3.get_object(Bucket=bucket, Key=key)
        body = response["Body"].read()
        decompressed = gzip.decompress(body)
        payload = json.loads(decompressed)
        return _extract_cache_ttl_from_payload(payload)

    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            return "unknown"
        raise
    except Exception:
        return "unknown"


def parse_record(record):
    if not isinstance(record, dict):
        return None
    identity = record.get("identity") or {}
    if not isinstance(identity, dict):
        identity = {}
    identity_arn = identity.get("arn", "") or ""
    username = extract_username(identity_arn)
    model_id = record.get("modelId", "unknown")
    inp = record.get("input") or {}
    out = record.get("output") or {}
    if not isinstance(inp, dict):
        inp = {}
    if not isinstance(out, dict):
        out = {}
    input_tokens = inp.get("inputTokenCount", 0) or 0
    output_tokens = out.get("outputTokenCount", 0) or 0
    cache_read_input_tokens = inp.get("cacheReadInputTokenCount", 0) or 0
    cache_write_input_tokens = inp.get("cacheWriteInputTokenCount", 0) or 0
    timestamp = record.get("timestamp", "")

    # inputBodyJson が直接ある場合はその場で cache_control を判定
    cache_ttl_type = "unknown"
    body_json = inp.get("inputBodyJson")
    if body_json and isinstance(body_json, dict):
        cache_ttl_type = _extract_cache_ttl_from_payload(body_json)

    return {
        "timestamp": timestamp,
        "username": username,
        "identity_arn": identity_arn,
        "model_id": model_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read_input_tokens,
        "cache_write_input_tokens": cache_write_input_tokens,
        "cache_ttl_type": cache_ttl_type,
    }


def ingest_logs(
    conn: sqlite3.Connection,
    bucket: str,
    year: int,
    month: int,
    region: str = "ap-northeast-1",
) -> int:
    """S3から未処理オブジェクトをダウンロードしてDBに保存。新規取り込み件数を返す。"""
    from botocore.config import Config
    # 並列実行のため connection pool を拡張（デフォルト10では不足し silent failure する）
    s3 = boto3.client("s3", config=Config(max_pool_connections=50, retries={"max_attempts": 5, "mode": "adaptive"}))
    fetch_failed = 0

    initial = s3.list_objects_v2(Bucket=bucket, Prefix="AWSLogs/", MaxKeys=1)
    if "Contents" not in initial:
        print(f"⚠️  バケット {bucket} にログが見つかりません")
        return 0

    account_id = initial["Contents"][0]["Key"].split("/")[1]
    prefix = (
        f"AWSLogs/{account_id}/BedrockModelInvocationLogs/"
        f"{region}/{year}/{month:02d}/"
    )

    print(f"📂 取り込み対象: s3://{bucket}/{prefix}")

    paginator = s3.get_paginator("list_objects_v2")

    new_files = 0
    new_records = 0
    skipped = 0
    start = time.time()
    last_log = start

    cur = conn.cursor()
    existing_keys = {
        row[0] for row in cur.execute("SELECT key FROM processed_objects")
    }

    def fetch_and_parse_s3_file(s3_client, bucket, key):
        """S3 ファイル取得・パース（並列実行用）"""
        try:
            response = s3_client.get_object(Bucket=bucket, Key=key)
            body = response["Body"].read()
            etag = response.get("ETag", "").strip('"')

            if key.endswith(".gz"):
                body = gzip.decompress(body)

            return key, etag, body.decode("utf-8")
        except Exception as e:
            print(f"Error fetching {key}: {e}")
            return None

    # S3 ファイル取得を並列化
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        if "Contents" not in page:
            continue

        # フィルタリング＆取得対象を抽出
        files_to_fetch = []
        for obj in page["Contents"]:
            key = obj["Key"]

            # ペイロード本体ファイル（Large data delivery to S3）は集計対象外
            if key.endswith("_input.json.gz") or key.endswith("_output.json.gz"):
                skipped += 1
                continue

            if key in existing_keys:
                skipped += 1
                continue

            files_to_fetch.append(key)

        # S3 ファイルを最大20並列で取得
        with ThreadPoolExecutor(max_workers=20) as executor:
            futures = {executor.submit(fetch_and_parse_s3_file, s3, bucket, key): key
                      for key in files_to_fetch}

            for future in as_completed(futures):
                result = future.result()
                if result is None:
                    fetch_failed += 1
                    continue

                key, etag, body_str = result

                records_for_key = []
                s3_paths_to_fetch = {}  # index -> (parsed, raw, s3_path)

                for line in body_str.splitlines():
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # 1行に複数レコード(list)が入っているケースに対応
                    items = record if isinstance(record, list) else [record]
                    for item in items:
                        parsed = parse_record(item)
                        if parsed is None:
                            continue

                        # cache_write がある場合、S3 ペイロード判定用にキュー
                        if parsed["cache_write_input_tokens"] > 0:
                            s3_path = item.get("input", {}).get("inputBodyS3Path")
                            if s3_path:
                                idx = len(records_for_key)
                                s3_paths_to_fetch[idx] = (parsed, s3_path)

                        records_for_key.append(parsed)

                # S3 ペイロード判定を並列実行（最大10並列）
                if s3_paths_to_fetch:
                    with ThreadPoolExecutor(max_workers=10) as ttl_executor:
                        ttl_futures = {ttl_executor.submit(get_cache_ttl_from_s3, path): idx
                                       for idx, (_, path) in s3_paths_to_fetch.items()}

                        for ttl_future in as_completed(ttl_futures):
                            idx = ttl_futures[ttl_future]
                            try:
                                cache_ttl = ttl_future.result()
                                parsed, _ = s3_paths_to_fetch[idx]
                                parsed["cache_ttl_type"] = cache_ttl
                                records_for_key[idx] = parsed
                            except Exception:
                                pass  # S3 アクセス失敗時は cache_ttl_type='unknown'

                cur.execute(
                    """
                    INSERT OR REPLACE INTO processed_objects
                        (key, etag, bucket, region, year, month, record_count, processed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        key, etag, bucket, region, year, month,
                        len(records_for_key),
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )
                cur.executemany(
                    """
                    INSERT INTO invocations
                        (key, timestamp, year, month, username, identity_arn,
                         model_id, input_tokens, output_tokens, cache_read_input_tokens,
                         cache_write_input_tokens, cache_ttl_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            key, p["timestamp"], year, month,
                            p["username"], p["identity_arn"], p["model_id"],
                            p["input_tokens"], p["output_tokens"],
                            p["cache_read_input_tokens"], p["cache_write_input_tokens"],
                            p["cache_ttl_type"],
                        )
                        for p in records_for_key
                    ],
                )
                conn.commit()

                new_files += 1
                new_records += len(records_for_key)

                now = time.time()
                if now - last_log >= 5 or new_files <= 3:
                    elapsed = now - start
                    rate = new_files / elapsed if elapsed > 0 else 0
                    print(
                        f"  [{elapsed:6.1f}s] new_files={new_files:,} "
                        f"new_records={new_records:,} skipped={skipped:,} "
                        f"({rate:.1f} files/s) latest={key}",
                        flush=True,
                    )
                    last_log = now

    print(
        f"✅ 取り込み完了: 新規 {new_files} files / {new_records} records, "
        f"スキップ済み {skipped} files, 取得失敗 {fetch_failed} files"
    )
    if fetch_failed > 0:
        print(f"⚠️  {fetch_failed} 件の取得失敗があります。再実行で再取得されます。")
    return new_records


def load_stats(conn: sqlite3.Connection, start_utc: str, end_utc: str, owner_map=None):
    """DBから集計データを構築。owner_mapがあればARNから所有者名を解決して集約"""
    stats = defaultdict(lambda: defaultdict(lambda: {
        "input": 0, "output": 0, "cache_read_input": 0,
        "cache_write_input_5min": 0, "cache_write_input_1h": 0,
        "calls": 0
    }))
    cur = conn.execute(
        """
        SELECT identity_arn, username, model_id,
               SUM(input_tokens), SUM(output_tokens),
               SUM(cache_read_input_tokens),
               SUM(CASE WHEN cache_ttl_type='1h' THEN 0 ELSE cache_write_input_tokens END),
               SUM(CASE WHEN cache_ttl_type='1h' THEN cache_write_input_tokens ELSE 0 END),
               COUNT(*)
        FROM invocations
        WHERE timestamp >= ? AND timestamp < ?
        GROUP BY identity_arn, username, model_id
        """,
        (start_utc, end_utc),
    )
    for identity_arn, username, model_id, inp, out, cache_read, cache_write_5m, cache_write_1h, calls in cur:
        if owner_map is not None and identity_arn:
            display, _auth = resolve_username(identity_arn, owner_map)
        else:
            display = username or "unknown"
        bucket = stats[display][model_id]
        bucket["input"] += inp or 0
        bucket["output"] += out or 0
        bucket["cache_read_input"] += cache_read or 0
        bucket["cache_write_input_5min"] += cache_write_5m or 0
        bucket["cache_write_input_1h"] += cache_write_1h or 0
        bucket["calls"] += calls or 0
    return stats


def normalize_model_id(model_id: str) -> str:
    """inference-profile ARN や region prefix を吸収してPRICING検索用キーに変換"""
    if not model_id:
        return ""
    if model_id.startswith("arn:"):
        # arn:aws:bedrock:us-east-1:...:inference-profile/us.anthropic.claude-opus-4-7
        model_id = model_id.rsplit("/", 1)[-1]
    # Strip regional prefix (us., apac., eu.) but NOT the anthropic. prefix
    for prefix in ("us.", "apac.", "eu.", "global."):
        if model_id.startswith(prefix):
            model_id = model_id[len(prefix):]
            break
    return model_id


def _base_key(key: str) -> str:
    """PRICINGキー/正規化済みmodel_idから日付/バージョンサフィックスを除いたベース部を返す

    例:
        anthropic.claude-opus-4-6-20250514-v1:0 → anthropic.claude-opus-4-6
        anthropic.claude-opus-4-6-v1           → anthropic.claude-opus-4-6
        anthropic.claude-opus-4-8              → anthropic.claude-opus-4-8
    """
    # -YYYYMMDD-vN:M, -YYYYMMDD, -vN:M, -vN を末尾から一括除去
    return re.sub(r"(-20\d{6})?(-v\d+(?::\d+)?)?$", "", key)


def lookup_price(model_id: str):
    """PRICING 辞書からモデルの単価を返す。見つからなければ None。"""
    norm = normalize_model_id(model_id)
    if norm in PRICING:
        return PRICING[norm]
    norm_base = _base_key(norm)
    best = None
    best_len = -1
    for k, v in PRICING.items():
        kb = _base_key(k)
        if norm_base == kb or norm == kb or norm_base == k:
            if len(kb) > best_len:
                best = v
                best_len = len(kb)
    return best  # 未登録モデルの場合 None


def calculate_cost(input_tokens: int, output_tokens: int, model_id: str,
                   cache_read_input_tokens: int = 0,
                   cache_write_input_tokens_5min: int = 0,
                   cache_write_input_tokens_1h: int = 0,
                   apply_regional_multiplier: bool = True):
    """トークン数からUSDコストを計算（キャッシング対応・Regional endpoint対応）

    cache_write_input_tokens_5min: 5分キャッシュ書き込みトークン
    cache_write_input_tokens_1h: 1時間キャッシュ書き込みトークン
    apply_regional_multiplier: Regional endpoint (+10%) を適用するか（デフォルトTrue）
    """
    price = lookup_price(model_id)
    if price is None:
        UNKNOWN_MODELS.add(model_id)
        return Decimal("0")
    base_input_price = price["input"]
    base_output_price = price["output"]

    # Regional endpoint プレミアムを適用
    if apply_regional_multiplier:
        base_input_price = base_input_price * REGIONAL_ENDPOINT_MULTIPLIER
        base_output_price = base_output_price * REGIONAL_ENDPOINT_MULTIPLIER

    # 通常の入力・出力トークン
    input_cost = (Decimal(input_tokens) / 1000) * base_input_price
    output_cost = (Decimal(output_tokens) / 1000) * base_output_price

    # キャッシュ読み取り（基本入力価格の10%）
    cache_read_cost = (Decimal(cache_read_input_tokens) / 1000) * base_input_price * CACHE_PRICING["read"]

    # キャッシュ書き込み（5分と1時間を分別）
    cache_write_5m_cost = (Decimal(cache_write_input_tokens_5min) / 1000) * base_input_price * CACHE_PRICING["5min_write"]
    cache_write_1h_cost = (Decimal(cache_write_input_tokens_1h) / 1000) * base_input_price * CACHE_PRICING["1h_write"]

    return input_cost + output_cost + cache_read_cost + cache_write_5m_cost + cache_write_1h_cost


def generate_report(stats, year: int, month: int, usd_to_jpy: Decimal,
                    tz_name: str = "Asia/Tokyo") -> str:
    """Markdownレポート生成"""
    lines = []
    lines.append(f"# Bedrock利用レポート: {year}年{month}月 ({tz_name})")
    lines.append("")
    lines.append(f"生成日時: {datetime.now().isoformat(timespec='seconds')}")
    lines.append("")

    grand_total_usd = Decimal("0")
    grand_input = 0
    grand_output = 0
    grand_cache_read = 0
    grand_cache_write_5m = 0
    grand_cache_write_1h = 0
    grand_calls = 0

    for user_data in stats.values():
        for model_id, m in user_data.items():
            grand_total_usd += calculate_cost(m["input"], m["output"], model_id,
                                              m["cache_read_input"],
                                              m["cache_write_input_5min"],
                                              m["cache_write_input_1h"])
            grand_input += m["input"]
            grand_output += m["output"]
            grand_cache_read += m["cache_read_input"]
            grand_cache_write_5m += m["cache_write_input_5min"]
            grand_cache_write_1h += m["cache_write_input_1h"]
            grand_calls += m["calls"]

    grand_total_jpy = grand_total_usd * usd_to_jpy

    # 未知のモデルが検出された場合は警告セクションを冒頭に挿入
    if UNKNOWN_MODELS:
        sys.stderr.write(
            f"⚠️  PRICING 辞書に未登録のモデルが {len(UNKNOWN_MODELS)} 件あります:\n"
        )
        for m in sorted(UNKNOWN_MODELS):
            sys.stderr.write(f"   - {m}\n")
        sys.stderr.write(
            "   bedrock_cost_report.py の PRICING 辞書に該当モデルの料金を追加してください。\n"
        )

        lines.append("## ⚠️ 未知のモデルが検出されました")
        lines.append("")
        lines.append(
            "以下のモデルは PRICING 辞書に未登録のため、**コスト計算から除外**されています。"
        )
        lines.append(
            "正確な集計のため、`bedrock_cost_report.py` の `PRICING` 辞書に該当モデルの料金を追加してください。"
        )
        lines.append("")
        for m in sorted(UNKNOWN_MODELS):
            lines.append(f"- `{m}`")
        lines.append("")
        lines.append("参考: <https://aws.amazon.com/bedrock/pricing/>")
        lines.append("")

    lines.append("## 全体サマリ")
    lines.append("")
    lines.append(f"- 総呼び出し回数: **{grand_calls:,}** 回")
    lines.append(f"- 入力トークン: **{grand_input:,}** tokens")
    lines.append(f"- 出力トークン: **{grand_output:,}** tokens")
    lines.append(f"- キャッシュ読み取り: **{grand_cache_read:,}** tokens")
    lines.append(f"- キャッシュ書き込み（5分）: **{grand_cache_write_5m:,}** tokens")
    lines.append(f"- キャッシュ書き込み（1時間）: **{grand_cache_write_1h:,}** tokens")
    lines.append(f"- 推計コスト: **${grand_total_usd:.2f} USD** (≒ ¥{grand_total_jpy:,.0f})")
    lines.append("")

    lines.append("## ユーザー別サマリ")
    lines.append("")
    lines.append("| ユーザー | 呼び出し回数 | 入力tokens | 出力tokens | 推計コスト(USD) | 推計コスト(JPY) |")
    lines.append("|---|---:|---:|---:|---:|---:|")

    user_totals = []
    for username, models in stats.items():
        u_input = sum(m["input"] for m in models.values())
        u_output = sum(m["output"] for m in models.values())
        u_cache_read = sum(m["cache_read_input"] for m in models.values())
        u_cache_write_5m = sum(m["cache_write_input_5min"] for m in models.values())
        u_cache_write_1h = sum(m["cache_write_input_1h"] for m in models.values())
        u_calls = sum(m["calls"] for m in models.values())
        u_cost_usd = sum(
            calculate_cost(m["input"], m["output"], mid,
                          m["cache_read_input"],
                          m["cache_write_input_5min"],
                          m["cache_write_input_1h"])
            for mid, m in models.items()
        )
        user_totals.append((username, u_calls, u_input, u_output, u_cache_read, u_cache_write_5m, u_cache_write_1h, u_cost_usd))

    user_totals.sort(key=lambda x: -x[7])

    for username, calls, inp, out, cache_r, cache_w5m, cache_w1h, cost in user_totals:
        jpy = cost * usd_to_jpy
        lines.append(
            f"| {username} | {calls:,} | {inp:,} | {out:,} "
            f"| ${cost:.2f} | ¥{jpy:,.0f} |"
        )
    lines.append("")

    lines.append("## ユーザー別・モデル別詳細")
    lines.append("")

    for username, _, _, _, _, _, _, _ in user_totals:
        lines.append(f"### {username}")
        lines.append("")
        lines.append("| モデル | 呼び出し回数 | 入力tok | 出力tok | cache読 | cache書 | コスト(USD) |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|")

        models = stats[username]
        model_list = sorted(
            models.items(),
            key=lambda x: -calculate_cost(x[1]["input"], x[1]["output"], x[0],
                                         x[1]["cache_read_input"],
                                         x[1]["cache_write_input_5min"],
                                         x[1]["cache_write_input_1h"]),
        )
        for model_id, m in model_list:
            cost = calculate_cost(m["input"], m["output"], model_id,
                                 m["cache_read_input"],
                                 m["cache_write_input_5min"],
                                 m["cache_write_input_1h"])
            short_name = model_id.replace("anthropic.", "").replace("apac.anthropic.", "[apac]")
            cache_write_total = m['cache_write_input_5min'] + m['cache_write_input_1h']
            lines.append(
                f"| {short_name} | {m['calls']:,} | {m['input']:,} "
                f"| {m['output']:,} | {m['cache_read_input']:,} | {cache_write_total:,} | ${cost:.4f} |"
            )
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("⚠️ **注意事項**")
    lines.append("")
    lines.append("- コストは公開単価からの推計値であり、実際のAWS請求額とは一致しない場合があります")
    lines.append("- 経験的に全カテゴリ一律 **約15〜17% トークンが不足** することが確認されています")
    lines.append("- 本レポートは **ユーザー別・モデル別の利用比率** を把握する用途に適しています")
    lines.append("- **正確な請求額は AWS Cost Explorer の usage type 別内訳を参照してください**")
    lines.append("  例: `aws ce get-cost-and-usage --filter ... --group-by Type=DIMENSION,Key=USAGE_TYPE`")
    lines.append("")
    lines.append("計算条件:")
    lines.append(f"- 為替レート: 1 USD = {usd_to_jpy} JPY で換算")
    lines.append("- 単価は AWS Bedrock 公開料金 × Regional endpoint プレミアム (+10%)")
    lines.append("- キャッシュ TTL（5分 vs 1時間）はペイロード S3 ファイルから自動判定")
    lines.append("- PRICING 辞書未登録のモデルはコスト計算から除外（レポート冒頭に警告表示）")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Bedrock利用ログ集計")
    parser.add_argument("--config", default="config.yaml", help="YAML 設定ファイル")
    parser.add_argument("--year", type=int, required=True, help="集計対象年")
    parser.add_argument("--month", type=int, required=True, help="集計対象月")
    parser.add_argument("--output", default=None, help="出力ファイル名(.md)")
    parser.add_argument("--skip-ingest", action="store_true",
                        help="S3取り込みをスキップしDBの内容のみでレポート生成")
    parser.add_argument("--tz", default="Asia/Tokyo",
                        help="タイムゾーン (既定: Asia/Tokyo, UTC 指定時は AWS 基準の UTC 境界で集計)")
    args = parser.parse_args()

    cfg_path = Path(args.config)
    cfg = load_base_config(cfg_path)

    tz = parse_tz(args.tz)
    bucket = cfg["bucket"]
    region = cfg["region"]
    db_path = Path(cfg["db_path"])
    if not db_path.is_absolute():
        db_path = (cfg_path.parent / db_path).resolve()
    usd_to_jpy = Decimal(str(cfg.get("usd_to_jpy", 155)))

    start_utc, end_utc = month_to_utc_range(args.year, args.month, tz)
    tz_name = str(tz)

    conn = init_db(db_path)
    print(f"🗄️  DB: {db_path}")
    print(f"📅 集計対象: {args.year}年{args.month}月 ({tz_name})  "
          f"UTC範囲: {start_utc} ~ {end_utc}", file=sys.stderr)

    try:
        if not args.skip_ingest:
            for y, m in months_to_ingest(start_utc, end_utc):
                ingest_logs(conn, bucket, y, m, region)

        owner_map = None
        if cfg.get("resolve_owner", True):
            try:
                owner_map = build_owner_map()
                print(f"👥 IAMタグから {len(owner_map)} 件の所有者情報を取得")
            except Exception as e:
                print(f"⚠️  所有者解決をスキップ: {e}")

        stats = load_stats(conn, start_utc, end_utc, owner_map)
        if not stats:
            print(f"⚠️  {args.year}年{args.month}月のデータがDBにありません")
            return

        report = generate_report(stats, args.year, args.month, usd_to_jpy, tz_name)

        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(report)
            print(f"📄 レポート出力: {args.output}")
        else:
            print(report)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
