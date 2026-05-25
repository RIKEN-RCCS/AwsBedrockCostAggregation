# AWS Bedrock Cost Aggregation

S3 に出力された AWS Bedrock の Model Invocation Logs を集計し、**JST 当日のユーザー別コスト** を算出して、しきい値を超えたユーザーを **Slack に通知** するツール。毎時 cron で起動する運用を想定する。

ダウンロード済みの S3 オブジェクトと展開済みレコードはローカル SQLite DB に保存され、再実行時は DB に無いオブジェクトのみを取得する差分処理に対応する。

参考用に、月次 Markdown レポートを生成する `bedrock_cost_report.py` も同梱している（[月次レポート (参考)](#月次レポート-参考) 節を参照）。

## 構成

| ファイル | 役割 |
| --- | --- |
| `bedrock_daily_alert.py` | 日次コストアラートのエントリポイント。S3 取り込み → 集計 → しきい値判定 → Slack 投稿 |
| `bedrock_cost_report.py` | 月次レポート生成スクリプト（参考機能）。S3 取り込みロジックは日次アラートからも再利用 |
| `resolve_owner.py` | `BedrockAPIKey-*` IAM ユーザーのタグから所有者名を解決するヘルパー |
| `config.yaml` | 日次アラートの設定（しきい値・Slack チャンネル・S3 バケット等）。`config.yaml.example` から複製 |
| `config.yaml.example` | 設定テンプレート |
| `run_daily_alert.sh` | cron から呼ぶラッパ。`SLACK_BOT_TOKEN` を読み込んで `bedrock_daily_alert.py` を起動 |

## 前提

- Python 3.9+ (`zoneinfo` 利用のため 3.9+ 必須)
- `pip install boto3 pyyaml slack_sdk`
- AWS 認証情報が設定済み（IAM / S3 / Bedrock の参照権限）
- Bedrock の Invocation Logging が有効化され、S3 にログが出力されていること
- Slack Bot トークン（`chat:write` スコープ、通知先チャンネルへの招待済み）

### モデル呼び出しログ記録の設定 (参考)

#### 1. S3 ストレージの設定

* AWS コンソールにて、リージョンが「米国 (バージニア北部)」であることを確認する
* AWS コンソールで S3 サービスへ移動、左メニューから「汎用バケット」を選択する
* 「バケットを作成」をクリックする
* 「バケットタイプ」は「汎用」、「バケット名前空間」は「アカウントのリージョナル名前空間」を選択、「バケット名の接頭辞」に `bedrock-invocation-logs` を入力する
* その他はデフォルト設定のまま
* 「バケットを作成」をクリックする
* 生成されたバケット `s3://bedrock-invocation-logs-...-us-east-1-an` がログの保存場所になる

#### 2. ログの設定

* AWS コンソールで Bedrock サービスへ移動、左メニューから「設定」を選択する
* 「モデル呼び出しのログ記録」を ON にする
* 「ログに含めるデータタイプを選択 - オプション」は全てチェックを外す（ログサイズ削減、データ流出リスク回避のため）
* 「ログ記録先を選択」は「S3 のみ」を選択
* 「S3 の場所」に先ほどのバケットの場所 `s3://bedrock-invocation-logs-...-us-east-1-an` を入力する

## セットアップ

1. 依存パッケージをインストール:
   ```bash
   pip install boto3 pyyaml slack_sdk
   ```
2. 設定ファイルを作成し、自環境の値を埋める:
   ```bash
   cp config.yaml.example config.yaml
   $EDITOR config.yaml
   ```
3. Slack Bot Token を `~/.secrets/slack_tokens.sh` に配置 (chmod 600 推奨):
   ```bash
   export SLACK_BOT_TOKEN="xoxb-XXXXXXXXXXXX-XXXXXXXXXXXX-XXXXXXXXXXXXXXXXXXXXXXXX"
   ```
4. 動作確認 (Slack には投げない):
   ```bash
   ./run_daily_alert.sh --dry-run
   ```
5. cron に登録（[cron 例](#cron-例) を参照）

## config.yaml

機密値（Slack Bot Token）は YAML には書かず、環境変数 `SLACK_BOT_TOKEN` で渡す。`config.yaml` 自体も `.gitignore` 対象（バケット名・チャンネル ID を含むため）。

| キー | 必須 | 説明 |
| --- | :---: | --- |
| `bucket` | ✅ | Bedrock invocation log の出力先 S3 バケット名 |
| `region` | ✅ | バケット内ログのリージョン (例: `us-east-1`) |
| `db_path` | ✅ | SQLite DB のパス（相対指定時は config からの相対） |
| `daily_threshold_usd` | ✅ | 1 ユーザーあたりの日次しきい値 (USD)。これを超えると通知 |
| `slack.channel_id` | ✅ | 通知先 Slack チャンネル ID（`C` で始まる ID） |
| `slack.username` | ─ | 投稿時の表示名（任意） |
| `slack.icon_emoji` | ─ | 投稿時のアイコン絵文字（任意、例: `:money_with_wings:`） |
| `usd_to_jpy` | ─ | 為替レート。指定時は通知メッセージに JPY も併記 |
| `resolve_owner` | ─ | IAM タグからの所有者解決（既定 `true`） |

### サンプル

機密値はマスク済み。実値はサイト側で埋めること。

```yaml
# 必須
bucket: bedrock-invocation-logs-XXXXXXXXXXXX-us-east-1-XX
region: us-east-1
db_path: ./bedrock_logs.db
daily_threshold_usd: 5.0

slack:
  channel_id: CXXXXXXXXXX           # 通知先チャンネル ID
  username: Bedrock Cost Bot        # 任意
  icon_emoji: ":money_with_wings:"  # 任意

# 任意
usd_to_jpy: 155.0
resolve_owner: true
```

`SLACK_BOT_TOKEN` は YAML には書かない:

```bash
# ~/.secrets/slack_tokens.sh (chmod 600)
export SLACK_BOT_TOKEN="xoxb-XXXXXXXXXXXX-XXXXXXXXXXXX-XXXXXXXXXXXXXXXXXXXXXXXX"
```

## 実行

```bash
# 通常実行 (毎時 cron 想定)
./run_daily_alert.sh

# 投稿せず stderr に整形済みメッセージのみ出力
./run_daily_alert.sh --dry-run

# 過去日のリプレイ
./run_daily_alert.sh --date 2026-05-24 --dry-run

# 通知履歴を無視して再投稿 (テスト用)
./run_daily_alert.sh --force-notify

# S3 取り込みをスキップし DB 内容だけで判定
./run_daily_alert.sh --skip-ingest --dry-run
```

### cron 例

```cron
5 * * * * /path/to/AwsBedrockCostAggregation/run_daily_alert.sh
```

`run_daily_alert.sh` が `~/.secrets/slack_tokens.sh` を source して `SLACK_BOT_TOKEN` を export するため、cron の環境にトークンを直書きする必要はない。

### ログ

`run_daily_alert.sh` は実行ごとの stdout/stderr を **JST 日付付きファイル** に追記する。

- 出力先: `logs/daily_alert-YYYY-MM-DD.log` (スクリプトと同階層、自動作成)
- 各起動の冒頭に `===== <JST タイムスタンプ> =====`、末尾に `----- exit=<code> -----` を記録
- ローテーションは行わない（無限 append）。長期運用で肥大化が気になる場合は手動・cron 等で古い `daily_alert-*.log` を削除する

### 主なオプション

| フラグ | 説明 |
| --- | --- |
| `--config <path>` | YAML 設定ファイル（デフォルト `config.yaml`） |
| `--date YYYY-MM-DD` | 集計対象 JST 日付。省略時は本日 (JST) |
| `--dry-run` | Slack 投稿せず stderr に出力。通知履歴も書き込まない |
| `--force-notify` | 通知履歴を無視して再投稿 |
| `--skip-ingest` | S3 取り込みをスキップし DB 内容のみで判定 |

## 仕組み

- 起動時刻の JST 日付に該当する UTC 範囲 `[JST 00:00, JST 24:00)` で `invocations.timestamp` を絞り込み、ユーザー × モデルでコスト計算
- しきい値超過ユーザーごとに 1 通の Slack メッセージを `chat.postMessage` で投稿（モデル別内訳を含む）
- 通知履歴は SQLite テーブル `notified_alerts(date, identity_arn, ...)` に `PRIMARY KEY (date, identity_arn)` で記録
- 月初 JST など UTC 範囲が前月をまたぐ日は、当該の 2 か月分の S3 取り込みが走る（`processed_objects` の dedup により再実行は安価）

### 通知抑制のリセットタイミング

通知履歴の `date` カラムには **JST の日付** が格納される。抑制は **JST 00:00 にリセット** される。

- cron `5 * * * *` で運用する場合、JST 0:00 をまたいだ最初の実行 (0:05) から新しい日扱い
- 引き続きしきい値を超えていれば、新しい日の 1 通目として再び投稿される
- 過去日の履歴は累積保存され削除されない（テスト等で同日に再投稿したい場合は `--force-notify`、または `DELETE FROM notified_alerts WHERE date='YYYY-MM-DD' AND identity_arn='...'`）

## 所有者名の解決 (任意)

`BedrockAPIKey-*` という命名の IAM ユーザーに以下のタグを付けておくと、通知メッセージで実利用者名に解決される。

| タグキー | 例 |
| --- | --- |
| `Owner` | メンバー識別子 |
| `Email` | 連絡先（任意） |
| `Department` | 部署名（任意） |
| `Project` | プロジェクト名（任意） |

タグが無い場合は `(未タグ)` として表示される。`config.yaml` で `resolve_owner: false` を指定すると IAM API 呼び出しを抑止できる。

## 料金単価

`bedrock_cost_report.py` の `PRICING` 辞書に Claude 系モデルの単価を定義しており、`bedrock_daily_alert.py` も同関数を再利用する。AWS Bedrock の Regional endpoint プレミアム (+10%) と Prompt Caching (5 分 / 1 時間 TTL) の単価補正に対応。

未登録のモデルが現れた場合はコスト計算から除外され、stderr に警告が出る。新しいモデルを追加する場合は `PRICING` を編集すること。

公式単価:

- Claude API 公開単価: <https://platform.claude.com/docs/ja/about-claude/pricing>
- AWS Bedrock 単価: <https://aws.amazon.com/bedrock/pricing/>

## 月次レポート (参考)

集計内容を Markdown ファイルでまとめて閲覧したい場合は `bedrock_cost_report.py` を直接実行する。日次アラートとは独立して動作するが、SQLite DB を共有するため取り込み済みデータをそのまま再利用できる。

```bash
python bedrock_cost_report.py \
    --bucket <ログ出力先 S3 バケット名> \
    --region us-east-1 \
    --year 2026 --month 5 \
    --output report_202605.md
```

## トラブルシュート

- **`❌ env SLACK_BOT_TOKEN is required`**: `~/.secrets/slack_tokens.sh` が読み込まれていない。`run_daily_alert.sh` 経由で起動するか、手動で `source` してから python を呼ぶ。
- **Slack に投稿されない / `not_in_channel`**: Bot を通知先チャンネルに `/invite` する。
- **`PRICING` 未登録モデル警告**: stderr のモデル ID を `bedrock_cost_report.py` の `PRICING` 辞書に追記。
- **タグ解決でハング・権限エラー**: `iam:ListUsers` / `iam:ListUserTags` 権限を確認。`config.yaml` で `resolve_owner: false` にすれば回避可能。

## 注意事項

- 出力されるコストは公開単価からの**推計値**であり、実際の AWS 請求額とは一致しない場合がある（経験的に全カテゴリ一律 15〜17% トークンが不足する傾向あり）。
- 正確な請求額は AWS Cost Explorer の usage type 別内訳を参照すること。
- 本ツールは「ユーザー別・モデル別の利用比率」を把握する用途に適している。
- ペイロード本体ファイル (`_input.json.gz` / `_output.json.gz`) は集計対象から除外している。
- S3 ログのパスに含まれる `YYYY/MM/DD/HH/` は **UTC** 表記である点に注意（本ツールは UTC ↔ JST 変換を内部で吸収する）。

## ライセンス

社内利用のための内製ツール。
