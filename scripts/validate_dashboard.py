#!/usr/bin/env python3
"""Fail deployment when the generated dashboard is incomplete or inconsistent."""

from __future__ import annotations

import csv
import html
import re
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
REPORTS_DIR = PROJECT_DIR / "reports"
DASHBOARD = REPORTS_DIR / "dashboard.html"
LATEST_REPORT = REPORTS_DIR / "latest_report.csv"
LATEST_NEWS = REPORTS_DIR / "latest_news.csv"


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise SystemExit(f"Missing required output: {path.relative_to(PROJECT_DIR)}")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def ordered_unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def main() -> None:
    report_rows = read_rows(LATEST_REPORT)
    news_rows = read_rows(LATEST_NEWS)
    if len(report_rows) != 10:
        raise SystemExit(f"Expected 10 report rows, found {len(report_rows)}")

    report_codes = [row.get("stock_code", "").strip().upper() for row in report_rows]
    news_codes = ordered_unique(
        [row.get("stock_code", "").strip().upper() for row in news_rows]
    )
    if report_codes != news_codes:
        raise SystemExit(
            f"Table/news ticker mismatch: table={report_codes}, news={news_codes}"
        )

    dates = {row.get("date", "").strip() for row in report_rows}
    dates.discard("")
    if len(dates) != 1:
        raise SystemExit(f"Expected one report date, found {sorted(dates)}")
    report_date = dates.pop()

    if not DASHBOARD.exists():
        raise SystemExit("Missing reports/dashboard.html")
    page = DASHBOARD.read_text(encoding="utf-8")
    table_codes = [
        html.unescape(code)
        for code in re.findall(
            r'<td class="txt code col-stock_code"[^>]*>([^<]+)</td>', page
        )
    ]
    news_card_codes = re.findall(
        r'<div class="news-card"><h3>([A-Z0-9]+)', page
    )
    if table_codes != report_codes or news_card_codes != report_codes:
        raise SystemExit(
            "Dashboard HTML does not contain the report/news tickers in matching order"
        )
    if f"Data through {report_date}" not in page:
        raise SystemExit(f"Dashboard does not display report date {report_date}")
    if "refreshed" not in page or "Official IDX ranking" not in page and "Estimated ranking" not in page:
        raise SystemExit("Dashboard is missing refresh or source-status information")

    print(f"Dashboard validated: {report_date} · {', '.join(report_codes)}")


if __name__ == "__main__":
    main()
