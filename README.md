# mf-automation

MoneyForward ME から入出金明細・口座残高を自動取得し、CSV 出力および Notion DB への書き込みを行うスクレイパーです。

## 機能

- **スクレイピング** — Playwright でログイン・明細・残高を取得
- **セッション永続化** — `session.json` を再利用し、毎回のログインを省略
- **OTP 自動化** — Google Apps Script (GAS) で Gmail の認証メールを読み取り、2段階認証を自動突破
- **日付範囲指定** — 今月 / 先月 / 今週 / 先週 / 指定月 / 任意期間
- **CSV 出力** — Excel 対応（UTF-8 BOM）
- **Notion Upsert** — 取引 DB に重複なしで書き込み

---

## ファイル構成

```
mf-automation/
├── .env.example              # 環境変数テンプレート
├── .gitignore
├── Makefile                  # 操作コマンド集
├── pyproject.toml            # pytest 設定
├── requirements.txt
├── gas/
│   ├── appsscript.json       # GAS マニフェスト
│   └── otp_reader.gs         # OTP 取得スクリプト
└── scraper/
    ├── main.py               # CLI エントリポイント
    ├── mf_scraper.py         # スクレイパー本体
    ├── notion_writer.py      # Notion Upsert
    └── tests/
        ├── test_unit.py      # ユニットテスト（37件）
        └── scenario_runner.py # 統合シナリオテスト（5種）
```

---

## セットアップ

### 1. 依存パッケージのインストール

```bash
make install
```

Python 仮想環境の作成・パッケージインストール・Playwright ブラウザのダウンロードを一括で行います。

### 2. 環境変数の設定

```bash
cp .env.example scraper/.env
```

`scraper/.env` を開いて値を入力します。

```env
# MoneyForward ME ログイン情報（必須）
MF_EMAIL=your_email@example.com
MF_PASSWORD=your_password

# Notion（オプション：Notion への書き込みが不要なら空欄でも可）
NOTION_TOKEN=secret_xxxxxxxxxxxxxxxxxxxx
NOTION_DB_ID=f783b354507648b697a2fecd0a10aaa8
MF_JOB_DB_ID=6b5a9b79d2564d3ca68d952de27c9103

# GAS OTP 自動取得（オプション：設定しなければ手動入力にフォールバック）
GAS_OTP_URL=https://script.google.com/macros/s/xxxxxxxxxx/exec
GAS_OTP_SECRET=your_secret_token
```

### 3. Notion 取引DBの準備

対象DBは以下です。

| 用途 | DB名 | 環境変数 | DB ID |
|------|------|----------|-------|
| 取引保存 | MF_取引履歴 | `NOTION_DB_ID` | `f783b354507648b697a2fecd0a10aaa8` |
| Workerジョブ履歴 | ジョブ実行履歴 | `MF_JOB_DB_ID` | `6b5a9b79d2564d3ca68d952de27c9103` |

Notion に書き込む場合は、`NOTION_TOKEN` のIntegrationに `MF_取引履歴` DBを共有してください。  
Workerからジョブページを作成する場合は、WorkerのNotion Integrationに `ジョブ実行履歴` DBも共有してください。  
現状仕様では、NotionにUpsertする対象は入出金明細のみです。口座残高はCSVに出力します。

`ジョブ実行履歴` DBは、Worker起動時にページを作成し、Python実行中に以下のタイミングで更新します。

| タイミング | 更新内容 |
|------------|----------|
| Worker起動直後 | 状態=実行中、実行モード、取得期間、開始日時 |
| GitHub Actions runner開始 | GitHub実行URL、進捗、最終更新日時 |
| MoneyForward明細取得中 | 取引件数をページ単位で更新 |
| 口座残高取得後 | 残高件数を更新 |
| Notion取引DB書き込み中 | 処理済み件数、作成件数、更新件数、エラー件数を5件ごとに更新 |
| 終了時 | 状態=完了/エラー、完了日時、エラー詳細 |

リアルタイムに近い進捗表示を使う場合は、`ジョブ実行履歴` DBに以下の任意プロパティを追加してください。未作成のプロパティは自動でスキップされます。

| プロパティ | 型 | 用途 |
|-----------|----|------|
| 進捗 | Text / Rich text | 現在の処理ステージ |
| 最終更新日時 | Date | 最後にジョブ履歴を更新した時刻 |
| 処理済み件数 | Number | Notion取引DBへ書き込み済みの件数 |
| 作成件数 | Number | 新規作成した取引件数 |
| 更新件数 | Number | 既存更新した取引件数 |
| エラー件数 | Number | 書き込みエラー件数 |

取引DBには以下のプロパティが必要です。

| プロパティ | 型 |
|-----------|----|
| メモ | Title |
| 取引日 | Date |
| 金額 | Number |
| カテゴリ | Select |
| サブカテゴリ | Select |
| 口座 | Select |
| 収支 | Select |
| スクレイプ日時 | Date |

Worker経由実行で取引とジョブ履歴を紐づけたい場合は、取引DBに任意で `ジョブID`（Text / Rich text）を追加してください。未作成の場合、`ジョブID` の保存だけをスキップして処理は継続します。  
実行前にDBアクセス権とプロパティ型を検証し、不足があればスクレイピング前に停止します。

口座名はNotion保存時に一部正規化します。

| MF上の口座名 | Notion保存名 |
|-------------|--------------|
| 三井住友カード (VpassID) / 三井住友カード（VPassID） | ANAカード |

`口座` Selectには `ANAカード` も追加してください。

---

## 使い方

### スクレイピング実行

```bash
make run                                              # 今月（デフォルト）
make run ARGS="--mode last_month"                     # 先月
make run ARGS="--mode this_week"                      # 今週
make run ARGS="--mode last_week"                      # 先週
make run ARGS="--mode month --month 2026-04"          # 指定月
make run ARGS="--mode range --from 2026-04-01 --to 2026-04-30"  # 任意期間
make run ARGS="--headless"                            # ヘッドレス（画面なし）
```

### 出力ファイル

実行後、`output/` に以下が生成されます（gitignore 済み）。

| ファイル | 内容 |
|---------|------|
| `transactions_YYYYMMDD_YYYYMMDD.csv` | 入出金明細（取引日 / 金額 / カテゴリ / 口座 / メモ） |
| `balances_YYYYMMDD_YYYYMMDD.csv` | 口座残高（口座名 / 残高 / 最終更新） |

---

## GAS OTP 自動化（任意）

MoneyForward のメール 2段階認証を自動突破するための Google Apps Script 設定です。  
**設定しない場合は、認証が必要なときに手動でコードを入力するプロンプトが表示されます。**

### 設定手順

1. [script.google.com](https://script.google.com) を **個人の Gmail アカウント** で開き、新規プロジェクトを作成
2. `gas/otp_reader.gs` の内容を貼り付け、`SECRET` を任意の文字列に変更
3. 左メニューから「サービス」→ **Gmail API** を追加
4. 「デプロイ」→「新しいデプロイ」で以下の設定でウェブアプリとして公開
   - 次のユーザーとして実行：**自分**
   - アクセスできるユーザー：**全員**
5. 発行されたデプロイ URL と SECRET を `.env` に設定

```env
GAS_OTP_URL=https://script.google.com/macros/s/XXXXXXXXXX/exec
GAS_OTP_SECRET=your_secret_token
```

> **注意:** Google Workspace アカウント（会社の Gmail）では「全員」アクセスが組織ポリシーで制限される場合があります。個人の Gmail アカウントを使用してください。

### 動作確認

```bash
curl "https://script.google.com/macros/s/YOUR_ID/exec?secret=YOUR_SECRET"
# {"code":"123456"} のように返れば成功
```

---

## テスト

### ユニットテスト（ブラウザ不要）

```bash
make test
```

日付範囲ロジック・金額パース・日付パース・Notionスキーマ処理をテストで検証します。

### 統合シナリオテスト（実ブラウザ・実アカウント使用）

```bash
make scenario                                         # 全シナリオ（S03 除く）
make scenario ARGS="--scenario valid_session"         # 個別実行
make scenario ARGS="--list"                           # シナリオ一覧を表示
```

| ID | シナリオ | 概要 |
|----|---------|------|
| S01 | `valid_session` | 有効なセッションでログイン不要スクレイピング |
| S02 | `no_session` | session.json なし → フルログイン → セッション保存 |
| S03 | `gas_otp_failure` | GAS 失敗時の手動入力フォールバック（手動実行のみ） |
| S04 | `date_range_modes` | 全日付モードで正しい期間のデータを取得 |
| S05 | `headless` | ヘッドレスモードで正常動作 |

ログは `output/test_logs/<scenario>_<timestamp>.log` に保存されます。

---

## セッションについて

初回実行時はブラウザが立ち上がり、ログインと 2段階認証を処理します。  
成功すると `scraper/session.json` が保存され、以降の実行ではログインをスキップします。

セッションが切れた場合は自動的に再ログインし、`session.json` を更新します。

> `session.json` はクッキー情報を含むため **`.gitignore` に設定済み**です。絶対にコミットしないでください。

---

## 注意事項

- **`.env` と `session.json` は絶対にコミットしない**（`.gitignore` で除外済み）
- MoneyForward ME の利用規約に従い、個人利用の範囲でご使用ください
- MF のページ構造が変更された場合、セレクタの更新が必要になることがあります
