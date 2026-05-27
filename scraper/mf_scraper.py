"""
MoneyForward ME スクレイパー
Playwright を使ってログイン・入出金明細・残高を取得する
"""

import calendar
import json
import re
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Optional
from playwright.sync_api import sync_playwright, Page, BrowserContext, TimeoutError as PlaywrightTimeoutError

# 事前コンパイル済み正規表現
_RE_TX_DATE_FULL = re.compile(r"(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})")
_RE_TX_DATE_SHORT = re.compile(r"(\d{1,2})[/\-](\d{1,2})")

SESSION_FILE = Path(__file__).parent / "session.json"


# ---------------------------------------------------------------------------
# データクラス
# ---------------------------------------------------------------------------

@dataclass
class Transaction:
    date: str
    amount: int
    category: str
    sub_category: str
    account: str
    memo: str


@dataclass
class AssetBalance:
    account: str
    balance: int
    updated_at: str


@dataclass
class ScrapingResult:
    transactions: list[Transaction] = field(default_factory=list)
    balances: list[AssetBalance] = field(default_factory=list)
    scraped_at: str = field(default_factory=lambda: datetime.now().isoformat())


@dataclass
class DateRange:
    start: date
    end: date

    def label(self) -> str:
        return f"{self.start.strftime('%Y%m%d')}_{self.end.strftime('%Y%m%d')}"

    def __str__(self) -> str:
        return f"{self.start} 〜 {self.end}"


ProgressCallback = Callable[[dict], None]


# ---------------------------------------------------------------------------
# 期間ヘルパー
# ---------------------------------------------------------------------------

def range_current_month() -> DateRange:
    today = date.today()
    return DateRange(start=today.replace(day=1), end=today)


def range_last_month() -> DateRange:
    today = date.today()
    last_day = today.replace(day=1) - timedelta(days=1)
    return DateRange(start=last_day.replace(day=1), end=last_day)


def range_specific_month(year: int, month: int) -> DateRange:
    start = date(year, month, 1)
    end = date(year, month, calendar.monthrange(year, month)[1])
    return DateRange(start=start, end=end)


def range_this_week() -> DateRange:
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    return DateRange(start=monday, end=today)


def range_last_week() -> DateRange:
    today = date.today()
    this_monday = today - timedelta(days=today.weekday())
    last_sunday = this_monday - timedelta(days=1)
    last_monday = last_sunday - timedelta(days=6)
    return DateRange(start=last_monday, end=last_sunday)


# ---------------------------------------------------------------------------
# スクレイパー
# ---------------------------------------------------------------------------

MF_BASE_URL = "https://moneyforward.com"
MF_LOGIN_URL = "https://id.moneyforward.com/sign_in"
MF_CF_URL = f"{MF_BASE_URL}/cf"
MF_ACCOUNTS_URL = f"{MF_BASE_URL}/accounts"


class MoneyForwardScraper:
    def __init__(
        self,
        email: str,
        password: str,
        headless: bool = False,
        gas_otp_url: Optional[str] = None,
        gas_otp_secret: Optional[str] = None,
        progress_callback: Optional[ProgressCallback] = None,
    ):
        self.email = email
        self.password = password
        self.headless = headless
        self.gas_otp_url = gas_otp_url
        self.gas_otp_secret = gas_otp_secret
        self.progress_callback = progress_callback

    def scrape(self, date_range: Optional[DateRange] = None) -> ScrapingResult:
        if date_range is None:
            date_range = range_current_month()
        print(f"[Scraper] 取得期間: {date_range}")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            context = self._new_context(browser)
            page = context.new_page()
            try:
                self._emit_progress(stage="login", message="MoneyForwardログイン/セッション確認中")
                self._ensure_logged_in(page, context)
                self._emit_progress(stage="transactions", message="入出金明細を取得中", transactions_count=0)
                transactions = self._scrape_transactions(page, date_range)
                self._emit_progress(
                    stage="balances",
                    message="口座残高を取得中",
                    transactions_count=len(transactions),
                )
                balances = self._scrape_balances(page)
                self._emit_progress(
                    stage="scraped",
                    message="MoneyForwardからの取得が完了",
                    transactions_count=len(transactions),
                    balances_count=len(balances),
                )
                return ScrapingResult(transactions=transactions, balances=balances)
            finally:
                browser.close()

    def _emit_progress(self, **event) -> None:
        if not self.progress_callback:
            return
        try:
            self.progress_callback(event)
        except Exception as e:
            print(f"[Scraper] 進捗通知をスキップしました: {e}")

    def _new_context(self, browser) -> BrowserContext:
        kwargs = dict(
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        if SESSION_FILE.exists():
            print("[Session] 保存済みセッションを読み込みます...")
            kwargs["storage_state"] = str(SESSION_FILE)
        return browser.new_context(**kwargs)

    def _ensure_logged_in(self, page: Page, context: BrowserContext) -> None:
        """セッションが有効なら何もしない。無効ならログインしてセッションを保存する。"""
        page.goto(MF_CF_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        current_url = page.url

        # moneyforward.com にいればセッション有効
        if "id.moneyforward.com" not in current_url:
            print(f"[Session] セッション有効: {current_url}")
            return

        if "/sign_in" in current_url:
            # メール・パスワード入力が必要な完全ログイン
            print("[Session] セッション切れ。フルログインします...")
            self._login(page)
        else:
            # ID側のセッションは生きているがアカウント選択が必要
            print(f"[Session] ID認証済み。アカウント選択へ: {current_url}")
            self._handle_account_selection(page)
            page.wait_for_timeout(2000)
            if "id.moneyforward.com" in page.url:
                print("[Session] アカウント選択失敗。フルログインします...")
                self._login(page)

        # ログイン後、MF_CF_URL へ再ナビゲートして OAuth リダイレクト完了
        if "id.moneyforward.com" in page.url:
            print(f"[Session] OAuth リダイレクト開始... (現在: {page.url})")
            page.goto(MF_CF_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            self._handle_account_selection(page)
            page.wait_for_timeout(2000)

        print(f"[Session] ログイン完了: {page.url}")
        context.storage_state(path=str(SESSION_FILE))
        print(f"[Session] セッション保存: {SESSION_FILE}")

    def _login(self, page: Page) -> None:
        print("[Login] ログインページへ移動...")
        page.goto(MF_LOGIN_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        # Step 1: メールアドレス入力（すでにログイン済みでスキップされる場合あり）
        try:
            email_field = page.wait_for_selector(
                'input[type="email"], input[name="mfid_user[email]"], input[id*="email"]',
                timeout=8000
            )
            email_field.fill(self.email)
            email_field.press("Enter")
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(2000)
        except PlaywrightTimeoutError:
            print(f"[Login] メール入力フィールドなし。現在のURL: {page.url}")

        # Step 2: パスワード入力（別ページに遷移した場合）
        try:
            pw_field = page.wait_for_selector(
                'input[type="password"], input[name="mfid_user[password]"]',
                timeout=8000
            )
            pw_field.fill(self.password)
            pw_field.press("Enter")
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(2000)
        except PlaywrightTimeoutError:
            pass

        # Step 3: 2段階認証チェック
        if "two_factor" in page.url or "otp" in page.url.lower() or "sms" in page.url.lower():
            print("[Login] 2段階認証が検出されました。")
            otp = self._get_otp()
            otp_field = page.wait_for_selector(
                'input[name*="otp"], input[name*="code"], input[autocomplete="one-time-code"], input[type="text"], input[type="number"]',
                timeout=10000
            )
            otp_field.click()
            otp_field.fill(otp)

            # type属性なしボタン（'認証する'）も対象に含める
            submit_btn = page.query_selector(
                "button[type='submit'], input[type='submit'], "
                "button:not([type='button']):not([type='reset'])"
            )
            if submit_btn:
                btn_text = submit_btn.inner_text().strip()
                submit_btn.click()
                print(f"[Login] OTP送信: {btn_text!r}")
            else:
                otp_field.press("Enter")
                print(f"[Login] OTP Enter送信")

            # OTP完了後のリダイレクト（email_otp → /me）を最大20秒待つ
            try:
                page.wait_for_url(
                    lambda url: "email_otp" not in url and "otp" not in url.lower(),
                    timeout=20000
                )
            except PlaywrightTimeoutError:
                print(f"[Login] OTPリダイレクト待機タイムアウト: {page.url}")
            page.wait_for_timeout(1000)

        if "id.moneyforward.com/sign_in" in page.url or "email_otp" in page.url:
            raise RuntimeError(f"ログインに失敗しました。URL: {page.url}")

        print(f"[Login] ログイン成功: {page.url}")

    def _get_otp(self) -> str:
        """GAS WebアプリからOTPを取得。未設定または取得失敗時は手動入力にフォールバック。"""
        if self.gas_otp_url and self.gas_otp_secret:
            print("[OTP] GAS からコードを取得中...")
            # MFがメール送信するまで最大30秒待機（3秒×10回）
            for attempt in range(10):
                try:
                    url = f"{self.gas_otp_url}?secret={self.gas_otp_secret}"
                    with urllib.request.urlopen(url, timeout=10) as resp:
                        data = json.loads(resp.read())
                    code = data.get("code")
                    if code:
                        print(f"[OTP] コード取得成功: {code}")
                        return code
                except Exception as e:
                    print(f"[OTP] GAS取得エラー: {e}")
                print(f"[OTP] メール未着。3秒後に再試行... ({attempt + 1}/10)")
                time.sleep(3)
            print("[OTP] GAS からの取得に失敗しました。手動入力に切り替えます。")

        return input("認証コード > ").strip()

    def _handle_account_selection(self, page: Page) -> None:
        """「アカウントを選択する」画面が出た場合に自動でアカウントをクリックし、OAuth完了を待つ"""
        try:
            heading = page.query_selector("h1, h2, h3")
            if not (heading and "アカウントを選択" in heading.inner_text()):
                return

            print("[Login] アカウント選択画面を検出。クリックします...")

            btn = page.query_selector("button:has-text('現在のアカウント')")
            if not btn:
                buttons = page.query_selector_all("button")
                btn = buttons[0] if buttons else None

            if not btn:
                print("[Login] アカウントボタンが見つかりません")
                return

            btn.click()
            page.wait_for_load_state("load", timeout=15000)
            page.wait_for_timeout(2000)
        except Exception as e:
            print(f"[Login] アカウント選択処理スキップ: {e}")

    @staticmethod
    def _parse_transaction_date(
        date_text: str,
        default_year: Optional[int] = None,
        date_range: Optional[DateRange] = None,
    ) -> Optional[date]:
        """明細の日付文字列を date オブジェクトに変換。パース失敗時は None を返す。"""
        m = _RE_TX_DATE_FULL.match(date_text)
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = _RE_TX_DATE_SHORT.match(date_text)
        if m:
            month = int(m.group(1))
            day = int(m.group(2))
            if date_range:
                years = list(dict.fromkeys([date_range.start.year, date_range.end.year]))
                for year in years:
                    try:
                        candidate = date(year, month, day)
                    except ValueError:
                        return None
                    if date_range.start <= candidate <= date_range.end:
                        return candidate
            try:
                return date(default_year or date.today().year, month, day)
            except ValueError:
                return None
        return None

    def _scrape_transactions(self, page: Page, date_range: DateRange, max_pages: int = 10) -> list[Transaction]:
        """入出金明細を取得。MFはURLクエリパラメータを無視するためPython側で日付フィルタを適用する。"""
        from_str = date_range.start.strftime("%Y/%m/%d")
        to_str = date_range.end.strftime("%Y/%m/%d")
        url = f"{MF_CF_URL}?from={from_str}&to={to_str}"

        print(f"[CF] 入出金明細ページへ移動... ({date_range})")
        page.goto(url, wait_until="domcontentloaded")
        # テーブルが現れたら即続行（最大10秒）、タイムアウト時はフォールバック
        try:
            page.wait_for_selector("#cf-detail-table", timeout=10000)
        except PlaywrightTimeoutError:
            page.wait_for_timeout(2000)
        self._handle_account_selection(page)

        transactions: list[Transaction] = []

        for page_num in range(1, max_pages + 1):
            print(f"[CF] ページ {page_num} を取得中...")
            rows = page.query_selector_all("#cf-detail-table tbody tr")

            if not rows:
                print(f"[CF] 明細テーブルが見つかりません（ページ {page_num}）")
                break

            oldest_in_page: Optional[date] = None
            page_transactions = 0

            for row in rows:
                cols = row.query_selector_all("td")
                if len(cols) < 7:
                    continue
                try:
                    date_text = cols[1].inner_text().strip()
                    content = cols[2].inner_text().strip()
                    # 振替など注釈付き金額（例: "-10000\n(振替)"）の改行以降を除去
                    amount_raw = cols[3].inner_text().strip().split("\n")[0]
                    amount_text = amount_raw.replace(",", "").replace("円", "").replace("−", "-").replace("－", "-").replace(" ", "")
                    account = cols[4].inner_text().strip()
                    category = cols[5].inner_text().strip()
                    sub_category = cols[6].inner_text().strip()

                    if not amount_text or not date_text:
                        continue

                    tx_date = self._parse_transaction_date(date_text, date_range=date_range)
                    if tx_date is not None:
                        if oldest_in_page is None or tx_date < oldest_in_page:
                            oldest_in_page = tx_date
                        if tx_date < date_range.start or tx_date > date_range.end:
                            continue

                    transactions.append(Transaction(
                        date=tx_date.isoformat() if tx_date else date_text,
                        amount=int(amount_text),
                        category=category,
                        sub_category=sub_category,
                        account=account,
                        memo=content,
                    ))
                    page_transactions += 1
                except (ValueError, IndexError) as e:
                    print(f"[CF] 行パースエラー: {e}")
                    continue

            self._emit_progress(
                stage="transactions",
                message=f"入出金明細ページ {page_num} を取得中",
                transactions_count=len(transactions),
                page_transactions_count=page_transactions,
            )

            # このページの最古日が期間開始より前 → 以降のページに対象データなし
            if oldest_in_page is not None and oldest_in_page < date_range.start:
                print(f"[CF] ページ {page_num} 終端: 最古 {oldest_in_page} < 開始 {date_range.start}、打ち切り")
                break

            next_btn = page.query_selector("a[rel='next'], .pagination .next a")
            if not next_btn:
                break
            next_btn.click()
            page.wait_for_load_state("domcontentloaded")
            time.sleep(1)

        print(f"[CF] 取得件数: {len(transactions)}")
        return transactions

    def _scrape_balances(self, page: Page) -> list[AssetBalance]:
        """資産・口座残高を取得"""
        print("[Assets] 資産状況ページへ移動...")
        page.goto(MF_ACCOUNTS_URL, wait_until="domcontentloaded")
        # テーブルが現れたら即続行（最大8秒）、タイムアウト時はフォールバック
        try:
            page.wait_for_selector("#account-table", timeout=8000)
        except PlaywrightTimeoutError:
            page.wait_for_timeout(1500)
        self._handle_account_selection(page)

        balances: list[AssetBalance] = []
        rows = page.query_selector_all("#account-table tbody tr")

        for row in rows:
            cols = row.query_selector_all("td")
            if len(cols) < 2:
                continue
            try:
                account = " ".join(cols[0].inner_text().split())
                balance_text = cols[1].inner_text().strip().replace(",", "").replace("円", "").replace(" ", "")
                if not balance_text or not account:
                    continue
                balance = int(balance_text)
                updated_at = " ".join(cols[2].inner_text().split()) if len(cols) > 2 else ""
                balances.append(AssetBalance(account=account, balance=balance, updated_at=updated_at))
            except (ValueError, IndexError):
                continue

        print(f"[Assets] 取得口座数: {len(balances)}")
        self._emit_progress(
            stage="balances",
            message="口座残高を取得済み",
            balances_count=len(balances),
        )
        return balances
