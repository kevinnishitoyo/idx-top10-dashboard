#!/usr/bin/env python3
"""Ingest IDX daily Stock Summary (Ringkasan Saham) files into SQLite.

Download the daily file from IDX (Market Data -> Trading Summary -> Stock
Summary) and drop it in the `downloads/` folder. Any .xlsx/.xls/.csv file there
is parsed; the trade date comes from the filename (8 digits such as 20260922,
or 2026-09-22) or from a date column inside the file.

Stored per stock per day: official transaction value, volume, frequency, close,
foreign buy, foreign sell, foreign net. pipeline.py uses this for the top-10
ranking and the foreign-flow columns whenever the report date is covered.

Re-running is safe: rows are upserted, so re-ingesting the same file changes
nothing. Run `python idx_summary.py --show-headers` to print the column names
detected in each file (use it once to confirm the mapping and the units).
"""

from __future__ import annotations

import argparse
import logging
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

PROJECT_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = PROJECT_DIR / "downloads"
DB_PATH = PROJECT_DIR / "data" / "market_data.db"
LOGS_DIR = PROJECT_DIR / "logs"

# Headers containing any of these are never used for the main columns:
# they are secondary boards or order-book fields, not the day's regular trade.
EXCLUDE_TERMS = (
    "non regular", "nonregular", "offer", "bid", "weight", "index",
    "listed", "tradeble", "tradable", "delisting", "remarks", "previous",
    "sebelumnya", "first", "open", "tertinggi", "terendah", "high", "low",
    "selisih", "change",
)

# field -> candidate header names, best first
FIELD_CANDIDATES = {
    "code": ("kode saham", "stock code", "kode", "code", "ticker"),
    "company": ("nama perusahaan", "company name", "nama emiten"),
    "close": ("penutupan", "close", "closing price", "last"),
    "volume": ("volume", "vol"),
    "value": ("nilai", "value", "turnover"),
    "frequency": ("frekuensi", "frequency", "freq"),
    "foreign_buy": ("foreign buy", "foreign buy value", "asing beli"),
    "foreign_sell": ("foreign sell", "foreign sell value", "asing jual"),
}
REQUIRED_FIELDS = ("code", "value")


def configure_logging() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOGS_DIR / "idx_summary.log"),
            logging.StreamHandler(),
        ],
    )


def create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS idx_summary (
            stock_code TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            company TEXT,
            close REAL,
            volume INTEGER,
            value INTEGER,
            frequency INTEGER,
            foreign_buy INTEGER,
            foreign_sell INTEGER,
            foreign_net INTEGER,
            vwap REAL,
            foreign_net_value INTEGER,
            source_file TEXT NOT NULL,
            ingested_at TEXT NOT NULL,
            PRIMARY KEY (stock_code, trade_date)
        );
        CREATE INDEX IF NOT EXISTS idx_summary_date ON idx_summary (trade_date);
        """
    )
    existing = {row[1] for row in connection.execute("PRAGMA table_info(idx_summary)")}
    for column, ddl in (
        ("company", "TEXT"), ("vwap", "REAL"), ("foreign_net_value", "INTEGER"),
    ):
        if column not in existing:
            connection.execute(f"ALTER TABLE idx_summary ADD COLUMN {column} {ddl}")
    connection.commit()


def date_from_name(name: str) -> str | None:
    match = re.search(r"(20\d{2})[-_]?(\d{2})[-_]?(\d{2})", name)
    if not match:
        return None
    try:
        return datetime(*(int(part) for part in match.groups())).date().isoformat()
    except ValueError:
        return None


def read_table(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in (".xlsx", ".xls"):
        return pd.read_excel(path)
    for separator in (",", ";", "\t"):
        frame = pd.read_csv(path, sep=separator, engine="python")
        if frame.shape[1] > 1:
            return frame
    raise RuntimeError(f"Could not parse {path.name} as a table")


def match_columns(columns: list[str]) -> dict[str, str]:
    normalized = {column: str(column).strip().lower() for column in columns}
    mapping: dict[str, str] = {}
    for field, candidates in FIELD_CANDIDATES.items():
        allow_excluded = field in ("foreign_buy", "foreign_sell", "close")
        best: tuple[int, str] | None = None
        for column, lowered in normalized.items():
            if column in mapping.values():
                continue
            if not allow_excluded and any(term in lowered for term in EXCLUDE_TERMS):
                continue
            if any(term in lowered for term in ("non regular", "nonregular")):
                continue
            for priority, candidate in enumerate(candidates):
                if lowered == candidate:
                    score = priority  # exact match wins
                elif lowered.startswith(candidate) or candidate in lowered:
                    score = 100 + priority
                else:
                    continue
                if best is None or score < best[0]:
                    best = (score, column)
                break
        if best is not None:
            mapping[field] = best[1]
    return mapping


def to_number(value) -> float | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(" ", "")
    if not text or text in ("-", "--"):
        return None
    # Indonesian files may use 1.234.567,89 or 1,234,567.89
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", "." if len(text.split(",")[-1]) == 2 else "")
    else:
        text = text.replace(" ", "")
    try:
        return float(text)
    except ValueError:
        return None


def ingest_file(
    connection: sqlite3.Connection, path: Path, show_headers: bool
) -> tuple[str | None, int]:
    frame = read_table(path)
    columns = list(frame.columns)
    mapping = match_columns([str(column) for column in columns])
    if show_headers:
        logging.info("%s headers: %s", path.name, columns)
        logging.info("%s mapping: %s", path.name, mapping)

    missing = [field for field in REQUIRED_FIELDS if field not in mapping]
    if missing:
        raise RuntimeError(
            f"{path.name}: could not find column(s) for {', '.join(missing)}. "
            f"Headers seen: {columns}"
        )

    trade_date = date_from_name(path.name)
    date_column = next(
        (
            str(column)
            for column in columns
            if str(column).strip().lower() in ("date", "tanggal", "trade date", "tanggal perdagangan")
        ),
        None,
    )
    if trade_date is None and date_column is not None:
        parsed = pd.to_datetime(frame[date_column], errors="coerce").dropna()
        if not parsed.empty:
            trade_date = parsed.iloc[0].date().isoformat()
    if trade_date is None:
        raise RuntimeError(
            f"{path.name}: no trade date in the filename or a date column. "
            "Rename the file to include the date, e.g. ringkasan-20260922.xlsx"
        )

    now = datetime.now().isoformat(timespec="seconds")
    records = []
    for _, row in frame.iterrows():
        code = str(row[mapping["code"]]).strip().upper()
        if not code or code in ("NAN", "NONE") or len(code) > 6:
            continue
        value = to_number(row[mapping["value"]])
        if value is None:
            continue
        buy = to_number(row[mapping["foreign_buy"]]) if "foreign_buy" in mapping else None
        sell = to_number(row[mapping["foreign_sell"]]) if "foreign_sell" in mapping else None
        net = None if buy is None or sell is None else buy - sell
        volume = to_number(row[mapping["volume"]]) if "volume" in mapping else None
        # IDX reports Foreign Buy/Sell as SHARE VOLUMES. VWAP (value / volume)
        # converts the net into an approximate rupiah figure.
        vwap = value / volume if volume else None
        net_value = None if net is None or vwap is None else net * vwap
        records.append(
            (
                code,
                trade_date,
                str(row[mapping["company"]]).strip() if "company" in mapping else None,
                to_number(row[mapping["close"]]) if "close" in mapping else None,
                None if volume is None else int(volume),
                int(value),
                int(to_number(row[mapping["frequency"]]) or 0) if "frequency" in mapping else None,
                None if buy is None else int(buy),
                None if sell is None else int(sell),
                None if net is None else int(net),
                vwap,
                None if net_value is None else int(net_value),
                path.name,
                now,
            )
        )

    if not records:
        raise RuntimeError(f"{path.name}: no usable stock rows found")

    connection.executemany(
        """
        INSERT INTO idx_summary (
            stock_code, trade_date, company, close, volume, value, frequency,
            foreign_buy, foreign_sell, foreign_net, vwap, foreign_net_value,
            source_file, ingested_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(stock_code, trade_date) DO UPDATE SET
            company=excluded.company,
            close=excluded.close,
            volume=excluded.volume,
            value=excluded.value,
            frequency=excluded.frequency,
            foreign_buy=excluded.foreign_buy,
            foreign_sell=excluded.foreign_sell,
            foreign_net=excluded.foreign_net,
            vwap=excluded.vwap,
            foreign_net_value=excluded.foreign_net_value,
            source_file=excluded.source_file,
            ingested_at=excluded.ingested_at
        """,
        records,
    )
    connection.commit()
    has_foreign = any(record[9] is not None for record in records)
    if not has_foreign:
        logging.warning(
            "%s: no foreign buy/sell columns detected - foreign flow will stay empty",
            path.name,
        )
    return trade_date, len(records)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--show-headers", action="store_true",
        help="Print the columns found in each file and how they were mapped",
    )
    args = parser.parse_args()

    configure_logging()
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    files = sorted(
        path
        for path in DOWNLOADS_DIR.iterdir()
        if path.is_file()
        and path.suffix.lower() in (".xlsx", ".xls", ".csv")
        and not path.name.startswith("~$")
    )
    if not files:
        logging.warning(
            "No IDX Stock Summary files in %s. The pipeline will fall back to the "
            "yfinance value estimate and leave foreign flow empty.", DOWNLOADS_DIR
        )
        return 0

    connection = sqlite3.connect(DB_PATH)
    try:
        create_schema(connection)
        loaded, failed = [], []
        for path in files:
            try:
                trade_date, count = ingest_file(connection, path, args.show_headers)
                loaded.append((trade_date, count, path.name))
                logging.info("%s: %s rows for %s", path.name, count, trade_date)
            except Exception as exc:
                failed.append(f"{path.name}: {exc}")
                logging.error("%s could not be ingested: %s", path.name, exc)

        dates = connection.execute(
            "SELECT trade_date, COUNT(*), SUM(foreign_net IS NOT NULL) FROM idx_summary "
            "GROUP BY trade_date ORDER BY trade_date DESC LIMIT 10"
        ).fetchall()
        for trade_date, rows, with_foreign in dates:
            logging.info(
                "stored %s: %s stocks (%s with foreign flow)", trade_date, rows, with_foreign
            )
        if failed:
            logging.warning("%s file(s) failed: %s", len(failed), " | ".join(failed))
        return 0 if loaded else 1
    finally:
        connection.close()


if __name__ == "__main__":
    sys.exit(main())
