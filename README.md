# AWS Bedrock Cost Aggregation

S3 に出力された AWS Bedrock の Model Invocation Logs を集計し、IAM ユーザー別・モデル別のトークン消費量と推計コストを月次 Markdown レポートとして出力するツール。

ダウンロード済みの S3 オブジェクトと展開済みレコードはローカル SQLite DB に保存され、再実行時は DB に無いオブジェクトのみを取得する差分処理に対応する。

## 構成

| ファイル | 役割 |
| --- | --- |
| `bedrock_cost_report.py` | メインスクリプト。S3 から取り込み・DB 保存・レポート生成を行う |
| `resolve_owner.py` | `BedrockAPIKey-*` IAM ユーザーのタグから所有者名を解決するヘルパー |

## 前提

- Python 3.9+
- `boto3` がインストール済み
- AWS 認証情報が設定済み（IAM / S3 / Bedrock の参照権限）
- Bedrock の Invocation Logging が有効化され、S3 にログが出力されていること

```bash
pip install boto3
```

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

## 注意事項

- 出力されるコストは公開単価からの**推計値**であり、実際の AWS 請求額とは一致しない場合がある（経験的に全カテゴリ一律 15〜17% トークンが不足する傾向あり）。
- 正確な請求額は AWS Cost Explorer の usage type 別内訳を参照すること。
- 本レポートは「ユーザー別・モデル別の利用比率」を把握する用途に適している。
- ペイロード本体ファイル（`_input.json.gz` / `_output.json.gz`）は集計対象から除外している。
- ログのパスに含まれる `YYYY/MM/DD/HH/` は **UTC** 表記である点に注意。

## ライセンス

社内利用のための内製ツール。
