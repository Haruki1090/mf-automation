"""
Notion API への書き込み（Upsert）
取引データを 家計簿_取引履歴 DB に書き込む
"""

import re
import time
from datetime import datetime
from typing import Optional
from notion_client import Client
from mf_scraper import Transaction, AssetBalance


class NotionWriter:
    def __init__(self, token: str, db_id: str):
        self.client = Client(auth=token)
        self.db_id = db_id

    def upsert_transactions(self, transactions: list[Transaction]) -> dict:
        """取引データをUpsert（作成 or 更新）する"""
        created = 0
        updated = 0
        errors = 0

        for tx in transactions:
            try:
                existing = self._find_existing(tx)
                if existing:
                    self._update_page(existing["id"], tx)
                    updated += 1
                else:
                    self._create_page(tx)
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

    def _create_page(self, tx: Transaction) -> None:
        self.client.pages.create(
            parent={"database_id": self.db_id},
            properties=self._build_properties(tx),
        )

    def _update_page(self, page_id: str, tx: Transaction) -> None:
        self.client.pages.update(
            page_id=page_id,
            properties=self._build_properties(tx),
        )

    def _build_properties(self, tx: Transaction) -> dict:
        return {
            "取引日": {"date": {"start": self._parse_date(tx.date)}},
            "金額": {"number": tx.amount},
            "カテゴリ": {"select": {"name": tx.category or "未分類"}},
            "サブカテゴリ": {"select": {"name": tx.sub_category or "未分類"}},
            "口座": {"select": {"name": tx.account or "不明"}},
            "メモ": {"rich_text": [{"text": {"content": tx.memo or ""}}]},
        }

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
