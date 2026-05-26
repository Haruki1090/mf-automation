PYTHON  := .venv/bin/python
PYTEST  := .venv/bin/pytest

.PHONY: install test scenario run

# 初回セットアップ
install:
	python3 -m venv .venv
	.venv/bin/pip install -r requirements.txt
	$(PYTHON) -m playwright install chromium

# ユニットテスト
test:
	$(PYTEST)

# 統合シナリオテスト
#   make scenario                          全シナリオ（S03除く）
#   make scenario ARGS="--scenario valid_session"
#   make scenario ARGS="--list"
scenario:
	$(PYTHON) scraper/tests/scenario_runner.py $(ARGS)

# スクレイピング実行
#   make run
#   make run ARGS="--mode last_month"
#   make run ARGS="--mode month --month 2026-04"
#   make run ARGS="--mode range --from 2026-04-01 --to 2026-04-30 --headless"
run:
	$(PYTHON) scraper/main.py $(ARGS)
