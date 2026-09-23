#!/usr/bin/env python3
"""Daily IDX top-10 pipeline.

Prices and indicators come from yfinance. The top-10 ranking and the foreign
flow come from the IDX daily Stock Summary files ingested by idx_summary.py
whenever the report date is covered there; otherwise the ranking falls back to
a yfinance estimate (raw close x volume) over the stocks in companies.csv and
the foreign-flow columns stay empty.

Methodology notes
- Report date is the last COMPLETED session: a bar dated today is ignored until
  after the Jakarta close, because Yahoo's current-day bar updates intraday.
- Returns use adjusted closes (dividends/splits removed). Moving averages,
  ATR, support, resistance and the trade scenario use RAW prices, so the levels
  are prices you could actually trade.
- Support and resistance both use the 20 sessions BEFORE the report date, so
  neither is measured over a different window than the other.
- A trade scenario is only printed when the trend supports it; otherwise the
  scenario columns are blank with a reason.
- Every scenario level is a technical reference, not a recommendation, and IDX
  auto-rejection limits cap how far price can move in one session.
"""

from __future__ import annotations

import math
import csv
import json
import logging
import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf


PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / "data"
RAW_DIR = PROJECT_DIR / "raw"
REPORTS_DIR = PROJECT_DIR / "reports"
LOGS_DIR = PROJECT_DIR / "logs"
DOWNLOADS_DIR = PROJECT_DIR / "downloads"
YFINANCE_CACHE_DIR = PROJECT_DIR / ".cache" / "yfinance"
COMPANIES_CSV = PROJECT_DIR / "companies.csv"
DB_PATH = DATA_DIR / "market_data.db"

WIB = ZoneInfo("Asia/Jakarta")
MARKET_CLOSE_WIB = time(17, 0)  # allow for the closing auction and Yahoo's lag
TOP_COUNT = 10
UNIVERSE_PERIOD = "6mo"          # enough history for the 20-session liquidity filter
MIN_AVG_VALUE_20D = 5_000_000_000  # Rp; skip stocks too thin to trade
FLOW_SESSIONS = 5
MIN_IDX_ROWS = 50                # below this the IDX file looks partial
BREAKOUT_PROXIMITY = 0.03        # breakout setup: within 3% of prior-20d resistance
LEVEL_PROXIMITY_ATR = 0.5        # a test must sit within half an ATR of its level
SUPPORT_STOP_BUFFER_ATR = 0.5    # pullback stops sit this far below support
BREAKOUT_STOP_BUFFER_ATR = 1.0   # breakout stop sits below the former resistance
MIN_REWARD_TO_RISK = 1.5         # reject setups with too little room to resistance


def ensure_directories() -> None:
    for directory in (DATA_DIR, RAW_DIR, REPORTS_DIR, LOGS_DIR, DOWNLOADS_DIR, YFINANCE_CACHE_DIR):
        directory.mkdir(parents=True, exist_ok=True)
    yf.set_tz_cache_location(str(YFINANCE_CACHE_DIR))


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(LOGS_DIR / "pipeline.log"),
            logging.StreamHandler(),
        ],
    )


def as_float(value: Any) -> float | None:
    if value is None or value == "" or (isinstance(value, float) and pd.isna(value)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(number) else number


def as_int(value: Any) -> int | None:
    number = as_float(value)
    return None if number is None else int(number)


def load_universe() -> tuple[list[str], dict[str, str]]:
    if not COMPANIES_CSV.exists():
        raise RuntimeError(f"Missing stock universe file: {COMPANIES_CSV}")
    codes: list[str] = []
    names: dict[str, str] = {}
    with COMPANIES_CSV.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            code = (row.get("code") or "").strip().upper()
            if code and code not in codes:
                codes.append(code)
                names[code] = (row.get("name") or "").strip()
    if not codes:
        raise RuntimeError("companies.csv contains no stock codes")
    return codes, names


def ticker_frame(download: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Extract one ticker from yfinance output across supported column layouts."""
    if not isinstance(download.columns, pd.MultiIndex):
        return download.copy()
    level_zero = download.columns.get_level_values(0)
    level_one = download.columns.get_level_values(1)
    if ticker in level_zero:
        return download[ticker].copy()
    if ticker in level_one:
        return download.xs(ticker, axis=1, level=1).copy()
    return pd.DataFrame()


def session_is_complete(trade_date: date, now: datetime | None = None) -> bool:
    """A bar dated today is only trusted after the Jakarta close."""
    now = now or datetime.now(WIB)
    if trade_date < now.date():
        return True
    if trade_date > now.date():
        return False
    return now.time() >= MARKET_CLOSE_WIB


def resolve_report_date(available: set[date]) -> tuple[date, bool]:
    """Return the newest COMPLETED session and whether a partial day was dropped."""
    if not available:
        raise RuntimeError("yfinance returned no usable daily bars")
    newest = max(available)
    if session_is_complete(newest):
        return newest, False
    completed = {day for day in available if session_is_complete(day)}
    if not completed:
        raise RuntimeError(
            f"Only the in-progress session {newest} is available; run again after "
            f"{MARKET_CLOSE_WIB.strftime('%H:%M')} WIB"
        )
    dropped = max(completed)
    logging.warning(
        "Ignoring the in-progress %s bar (before %s WIB); reporting on %s instead",
        newest, MARKET_CLOSE_WIB.strftime("%H:%M"), dropped,
    )
    return dropped, True


def download_universe(tickers: list[str]) -> pd.DataFrame:
    logging.info("Downloading ranking history for %s configured stocks", len(tickers))
    download = yf.download(
        tickers,
        period=UNIVERSE_PERIOD,
        interval="1d",
        group_by="ticker",
        auto_adjust=False,   # raw closes: value estimate and levels must be tradeable
        actions=False,
        progress=False,
        threads=True,
    )
    if download.empty:
        raise RuntimeError("yfinance returned no data for the configured stock universe")
    return download


def universe_snapshot(download: pd.DataFrame, codes: list[str]) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {}
    for code in codes:
        frame = ticker_frame(download, f"{code}.JK")
        if frame.empty or "Close" not in frame or "Volume" not in frame:
            logging.warning("No usable ranking data for %s.JK", code)
            continue
        frame = frame.dropna(subset=["Close", "Volume"])
        frame = frame[frame["Volume"] > 0]
        if frame.empty:
            logging.warning("No non-zero-volume bar for %s.JK", code)
            continue
        frames[code] = frame
    if not frames:
        raise RuntimeError("No usable yfinance bars for any configured stock")
    return frames


def rank_from_yfinance(
    frames: dict[str, pd.DataFrame], report_date: date
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for code, frame in frames.items():
        history = frame[frame.index.map(lambda ts: pd.Timestamp(ts).date() <= report_date)]
        if history.empty:
            continue
        if pd.Timestamp(history.index[-1]).date() != report_date:
            logging.info("%s has no bar on %s; excluded from the ranking", code, report_date)
            continue
        value_series = history["Close"].astype(float) * history["Volume"].astype(float)
        if len(value_series) < 20:
            logging.info("%s has under 20 sessions of history; excluded", code)
            continue
        average_value = float(value_series.tail(20).mean())
        if average_value < MIN_AVG_VALUE_20D:
            logging.info(
                "%s 20-session average value %.0f below the %.0f floor; excluded",
                code, average_value, MIN_AVG_VALUE_20D,
            )
            continue
        latest = history.iloc[-1]
        close = float(latest["Close"])
        previous_close = float(history.iloc[-2]["Close"]) if len(history) > 1 else None
        candidates.append(
            {
                "code": code,
                "close": close,
                "volume": int(latest["Volume"]),
                "value": int(close * float(latest["Volume"])),
                "avg_value_20d": average_value,
                "daily_change_pct": (
                    None if not previous_close else (close / previous_close - 1) * 100
                ),
            }
        )
    candidates.sort(key=lambda row: row["value"], reverse=True)
    top = candidates[:TOP_COUNT]
    for rank, row in enumerate(top, start=1):
        row["rank"] = rank
        row["value_source"] = "yfinance estimate (raw close x volume)"
    return top


def table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def idx_coverage(connection: sqlite3.Connection, report_date: date) -> int:
    if not table_exists(connection, "idx_summary"):
        return 0
    row = connection.execute(
        "SELECT COUNT(*) FROM idx_summary WHERE trade_date=?", (report_date.isoformat(),)
    ).fetchone()
    return int(row[0]) if row else 0


def rank_from_idx(
    connection: sqlite3.Connection, report_date: date
) -> list[dict[str, Any]]:
    """Official top 10 by transaction value from the ingested IDX Stock Summary."""
    rows = connection.execute(
        """
        SELECT stock_code, close, volume, value, frequency, foreign_net, company
        FROM idx_summary
        WHERE trade_date=? AND value IS NOT NULL
        ORDER BY value DESC
        LIMIT ?
        """,
        (report_date.isoformat(), TOP_COUNT),
    ).fetchall()
    top = []
    for rank, (code, close, volume, value, frequency, foreign_net, company) in enumerate(rows, start=1):
        top.append(
            {
                "rank": rank,
                "code": str(code).upper(),
                "close": as_float(close),
                "volume": as_int(volume),
                "value": as_int(value),
                "frequency": as_int(frequency),
                "avg_value_20d": None,
                "daily_change_pct": None,
                "value_source": "IDX Stock Summary (official)",
                "company": (company or "").strip(),
            }
        )
    return top


def connect_database() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.execute("PRAGMA journal_mode=WAL")
    return connection


def create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS top_stocks (
            trade_date TEXT NOT NULL,
            rank INTEGER NOT NULL,
            stock_code TEXT NOT NULL,
            close_price REAL,
            daily_change REAL,
            volume INTEGER,
            transaction_value INTEGER,
            value_source TEXT,
            PRIMARY KEY (trade_date, stock_code)
        );

        CREATE TABLE IF NOT EXISTS stock_prices (
            stock_code TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            adj_close REAL,
            volume INTEGER,
            source TEXT NOT NULL DEFAULT 'yfinance',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (stock_code, trade_date)
        );

        CREATE TABLE IF NOT EXISTS refresh_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            report_date TEXT,
            status TEXT NOT NULL,
            message TEXT
        );
        """
    )
    connection.commit()


def migrate_schema(connection: sqlite3.Connection) -> None:
    """Add columns introduced after the first version, re-fetching prices once.

    Older databases stored ADJUSTED closes in stock_prices.close. Levels must be
    raw, so those rows are dropped and refetched from Yahoo (no data is lost:
    prices always come from Yahoo).
    """
    price_columns = {row[1] for row in connection.execute("PRAGMA table_info(stock_prices)")}
    if price_columns and "adj_close" not in price_columns:
        connection.execute("ALTER TABLE stock_prices ADD COLUMN adj_close REAL")
        connection.execute("DELETE FROM stock_prices")
        logging.warning(
            "stock_prices held adjusted closes; cleared for a one-time refetch of raw prices"
        )
    top_columns = {row[1] for row in connection.execute("PRAGMA table_info(top_stocks)")}
    if top_columns:
        if "transaction_value" not in top_columns:
            connection.execute("ALTER TABLE top_stocks ADD COLUMN transaction_value INTEGER")
            if "estimated_transaction_value" in top_columns:
                connection.execute(
                    "UPDATE top_stocks SET transaction_value=estimated_transaction_value"
                )
        if "value_source" not in top_columns:
            connection.execute("ALTER TABLE top_stocks ADD COLUMN value_source TEXT")
    connection.commit()


def archive_ranking(
    report_date: date, rows: list[dict[str, Any]], universe_size: int, source: str
) -> None:
    destination = RAW_DIR / report_date.isoformat()
    destination.mkdir(parents=True, exist_ok=True)
    payload = {
        "report_date": report_date.isoformat(),
        "ranking_source": source,
        "universe_size": universe_size,
        "min_avg_value_20d": MIN_AVG_VALUE_20D,
        "rows": rows,
    }
    with (destination / "top_stocks.json").open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)


def save_top_stocks(
    connection: sqlite3.Connection, report_date: date, rows: list[dict[str, Any]]
) -> None:
    codes = [row["code"] for row in rows]
    placeholders = ",".join("?" for _ in codes)
    removed = connection.execute(
        f"DELETE FROM top_stocks WHERE trade_date=? AND stock_code NOT IN ({placeholders})",
        (report_date.isoformat(), *codes),
    ).rowcount
    if removed:
        logging.info(
            "Removed %s stale top-10 row(s) for %s from an earlier run", removed, report_date
        )
    connection.executemany(
        """
        INSERT INTO top_stocks (
            trade_date, rank, stock_code, close_price, daily_change, volume,
            transaction_value, value_source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(trade_date, stock_code) DO UPDATE SET
            rank=excluded.rank,
            close_price=excluded.close_price,
            daily_change=excluded.daily_change,
            volume=excluded.volume,
            transaction_value=excluded.transaction_value,
            value_source=excluded.value_source
        """,
        [
            (
                report_date.isoformat(),
                row["rank"],
                row["code"],
                row.get("close"),
                row.get("daily_change_pct"),
                row.get("volume"),
                row.get("value"),
                row.get("value_source"),
            )
            for row in rows
        ],
    )
    connection.commit()


def latest_price_date(connection: sqlite3.Connection, stock_code: str) -> str | None:
    row = connection.execute(
        "SELECT MAX(trade_date) FROM stock_prices WHERE stock_code=?", (stock_code,)
    ).fetchone()
    return row[0] if row else None


def update_ohlcv(
    connection: sqlite3.Connection, stock_code: str, report_date: date
) -> int:
    latest = latest_price_date(connection, stock_code)
    if latest is None:
        start_date = report_date - timedelta(days=550)
        logging.info("%s is new; requesting the initial OHLCV backfill", stock_code)
    else:
        latest_date = datetime.strptime(latest, "%Y-%m-%d").date()
        start_date = min(latest_date, report_date) - timedelta(days=550)

    prices = yf.download(
        f"{stock_code}.JK",
        start=start_date.isoformat(),
        end=(report_date + timedelta(days=1)).isoformat(),
        interval="1d",
        auto_adjust=False,   # keep raw OHLC; Adj Close is stored separately
        actions=False,
        progress=False,
        threads=False,
        multi_level_index=False,
    )
    if prices.empty:
        raise RuntimeError(f"No yfinance OHLCV returned for {stock_code}.JK")

    prices = prices.reset_index()
    if "Date" not in prices.columns:
        # yfinance names the index Date, but be tolerant of other spellings
        prices = prices.rename(columns={prices.columns[0]: "Date"})
    now = datetime.now().isoformat(timespec="seconds")
    records = []
    for _, row in prices.iterrows():
        record_date = pd.Timestamp(row["Date"]).date()
        if record_date > report_date:
            continue  # never store the in-progress session
        close = as_float(row.get("Close"))
        records.append(
            (
                stock_code,
                record_date.isoformat(),
                as_float(row.get("Open")),
                as_float(row.get("High")),
                as_float(row.get("Low")),
                close,
                as_float(row.get("Adj Close")) if "Adj Close" in prices.columns else close,
                as_int(row.get("Volume")),
                now,
            )
        )

    connection.executemany(
        """
        INSERT INTO stock_prices (
            stock_code, trade_date, open, high, low, close, adj_close, volume, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(stock_code, trade_date) DO UPDATE SET
            open=excluded.open,
            high=excluded.high,
            low=excluded.low,
            close=excluded.close,
            adj_close=excluded.adj_close,
            volume=excluded.volume,
            source='yfinance',
            updated_at=excluded.updated_at
        """,
        records,
    )
    connection.commit()
    return len(records)


def wilder(series: pd.Series) -> pd.Series:
    return series.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()


def calculate_price_metrics(
    connection: sqlite3.Connection, stock_code: str, report_date: date
) -> dict[str, Any]:
    prices = pd.read_sql_query(
        """
        SELECT trade_date, open, high, low, close, adj_close, volume
        FROM stock_prices
        WHERE stock_code=? AND trade_date<=?
        ORDER BY trade_date
        """,
        connection,
        params=(stock_code, report_date.isoformat()),
    )
    if prices.empty:
        return {}

    close = prices["close"].astype(float)                      # raw: levels
    adjusted = prices["adj_close"].astype(float).fillna(close)  # adjusted: returns
    high = prices["high"].astype(float)
    low = prices["low"].astype(float)
    volume = prices["volume"].astype(float)

    def period_return(sessions_back: int) -> float | None:
        if len(adjusted) <= sessions_back:
            return None
        base = adjusted.iloc[-sessions_back - 1]
        if not base:
            return None
        return adjusted.iloc[-1] / base - 1

    delta = close.diff()
    gain = wilder(delta.clip(lower=0))
    loss = wilder(-delta.clip(upper=0))
    rsi14 = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))

    previous_close = close.shift(1)
    true_range = pd.concat(
        [(high - low), (high - previous_close).abs(), (low - previous_close).abs()], axis=1
    ).max(axis=1)
    atr14 = wilder(true_range)

    ma20 = close.rolling(20).mean()
    ma50 = close.rolling(50).mean()
    volume_ma20 = volume.rolling(20).mean()
    # Estimated traded value per session; an average of it says whether today's
    # official turnover is normal for this stock or a spike.
    value_ma20 = (close * volume).rolling(20).mean()

    # Both levels use the 20 sessions BEFORE the report date (symmetric window).
    window_low = low.iloc[-21:-1] if len(low) >= 21 else low.iloc[:-1]
    window_high = high.iloc[-21:-1] if len(high) >= 21 else high.iloc[:-1]
    support = float(window_low.min()) if not window_low.empty else float(low.iloc[-1])
    resistance = float(window_high.max()) if not window_high.empty else float(high.iloc[-1])

    current_close = float(close.iloc[-1])
    current_ma20 = None if pd.isna(ma20.iloc[-1]) else float(ma20.iloc[-1])
    current_ma50 = None if pd.isna(ma50.iloc[-1]) else float(ma50.iloc[-1])
    previous_ma20 = (
        None if len(ma20) < 2 or pd.isna(ma20.iloc[-2]) else float(ma20.iloc[-2])
    )
    ma20_rising = (
        current_ma20 is not None
        and previous_ma20 is not None
        and current_ma20 > previous_ma20
    )
    current_atr = None if pd.isna(atr14.iloc[-1]) else float(atr14.iloc[-1])

    if current_ma20 is None or current_ma50 is None:
        trend = "Insufficient history"
    elif current_close > current_ma20 > current_ma50:
        trend = "Bullish"
    elif current_close < current_ma20 < current_ma50:
        trend = "Bearish"
    else:
        trend = "Mixed"

    scenario = trade_scenario(
        trend,
        current_close,
        support,
        resistance,
        current_atr,
        current_ma20,
        current_ma50,
        ma20_rising,
    )

    metrics = {
        "ohlcv_date": prices.iloc[-1]["trade_date"],
        "close": current_close,
        "return_1d": period_return(1),
        "return_3d": period_return(3),
        "return_5d": period_return(5),
        "ma20": current_ma20,
        "ma50": current_ma50,
        "ma20_rising": ma20_rising,
        "rsi14": None if pd.isna(rsi14.iloc[-1]) else float(rsi14.iloc[-1]),
        "atr14": current_atr,
        "atr_pct": None if not current_atr else current_atr / current_close,
        "volume_vs_20d": (
            None
            if pd.isna(volume_ma20.iloc[-1]) or not volume_ma20.iloc[-1]
            else float(volume.iloc[-1] / volume_ma20.iloc[-1])
        ),
        "avg_value_20d": (
            None if pd.isna(value_ma20.iloc[-1]) else float(value_ma20.iloc[-1])
        ),
        "support_prior_20d": support,
        "resistance_prior_20d": resistance,
        "trend": trend,
    }
    metrics.update(scenario)
    return metrics

def idx_tick_size(price: float) -> int:
    """Regular-market IDX tick size for a given price."""
    if price < 200:
        return 1
    if price < 500:
        return 2
    if price < 2_000:
        return 5
    if price < 5_000:
        return 10
    return 25


def round_idx_price(price: float, direction: str) -> int:
    """Round a calculated level onto a valid IDX regular-market price."""
    if not math.isfinite(price) or price <= 0:
        raise ValueError(f"Invalid IDX price: {price}")

    tick = idx_tick_size(price)

    if direction == "up":
        return int(math.ceil((price - 1e-9) / tick) * tick)
    if direction == "down":
        return int(math.floor((price + 1e-9) / tick) * tick)

    raise ValueError("direction must be 'up' or 'down'")


def is_valid_idx_price(price: float) -> bool:
    """Return True when price is on a valid regular-market tick."""
    tick = idx_tick_size(price)
    return price > 0 and abs(price / tick - round(price / tick)) < 1e-9

def trade_scenario(
    trend: str,
    close: float,
    support: float,
    resistance: float,
    atr: float | None,
    ma20: float | None,
    ma50: float | None,
    ma20_rising: bool,
) -> dict[str, Any]:
    """Pick the setup the chart actually presents and show reference levels.

    Breakout long: price at or near the prior-20-session high.
    Pullback long: a confirmed rising regime testing MA20 or support. Pullback
      stops sit below structural support and targets are capped at resistance.
    Rows that fail the setup rules retain tick-valid reference levels, but are
    explicitly labelled ``Watch only`` rather than presented as trade signals.
    """
    blank: dict[str, Any] = {
        "setup": None,
        "entry": None,
        "stop": None,
        "target": None,
        "distance_to_entry": None,
        "risk_per_share": None,
        "risk_pct_of_entry": None,
        "reward_to_risk": None,
        "setup_status": None,
    }
    if trend == "Insufficient history":
        return {**blank, "setup": "No setup (insufficient history)"}
    if atr is None:
        return {**blank, "setup": "No setup (ATR needs 14 sessions)"}

    def reference_levels(
        reason: str,
        entry: float,
        stop: float,
        target: float,
    ) -> dict[str, Any]:
        """Return a non-actionable, tick-valid plan so every row remains useful."""
        rounded_entry = round_idx_price(entry, "up")
        rounded_stop = round_idx_price(stop, "down")
        if rounded_stop <= 0 or rounded_stop >= rounded_entry:
            rounded_stop = round_idx_price(rounded_entry - atr, "down")
        risk = rounded_entry - rounded_stop
        rounded_target = round_idx_price(target, "down")
        if rounded_target <= rounded_entry:
            rounded_target = round_idx_price(rounded_entry + 2 * risk, "down")
        reward_to_risk = (rounded_target - rounded_entry) / risk
        return {
            "setup": reason,
            "entry": rounded_entry,
            "stop": rounded_stop,
            "target": rounded_target,
            "distance_to_entry": rounded_entry / close - 1,
            "risk_per_share": risk,
            "risk_pct_of_entry": risk / rounded_entry,
            "reward_to_risk": reward_to_risk,
            "setup_status": "Watch only",
        }

    if trend == "Bearish":
        return reference_levels(
            "No long setup (downtrend)",
            max(close, ma20 or close),
            support - SUPPORT_STOP_BUFFER_ATR * atr,
            resistance,
        )

    def priced(
        setup: str,
        entry: float,
        stop: float,
        target_cap: float | None = None,
    ) -> dict[str, Any]:
        rounded_entry = round_idx_price(entry, "up")
        rounded_stop = round_idx_price(stop, "down")

        risk = rounded_entry - rounded_stop
        if rounded_stop <= 0 or risk <= 0:
            return {
                **blank,
                "setup": "No setup (stop would sit at or above entry)",
            }

        target = rounded_entry + 2 * risk
        if target_cap is not None:
            target = min(target, target_cap)
        rounded_target = round_idx_price(target, "down")
        if rounded_target <= rounded_entry:
            return {**blank, "setup": "No setup (resistance is at or below entry)"}

        reward_to_risk = (rounded_target - rounded_entry) / risk
        if reward_to_risk < MIN_REWARD_TO_RISK:
            return reference_levels(
                "No setup (insufficient room to resistance)",
                rounded_entry,
                rounded_stop,
                rounded_target,
            )

        return {
            "setup": setup,
            "entry": rounded_entry,
            "stop": rounded_stop,
            "target": rounded_target,
            "distance_to_entry": rounded_entry / close - 1,
            "risk_per_share": risk,
            "risk_pct_of_entry": risk / rounded_entry,
            "reward_to_risk": reward_to_risk,
            "setup_status": "At trigger" if rounded_entry <= close else "Pending",
        }

    # 1. Breakout: at or near the prior high.
    if close >= resistance * (1 - BREAKOUT_PROXIMITY):
        entry = max(close, resistance)
        stop = max(support - SUPPORT_STOP_BUFFER_ATR * atr, resistance - BREAKOUT_STOP_BUFFER_ATR * atr)
        if stop >= entry:
            stop = entry - atr
        label = "Breakout long" if trend == "Bullish" else "Breakout long (mixed trend)"
        return priced(label, entry, stop)

    if close < support:
        return reference_levels(
            "No setup (below 20-session support)",
            support,
            support - SUPPORT_STOP_BUFFER_ATR * atr,
            resistance,
        )

    uptrend_regime = bool(
        ma20 is not None
        and ma50 is not None
        and ma20 > ma50
        and ma20_rising
        and close > ma50
    )
    if not uptrend_regime:
        return reference_levels(
            "No long setup (uptrend regime not confirmed)",
            max(close, ma20 or close),
            support - SUPPORT_STOP_BUFFER_ATR * atr,
            resistance,
        )

    pullback_stop = support - SUPPORT_STOP_BUFFER_ATR * atr

    # 2. Pullback to MA20, distinguishing a test from a pending reclaim.
    if ma20 is not None and abs(close - ma20) <= LEVEL_PROXIMITY_ATR * atr:
        if close >= ma20:
            return priced(
                "Pullback long (test MA20 from above)",
                close,
                pullback_stop,
                resistance,
            )
        return priced(
            "Pullback long (pending MA20 reclaim)",
            ma20,
            pullback_stop,
            resistance,
        )

    # 3. Support test: within half an ATR above support, never a loose % band.
    support_distance = close - support
    if 0 <= support_distance <= LEVEL_PROXIMITY_ATR * atr:
        return priced(
            "Pullback long (test support)",
            close,
            pullback_stop,
            resistance,
        )

    breakout_entry = resistance
    breakout_stop = max(
        support - SUPPORT_STOP_BUFFER_ATR * atr,
        resistance - BREAKOUT_STOP_BUFFER_ATR * atr,
    )
    breakout_risk = round_idx_price(breakout_entry, "up") - round_idx_price(
        breakout_stop, "down"
    )
    return reference_levels(
        "No setup (mid-range, no trigger nearby)",
        breakout_entry,
        breakout_stop,
        breakout_entry + 2 * breakout_risk,
    )


def session_calendar(
    connection: sqlite3.Connection, stock_code: str, report_date: date, count: int
) -> list[str]:
    """The last `count` traded sessions up to report_date, newest first."""
    rows = connection.execute(
        """
        SELECT trade_date FROM stock_prices
        WHERE stock_code=? AND trade_date<=? AND COALESCE(volume, 0)>0
        ORDER BY trade_date DESC LIMIT ?
        """,
        (stock_code, report_date.isoformat(), count),
    ).fetchall()
    return [row[0] for row in rows]


def calculate_flow_metrics(
    connection: sqlite3.Connection, stock_code: str, report_date: date
) -> dict[str, Any]:
    """Foreign flow from the ingested IDX Stock Summary, over real sessions."""
    empty = {
        "foreign_buy_1d_shares": None,
        "foreign_sell_1d_shares": None,
        "foreign_net_1d_shares": None,
        "foreign_net_1d_idr": None,
        "foreign_net_3d_idr": None,
        "foreign_net_5d_idr": None,
        "foreign_sessions_5d": 0,
        "foreign_unit_1d": None,
    }
    if not table_exists(connection, "idx_summary"):
        return empty
    sessions = session_calendar(connection, stock_code, report_date, FLOW_SESSIONS)
    if not sessions:
        return empty
    placeholders = ",".join("?" for _ in sessions)
    stored = {
        row[0]: row[1:]
        for row in connection.execute(
            f"""
            SELECT trade_date, foreign_buy, foreign_sell, foreign_net,
                   foreign_net_value, foreign_unit
            FROM idx_summary
            WHERE stock_code=? AND trade_date IN ({placeholders})
            """,
            (stock_code, *sessions),
        )
    }
    if not stored:
        return empty

    blank = (None, None, None, None, None)

    def window_sum(count: int) -> int | None:
        """Rupiah estimate of net foreign flow; blank unless every session is present."""
        if len(sessions) < count:
            return None
        values = [stored.get(day, blank)[3] for day in sessions[:count]]
        return None if any(value is None for value in values) else int(sum(values))

    today = stored.get(report_date.isoformat(), blank)
    return {
        "foreign_buy_1d_shares": today[0],
        "foreign_sell_1d_shares": today[1],
        "foreign_net_1d_shares": today[2],
        "foreign_net_1d_idr": today[3],
        "foreign_net_3d_idr": window_sum(3),
        "foreign_net_5d_idr": window_sum(5),
        "foreign_sessions_5d": sum(
            1 for day in sessions if stored.get(day, blank)[3] is not None
        ),
        "foreign_unit_1d": today[4],
    }


def build_report(
    connection: sqlite3.Connection,
    report_date: date,
    top_rows: list[dict[str, Any]],
    names: dict[str, str],
) -> pd.DataFrame:
    previous_row = connection.execute(
        "SELECT MAX(trade_date) FROM top_stocks WHERE trade_date < ?",
        (report_date.isoformat(),),
    ).fetchone()
    previous_date = previous_row[0] if previous_row and previous_row[0] else None
    previous_ranks = dict(
        connection.execute(
            "SELECT stock_code, rank FROM top_stocks WHERE trade_date=?",
            (previous_date,),
        ).fetchall()
    ) if previous_date else {}

    report_rows = []
    for row in top_rows:
        code = row["code"]
        prior_rank = previous_ranks.get(code)
        rank_change = None
        if previous_date:
            rank_change = "New" if prior_rank is None else int(prior_rank) - int(row["rank"])
        combined: dict[str, Any] = {
            "date": report_date.isoformat(),
            "rank": row["rank"],
            "rank_change": rank_change,
            "previous_rank_date": previous_date,
            "stock_code": code,
            "company": names.get(code) or row.get("company") or "",
            "transaction_value": row.get("value"),
            "value_source": row.get("value_source"),
            "volume": row.get("volume"),
            "avg_value_20d": row.get("avg_value_20d"),
        }
        combined.update(calculate_price_metrics(connection, code, report_date))
        combined.update(calculate_flow_metrics(connection, code, report_date))
        value, average = combined.get("transaction_value"), combined.get("avg_value_20d")
        combined["value_vs_20d_avg"] = (
            value / average if value and average else None
        )
        foreign_1d = combined.get("foreign_net_1d_idr")
        combined["foreign_net_pct_turnover"] = (
            foreign_1d / value if foreign_1d is not None and value else None
        )

        activity_ratio = max(
            combined.get("value_vs_20d_avg") or 0,
            combined.get("volume_vs_20d") or 0,
        )
        foreign_share = combined["foreign_net_pct_turnover"]
        daily_return = combined.get("return_1d") or 0
        if activity_ratio >= 1.2 and abs(daily_return) >= 0.02:
            combined["activity_signal"] = "Price-led activity"
        elif activity_ratio >= 1.2:
            combined["activity_signal"] = "High activity"
        else:
            combined["activity_signal"] = "Normal activity"

        if foreign_share is None:
            combined["foreign_signal"] = "Unavailable"
        elif foreign_share >= 0.05:
            combined["foreign_signal"] = "Foreign accumulation"
        elif foreign_share <= -0.05:
            combined["foreign_signal"] = "Foreign distribution"
        else:
            combined["foreign_signal"] = "Foreign neutral"

        setup = str(combined.get("setup") or "")
        flow_1d = combined.get("foreign_net_1d_idr")
        flow_3d = combined.get("foreign_net_3d_idr")
        if not setup.startswith(("Breakout", "Pullback")):
            combined["flow_check"] = "No trigger"
        elif flow_1d is None or flow_3d is None:
            combined["flow_check"] = "Flow unavailable"
        elif flow_1d > 0 and flow_3d > 0:
            combined["flow_check"] = "Flow confirms"
        elif flow_1d < 0 and flow_3d < 0:
            combined["flow_check"] = "Flow diverges"
        else:
            combined["flow_check"] = "Mixed flow"
        report_rows.append(combined)
    report = pd.DataFrame(report_rows)
    return report.sort_values("rank") if "rank" in report else report


# --- report rendering -------------------------------------------------------
# Samuel Sekuritas palette, sampled from samuel.co.id
BRAND = {
    "primary": "#1428A0",
    "primary_dark": "#0e1c73",
    "accent": "#00BDFF",
    "negative": "#CC3366",
    "positive": "#0F7A5A",
    "text": "#333333",
    "muted": "#69727D",
    "line": "#E4E7EC",
    "shade": "#F6F6F6",
}

# (group, [(column, heading, tooltip, kind)]) - kind drives formatting and colour
COLUMN_GROUPS: list[tuple[str, list[tuple[str, str, str, str]]]] = [
    ("Stock", [
        ("rank", "#", "Rank by the day's transaction value", "rank"),
        ("rank_change", "Move", "Rank change versus the previous report", "rank_delta"),
        ("stock_code", "Code", "IDX ticker", "code"),
        ("company", "Company", "Listed company name", "text"),
    ]),
    ("Turnover", [
        ("transaction_value", "Turnover", "Transaction value for the session", "rp"),
        ("value_vs_20d_avg", "vs 20d", "Turnover over its 20-session average; above 1.0 is unusually active", "x"),
    ]),
    ("Price performance", [
        ("close", "Close", "Closing price (Rp)", "price"),
        ("return_1d", "1 day", "Return over the last session (adjusted closes)", "pct"),
        ("return_3d", "3 days", "Return over the last 3 sessions", "pct"),
        ("return_5d", "1 week", "Return over the last 5 sessions", "pct"),
    ]),
    ("Foreign flow (net, est. Rp)", [
        ("foreign_net_1d_idr", "1 day", "Net foreign buy (+) or sell (-) for the session, shares x VWAP", "rp_signed"),
        ("foreign_net_3d_idr", "3 days", "Net foreign flow over 3 sessions; blank if a session is missing", "rp_signed"),
        ("foreign_net_5d_idr", "1 week", "Net foreign flow over 5 sessions; blank if a session is missing", "rp_signed"),
        ("foreign_sessions_5d", "Days", "How many of the last 5 sessions have foreign-flow data", "int"),
    ]),
    ("Market participation", [
        ("activity_signal", "Activity", "Price, volume and turnover classification", "activity"),
        ("foreign_signal", "Foreign signal", "Foreign net flow as a share of turnover", "activity"),
        ("foreign_net_pct_turnover", "Foreign / turnover", "Estimated one-day foreign net flow divided by turnover", "pct_plain"),
    ]),
    ("Technical picture", [
        ("trend", "Trend", "Close vs MA20 vs MA50", "trend"),
        ("ma20", "MA20", "20-session moving average (raw prices)", "price"),
        ("ma50", "MA50", "50-session moving average (raw prices)", "price"),
        ("rsi14", "RSI", "Wilder RSI(14); above 70 overbought, below 30 oversold", "num1"),
        ("atr_pct", "ATR", "ATR(14) as a percent of price: daily volatility", "pct_plain"),
        ("volume_vs_20d", "Vol vs 20d", "Session volume over its 20-session average", "x"),
        ("support_prior_20d", "Support", "Lowest low of the 20 sessions before this one", "price"),
        ("resistance_prior_20d", "Resistance", "Highest high of the 20 sessions before this one", "price"),
    ]),
    ("Trade scenario (technical, not advice)", [
        ("setup", "Setup", "Which setup the chart presents, or why there is none", "setup"),
        ("setup_status", "Status", "At trigger, pending, or watch only when the setup rules fail", "text"),
        ("entry", "Entry", "Trigger level; a reference recovery or breakout level for watch-only rows", "price"),
        ("stop", "Stop", "Reference invalidation level", "price"),
        ("target", "Target", "Reference objective; pullbacks are capped at resistance and breakouts use a 2R extension", "price"),
        ("distance_to_entry", "To entry", "How far the close is from the trigger", "pct_plain"),
        ("risk_pct_of_entry", "Risk", "Entry minus stop, as a percent of entry", "pct_plain"),
        ("reward_to_risk", "R:R", "Reward-to-risk ratio", "num1"),
        ("flow_check", "Flow check", "Whether one- and three-day foreign flow support the setup", "flow_check"),
    ]),
    ("As of", [
        ("ohlcv_date", "Price date", "Date of the price bar used", "text"),
    ]),
]
NUMERIC_KINDS = {"rp", "rp_signed", "x", "pct", "pct_plain", "price", "num1", "int", "rank", "rank_delta"}


def compact_rupiah(value: float) -> str:
    magnitude = abs(value)
    for size, suffix in ((1e12, "tn"), (1e9, "bn"), (1e6, "mn"), (1e3, "k")):
        if magnitude >= size:
            return f"{value / size:,.1f}{suffix}"
    return f"{value:,.0f}"


def render_cell(value: Any, kind: str) -> tuple[str, str]:
    """Return (text, css class) for one value."""
    if value is None or (isinstance(value, float) and pd.isna(value)) or value == "":
        return "&ndash;", "empty"
    if kind in ("text", "code", "rank", "int"):
        return html_escape(f"{int(value)}" if kind in ("rank", "int") else str(value)), kind
    if kind == "rank_delta":
        if str(value) == "New":
            return '<span class="rank-move new">New</span>', "rank_delta"
        change = int(float(value))
        if change > 0:
            return f'<span class="rank-move pos">▲ {change}</span>', "rank_delta"
        if change < 0:
            return f'<span class="rank-move neg">▼ {abs(change)}</span>', "rank_delta"
        return '<span class="rank-move flat">—</span>', "rank_delta"
    if kind == "trend":
        label = str(value)
        tone = {"Bullish": "pos", "Bearish": "neg"}.get(label, "flat")
        return f'<span class="pill {tone}">{html_escape(label)}</span>', "trend"
    if kind == "setup":
        label = str(value)
        tone = "pos" if label.startswith(("Breakout", "Pullback")) else "flat"
        return f'<span class="pill {tone}">{html_escape(label)}</span>', "setup"
    if kind == "activity":
        label = str(value)
        tone = "pos" if label == "Foreign accumulation" else ("neg" if label == "Foreign distribution" else "flat")
        return f'<span class="pill {tone}">{html_escape(label)}</span>', "activity"
    if kind == "flow_check":
        label = str(value)
        tone = "pos" if label == "Flow confirms" else ("neg" if label == "Flow diverges" else "flat")
        return f'<span class="pill {tone}">{html_escape(label)}</span>', "flow_check"
    number = float(value)
    if kind == "rp":
        return compact_rupiah(number), "num"
    if kind == "rp_signed":
        sign = "pos" if number > 0 else ("neg" if number < 0 else "")
        return f"{'+' if number > 0 else ''}{compact_rupiah(number)}", f"num {sign}"
    if kind == "pct":
        sign = "pos" if number > 0 else ("neg" if number < 0 else "")
        return f"{number:+.2%}", f"num {sign}"
    if kind == "pct_plain":
        return f"{number:.1%}", "num"
    if kind == "x":
        tone = "strong" if number >= 1.5 else ""
        return f"{number:.2f}x", f"num {tone}"
    if kind == "price":
        return f"{number:,.0f}" if abs(number) >= 100 else f"{number:,.2f}", "num"
    if kind == "num1":
        return f"{number:,.1f}", "num"
    return html_escape(str(value)), "num"


def html_escape(text: str) -> str:
    return (
        str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;").replace("'", "&#39;")
    )


def render_table(report: pd.DataFrame) -> str:
    groups = [
        (name, [col for col in cols if col[0] in report.columns])
        for name, cols in COLUMN_GROUPS
    ]
    groups = [(name, cols) for name, cols in groups if cols]

    group_row = "".join(
        f'<th class="group" colspan="{len(cols)}">{html_escape(name)}</th>'
        for name, cols in groups
    )
    head_row = "".join(
        f'<th class="{kind if kind in NUMERIC_KINDS else "txt"} col-{column}" '
        f'data-column="{html_escape(column)}" aria-sort="none">'
        f'<button type="button">{html_escape(label)}<span class="sort-mark" aria-hidden="true"></span></button></th>'
        for _, cols in groups
        for column, label, _tip, kind in cols
    )
    body = []
    for _, row in report.iterrows():
        cells = []
        for _, cols in groups:
            for column, _label, _tip, kind in cols:
                text, css = render_cell(row.get(column), kind)
                align = "num" if kind in NUMERIC_KINDS else "txt"
                raw = row.get(column)
                if raw is None or (isinstance(raw, float) and pd.isna(raw)):
                    sort_value = ""
                elif isinstance(raw, (int, float, np.integer, np.floating)):
                    sort_value = f"{float(raw):.12g}"
                else:
                    sort_value = str(raw).lower()
                cells.append(
                    f'<td class="{align} {css} col-{column}" '
                    f'data-sort="{html_escape(sort_value)}">{text}</td>'
                )
        body.append("<tr>" + "".join(cells) + "</tr>")

    return (
        '<table class="report"><thead>'
        f"<tr class=\"groups\">{group_row}</tr><tr>{head_row}</tr>"
        "</thead><tbody>" + "".join(body) + "</tbody></table>"
    )


def render_summary(report: pd.DataFrame) -> str:
    def total(column: str) -> float | None:
        if column not in report:
            return None
        values = report[column].dropna()
        return float(values.sum()) if len(values) else None

    turnover = total("transaction_value")
    flow = total("foreign_net_1d_idr")
    candidates = at_trigger = pending = watch_only = 0
    if "setup_status" in report:
        statuses = report["setup_status"].fillna("")
        at_trigger = int((statuses == "At trigger").sum())
        pending = int((statuses == "Pending").sum())
        watch_only = int((statuses == "Watch only").sum())
        candidates = at_trigger + pending
    flow_days = None
    if "foreign_sessions_5d" in report:
        available = report["foreign_sessions_5d"].dropna()
        flow_days = int(available.min()) if len(available) else None
    tiles = [
        ("Stocks", f"{len(report)}"),
        ("Combined turnover", f"Rp {compact_rupiah(turnover)}" if turnover else "&ndash;"),
        (
            "Net foreign flow, 1 day",
            f"{'+' if (flow or 0) > 0 else ''}Rp {compact_rupiah(flow)}" if flow is not None else "&ndash;",
        ),
        (
            "Setup candidates",
            f"{candidates} · {at_trigger} at trigger · {pending} pending · {watch_only} watch only",
        ),
        ("Foreign-flow coverage", f"{flow_days} of 5 sessions" if flow_days is not None else "Unavailable"),
    ]
    return "".join(
        f'<div class="tile"><span class="tile-label">{label}</span>'
        f'<span class="tile-value">{value}</span></div>'
        for label, value in tiles
    )


def save_reports(
    report: pd.DataFrame,
    report_date: date,
    universe_size: int,
    ranking_source: str,
    dropped_partial: bool,
    foreign_available: bool,
) -> None:
    stamp = report_date.isoformat()
    report.to_csv(REPORTS_DIR / f"top10_report_{stamp}.csv", index=False)
    report.to_csv(REPORTS_DIR / "latest_report.csv", index=False)

    source_detail = f"Top-10 ranking: {html_escape(ranking_source)}."
    if ranking_source.startswith("yfinance"):
        source_detail += (
            f" Turnover is estimated from close × volume across {universe_size} configured "
            "stocks, not official IDX transaction value."
        )

    method_detail = (
        "Returns use adjusted Yahoo Finance closes. Indicators and trade levels use raw "
        "prices; foreign-flow rupiah values estimate IDX share flows at the day's VWAP. "
        "Activity and foreign-participation signals are shown separately. Pullbacks "
        "require a rising regime, use stops below support, and cap targets at prior "
        "resistance. Rows that fail the setup rules still show reference levels and "
        "are labelled Watch only. Trade scenarios use a days-to-weeks horizon."
    )

    limit_parts = [
        "Technical setups are reference scenarios, not recommendations",
        "Yahoo data may be delayed or revised",
    ]
    if not foreign_available:
        limit_parts.append("foreign flow is blank when IDX session data is unavailable")
    if dropped_partial:
        limit_parts.append("today's incomplete session was excluded")
    limits_detail = "; ".join(limit_parts)
    limits_detail = limits_detail[0].upper() + limits_detail[1:] + "."
    generated_at = datetime.now(WIB).strftime("%d %b %Y, %H:%M WIB")
    official_ranking = ranking_source.startswith("IDX Stock Summary")
    ranking_status = "Official IDX ranking" if official_ranking else "Estimated ranking"
    foreign_status = "Foreign flow available" if foreign_available else "Foreign flow unavailable"

    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>IDX Top 10 &mdash; {stamp}</title>
<style>
  :root {{
    --primary: {BRAND['primary']};
    --primary-dark: {BRAND['primary_dark']};
    --accent: {BRAND['accent']};
    --neg: {BRAND['negative']};
    --pos: {BRAND['positive']};
    --text: {BRAND['text']};
    --muted: {BRAND['muted']};
    --line: {BRAND['line']};
    --shade: {BRAND['shade']};
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; background: #fff; color: var(--text);
         font: 15px/1.5 -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }}
  header.page {{ background: var(--primary); color: #fff; padding: 22px 28px 18px; }}
  header.page h1 {{ margin: 0; font-size: 21px; font-weight: 600; letter-spacing: .2px; }}
  header.page .sub {{ margin-top: 4px; font-size: 13px; color: #c9d2f5; }}
  header.page .status-line {{ display: flex; flex-wrap: wrap; gap: 7px; margin-top: 10px; }}
  header.page .status {{ border: 1px solid rgba(255,255,255,.28); border-radius: 12px;
                         padding: 2px 8px; font-size: 11px; color: #eef2ff; }}
  header.page .rule {{ width: 54px; height: 4px; background: var(--accent);
                       border-radius: 2px; margin-top: 12px; }}
  main {{ padding: 20px 28px 48px; }}
  .tiles {{ display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 20px; }}
  .tile {{ flex: 1 1 190px; border: 1px solid var(--line); border-left: 3px solid var(--accent);
           border-radius: 4px; padding: 10px 14px; background: #fff; }}
  .tile-label {{ display: block; font-size: 11px; text-transform: uppercase;
                 letter-spacing: .6px; color: var(--muted); }}
  .tile-value {{ display: block; font-size: 18px; font-weight: 600; margin-top: 2px;
                 font-variant-numeric: tabular-nums; }}
  .wrap {{ overflow-x: auto; border: 1px solid var(--line); border-radius: 4px; }}
  table.report {{ border-collapse: separate; border-spacing: 0; width: 100%; font-size: 13px; }}
  table.report th, table.report td {{ padding: 7px 10px; white-space: nowrap; }}
  table.report thead th {{ background: var(--primary); color: #fff; font-weight: 600;
                           text-align: left; position: sticky; top: 0; }}
  table.report thead th button {{ appearance: none; border: 0; padding: 0; margin: 0;
                                  color: inherit; background: transparent; font: inherit;
                                  cursor: pointer; white-space: nowrap; }}
  table.report thead th button:focus-visible {{ outline: 2px solid #fff; outline-offset: 3px; }}
  .sort-mark {{ color: var(--accent); margin-left: 3px; }}
  table.report thead tr.groups th {{ background: var(--primary-dark); font-size: 11px;
        text-transform: uppercase; letter-spacing: .7px; text-align: left;
        border-right: 1px solid rgba(255,255,255,.25); position: static; }}
  table.report thead th.num {{ text-align: right; }}
  table.report tbody td {{ border-bottom: 1px solid var(--line);
                           font-variant-numeric: tabular-nums; }}
  table.report tbody tr:nth-child(even) td {{ background: var(--shade); }}
  table.report tbody tr:hover td {{ background: #eef1fb; }}
  table.report th.col-rank, table.report td.col-rank {{ position: sticky; left: 0;
                                                        min-width: 42px; width: 42px; }}
  table.report th.col-rank_change, table.report td.col-rank_change {{ position: sticky; left: 42px;
                                                                      min-width: 62px; width: 62px; }}
  table.report th.col-stock_code, table.report td.col-stock_code {{ position: sticky; left: 104px;
                                                                    min-width: 70px; width: 70px; }}
  table.report th.col-company, table.report td.col-company {{ position: sticky; left: 174px;
                                                              min-width: 190px; width: 190px;
                                                              border-right: 1px solid #c9cfda; }}
  table.report tbody td.col-rank, table.report tbody td.col-rank_change,
  table.report tbody td.col-stock_code, table.report tbody td.col-company {{ background: #fff; z-index: 2; }}
  table.report tbody tr:nth-child(even) td.col-rank,
  table.report tbody tr:nth-child(even) td.col-rank_change,
  table.report tbody tr:nth-child(even) td.col-stock_code,
  table.report tbody tr:nth-child(even) td.col-company {{ background: var(--shade); }}
  table.report tbody tr:hover td.col-rank, table.report tbody tr:hover td.col-rank_change,
  table.report tbody tr:hover td.col-stock_code, table.report tbody tr:hover td.col-company {{ background: #eef1fb; }}
  table.report thead tr:not(.groups) th.col-rank,
  table.report thead tr:not(.groups) th.col-rank_change,
  table.report thead tr:not(.groups) th.col-stock_code,
  table.report thead tr:not(.groups) th.col-company {{ z-index: 4; }}
  td.num {{ text-align: right; }}
  td.code {{ font-weight: 700; color: var(--primary); }}
  td.text {{ max-width: 210px; overflow: hidden; text-overflow: ellipsis; }}
  td.pos {{ color: var(--pos); }}
  td.neg {{ color: var(--neg); }}
  td.strong {{ font-weight: 700; }}
  td.empty {{ color: #b9bec7; text-align: center; }}
  .pill {{ display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 11.5px;
           white-space: nowrap; background: #eceef2; color: #4a5160; }}
  .pill.pos {{ background: #e3f3ec; color: var(--pos); }}
  .pill.neg {{ background: #fbe9ef; color: var(--neg); }}
  .rank-move {{ font-size: 11.5px; font-weight: 600; }}
  .rank-move.new {{ color: var(--primary); }}
  .rank-move.pos {{ color: var(--pos); }}
  .rank-move.neg {{ color: var(--neg); }}
  .rank-move.flat {{ color: var(--muted); }}
  .table-note {{ margin: -8px 0 8px; color: var(--muted); font-size: 11.5px; }}
  details.method {{ margin-top: 20px; border: 1px solid var(--line); border-radius: 4px;
                    padding: 10px 14px; background: var(--shade); font-size: 13px; }}
  details.method summary {{ cursor: pointer; font-weight: 600; color: var(--primary); }}
  details.method dl {{ display: grid; grid-template-columns: 82px 1fr; gap: 6px 12px;
                       margin: 10px 0 2px; color: #4a5160; }}
  details.method dt {{ font-weight: 600; color: var(--text); }}
  details.method dd {{ margin: 0; }}
  code {{ background: #e8ebf4; padding: 1px 4px; border-radius: 3px; font-size: 12px; }}
  @media (max-width: 720px) {{
    header.page, main {{ padding-left: 16px; padding-right: 16px; }}
  }}
</style>
</head>
<body>
<header class="page">
  <h1>IDX Top 10 by Transaction Value</h1>
  <div class="sub">Data through {stamp} &middot; refreshed {generated_at}</div>
  <div class="status-line"><span class="status">{ranking_status}</span><span class="status">{foreign_status}</span></div>
  <div class="rule"></div>
</header>
<main>
  <div class="tiles">{render_summary(report)}</div>
  <p class="table-note">Select a column heading to sort. Rank, move, code and company stay visible while scrolling.</p>
  <div class="wrap">{render_table(report)}</div>
  <details class="method">
    <summary>Method, sources and limits</summary>
    <dl>
      <dt>Sources</dt><dd>{source_detail} Prices and volume: Yahoo Finance; foreign flow: IDX Stock Summary.</dd>
      <dt>Method</dt><dd>{method_detail}</dd>
      <dt>Limits</dt><dd>{limits_detail}</dd>
    </dl>
  </details>
</main>
<script>
  document.querySelectorAll('table.report thead th[data-column]').forEach((heading) => {{
    heading.querySelector('button').addEventListener('click', () => {{
      const table = heading.closest('table');
      const headings = [...table.querySelectorAll('thead th[data-column]')];
      const columnIndex = headings.indexOf(heading);
      const ascending = heading.getAttribute('aria-sort') !== 'ascending';
      const rows = [...table.tBodies[0].rows];
      rows.sort((rowA, rowB) => {{
        const a = rowA.cells[columnIndex].dataset.sort || '';
        const b = rowB.cells[columnIndex].dataset.sort || '';
        if (!a && !b) return 0;
        if (!a) return 1;
        if (!b) return -1;
        const aNumber = Number(a);
        const bNumber = Number(b);
        let comparison;
        if (Number.isFinite(aNumber) && Number.isFinite(bNumber)) {{
          comparison = aNumber - bNumber;
        }} else {{
          comparison = a.localeCompare(b, undefined, {{ numeric: true, sensitivity: 'base' }});
        }}
        return ascending ? comparison : -comparison;
      }});
      rows.forEach((row) => table.tBodies[0].appendChild(row));
      headings.forEach((item) => {{
        item.setAttribute('aria-sort', 'none');
        item.querySelector('.sort-mark').textContent = '';
      }});
      heading.setAttribute('aria-sort', ascending ? 'ascending' : 'descending');
      heading.querySelector('.sort-mark').textContent = ascending ? '▲' : '▼';
    }});
  }});
</script>
</body>
</html>"""
    (REPORTS_DIR / "dashboard.html").write_text(html, encoding="utf-8")


def start_run(connection: sqlite3.Connection) -> int:
    cursor = connection.execute(
        "INSERT INTO refresh_runs (started_at, status) VALUES (?, 'RUNNING')",
        (datetime.now().isoformat(timespec="seconds"),),
    )
    connection.commit()
    return int(cursor.lastrowid)


def finish_run(
    connection: sqlite3.Connection,
    run_id: int,
    status: str,
    report_date: date | None,
    message: str,
) -> None:
    connection.execute(
        """
        UPDATE refresh_runs
        SET completed_at=?, report_date=?, status=?, message=?
        WHERE id=?
        """,
        (
            datetime.now().isoformat(timespec="seconds"),
            report_date.isoformat() if report_date else None,
            status,
            message,
            run_id,
        ),
    )
    connection.commit()


def main() -> None:
    ensure_directories()
    configure_logging()
    connection = connect_database()
    create_schema(connection)
    migrate_schema(connection)
    run_id = start_run(connection)
    report_date: date | None = None

    try:
        codes, names = load_universe()
        download = download_universe([f"{code}.JK" for code in codes])
        frames = universe_snapshot(download, codes)
        available = {
            pd.Timestamp(timestamp).date()
            for frame in frames.values()
            for timestamp in frame.index
        }
        report_date, dropped_partial = resolve_report_date(available)

        coverage = idx_coverage(connection, report_date)
        if coverage >= MIN_IDX_ROWS:
            top_rows = rank_from_idx(connection, report_date)
            ranking_source = f"IDX Stock Summary ({coverage} stocks on {report_date})"
            logging.info("Ranking from the official IDX file: %s stocks", coverage)
        else:
            if coverage:
                logging.warning(
                    "IDX file for %s covers only %s stocks (<%s); using the yfinance estimate",
                    report_date, coverage, MIN_IDX_ROWS,
                )
            top_rows = rank_from_yfinance(frames, report_date)
            ranking_source = "yfinance estimate"

        if not top_rows:
            raise RuntimeError(f"No stock passed the ranking filters on {report_date}")
        if len(top_rows) < TOP_COUNT:
            logging.warning(
                "Only %s stocks passed the filters on %s", len(top_rows), report_date
            )

        archive_ranking(report_date, top_rows, len(codes), ranking_source)
        save_top_stocks(connection, report_date, top_rows)

        failures = []
        for row in top_rows:
            code = row["code"]
            logging.info("Refreshing %s", code)
            try:
                count = update_ohlcv(connection, code, report_date)
                logging.info("Saved %s OHLCV rows for %s", count, code)
            except Exception as exc:
                failures.append(f"{code} OHLCV: {exc}")
                logging.exception("OHLCV failed for %s", code)

        report = build_report(connection, report_date, top_rows, names)
        foreign_available = bool(
            "foreign_net_1d_idr" in report and report["foreign_net_1d_idr"].notna().any()
        )
        save_reports(
            report, report_date, len(codes), ranking_source, dropped_partial, foreign_available
        )

        status = "PARTIAL" if failures else "SUCCESS"
        message = " | ".join(failures) if failures else f"Ranking: {ranking_source}"
        finish_run(connection, run_id, status, report_date, message)
        logging.info("Pipeline completed with status %s (%s)", status, ranking_source)
        if not foreign_available:
            logging.warning(
                "Foreign flow empty - add IDX Stock Summary files to downloads/ and run idx_summary.py"
            )
    except Exception as exc:
        finish_run(connection, run_id, "FAILED", report_date, str(exc))
        logging.exception("Pipeline failed")
        raise
    finally:
        try:
            connection.commit()
            checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if checkpoint and checkpoint[0] != 0:
                raise RuntimeError(f"SQLite WAL checkpoint remained busy: {checkpoint}")
        finally:
            connection.close()


if __name__ == "__main__":
    main()
