#!/usr/bin/env python3
"""Try to download IDX daily Stock Summary data without the browser.

The Stock Summary page fetches its table from a JSON endpoint on idx.co.id.
This script calls that endpoint and writes a CSV into downloads/ with the same
column names as the Excel export, so idx_summary.py ingests it unchanged.

IDX sits behind bot protection, so this may be refused. If it is, the script
says so and exits without failing the rest of the pipeline: download the file
by hand for that day and everything else still works.

Usage:
  python idx_download.py                # last completed session
  python idx_download.py --days 5       # that session and the 4 before it
  python idx_download.py --date 2026-09-21
  python idx_download.py --force        # overwrite files already present
"""

from __future__ import annotations

import argparse
import csv
import logging
import random
import sys
import time as time_module
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

# IDX's WAF fingerprints TLS handshakes, so plain requests is often refused.
# curl_cffi impersonates a real browser's handshake and usually gets through.
try:  # optional dependency
    from curl_cffi import requests as curl_requests
except ImportError:  # pragma: no cover
    curl_requests = None

IMPERSONATE_TARGETS = ("chrome131", "chrome124", "safari17_0")
ATTEMPTS_PER_DATE = 4
BACKOFF_SECONDS = (2, 5, 12)

PROJECT_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = PROJECT_DIR / "downloads"
LOGS_DIR = PROJECT_DIR / "logs"

WIB = ZoneInfo("Asia/Jakarta")
MARKET_CLOSE_WIB = time(17, 0)
ENDPOINTS = (
    "https://www.idx.co.id/primary/TradingSummary/GetStockSummary?length=9999&start=0&date={ymd}",
    "https://www.idx.co.id/Portal/TradingSummary/StockSummary?date={ymd}&length=9999&start=0",
)
PAGE_URL = "https://www.idx.co.id/en/market-data/trading-summary/stock-summary"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,id;q=0.8",
    "Referer": PAGE_URL,
    "X-Requested-With": "XMLHttpRequest",
}
# JSON field -> Excel-style header that idx_summary.py already understands
FIELD_MAP = {
    "StockCode": "Stock Code",
    "StockName": "Company Name",
    "Previous": "Previous",
    "OpenPrice": "Open Price",
    "High": "High",
    "Low": "Low",
    "Close": "Close",
    "Change": "Change",
    "Volume": "Volume",
    "Value": "Value",
    "Frequency": "Frequency",
    "ForeignBuy": "Foreign Buy",
    "ForeignSell": "Foreign Sell",
}
MIN_ROWS = 100


def configure_logging() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOGS_DIR / "idx_download.log"),
            logging.StreamHandler(),
        ],
    )


def last_completed_session(now: datetime | None = None) -> date:
    now = now or datetime.now(WIB)
    day = now.date() if now.time() >= MARKET_CLOSE_WIB else now.date() - timedelta(days=1)
    while day.weekday() >= 5:  # Sat/Sun are never trading days
        day -= timedelta(days=1)
    return day


def previous_weekdays(end: date, count: int) -> list[date]:
    days, cursor = [], end
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return days


def new_session(attempt: int):
    """A curl_cffi session impersonating a browser, or plain requests."""
    if curl_requests is not None:
        target = IMPERSONATE_TARGETS[attempt % len(IMPERSONATE_TARGETS)]
        session = curl_requests.Session(impersonate=target)
        session.headers.update(HEADERS)
        logging.debug("Using curl_cffi impersonating %s", target)
    else:
        session = requests.Session()
        session.headers.update(HEADERS)
    try:  # a normal page visit first, so any cookie the endpoint expects is set
        session.get(PAGE_URL, timeout=60)
    except Exception as exc:
        logging.debug("Page visit failed: %s", exc)
    return session


def try_endpoints(session, day: date) -> list[dict] | None:
    ymd = day.strftime("%Y%m%d")
    for url in ENDPOINTS:
        try:
            response = session.get(url.format(ymd=ymd), timeout=60)
        except Exception as exc:
            logging.info("%s: request error for %s: %s", url.split("?")[0], day, exc)
            continue
        if response.status_code != 200:
            logging.info("%s returned HTTP %s for %s", url.split("?")[0], response.status_code, day)
            continue
        if "json" not in (response.headers.get("Content-Type") or "").lower():
            logging.info("%s returned HTML (bot protection) for %s", url.split("?")[0], day)
            continue
        try:
            payload = response.json()
        except ValueError:
            logging.info("Response for %s was not valid JSON", day)
            continue
        rows = payload.get("data") if isinstance(payload, dict) else payload
        if isinstance(rows, list):
            return rows
    return None


def fetch_day(day: date) -> list[dict] | None:
    """Return the day's rows, or None after all attempts were refused.

    IDX's block is intermittent, so each date gets several attempts with a new
    session, a different browser fingerprint and a backoff in between.
    """
    for attempt in range(ATTEMPTS_PER_DATE):
        if attempt:
            delay = BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)]
            delay += random.uniform(0, 1.5)
            logging.info("Retrying %s in %.1fs (attempt %s)", day, delay, attempt + 1)
            time_module.sleep(delay)
        rows = try_endpoints(new_session(attempt), day)
        if rows is not None:
            return rows
    return None


def write_csv(day: date, rows: list[dict]) -> Path:
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    path = DOWNLOADS_DIR / f"Stock Summary-{day.strftime('%Y%m%d')}.csv"
    headers = [FIELD_MAP[key] for key in FIELD_MAP]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        for row in rows:
            writer.writerow([row.get(key, "") for key in FIELD_MAP])
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="Single date to fetch (YYYY-MM-DD)")
    parser.add_argument("--days", type=int, default=1, help="How many recent sessions to fetch")
    parser.add_argument("--force", action="store_true", help="Overwrite files already downloaded")
    args = parser.parse_args()

    configure_logging()
    if args.date:
        targets = [datetime.strptime(args.date, "%Y-%m-%d").date()]
    else:
        targets = previous_weekdays(last_completed_session(), max(1, args.days))

    if curl_requests is None:
        logging.warning(
            "curl_cffi is not installed; falling back to plain requests, which IDX "
            "usually refuses. Install it with: pip install curl_cffi"
        )

    saved, skipped, refused = [], [], []
    for day in targets:
        existing = list(DOWNLOADS_DIR.glob(f"*{day.strftime('%Y%m%d')}*"))
        if existing and not args.force:
            skipped.append(f"{day} (already have {existing[0].name})")
            continue
        rows = fetch_day(day)
        if rows is None:
            refused.append(str(day))
            continue
        if len(rows) < MIN_ROWS:
            logging.info("%s returned %s rows - holiday or not published yet", day, len(rows))
            skipped.append(f"{day} (no data)")
            continue
        path = write_csv(day, rows)
        saved.append(f"{day} -> {path.name} ({len(rows)} stocks)")
        logging.info("Saved %s (%s stocks)", path.name, len(rows))

    for line in skipped:
        logging.info("Skipped %s", line)
    if refused:
        logging.warning(
            "IDX refused the direct download for %s. Download those days by hand from "
            "https://www.idx.co.id/en/market-data/trading-summary/stock-summary into %s",
            ", ".join(refused), DOWNLOADS_DIR,
        )
    if not saved and not skipped:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
