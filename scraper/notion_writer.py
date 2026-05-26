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
    ACCOUNT_ALIASES = {
        "ANAカード": [
            "三井住友カード(vpassid)",
        ],
    }

    REQUIRED_TRANSACTION_PROPERTIES = {
        "メモ": "title",
        "取引日": "date",
        "金額": "number",
        "カテゴリ": "select",
        "サブカテゴリ": "select",
        "口座": "select",
        "収支": "select",
        "スクレイプ日時": "date",
    }

    def __init__(self, token: str, db_id: str):
        self.client = Client(auth=token)
        self.db_id = db_id

    def validate_database(self, include_job_page_id: bool = False) -> None:
        """書き込み前にDBアクセス権と必要プロパティを検証する"""
        try:
            database = self.client.databases.retrieve(database_id=self.db_id)
        except Exception as e:
            raise RuntimeError(
                "Notion DBにアクセスできません。NOTION_DB_ID とIntegration共有設定を確認してください。"
            ) from e
        self._validate_database_properties(database, include_job_page_id=include_job_page_id)

    @classmethod
    def _validate_database_properties(cls, database: dict, include_job_page_id: bool = False) -> None:
        required = dict(cls.REQUIRED_TRANSACTION_PROPERTIES)
        if include_job_page_id:
            required["ジョブID"] = "rich_text"

        properties = database.get("properties", {})
        missing = [name for name in required if name not in properties]
        wrong_types = []
        for name, expected_type in required.items():
            if name in properties and properties[name].get("type") != expected_type:
                actual_type = properties[name].get("type", "unknown")
                wrong_types.append(f"{name}: expected={expected_type}, actual={actual_type}")

        if missing or wrong_types:
            details = []
            if missing:
                details.append("不足=" + ", ".join(missing))
            if wrong_types:
                details.append("型不一致=" + "; ".join(wrong_types))
            raise RuntimeError("Notion DBスキーマが不足しています: " + " / ".join(details))

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
                    self._account_filter(tx),
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
            "口座": {"select": {"name": self.normalize_account(tx.account)}},
            "収支": {"select": {"name": self._derive_incexp(tx)}},
        }
        if scraped_at:
            props["スクレイプ日時"] = {"date": {"start": scraped_at}}
        if job_page_id:
            props["ジョブID"] = {"rich_text": [{"text": {"content": job_page_id}}]}
        return props

    @classmethod
    def normalize_account(cls, account: str) -> str:
        normalized = " ".join((account or "").split())
        if not normalized:
            return "不明"

        key = cls._account_alias_key(normalized)
        for canonical, aliases in cls.ACCOUNT_ALIASES.items():
            if key in aliases:
                return canonical
        return normalized

    @classmethod
    def _account_filter(cls, tx: Transaction) -> dict:
        accounts = []
        for account in (cls.normalize_account(tx.account), " ".join((tx.account or "").split())):
            if account and account not in accounts:
                accounts.append(account)

        filters = [{"property": "口座", "select": {"equals": account}} for account in accounts]
        return filters[0] if len(filters) == 1 else {"or": filters}

    @staticmethod
    def _account_alias_key(account: str) -> str:
        return (
            account
            .replace("（", "(")
            .replace("）", ")")
            .replace(" ", "")
            .lower()
        )

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
