"""
MoneyForward ME スクレイピング → CSV出力 → Notion書き込み（オプション）

使用例:
  python main.py                              # 今月（デフォルト）
  python main.py --mode last_month            # 先月
  python main.py --mode this_week             # 今週
  python main.py --mode last_week             # 先週
  python main.py --mode month --month 2026-04 # 指定月
  python main.py --mode range --from 2026-04-01 --to 2026-04-30  # 任意期間
  python main.py --headless                   # ヘッドレスモード
"""

import argparse
import csv
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")
load_dotenv(Path(__file__).parent.parent / ".env")

from mf_scraper import (
    MoneyForwardScraper, ScrapingResult, DateRange,
    range_current_month, range_last_month, range_specific_month,
    range_this_week, range_last_week,
)
from notion_writer import NotionWriter, NotionJobUpdater


OUTPUT_DIR = Path(__file__).parent.parent / "output"


def utc_now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def update_job_page(job_updater: Optional[NotionJobUpdater], job_page_id: Optional[str], **props) -> None:
    """ジョブ履歴の更新はベストエフォートにして本処理を止めない。"""
    if not job_updater or not job_page_id:
        return
    try:
        job_updater.update(job_page_id, **props)
    except Exception as e:
        print(f"[Notion] ジョブページ更新をスキップしました: {e}")


def build_progress_callback(job_updater: Optional[NotionJobUpdater], job_page_id: Optional[str]):
    def callback(event: dict) -> None:
        props: dict = {
            "状態": "実行中",
            "最終更新日時": utc_now_iso(),
        }
        message = event.get("message") or event.get("stage")
        if message:
            props["進捗"] = str(message)
        if "transactions_count" in event:
            props["取引件数"] = event["transactions_count"]
        if "balances_count" in event:
            props["残高件数"] = event["balances_count"]
        if "processed_count" in event:
            props["処理済み件数"] = event["processed_count"]
        if "created_count" in event:
            props["作成件数"] = event["created_count"]
        if "updated_count" in event:
            props["更新件数"] = event["updated_count"]
        if "error_count" in event:
            props["エラー件数"] = event["error_count"]
        update_job_page(job_updater, job_page_id, **props)

    return callback


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MoneyForward ME スクレイパー")
    parser.add_argument("--headless", action="store_true", help="ヘッドレスモードで実行")
    parser.add_argument(
        "--mode",
        choices=["current_month", "last_month", "this_week", "last_week", "month", "range"],
        default="current_month",
        help="取得期間モード（デフォルト: current_month）",
    )
    parser.add_argument("--month", metavar="YYYY-MM", help="指定月 (--mode month 時に使用)")
    parser.add_argument("--from", dest="from_date", metavar="YYYY-MM-DD", help="開始日 (--mode range 時に使用)")
    parser.add_argument("--to", dest="to_date", metavar="YYYY-MM-DD", help="終了日 (--mode range 時に使用)")
    parser.add_argument("--job-page-id", dest="job_page_id", metavar="PAGE_ID", help="ジョブ実行履歴ページID（Worker経由時に自動設定）")
    return parser.parse_args()


def resolve_date_range(args: argparse.Namespace) -> DateRange:
    if args.mode == "current_month":
        return range_current_month()
    if args.mode == "last_month":
        return range_last_month()
    if args.mode == "this_week":
        return range_this_week()
    if args.mode == "last_week":
        return range_last_week()
    if args.mode == "month":
        if not args.month:
            print("ERROR: --mode month には --month YYYY-MM が必要です")
            sys.exit(1)
        try:
            year, month = map(int, args.month.split("-"))
        except ValueError:
            print("ERROR: --month は YYYY-MM 形式で指定してください（例: 2026-04）")
            sys.exit(1)
        return range_specific_month(year, month)
    if args.mode == "range":
        if not args.from_date or not args.to_date:
            print("ERROR: --mode range には --from YYYY-MM-DD --to YYYY-MM-DD が必要です")
            sys.exit(1)
        try:
            return DateRange(
                start=date.fromisoformat(args.from_date),
                end=date.fromisoformat(args.to_date),
            )
        except ValueError:
            print("ERROR: 日付は YYYY-MM-DD 形式で指定してください（例: 2026-04-01）")
            sys.exit(1)
    # ここには到達しない
    return range_current_month()


def save_csv(result: ScrapingResult, date_range: DateRange) -> tuple[Path, Path]:
    OUTPUT_DIR.mkdir(exist_ok=True)
    label = date_range.label()

    tx_path = OUTPUT_DIR / f"transactions_{label}.csv"
    with open(tx_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["取引日", "金額", "カテゴリ", "サブカテゴリ", "口座", "メモ"])
        for tx in result.transactions:
            writer.writerow([tx.date, tx.amount, tx.category, tx.sub_category, tx.account, tx.memo])

    bal_path = OUTPUT_DIR / f"balances_{label}.csv"
    with open(bal_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["口座名", "残高", "更新日時"])
        for b in result.balances:
            writer.writerow([b.account, b.balance, b.updated_at])

    return tx_path, bal_path


def ensure_notion_write_succeeded(write_result: dict) -> None:
    errors = int(write_result.get("errors", 0))
    if errors:
        created = int(write_result.get("created", 0))
        updated = int(write_result.get("updated", 0))
        raise RuntimeError(
            "Notion書き込みでエラーが発生しました: "
            f"created={created}, updated={updated}, errors={errors}"
        )


def main():
    args = parse_args()

    email = os.getenv("MF_EMAIL")
    password = os.getenv("MF_PASSWORD")
    if not email or not password:
        print("ERROR: .env に MF_EMAIL と MF_PASSWORD を設定してください")
        sys.exit(1)

    date_range = resolve_date_range(args)
    token = os.getenv("NOTION_TOKEN")
    db_id = os.getenv("NOTION_DB_ID")
    job_page_id: str | None = args.job_page_id or None
    job_updater = NotionJobUpdater(token=token) if (token and job_page_id) else None

    print("=" * 50)
    print("MoneyForward ME スクレイピング開始")
    print(f"モード: {args.mode}  期間: {date_range}")
    print(f"headless: {args.headless}")
    print("=" * 50)

    try:
        update_job_page(
            job_updater,
            job_page_id,
            状態="実行中",
            取得期間=str(date_range),
            GitHub実行URL=os.getenv("ACTIONS_RUN_URL", ""),
            進捗="GitHub Actions runner 起動済み",
            最終更新日時=utc_now_iso(),
        )
        progress_callback = build_progress_callback(job_updater, job_page_id)

        writer = None
        if token and db_id:
            print("[Notion] DBアクセスとスキーマを検証します...")
            progress_callback({"message": "Notion取引DBのアクセスとスキーマを検証中"})
            writer = NotionWriter(token=token, db_id=db_id)
            writer.validate_database(include_job_page_id=bool(job_page_id))
            print("[Notion] DB検証OK")

        scraper = MoneyForwardScraper(
            email=email,
            password=password,
            headless=args.headless,
            gas_otp_url=os.getenv("GAS_OTP_URL"),
            gas_otp_secret=os.getenv("GAS_OTP_SECRET"),
            progress_callback=progress_callback,
        )
        result = scraper.scrape(date_range=date_range)

        # CSV 保存
        tx_path, bal_path = save_csv(result, date_range)
        print(f"\n[CSV] 明細: {tx_path}")
        print(f"[CSV] 残高: {bal_path}")

        # プレビュー（先頭5件）
        preview = {
            "scraped_at": result.scraped_at,
            "period": str(date_range),
            "transactions_count": len(result.transactions),
            "transactions": [vars(t) for t in result.transactions[:5]],
            "balances": [vars(b) for b in result.balances],
        }
        print("\n--- プレビュー（先頭5件） ---")
        print(json.dumps(preview, ensure_ascii=False, indent=2))

        # Notion 書き込み（NOTION_TOKEN と NOTION_DB_ID が設定されている場合のみ）
        if writer:
            print("\n--- Notion ステージングDBへの書き込みを開始します ---")
            progress_callback({
                "message": "Notion取引DBへ書き込み開始",
                "transactions_count": len(result.transactions),
                "balances_count": len(result.balances),
                "processed_count": 0,
                "created_count": 0,
                "updated_count": 0,
                "error_count": 0,
            })
            write_result = writer.upsert_transactions(
                result.transactions,
                scraped_at=result.scraped_at,
                job_page_id=job_page_id,
                progress_callback=progress_callback,
            )
            print(f"書き込み結果: {write_result}")
            ensure_notion_write_succeeded(write_result)
        else:
            print("\n--- Notion設定なし: CSV のみ出力しました ---")

        # ジョブページ: 完了に更新
        if job_updater and job_page_id:
            update_job_page(
                job_updater,
                job_page_id,
                状態="完了",
                完了日時=utc_now_iso(),
                取引件数=len(result.transactions),
                残高件数=len(result.balances),
                取得期間=str(date_range),
                GitHub実行URL=os.getenv("ACTIONS_RUN_URL", ""),
                進捗="完了",
                最終更新日時=utc_now_iso(),
            )
            print(f"[Notion] ジョブページを完了に更新しました: {job_page_id}")

    except Exception as e:
        print(f"ERROR: スクレイピング中に例外が発生しました: {e}")
        # ジョブページ: エラーに更新
        if job_updater and job_page_id:
            update_job_page(
                job_updater,
                job_page_id,
                状態="エラー",
                完了日時=utc_now_iso(),
                エラー詳細=str(e),
                GitHub実行URL=os.getenv("ACTIONS_RUN_URL", ""),
                進捗="エラー",
                最終更新日時=utc_now_iso(),
            )
            print(f"[Notion] ジョブページをエラーに更新しました: {job_page_id}")
        sys.exit(1)


if __name__ == "__main__":
    main()
