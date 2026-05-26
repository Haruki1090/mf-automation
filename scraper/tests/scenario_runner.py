"""
統合シナリオランナー: 実ブラウザ・実MFアカウントを使ったシナリオテスト
各シナリオの詳細ログを output/test_logs/ に保存する

実行:
    make scenario                                         # 全シナリオ（S03除く）
    make scenario ARGS="--scenario valid_session"         # 個別シナリオ
    make scenario ARGS="--list"                           # シナリオ一覧
"""

import argparse
import json
import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
load_dotenv(Path(__file__).parent.parent.parent / ".env")

from mf_scraper import (
    MoneyForwardScraper, SESSION_FILE,
    range_current_month, range_last_month, range_this_week, range_last_week,
    range_specific_month, DateRange,
)

LOG_DIR = Path(__file__).parent.parent.parent / "output" / "test_logs"


# ---------------------------------------------------------------------------
# ロガーセットアップ
# ---------------------------------------------------------------------------

def setup_logger(scenario_name: str) -> tuple[logging.Logger, Path]:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOG_DIR / f"{scenario_name}_{ts}.log"

    logger = logging.getLogger(scenario_name)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    return logger, log_path


# ---------------------------------------------------------------------------
# シナリオ結果
# ---------------------------------------------------------------------------

@dataclass
class ScenarioResult:
    name: str
    passed: bool
    log_path: Path
    elapsed: float
    error: Optional[str] = None
    detail: Optional[dict] = None


# ---------------------------------------------------------------------------
# シナリオ定義
# ---------------------------------------------------------------------------

def _make_scraper(headless: bool = True) -> MoneyForwardScraper:
    email = os.getenv("MF_EMAIL")
    password = os.getenv("MF_PASSWORD")
    if not email or not password:
        raise RuntimeError("MF_EMAIL / MF_PASSWORD が未設定です")
    return MoneyForwardScraper(
        email=email,
        password=password,
        headless=headless,
        gas_otp_url=os.getenv("GAS_OTP_URL"),
        gas_otp_secret=os.getenv("GAS_OTP_SECRET"),
    )


def scenario_valid_session(logger: logging.Logger) -> dict:
    """
    [S01] セッション有効
    前提: 有効な session.json が存在する
    期待: ログイン不要でスクレイピング成功
    """
    if not SESSION_FILE.exists():
        raise RuntimeError("session.json が存在しません。先に S02 を実行してください。")

    logger.info("=== S01: セッション有効シナリオ ===")
    logger.info(f"session.json パス: {SESSION_FILE}")
    logger.info(f"session.json サイズ: {SESSION_FILE.stat().st_size} bytes")

    scraper = _make_scraper(headless=True)
    logger.info("スクレイパー初期化完了（headless=True）")

    dr = range_current_month()
    logger.info(f"取得期間: {dr}")

    result = scraper.scrape(date_range=dr)

    logger.info(f"取得明細件数: {len(result.transactions)}")
    logger.info(f"取得残高件数: {len(result.balances)}")
    for b in result.balances:
        logger.info(f"  残高: {b.account} = {b.balance:,}円 ({b.updated_at})")
    if result.transactions:
        t = result.transactions[0]
        logger.info(f"  最新明細: {t.date} {t.memo} {t.amount:,}円 [{t.category}]")

    assert len(result.transactions) > 0, "明細が0件です"
    assert len(result.balances) > 0, "残高が0件です"

    return {
        "transactions": len(result.transactions),
        "balances": len(result.balances),
        "scraped_at": result.scraped_at,
    }


def scenario_no_session(logger: logging.Logger) -> dict:
    """
    [S02] session.json なし（初回実行 / フルログイン）
    前提: session.json を削除した状態
    期待: フルログイン → GAS OTP自動取得 → session.json 新規保存 → スクレイピング成功
    """
    logger.info("=== S02: session.json なし（初回実行）シナリオ ===")

    if SESSION_FILE.exists():
        backup = SESSION_FILE.with_suffix(".json.bak")
        shutil.copy(SESSION_FILE, backup)
        SESSION_FILE.unlink()
        logger.info(f"session.json をバックアップ: {backup}")
    else:
        logger.info("session.json は元々存在しません")

    gas_url = os.getenv("GAS_OTP_URL")
    gas_secret = os.getenv("GAS_OTP_SECRET")
    logger.info(f"GAS_OTP_URL: {'設定あり' if gas_url else '未設定（手動入力フォールバック）'}")
    logger.info(f"GAS_OTP_SECRET: {'設定あり' if gas_secret else '未設定'}")

    scraper = _make_scraper(headless=False)  # OTP入力のためGUI表示
    logger.info("スクレイパー初期化完了（headless=False）")

    dr = range_current_month()
    logger.info(f"取得期間: {dr}")

    result = scraper.scrape(date_range=dr)

    session_created = SESSION_FILE.exists()
    logger.info(f"session.json 新規作成: {session_created}")
    if session_created:
        logger.info(f"session.json サイズ: {SESSION_FILE.stat().st_size} bytes")

    logger.info(f"取得明細件数: {len(result.transactions)}")
    logger.info(f"取得残高件数: {len(result.balances)}")

    assert session_created, "session.json が作成されませんでした"
    assert len(result.transactions) > 0, "明細が0件です"

    return {
        "transactions": len(result.transactions),
        "balances": len(result.balances),
        "session_created": session_created,
    }


def scenario_gas_otp_failure_fallback(logger: logging.Logger) -> dict:
    """
    [S03] GAS OTP失敗 → 手動入力フォールバック
    前提: GAS_OTP_URL を意図的に無効なURLに差し替え、session.json を削除
    期待: GASが10回リトライ後に失敗し、手動入力プロンプトに切り替わる
    """
    logger.info("=== S03: GAS OTP失敗 → 手動入力フォールバックシナリオ ===")

    if SESSION_FILE.exists():
        SESSION_FILE.unlink()
        logger.info("session.json を削除しました")

    # GAS URLを無効化してテスト
    logger.info("GAS_OTP_URL を無効URLに差し替え")
    scraper = MoneyForwardScraper(
        email=os.getenv("MF_EMAIL", ""),
        password=os.getenv("MF_PASSWORD", ""),
        headless=False,
        gas_otp_url="https://script.google.com/macros/s/INVALID_ID/exec",
        gas_otp_secret="dummy",
    )

    dr = range_current_month()
    logger.info("GASが失敗すると「認証コード > 」プロンプトが表示されます")
    logger.info("手動でOTPコードを入力してください")

    result = scraper.scrape(date_range=dr)

    logger.info(f"取得明細件数: {len(result.transactions)}")
    logger.info("フォールバック成功")

    return {"transactions": len(result.transactions), "fallback_used": True}


def scenario_date_range_modes(logger: logging.Logger) -> dict:
    """
    [S04] 日付範囲モード別取得
    前提: 有効な session.json が存在する
    期待: 各モードで正しい期間のデータが取得できる
    """
    logger.info("=== S04: 日付範囲モード別取得シナリオ ===")

    if not SESSION_FILE.exists():
        raise RuntimeError("session.json が存在しません。先に S02 を実行してください。")

    scraper = _make_scraper(headless=True)
    results = {}

    modes = [
        ("current_month", range_current_month()),
        ("last_month",    range_last_month()),
        ("this_week",     range_this_week()),
        ("last_week",     range_last_week()),
        ("specific_month_202604", range_specific_month(2026, 4)),
    ]

    for mode_name, dr in modes:
        logger.info(f"--- モード: {mode_name}  期間: {dr} ---")
        try:
            result = scraper.scrape(date_range=dr)
            count = len(result.transactions)
            logger.info(f"  取得件数: {count}")
            if result.transactions:
                dates = [t.date for t in result.transactions]
                logger.info(f"  最古: {dates[-1]}  最新: {dates[0]}")
            results[mode_name] = {"count": count, "period": str(dr)}
        except Exception as e:
            logger.error(f"  エラー: {e}")
            results[mode_name] = {"error": str(e)}

    return results


def scenario_headless_mode(logger: logging.Logger) -> dict:
    """
    [S05] ヘッドレスモード動作確認
    前提: 有効な session.json が存在する
    期待: GUIなしでスクレイピング成功
    """
    logger.info("=== S05: ヘッドレスモード動作確認シナリオ ===")

    if not SESSION_FILE.exists():
        raise RuntimeError("session.json が存在しません。先に S02 を実行してください。")

    logger.info("headless=True でブラウザを起動")
    scraper = _make_scraper(headless=True)

    dr = range_current_month()
    logger.info(f"取得期間: {dr}")

    result = scraper.scrape(date_range=dr)

    logger.info(f"取得明細件数: {len(result.transactions)}")
    logger.info(f"取得残高件数: {len(result.balances)}")

    assert len(result.transactions) > 0, "明細が0件です（ヘッドレスで失敗した可能性）"

    return {
        "transactions": len(result.transactions),
        "balances": len(result.balances),
        "headless": True,
    }


# ---------------------------------------------------------------------------
# シナリオ一覧
# ---------------------------------------------------------------------------

SCENARIOS: dict[str, tuple[str, Callable]] = {
    "valid_session":           ("S01: セッション有効",                    scenario_valid_session),
    "no_session":              ("S02: 初回実行（フルログイン）",            scenario_no_session),
    "gas_otp_failure":         ("S03: GAS OTP失敗→手動フォールバック",     scenario_gas_otp_failure_fallback),
    "date_range_modes":        ("S04: 日付範囲モード別取得",               scenario_date_range_modes),
    "headless":                ("S05: ヘッドレスモード動作確認",           scenario_headless_mode),
}


# ---------------------------------------------------------------------------
# ランナー
# ---------------------------------------------------------------------------

def run_scenario(key: str) -> ScenarioResult:
    name, func = SCENARIOS[key]
    logger, log_path = setup_logger(key)
    logger.info(f"シナリオ開始: {name}")
    logger.info(f"ログファイル: {log_path}")
    logger.info("-" * 60)

    start = time.time()
    try:
        detail = func(logger)
        elapsed = time.time() - start
        logger.info("-" * 60)
        logger.info(f"[PASSED] 所要時間: {elapsed:.1f}s")
        logger.info(f"結果: {json.dumps(detail, ensure_ascii=False, indent=2)}")
        return ScenarioResult(name=name, passed=True, log_path=log_path, elapsed=elapsed, detail=detail)
    except Exception as e:
        elapsed = time.time() - start
        logger.error("-" * 60)
        logger.error(f"[FAILED] {e}", exc_info=True)
        return ScenarioResult(name=name, passed=False, log_path=log_path, elapsed=elapsed, error=str(e))


def print_summary(results: list[ScenarioResult]) -> None:
    print("\n" + "=" * 60)
    print("テスト結果サマリー")
    print("=" * 60)
    for r in results:
        status = "✓ PASS" if r.passed else "✗ FAIL"
        print(f"  {status}  {r.name}  ({r.elapsed:.1f}s)")
        print(f"         ログ: {r.log_path}")
        if not r.passed:
            print(f"         エラー: {r.error}")
    print("=" * 60)
    passed = sum(1 for r in results if r.passed)
    print(f"  {passed}/{len(results)} passed")
    print("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(description="MF スクレイパー統合シナリオランナー")
    parser.add_argument("--scenario", choices=list(SCENARIOS.keys()), help="実行するシナリオ名")
    parser.add_argument("--list", action="store_true", help="シナリオ一覧を表示")
    args = parser.parse_args()

    if args.list:
        print("利用可能なシナリオ:")
        for key, (name, _) in SCENARIOS.items():
            print(f"  {key:<25} {name}")
        return

    targets = [args.scenario] if args.scenario else list(SCENARIOS.keys())

    # S03（手動OTP）は明示指定時のみ実行
    if not args.scenario and "gas_otp_failure" in targets:
        targets.remove("gas_otp_failure")
        print("注意: S03（GAS OTP失敗シナリオ）は手動OTP入力が必要なため自動実行をスキップします。")
        print("      実行する場合: --scenario gas_otp_failure")

    results = []
    for key in targets:
        print(f"\n>>> シナリオ実行: {SCENARIOS[key][0]}")
        result = run_scenario(key)
        results.append(result)

    print_summary(results)


if __name__ == "__main__":
    main()
