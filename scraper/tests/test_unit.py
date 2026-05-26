"""
ユニットテスト: ブラウザ・認証不要で実行できるロジックテスト

実行:
    make test
"""

from datetime import date

import pytest

from mf_scraper import (
    DateRange,
    MoneyForwardScraper,
    Transaction,
    range_current_month,
    range_last_month,
    range_specific_month,
    range_this_week,
    range_last_week,
)
from notion_writer import NotionWriter, NotionJobUpdater
from main import ensure_notion_write_succeeded


# ---------------------------------------------------------------------------
# DateRange
# ---------------------------------------------------------------------------

class TestDateRange:
    def test_label_format(self):
        dr = DateRange(start=date(2026, 4, 1), end=date(2026, 4, 30))
        assert dr.label() == "20260401_20260430"

    def test_str_format(self):
        dr = DateRange(start=date(2026, 4, 1), end=date(2026, 4, 30))
        assert str(dr) == "2026-04-01 〜 2026-04-30"


# ---------------------------------------------------------------------------
# Notion書き込み結果
# ---------------------------------------------------------------------------

class TestNotionWriteResult:
    def test_success_when_no_errors(self):
        ensure_notion_write_succeeded({"created": 3, "updated": 2, "errors": 0})

    def test_raises_when_errors_exist(self):
        with pytest.raises(RuntimeError, match="errors=1"):
            ensure_notion_write_succeeded({"created": 3, "updated": 2, "errors": 1})


class TestNotionDatabaseValidation:
    @staticmethod
    def database_with_properties(**types: str) -> dict:
        return {"properties": {name: {"type": prop_type} for name, prop_type in types.items()}}

    def test_accepts_required_transaction_schema(self):
        db = self.database_with_properties(**NotionWriter.REQUIRED_TRANSACTION_PROPERTIES)
        NotionWriter._validate_database_properties(db)

    def test_job_id_is_optional_when_worker_job_is_used(self):
        db = self.database_with_properties(**NotionWriter.REQUIRED_TRANSACTION_PROPERTIES)
        NotionWriter._validate_database_properties(db, include_job_page_id=True)

    def test_rejects_wrong_property_type(self):
        props = dict(NotionWriter.REQUIRED_TRANSACTION_PROPERTIES)
        props["金額"] = "rich_text"
        db = self.database_with_properties(**props)
        with pytest.raises(RuntimeError, match="金額"):
            NotionWriter._validate_database_properties(db)

    def test_includes_job_id_when_optional_property_exists(self):
        writer = NotionWriter.__new__(NotionWriter)
        writer._transaction_property_types = {"ジョブID": "rich_text"}
        writer._optional_property_warnings = set()

        props = writer._build_properties(
            Transaction(
                date="2026-05-01",
                amount=-100,
                category="食費",
                sub_category="外食",
                account="カード",
                memo="test",
            ),
            scraped_at="2026-05-01T00:00:00Z",
            job_page_id="job-page-id",
        )

        assert props["ジョブID"] == {"rich_text": [{"text": {"content": "job-page-id"}}]}

    def test_skips_job_id_when_optional_property_is_missing(self):
        writer = NotionWriter.__new__(NotionWriter)
        writer._transaction_property_types = {}
        writer._optional_property_warnings = set()

        props = writer._build_properties(
            Transaction(
                date="2026-05-01",
                amount=-100,
                category="食費",
                sub_category="外食",
                account="カード",
                memo="test",
            ),
            scraped_at="2026-05-01T00:00:00Z",
            job_page_id="job-page-id",
        )

        assert "ジョブID" not in props


class FakePagesClient:
    def __init__(self, properties: dict):
        self.properties = properties
        self.updated_properties = None

    def retrieve(self, page_id: str) -> dict:
        return {"properties": self.properties}

    def update(self, page_id: str, properties: dict) -> None:
        self.updated_properties = properties


class FakeNotionClient:
    def __init__(self, properties: dict):
        self.pages = FakePagesClient(properties)


class TestNotionJobUpdater:
    @staticmethod
    def updater_with_properties(**types: str) -> NotionJobUpdater:
        updater = NotionJobUpdater.__new__(NotionJobUpdater)
        updater.client = FakeNotionClient({
            name: {"type": prop_type}
            for name, prop_type in types.items()
        })
        updater._property_cache = {}
        return updater

    def test_updates_only_existing_job_properties(self):
        updater = self.updater_with_properties(
            状態="select",
            進捗="rich_text",
            取引件数="number",
            最終更新日時="date",
        )

        updater.update(
            "job-page-id",
            状態="実行中",
            進捗="明細取得中",
            取引件数=12,
            残高件数=3,
            最終更新日時="2026-05-27T00:00:00Z",
        )

        assert updater.client.pages.updated_properties == {
            "状態": {"select": {"name": "実行中"}},
            "取引件数": {"number": 12},
            "進捗": {"rich_text": [{"text": {"content": "明細取得中"}}]},
            "最終更新日時": {"date": {"start": "2026-05-27T00:00:00Z"}},
        }

    def test_skips_when_property_type_does_not_match(self):
        updater = self.updater_with_properties(取引件数="rich_text")

        updater.update("job-page-id", 取引件数=12)

        assert updater.client.pages.updated_properties is None


class TestAccountNormalization:
    def test_normalizes_vpass_account_to_ana_card(self):
        assert NotionWriter.normalize_account("三井住友カード (VpassID)") == "ANAカード"

    def test_normalizes_fullwidth_vpass_account_to_ana_card(self):
        assert NotionWriter.normalize_account("三井住友カード（VPassID）") == "ANAカード"

    def test_keeps_non_alias_account(self):
        assert NotionWriter.normalize_account("住信SBIネット銀行") == "住信SBIネット銀行"

    def test_account_filter_matches_new_and_legacy_names(self):
        tx = Transaction(
            date="2026-05-01",
            amount=-100,
            category="食費",
            sub_category="外食",
            account="三井住友カード (VpassID)",
            memo="test",
        )
        assert NotionWriter._account_filter(tx) == {
            "or": [
                {"property": "口座", "select": {"equals": "ANAカード"}},
                {"property": "口座", "select": {"equals": "三井住友カード (VpassID)"}},
            ]
        }


# ---------------------------------------------------------------------------
# 期間ヘルパー
# ---------------------------------------------------------------------------

class TestRangeHelpers:
    def test_current_month_start_is_first(self):
        dr = range_current_month()
        assert dr.start.day == 1

    def test_current_month_end_is_today(self):
        dr = range_current_month()
        assert dr.end == date.today()

    def test_last_month_start_is_first(self):
        dr = range_last_month()
        assert dr.start.day == 1

    def test_last_month_is_different_from_current(self):
        curr = range_current_month()
        last = range_last_month()
        assert last.end < curr.start

    def test_specific_month_january(self):
        dr = range_specific_month(2026, 1)
        assert dr.start == date(2026, 1, 1)
        assert dr.end == date(2026, 1, 31)

    def test_specific_month_february_non_leap(self):
        dr = range_specific_month(2025, 2)
        assert dr.start == date(2025, 2, 1)
        assert dr.end == date(2025, 2, 28)

    def test_specific_month_february_leap(self):
        dr = range_specific_month(2024, 2)
        assert dr.end == date(2024, 2, 29)

    def test_specific_month_april(self):
        dr = range_specific_month(2026, 4)
        assert dr.end == date(2026, 4, 30)

    def test_this_week_starts_monday(self):
        dr = range_this_week()
        assert dr.start.weekday() == 0  # 月曜日

    def test_this_week_ends_today(self):
        dr = range_this_week()
        assert dr.end == date.today()

    def test_last_week_starts_monday(self):
        dr = range_last_week()
        assert dr.start.weekday() == 0

    def test_last_week_ends_sunday(self):
        dr = range_last_week()
        assert dr.end.weekday() == 6  # 日曜日

    def test_last_week_is_7_days(self):
        dr = range_last_week()
        assert (dr.end - dr.start).days == 6

    def test_last_week_precedes_this_week(self):
        this = range_this_week()
        last = range_last_week()
        assert last.end < this.start


# ---------------------------------------------------------------------------
# 金額パース（scraper内のロジックを直接検証）
# ---------------------------------------------------------------------------

class TestAmountParsing:
    """_scrape_transactions 内の金額パース処理を単独でテスト"""

    @staticmethod
    def parse_amount(raw: str) -> int:
        first_line = raw.strip().split("\n")[0]
        normalized = (
            first_line
            .replace(",", "")
            .replace("円", "")
            .replace("−", "-")
            .replace("－", "-")
            .replace(" ", "")
        )
        return int(normalized)

    def test_negative_amount(self):
        assert self.parse_amount("-1,450") == -1450

    def test_positive_amount(self):
        assert self.parse_amount("200,000") == 200000

    def test_transfer_with_annotation(self):
        assert self.parse_amount("-10000\n(振替)") == -10000

    def test_em_dash_minus(self):
        assert self.parse_amount("−500") == -500

    def test_fullwidth_minus(self):
        assert self.parse_amount("－1200") == -1200

    def test_with_yen_symbol(self):
        assert self.parse_amount("3,000円") == 3000

    def test_with_spaces(self):
        assert self.parse_amount(" 500 ") == 500


# ---------------------------------------------------------------------------
# 日付パース（NotionWriter._parse_date）
# ---------------------------------------------------------------------------

class TestDateParsing:
    def test_slash_full_date(self):
        assert NotionWriter._parse_date("2026/05/01") == "2026-05-01"

    def test_hyphen_full_date(self):
        assert NotionWriter._parse_date("2026-05-01") == "2026-05-01"

    def test_month_day_slash(self):
        result = NotionWriter._parse_date("05/22")
        assert result == f"{date.today().year}-05-22"

    def test_month_day_with_weekday(self):
        # "05/22(金)" のような形式 — re.match で (金) は無視される
        result = NotionWriter._parse_date("05/22(金)")
        assert result == f"{date.today().year}-05-22"

    def test_single_digit_month_day(self):
        result = NotionWriter._parse_date("1/3")
        assert result == f"{date.today().year}-01-03"

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            NotionWriter._parse_date("invalid-date")


# ---------------------------------------------------------------------------
# 明細日付パース（MoneyForwardScraper._parse_transaction_date）
# ---------------------------------------------------------------------------

class TestParseTransactionDate:
    def test_full_date_slash(self):
        assert MoneyForwardScraper._parse_transaction_date("2026/05/22") == date(2026, 5, 22)

    def test_full_date_hyphen(self):
        assert MoneyForwardScraper._parse_transaction_date("2026-05-22") == date(2026, 5, 22)

    def test_month_day_with_weekday(self):
        result = MoneyForwardScraper._parse_transaction_date("05/22(金)")
        assert result == date(date.today().year, 5, 22)

    def test_month_day_no_weekday(self):
        result = MoneyForwardScraper._parse_transaction_date("05/01")
        assert result == date(date.today().year, 5, 1)

    def test_month_day_uses_date_range_year(self):
        dr = DateRange(start=date(2025, 12, 1), end=date(2025, 12, 31))
        result = MoneyForwardScraper._parse_transaction_date("12/15(月)", date_range=dr)
        assert result == date(2025, 12, 15)

    def test_month_day_resolves_cross_year_range(self):
        dr = DateRange(start=date(2025, 12, 29), end=date(2026, 1, 4))
        dec_result = MoneyForwardScraper._parse_transaction_date("12/31(水)", date_range=dr)
        jan_result = MoneyForwardScraper._parse_transaction_date("01/01(木)", date_range=dr)
        assert dec_result == date(2025, 12, 31)
        assert jan_result == date(2026, 1, 1)

    def test_single_digit(self):
        result = MoneyForwardScraper._parse_transaction_date("1/3")
        assert result == date(date.today().year, 1, 3)

    def test_invalid_returns_none(self):
        assert MoneyForwardScraper._parse_transaction_date("invalid") is None

    def test_date_in_range(self):
        dr = DateRange(start=date(2026, 4, 1), end=date(2026, 4, 30))
        tx_date = MoneyForwardScraper._parse_transaction_date("04/15")
        assert tx_date is not None
        assert dr.start <= tx_date <= dr.end

    def test_date_out_of_range(self):
        dr = DateRange(start=date(2026, 4, 1), end=date(2026, 4, 30))
        tx_date = MoneyForwardScraper._parse_transaction_date("05/01(金)")
        assert tx_date is not None
        assert not (dr.start <= tx_date <= dr.end)
