"""
Notion API への書き込み（Upsert）
取引データを 家計簿_取引履歴 DB に書き込む
"""

import re
import time
from datetime import datetime
from typing import Optional
from notion_client import Client
from mf_scraper import Transaction, AssetBalance, ScrapingResult, DateRange


class NotionWriter:
    def __init__(self, token: str, db_id: str):
        self.client = Client(auth=token)
        self.db_id = db_id

    def upsert_transactions(
        self,
        transactions: list[Transaction],
        scraped_at: Optional[str] = None,
        job_page_id: Optional[str] = None,
    ) -> dict:
        """取引データをUpsert（作成 or 更新）する"""
        created = 0
        updated = 0
        errors = 0

        for tx in transactions:
            try:
                existing = self._find_existing(tx)
                if existing:
                    self._update_page(existing["id"], tx, scraped_at, job_page_id)
                    updated += 1
                else:
                    self._create_page(tx, scraped_at, job_page_id)
                    created += 1
                # Notion API レート制限対策（3req/s）
                time.sleep(0.4)
            except Exception as e:
                print(f"[Notion] エラー: {tx.date} {tx.amount}円 - {e}")
                errors += 1

        result = {"created": created, "updated": updated, "errors": errors}
        print(f"[Notion] Upsert完了: {result}")
        return result

    def _find_existing(self, tx: Transaction) -> Optional[dict]:
        """ユニークキー（取引日 + 金額 + 口座名）で既存ページを検索"""
        response = self.client.databases.query(
            database_id=self.db_id,
            filter={
                "and": [
                    {"property": "取引日", "date": {"equals": self._parse_date(tx.date)}},
                    {"property": "金額", "number": {"equals": tx.amount}},
                    {"property": "口座", "select": {"equals": tx.account}},
                ]
            }
        )
        results = response.get("results", [])
        return results[0] if results else None

    def _create_page(self, tx: Transaction, scraped_at: Optional[str], job_page_id: Optional[str]) -> None:
        self.client.pages.create(
            parent={"database_id": self.db_id},
            properties=self._build_properties(tx, scraped_at, job_page_id),
        )

    def _update_page(self, page_id: str, tx: Transaction, scraped_at: Optional[str], job_page_id: Optional[str]) -> None:
        self.client.pages.update(
            page_id=page_id,
            properties=self._build_properties(tx, scraped_at, job_page_id),
        )

    def _build_properties(self, tx: Transaction, scraped_at: Optional[str] = None, job_page_id: Optional[str] = None) -> dict:
        props: dict = {
            "メモ": {"title": [{"text": {"content": tx.memo or ""}}]},
            "取引日": {"date": {"start": self._parse_date(tx.date)}},
            "金額": {"number": tx.amount},
            "カテゴリ": {"select": {"name": tx.category or "未分類"}},
            "サブカテゴリ": {"select": {"name": tx.sub_category or "未分類"}},
            "口座": {"select": {"name": tx.account or "不明"}},
            "収支": {"select": {"name": self._derive_incexp(tx)}},
        }
        if scraped_at:
            props["スクレイプ日時"] = {"date": {"start": scraped_at}}
        if job_page_id:
            props["ジョブID"] = {"rich_text": [{"text": {"content": job_page_id}}]}
        return props

    @staticmethod
    def _derive_incexp(tx: Transaction) -> str:
        """amount の符号とカテゴリから収支区分を判定"""
        cat = (tx.category or "").strip()
        if cat == "振替":
            return "振替"
        return "収入" if tx.amount > 0 else "支出"

    @staticmethod
    def _parse_date(date_str: str) -> str:
        """MM/DD や YYYY/MM/DD 形式を YYYY-MM-DD に変換"""
        date_str = date_str.strip()

        # YYYY/MM/DD または YYYY-MM-DD
        m = re.match(r"(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})", date_str)
        if m:
            return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

        # MM/DD（当年として処理）
        m = re.match(r"(\d{1,2})[/\-](\d{1,2})", date_str)
        if m:
            year = datetime.now().year
            return f"{year}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"

        raise ValueError(f"日付パース失敗: {date_str}")


class NotionJobUpdater:
    """ジョブ実行履歴ページのステータスを更新する"""

    def __init__(self, token: str):
        self.client = Client(auth=token)

    def update(self, job_page_id: str, **props) -> None:
        notion_props: dict = {}
        if "状態" in props:
            notion_props["状態"] = {"select": {"name": props["状態"]}}
        if "完了日時" in props:
            notion_props["完了日時"] = {"date": {"start": props["完了日時"]}}
        if "取引件数" in props:
            notion_props["取引件数"] = {"number": props["取引件数"]}
        if "残高件数" in props:
            notion_props["残高件数"] = {"number": props["残高件数"]}
        if "取得期間" in props:
            notion_props["取得期間"] = {"rich_text": [{"text": {"content": props["取得期間"]}}]}
        if "GitHub実行URL" in props:
            notion_props["GitHub実行URL"] = {"url": props["GitHub実行URL"]}
        if "エラー詳細" in props:
            notion_props["エラー詳細"] = {"rich_text": [{"text": {"content": str(props["エラー詳細"])[:2000]}}]}
        self.client.pages.update(page_id=job_page_id, properties=notion_props)
