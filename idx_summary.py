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
            foreign_buy_value INTEGER,
            foreign_sell_value INTEGER,
            vwap REAL,
            foreign_net_value INTEGER,
            foreign_unit TEXT,
            source_file TEXT NOT NULL,
            ingested_at TEXT NOT NULL,
            PRIMARY KEY (stock_code, trade_date)
        );
        CREATE INDEX IF NOT EXISTS idx_summary_date ON idx_summary (trade_date);
        """
    )
    existing = {row[1] for row in connection.execute("PRAGMA table_info(idx_summary)")}
    for column, ddl in (
        ("company", "TEXT"),
        ("vwap", "REAL"),
        ("foreign_buy_value", "INTEGER"),
        ("foreign_sell_value", "INTEGER"),
        ("foreign_net_value", "INTEGER"),
        ("foreign_unit", "TEXT"),
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


def foreign_header_unit(header: str) -> str | None:
    normalized = re.sub(r"[^a-z0-9]+", " ", header.lower()).strip()
    if any(term in normalized.split() for term in ("value", "nilai", "idr", "rupiah")):
        return "idr"
    if any(
        term in normalized.split()
        for term in ("volume", "share", "shares", "saham", "lembar")
    ):
        return "shares"
    return None


def detect_foreign_unit(
    frame: pd.DataFrame, mapping: dict[str, str], source_name: str
) -> str | None:
    """Classify foreign buy/sell as shares or IDR, refusing unsafe guesses."""
    has_buy = "foreign_buy" in mapping
    has_sell = "foreign_sell" in mapping
    if not has_buy and not has_sell:
        return None
    if has_buy != has_sell:
        raise RuntimeError(
            f"{source_name}: found only one foreign buy/sell column; refusing partial flow"
        )

    buy_header = mapping["foreign_buy"]
    sell_header = mapping["foreign_sell"]
    buy_unit = foreign_header_unit(buy_header)
    sell_unit = foreign_header_unit(sell_header)
    explicit_units = {unit for unit in (buy_unit, sell_unit) if unit is not None}
    if len(explicit_units) > 1:
        raise RuntimeError(
            f"{source_name}: foreign headers disagree on units: "
            f"{buy_header!r}, {sell_header!r}"
        )
    explicit_unit = next(iter(explicit_units), None)

    checked = 0
    share_plausible = True
    idr_plausible = True
    for _, row in frame.iterrows():
        buy = to_number(row[buy_header])
        sell = to_number(row[sell_header])
        if buy is None and sell is None:
            continue
        if buy is None or sell is None:
            raise RuntimeError(
                f"{source_name}: a row has only one foreign buy/sell value"
            )
        if buy < 0 or sell < 0:
            raise RuntimeError(f"{source_name}: foreign buy/sell cannot be negative")
        checked += 1

        volume = to_number(row[mapping["volume"]]) if "volume" in mapping else None
        value = to_number(row[mapping["value"]])
        if volume is None or volume < 0 or buy > volume or sell > volume:
            share_plausible = False
        if value is None or value < 0 or buy > value * 1.05 or sell > value * 1.05:
            idr_plausible = False

    if checked == 0:
        return None
    if explicit_unit == "shares":
        if not share_plausible:
            raise RuntimeError(
                f"{source_name}: share-labelled foreign buy/sell exceeds total volume"
            )
        return "shares"
    if explicit_unit == "idr":
        if not idr_plausible:
            raise RuntimeError(
                f"{source_name}: IDR-labelled foreign buy/sell exceeds turnover"
            )
        return "idr"

    # IDX's current generic Foreign Buy/Sell headers contain share volumes.
    # Accept that format only while every populated row satisfies the strict
    # share-volume relationship. A future value-format change will fail closed.
    if share_plausible:
        return "shares"
    raise RuntimeError(
        f"{source_name}: generic Foreign Buy/Sell headers are not plausible share "
        "volumes; add explicit unit mapping before importing this file"
    )


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

    foreign_unit = detect_foreign_unit(frame, mapping, path.name)
    if show_headers:
        logging.info("%s foreign-flow unit: %s", path.name, foreign_unit or "unavailable")

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
        volume = to_number(row[mapping["volume"]]) if "volume" in mapping else None
        vwap = value / volume if volume else None
        raw_net = None if buy is None or sell is None else buy - sell
        if foreign_unit == "shares":
            share_buy, share_sell, share_net = buy, sell, raw_net
            buy_value = None if buy is None or vwap is None else buy * vwap
            sell_value = None if sell is None or vwap is None else sell * vwap
            net_value = None if raw_net is None or vwap is None else raw_net * vwap
        elif foreign_unit == "idr":
            share_buy = share_sell = share_net = None
            buy_value, sell_value, net_value = buy, sell, raw_net
        else:
            share_buy = share_sell = share_net = None
            buy_value = sell_value = net_value = None
        records.append(
            (
                code,
                trade_date,
                str(row[mapping["company"]]).strip() if "company" in mapping else None,
                to_number(row[mapping["close"]]) if "close" in mapping else None,
                None if volume is None else int(volume),
                int(value),
                int(to_number(row[mapping["frequency"]]) or 0) if "frequency" in mapping else None,
                None if share_buy is None else int(share_buy),
                None if share_sell is None else int(share_sell),
                None if share_net is None else int(share_net),
                None if buy_value is None else int(buy_value),
                None if sell_value is None else int(sell_value),
                vwap,
                None if net_value is None else int(net_value),
                foreign_unit,
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
            foreign_buy, foreign_sell, foreign_net, foreign_buy_value,
            foreign_sell_value, vwap, foreign_net_value, foreign_unit,
            source_file, ingested_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(stock_code, trade_date) DO UPDATE SET
            company=excluded.company,
            close=excluded.close,
            volume=excluded.volume,
            value=excluded.value,
            frequency=excluded.frequency,
            foreign_buy=excluded.foreign_buy,
            foreign_sell=excluded.foreign_sell,
            foreign_net=excluded.foreign_net,
            foreign_buy_value=excluded.foreign_buy_value,
            foreign_sell_value=excluded.foreign_sell_value,
            vwap=excluded.vwap,
            foreign_net_value=excluded.foreign_net_value,
            foreign_unit=excluded.foreign_unit,
            source_file=excluded.source_file,
            ingested_at=excluded.ingested_at
        """,
        records,
    )
    connection.commit()
    has_foreign = any(record[13] is not None for record in records)
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
        return 1 if failed or not loaded else 0
    finally:
        connection.close()


if __name__ == "__main__":
    sys.exit(main())
