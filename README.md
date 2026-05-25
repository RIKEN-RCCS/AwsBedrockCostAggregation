# AWS Bedrock Cost Aggregation

S3 に出力された AWS Bedrock の Model Invocation Logs を集計し、IAM ユーザー別・モデル別のトークン消費量と推計コストを月次 Markdown レポートとして出力するツール。

ダウンロード済みの S3 オブジェクトと展開済みレコードはローカル SQLite DB に保存され、再実行時は DB に無いオブジェクトのみを取得する差分処理に対応する。

## 構成

| ファイル | 役割 |
| --- | --- |
| `bedrock_cost_report.py` | 月次レポート生成スクリプト。S3 から取り込み・DB 保存・Markdown 出力 |
| `bedrock_daily_alert.py` | 日次コストアラート (毎時 cron 起動)。しきい値超過ユーザーを Slack 通知 |
| `resolve_owner.py` | `BedrockAPIKey-*` IAM ユーザーのタグから所有者名を解決するヘルパー |
| `config.yaml` | 日次アラートの設定 (しきい値・Slack チャンネル・S3 バケット等)。`config.yaml.example` から複製 |
| `run_daily_alert.sh` | cron から呼ぶラッパ。`SLACK_BOT_TOKEN` を読み込んで `bedrock_daily_alert.py` を起動 |

## 前提

- Python 3.9+ (日次アラートは `zoneinfo` 利用のため 3.9+ 必須)
- `boto3` がインストール済み
- 日次アラートを使う場合: `pip install pyyaml slack_sdk`
- AWS 認証情報が設定済み（IAM / S3 / Bedrock の参照権限）
- Bedrock の Invocation Logging が有効化され、S3 にログが出力されていること

### モデル呼び出しログ記録の設定(参考)

#### 1. S3ストレージの設定

* AWSコンソールにて、リージョンが「米国(バージニア北部)」であることを確認する
* AWSコンソールでS3サービスへ移動、左メニューから「汎用バケット」を選択する
* 「バケットを作成」をクリックする
* 「バケットタイプ」は「汎用」、「バケット名前空間」は「アカウントのリージョナル名前空間」を選択、「バケット名の接頭辞」に「bderock-invocation-logs」を入力する
* その他はデフォルト設定のまま
* 「バケットを作成」をクリックする
* 生成されたバケット「s3://bedrock-invocation-logs-...-us-east-1-an」がログの保存場所になる

#### 2. ログの設定

* AWSコンソールでBedrock サービスへ移動、左メニューから「設定」を選択する
* 「モデル呼び出しのログ記録」をONにする
* 「ログに含めるデータタイプを選択 - オプション」は全てチェックを外す（ログサイズ削減、データ流出リスク回避のため）
* 「ログ記録先を選択」は「S3のみ」を選択
* 「S3の場所」に先ほどのバケットの場所「s3://bedrock-invocation-logs-...-us-east-1-an」を入力する

## 使い方

```bash
python bedrock_cost_report.py \
    --bucket <ログ出力先S3バケット名> \
    --region us-east-1 \
    --year 2026 --month 5 \
    --output report_202605.md
```

主なオプション:

| フラグ | 説明 |
| --- | --- |
| `--bucket` | Bedrock invocation log の出力先 S3 バケット名（必須） |
| `--year`, `--month` | 集計対象の年月（必須） |
| `--region` | バケット内ログのリージョン。デフォルト `ap-northeast-1` |
| `--output` | 出力 Markdown ファイル。省略時は標準出力 |
| `--db` | SQLite DB のパス。デフォルトはスクリプトと同階層の `bedrock_logs.db` |
| `--skip-ingest` | S3 取り込みをスキップし、DB の内容だけでレポートを生成 |
| `--no-resolve-owner` | IAM タグによる所有者名解決を無効化 |

## 所有者名の解決（任意）

`BedrockAPIKey-*` という命名の IAM ユーザーに以下のタグを付けておくと、レポート上で実利用者名に解決される。

| タグキー | 例 |
| --- | --- |
| `Owner` | メンバー識別子 |
| `Email` | 連絡先（任意） |
| `Department` | 部署名（任意） |
| `Project` | プロジェクト名（任意） |

タグが無い場合は `(未タグ)` として表示される。

## 料金単価

`bedrock_cost_report.py` の `PRICING` 辞書に Claude 系モデルの単価を定義している。AWS Bedrock の Regional endpoint プレミアム（+10%）と Prompt Caching（5 分 / 1 時間 TTL）の単価補正に対応。

未登録のモデルが現れた場合は、レポート冒頭およびスクリプト標準エラー出力に警告が出る。新しいモデルを追加する場合は `PRICING` を編集すること。

公式単価:

- Claude API 公開単価: <https://platform.claude.com/docs/ja/about-claude/pricing>
- AWS Bedrock 単価: <https://aws.amazon.com/bedrock/pricing/>

## 日次コストアラート (Slack 通知)

`bedrock_daily_alert.py` は **JST 当日のユーザー別コスト** を集計し、しきい値を超えたユーザーを Slack に通知する。毎時 cron で起動する運用を想定。同一 (日付, ユーザー) には 1 日 1 通までに抑制される。

### セットアップ

1. 依存追加: `pip install pyyaml slack_sdk`
2. 設定ファイル作成: `cp config.yaml.example config.yaml` し、`bucket` / `daily_threshold_usd` / `slack.channel_id` などを編集
3. Slack Bot Token を `~/.secrets/slack_tokens.sh` に配置 (chmod 600):
   ```bash
   export SLACK_BOT_TOKEN="xoxb-..."
   ```
   Bot に `chat:write` スコープと、通知先チャンネルへの招待が必要。

### 実行

```bash
# 通常実行 (毎時 cron 想定)
./run_daily_alert.sh

# 投稿せずに stderr にプレビュー
./run_daily_alert.sh --dry-run

# 過去日のリプレイ
./run_daily_alert.sh --date 2026-05-24 --dry-run

# dedup を無視して再投稿 (テスト用)
./run_daily_alert.sh --force-notify
```

### cron 例

```
5 * * * * /lvs0/dne1/rccs-nghpcadu/hikaru.inoue/AwsBedrockCostAggregation/run_daily_alert.sh >> /tmp/bedrock_daily_alert.log 2>&1
```

### 主なオプション

| フラグ | 説明 |
| --- | --- |
| `--config <path>` | YAML 設定ファイル (デフォルト `config.yaml`) |
| `--date YYYY-MM-DD` | 集計対象 JST 日付。省略時は本日 (JST) |
| `--dry-run` | Slack 投稿せず stderr に出力。通知履歴も書き込まない |
| `--force-notify` | 通知履歴を無視して再投稿 |
| `--skip-ingest` | S3 取り込みをスキップし DB 内容のみで判定 |

### 仕組み

- JST 当日に該当する UTC 範囲 `[JST 00:00, JST 24:00)` で `invocations.timestamp` を絞り込み、ユーザー × モデルでコスト計算
- 通知履歴は SQLite テーブル `notified_alerts(date, identity_arn, ...)` に PRIMARY KEY 制約で記録
- 月初 JST など UTC 範囲が前月をまたぐ日は、当該の 2 か月分の `ingest_logs` が走る (`processed_objects` の dedup により再実行は安価)

### 通知抑制のリセットタイミング

通知履歴の `date` カラムには **JST の日付** が格納される。`jst_today()` は実行時刻の JST 日付を返すため、抑制は **JST 00:00 にリセット** される。

- cron `5 * * * *` で運用する場合、JST 0:00 をまたいだ最初の実行 (0:05) から新しい日扱い
- 引き続きしきい値を超えていれば、新しい日の 1 通目として再び投稿される
- 過去日の履歴は累積保存され削除されない (テスト等で同日に再投稿したい場合は `--force-notify`、または `DELETE FROM notified_alerts WHERE date='YYYY-MM-DD' AND identity_arn='...'`)

## 注意事項

- 出力されるコストは公開単価からの**推計値**であり、実際の AWS 請求額とは一致しない場合がある（経験的に全カテゴリ一律 15〜17% トークンが不足する傾向あり）。
- 正確な請求額は AWS Cost Explorer の usage type 別内訳を参照すること。
- 本レポートは「ユーザー別・モデル別の利用比率」を把握する用途に適している。
- ペイロード本体ファイル（`_input.json.gz` / `_output.json.gz`）は集計対象から除外している。
- ログのパスに含まれる `YYYY/MM/DD/HH/` は **UTC** 表記である点に注意。

## ライセンス

社内利用のための内製ツール。
