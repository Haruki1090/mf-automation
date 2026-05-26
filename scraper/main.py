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
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")
load_dotenv(Path(__file__).parent.parent / ".env")

from mf_scraper import (
    MoneyForwardScraper, ScrapingResult, DateRange,
    range_current_month, range_last_month, range_specific_month,
    range_this_week, range_last_week,
)
from notion_writer import NotionWriter


OUTPUT_DIR = Path(__file__).parent.parent / "output"


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


def main():
    args = parse_args()

    email = os.getenv("MF_EMAIL")
    password = os.getenv("MF_PASSWORD")
    if not email or not password:
        print("ERROR: .env に MF_EMAIL と MF_PASSWORD を設定してください")
        sys.exit(1)

    date_range = resolve_date_range(args)

    print("=" * 50)
    print("MoneyForward ME スクレイピング開始")
    print(f"モード: {args.mode}  期間: {date_range}")
    print(f"headless: {args.headless}")
    print("=" * 50)

    scraper = MoneyForwardScraper(
        email=email,
        password=password,
        headless=args.headless,
        gas_otp_url=os.getenv("GAS_OTP_URL"),
        gas_otp_secret=os.getenv("GAS_OTP_SECRET"),
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
    token = os.getenv("NOTION_TOKEN")
    db_id = os.getenv("NOTION_DB_ID")

    if token and db_id:
        print("\n--- Notion への書き込みを開始します ---")
        writer = NotionWriter(token=token, db_id=db_id)
        write_result = writer.upsert_transactions(result.transactions)
        print(f"書き込み結果: {write_result}")
    else:
        print("\n--- Notion設定なし: CSV のみ出力しました ---")


if __name__ == "__main__":
    main()
