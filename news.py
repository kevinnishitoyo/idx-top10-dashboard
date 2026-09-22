#!/usr/bin/env python3
"""Fetch recent news headlines for the latest yfinance-ranked top-10 stocks.

Run after pipeline.py. Reads the date and ordered stock list from the latest
CSV report, searches Google News RSS for each stock, stores headlines in the `news` table,
and writes:
  - reports/latest_news.csv       (Excel)
  - reports/news_YYYY-MM-DD.csv   (dated snapshot)
  - a "Recent news" section inside reports/dashboard.html

A news failure never affects the price/flow data. Headlines are leads to read,
not verified information.
"""

from __future__ import annotations

import argparse
import csv
import html
import logging
import re
import sqlite3
import sys
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests

PROJECT_DIR = Path(__file__).resolve().parent
DB_PATH = PROJECT_DIR / "data" / "market_data.db"
REPORTS_DIR = PROJECT_DIR / "reports"
LOGS_DIR = PROJECT_DIR / "logs"
COMPANIES_CSV = PROJECT_DIR / "companies.csv"
LATEST_REPORT_CSV = REPORTS_DIR / "latest_report.csv"

LOOKBACK_DAYS = 7
HEADLINES_PER_STOCK = 5

# Corporate-action words. A headline matching one of these is flagged, so the
# report separates corporate developments from ordinary price commentary.
ACTION_KEYWORDS = {
    "dividend": ("dividen", "dividend"),
    "rights issue": ("rights issue", "hmetd", "right issue"),
    "private placement": ("private placement", "pmthmetd"),
    "buyback": ("buyback", "buy back", "pembelian kembali"),
    "stock split": ("stock split", "stocksplit", "pemecahan saham", "reverse split"),
    "M&A": ("akuisisi", "acquisition", "merger", "divestasi", "divestment", "caplok"),
    "earnings": ("laba", "rugi", "kinerja keuangan", "earnings", "pendapatan"),
    "capex/expansion": ("ekspansi", "capex", "pabrik baru", "smelter"),
    "debt/funding": ("obligasi", "bond", "sukuk", "pinjaman", "refinancing", "utang"),
    "suspension/UMA": ("suspensi", "suspend", "uma", "unusual market activity", "gocap"),
    "management": ("direksi", "komisaris", "rups", "ceo baru", "resign"),
    "guidance/target": ("target harga", "proyeksi", "guidance", "outlook"),
}
RSS_URL = "https://news.google.com/rss/search?q={query}&hl=id&gl=ID&ceid=ID:id"
IDX_PROFILE_URL = "https://www.idx.co.id/id/perusahaan-tercatat/profil-perusahaan-tercatat/{code}"
IDX_DISCLOSURE_URL = "https://www.idx.co.id/id/perusahaan-tercatat/keterbukaan-informasi/"
STOCKBIT_URL = "https://stockbit.com/symbol/{code}"
NEWS_START = "<!--NEWS-START-->"
NEWS_END = "<!--NEWS-END-->"


def configure_logging() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOGS_DIR / "news.log"), logging.StreamHandler()],
    )


def load_companies() -> dict[str, dict[str, list[str] | str]]:
    """companies.csv: code,name,aliases (aliases separated by |)."""
    companies: dict[str, dict] = {}
    if not COMPANIES_CSV.exists():
        return companies
    with COMPANIES_CSV.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            code = (row.get("code") or "").strip().upper()
            if not code:
                continue
            aliases = [a.strip() for a in (row.get("aliases") or "").split("|") if a.strip()]
            companies[code] = {"name": (row.get("name") or "").strip(), "aliases": aliases}
    return companies


def create_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS news (
            stock_code TEXT NOT NULL,
            link TEXT NOT NULL,
            title TEXT NOT NULL,
            source TEXT,
            published_at TEXT,
            first_report_date TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (stock_code, link)
        )
        """
    )
    connection.commit()


def latest_top_stocks(connection: sqlite3.Connection, report_date: str | None) -> tuple[str, list[str]]:
    # With no explicit date, use the exact report that pipeline.py just wrote.
    # The database can already contain a newer, partial-session ranking, so
    # selecting MAX(trade_date) here can attach news for the wrong ten stocks.
    if report_date is None and LATEST_REPORT_CSV.exists():
        with LATEST_REPORT_CSV.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
        if rows:
            dates = {(row.get("date") or "").strip() for row in rows}
            codes = [(row.get("stock_code") or "").strip().upper() for row in rows]
            codes = [code for code in codes if code]
            dates.discard("")
            if len(dates) == 1 and codes:
                return dates.pop(), codes

    if report_date is None:
        row = connection.execute("SELECT MAX(trade_date) FROM top_stocks").fetchone()
        if not row or row[0] is None:
            raise RuntimeError("No top_stocks data found. Run pipeline.py first.")
        report_date = row[0]
    codes = [
        r[0]
        for r in connection.execute(
            "SELECT stock_code FROM top_stocks WHERE trade_date=? ORDER BY rank",
            (report_date,),
        )
    ]
    if not codes:
        raise RuntimeError(f"No top_stocks rows for {report_date}.")
    return report_date, codes


def fetch_rss(session: requests.Session, query: str) -> list[dict]:
    url = RSS_URL.format(query=urllib.parse.quote(f"{query} when:{LOOKBACK_DAYS}d"))
    response = session.get(url, timeout=30)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        source = (item.findtext("source") or "").strip()
        # Google News appends " - Source" to titles; drop it.
        if source and title.endswith(f" - {source}"):
            title = title[: -len(f" - {source}")].strip()
        published = None
        if item.findtext("pubDate"):
            try:
                published = parsedate_to_datetime(item.findtext("pubDate")).astimezone(timezone.utc)
            except (TypeError, ValueError):
                published = None
        items.append(
            {"title": title, "link": (item.findtext("link") or "").strip(),
             "source": source, "published": published}
        )
    return items


def is_relevant(title: str, code: str, company: dict | None) -> bool:
    """Keep a headline only if it mentions the code or the company name/alias."""
    if re.search(rf"\b{re.escape(code)}\b", title):
        return True
    if company:
        lowered = title.lower()
        for term in [company.get("name", ""), *company.get("aliases", [])]:
            if term and re.search(rf"\b{re.escape(term.lower())}\b", lowered):
                return True
    return False


def news_for_stock(session: requests.Session, code: str, company: dict | None) -> list[dict]:
    queries = [f'"{code}" saham']
    if company and company.get("name"):
        queries.insert(0, f'"{company["name"]}"')

    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    seen_titles: set[str] = set()
    results = []
    for query in queries:
        for item in fetch_rss(session, query):
            key = re.sub(r"\W+", " ", item["title"].lower()).strip()
            if not item["link"] or key in seen_titles:
                continue
            if item["published"] and item["published"] < cutoff:
                continue
            if not is_relevant(item["title"], code, company):
                continue
            seen_titles.add(key)
            results.append(item)
    results.sort(key=lambda i: i["published"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return results


def save_news(connection: sqlite3.Connection, code: str, report_date: str, items: list[dict]) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    connection.executemany(
        """
        INSERT INTO news (stock_code, link, title, source, published_at, first_report_date, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(stock_code, link) DO UPDATE SET
            title=excluded.title, source=excluded.source,
            published_at=excluded.published_at, fetched_at=excluded.fetched_at
        """,
        [
            (code, i["link"], i["title"], i["source"],
             i["published"].isoformat() if i["published"] else None, report_date, now)
            for i in items
        ],
    )
    connection.commit()


def action_flags(title: str) -> str:
    """Corporate-action labels found in a headline, comma separated."""
    lowered = title.lower()
    found = [
        label
        for label, terms in ACTION_KEYWORDS.items()
        if any(term in lowered for term in terms)
    ]
    return ", ".join(found)


def local_time(iso_value: str | None) -> str:
    if not iso_value:
        return ""
    wib = timezone(timedelta(hours=7))
    return datetime.fromisoformat(iso_value).astimezone(wib).strftime("%d %b %H:%M WIB")


def write_outputs(
    connection: sqlite3.Connection, report_date: str, codes: list[str],
    companies: dict, status: dict[str, str],
) -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    rows_by_code = {}
    for code in codes:
        candidate_rows = connection.execute(
            """
            SELECT title, source, published_at, link FROM news
            WHERE stock_code=? AND (published_at IS NULL OR published_at>=?)
            ORDER BY published_at DESC LIMIT ?
            """,
            (code, cutoff, HEADLINES_PER_STOCK * 4),
        ).fetchall()
        seen_titles: set[str] = set()
        unique_rows = []
        for row in candidate_rows:
            normalized = re.sub(r"\W+", " ", row[0].lower()).strip()
            if normalized in seen_titles:
                continue
            seen_titles.add(normalized)
            unique_rows.append(row)
            if len(unique_rows) == HEADLINES_PER_STOCK:
                break
        rows_by_code[code] = unique_rows

    # CSV for Excel
    header = ["report_date", "stock_code", "company", "fetch_status", "published_wib", "source", "flags", "title", "link"]
    for path in (REPORTS_DIR / "latest_news.csv", REPORTS_DIR / f"news_{report_date}.csv"):
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(header)
            for code in codes:
                name = companies.get(code, {}).get("name", "")
                if not rows_by_code[code]:
                    writer.writerow([report_date, code, name, status[code], "", "", "", "", ""])
                for title, source, published, link in rows_by_code[code]:
                    writer.writerow([report_date, code, name, status[code], local_time(published), source, action_flags(title), title, link])

    # HTML section injected into the dashboard
    blocks = []
    for code in codes:
        name = companies.get(code, {}).get("name", "")
        links = (
            f'<a href="{IDX_PROFILE_URL.format(code=code)}">IDX profile</a> · '
            f'<a href="{IDX_DISCLOSURE_URL}">IDX disclosures</a> · '
            f'<a href="{STOCKBIT_URL.format(code=code)}">Stockbit</a>'
        )
        if status[code] != "ok":
            body = f'<p class="warn">Headlines not fetched ({html.escape(status[code])}). Check manually.</p>'
        elif not rows_by_code[code]:
            body = f"<p class=\"muted\">No matching headlines in the last {LOOKBACK_DAYS} days.</p>"
        else:
            items = []
            for t, s, p, l in rows_by_code[code]:
                flags = action_flags(t)
                badge = f'<span class="flag">{html.escape(flags)}</span> ' if flags else ""
                items.append(
                    f'<li><span class="muted">{html.escape(local_time(p))} · '
                    f'{html.escape(s or "")}</span> {badge}'
                    f'<a href="{html.escape(l)}">{html.escape(t)}</a></li>'
                )
            body = "<ul>" + "".join(items) + "</ul>"
        title = f"{code}" + (f" — {html.escape(name)}" if name else "")
        blocks.append(f'<div class="news-card"><h3>{title}</h3><p class="links">{links}</p>{body}</div>')

    refreshed_wib = local_time(datetime.now(timezone.utc).isoformat())
    section = f"""{NEWS_START}
<style>
  .news {{ margin-top: 28px; }}
  .news h2 {{ font-size: 17px; color: #1428A0; margin: 0 0 2px; }}
  .news .intro {{ font-size: 12.5px; color: #69727D; margin: 0 0 12px; }}
  .news-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(330px, 1fr));
                gap: 12px; }}
  .news-card {{ border: 1px solid #E4E7EC; border-top: 3px solid #00BDFF; border-radius: 4px;
                padding: 10px 14px; background: #fff; }}
  .news-card h3 {{ font-size: 14px; margin: 0 0 2px; color: #1428A0; }}
  .news-card ul {{ margin: 8px 0 0; padding-left: 16px; font-size: 12.5px; }}
  .news-card li {{ margin: 5px 0; }}
  .news-card a {{ color: #333; text-decoration: none; border-bottom: 1px solid #cfd5e4; }}
  .news-card a:hover {{ color: #1428A0; }}
  .news .muted {{ color: #69727D; font-size: 11.5px; }}
  .news .warn {{ color: #CC3366; font-size: 12.5px; }}
  .news .links {{ font-size: 11.5px; margin: 2px 0; }}
  .news .links a {{ color: #1428A0; border: 0; }}
  .news .flag {{ display: inline-block; background: #e8f0ff; color: #1428A0;
                 border: 1px solid #c3d6f5; border-radius: 10px; padding: 0 6px;
                 font-size: 10.5px; margin-right: 2px; }}
</style>
<section class="news">
  <h2>Recent news &middot; last {LOOKBACK_DAYS} days</h2>
  <p class="intro">Google News headlines matched to each stock. Corporate-action labels are
  keyword flags, not verified facts; confirm them in IDX disclosures. Updated {refreshed_wib}.</p>
  <div class="news-grid">{''.join(blocks)}</div>
</section>
{NEWS_END}"""

    dashboard = REPORTS_DIR / "dashboard.html"
    if dashboard.exists():
        page = dashboard.read_text(encoding="utf-8")
        page = re.sub(re.escape(NEWS_START) + ".*?" + re.escape(NEWS_END), "", page, flags=re.S)
        page = page.replace("</body>", section + "\n</body>", 1)
    else:
        page = f"<!doctype html><html><head><meta charset='utf-8'><title>News {report_date}</title></head><body style='font-family:Arial,sans-serif;margin:32px'>{section}</body></html>"
    dashboard.write_text(page, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="Report date YYYY-MM-DD (default: latest in database)")
    args = parser.parse_args()

    configure_logging()
    if not DB_PATH.exists():
        logging.error("Database not found at %s. Run pipeline.py first.", DB_PATH)
        return 1

    connection = sqlite3.connect(DB_PATH)
    try:
        create_schema(connection)
        report_date, codes = latest_top_stocks(connection, args.date)
        companies = load_companies()
        session = requests.Session()
        session.headers["User-Agent"] = "Mozilla/5.0 (samuel-stock-pipeline news)"

        status: dict[str, str] = {}
        for code in codes:
            company = companies.get(code)
            if company is None:
                logging.warning("%s not in companies.csv; searching by code only (noisier)", code)
            try:
                items = news_for_stock(session, code, company)
                save_news(connection, code, report_date, items)
                status[code] = "ok"
                logging.info("%s: %d matching headlines", code, len(items))
            except Exception as exc:
                status[code] = f"failed: {type(exc).__name__}"
                logging.exception("News fetch failed for %s", code)

        write_outputs(connection, report_date, codes, companies, status)
        failed = [c for c, s in status.items() if s != "ok"]
        logging.info("News done for %s. Failed: %s", report_date, ", ".join(failed) or "none")
        return 0
    finally:
        connection.close()


if __name__ == "__main__":
    sys.exit(main())
