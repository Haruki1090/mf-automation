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
    range_current_month,
    range_last_month,
    range_specific_month,
    range_this_week,
    range_last_week,
)
from notion_writer import NotionWriter


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
