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
MIN_REWARD_TO_RISK = 1.5


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise SystemExit(f"Missing required output: {path.relative_to(PROJECT_DIR)}")
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def ordered_unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def numeric(row: dict[str, str], column: str) -> float | None:
    value = (row.get(column) or "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise SystemExit(
            f"{row.get('stock_code', '?')}: invalid number in {column}: {value!r}"
        ) from exc


def idx_tick_size(price: float) -> int:
    if price < 200:
        return 1
    if price < 500:
        return 2
    if price < 2_000:
        return 5
    if price < 5_000:
        return 10
    return 25


def valid_idx_price(price: float) -> bool:
    tick = idx_tick_size(price)
    return price > 0 and abs(price / tick - round(price / tick)) < 1e-9


def boolean(row: dict[str, str], column: str) -> bool | None:
    value = (row.get(column) or "").strip().lower()
    if not value:
        return None
    if value in ("true", "1", "yes"):
        return True
    if value in ("false", "0", "no"):
        return False
    raise SystemExit(f"{row.get('stock_code', '?')}: invalid boolean in {column}: {value!r}")


def validate_report_row(row: dict[str, str]) -> None:
    code = (row.get("stock_code") or "?").strip().upper()
    report_date = (row.get("date") or "").strip()
    price_date = (row.get("ohlcv_date") or "").strip()
    if price_date != report_date:
        raise SystemExit(
            f"{code}: price date {price_date!r} does not match report date {report_date!r}"
        )

    volume = numeric(row, "volume")
    foreign_buy = numeric(row, "foreign_buy_1d_shares")
    foreign_sell = numeric(row, "foreign_sell_1d_shares")
    foreign_net_value = numeric(row, "foreign_net_1d_idr")
    turnover = numeric(row, "transaction_value")
    foreign_unit = (row.get("foreign_unit_1d") or "").strip().lower()

    if foreign_net_value is not None and foreign_unit not in ("shares", "idr"):
        raise SystemExit(f"{code}: foreign flow is missing a recognized source unit")
    if foreign_unit == "shares" and (foreign_buy is None or foreign_sell is None):
        raise SystemExit(f"{code}: share-based foreign flow is missing share values")
    if foreign_unit == "idr" and (foreign_buy is not None or foreign_sell is not None):
        raise SystemExit(f"{code}: IDR foreign flow was incorrectly exposed as shares")

    for label, value in (("foreign buy", foreign_buy), ("foreign sell", foreign_sell)):
        if value is not None and value < 0:
            raise SystemExit(f"{code}: {label} cannot be negative")
        if value is not None and volume is not None and value > volume:
            raise SystemExit(
                f"{code}: {label} exceeds total volume; foreign-flow units may be wrong"
            )

    if (
        foreign_net_value is not None
        and turnover is not None
        and abs(foreign_net_value) > turnover * 1.05
    ):
        raise SystemExit(
            f"{code}: foreign net value exceeds turnover; foreign-flow units may be wrong"
        )

    sessions = numeric(row, "foreign_sessions_5d") or 0
    if numeric(row, "foreign_net_3d_idr") is not None and sessions < 3:
        raise SystemExit(f"{code}: 3-day flow has fewer than 3 covered sessions")
    if numeric(row, "foreign_net_5d_idr") is not None and sessions < 5:
        raise SystemExit(f"{code}: 5-day flow has fewer than 5 covered sessions")

    setup = row.get("setup") or ""
    if not setup.startswith(("Breakout", "Pullback")):
        if (row.get("setup_status") or "").strip():
            raise SystemExit(f"{code}: inactive setup unexpectedly has a status")
        return

    entry = numeric(row, "entry")
    stop = numeric(row, "stop")
    target = numeric(row, "target")
    reported_rr = numeric(row, "reward_to_risk")
    setup_status = (row.get("setup_status") or "").strip()
    if entry is None or stop is None or target is None:
        raise SystemExit(f"{code}: active setup is missing a trade level")
    if reported_rr is None:
        raise SystemExit(f"{code}: active setup is missing reward-to-risk")

    for label, price in (("entry", entry), ("stop", stop), ("target", target)):
        if not valid_idx_price(price):
            raise SystemExit(f"{code}: {label} {price:g} is not on a valid IDX tick")
    if stop >= entry:
        raise SystemExit(f"{code}: stop must be below entry")
    if target <= entry:
        raise SystemExit(f"{code}: target must be above entry")
    calculated_rr = (target - entry) / (entry - stop)
    if abs(calculated_rr - reported_rr) > 1e-6:
        raise SystemExit(f"{code}: reported reward-to-risk does not match rounded levels")
    if calculated_rr < MIN_REWARD_TO_RISK - 1e-9:
        raise SystemExit(
            f"{code}: reward-to-risk is below {MIN_REWARD_TO_RISK:.1f}"
        )

    close = numeric(row, "close")
    if close is None:
        raise SystemExit(f"{code}: active setup is missing its close")
    expected_status = "At trigger" if entry <= close else "Pending"
    if setup_status != expected_status:
        raise SystemExit(
            f"{code}: setup status {setup_status!r} should be {expected_status!r}"
        )

    if setup.startswith("Pullback"):
        support = numeric(row, "support_prior_20d")
        resistance = numeric(row, "resistance_prior_20d")
        ma20 = numeric(row, "ma20")
        ma50 = numeric(row, "ma50")
        ma20_rising = boolean(row, "ma20_rising")
        if None in (support, resistance, ma20, ma50):
            raise SystemExit(f"{code}: pullback is missing regime or level data")
        assert support is not None and resistance is not None
        assert ma20 is not None and ma50 is not None
        if not (ma20 > ma50 and ma20_rising is True and close > ma50):
            raise SystemExit(f"{code}: pullback lacks a confirmed rising regime")
        if stop >= support:
            raise SystemExit(f"{code}: pullback stop must sit below support")
        if target > resistance:
            raise SystemExit(f"{code}: pullback target exceeds prior resistance")


def main() -> None:
    report_rows = read_rows(LATEST_REPORT)
    news_rows = read_rows(LATEST_NEWS)
    if len(report_rows) != 10:
        raise SystemExit(f"Expected 10 report rows, found {len(report_rows)}")

    report_codes = [row.get("stock_code", "").strip().upper() for row in report_rows]
    if any(not code for code in report_codes) or len(set(report_codes)) != len(report_codes):
        raise SystemExit(f"Report contains a blank or duplicate ticker: {report_codes}")
    for row in report_rows:
        validate_report_row(row)

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
