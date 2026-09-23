# IDX Top 10 Stock Pipeline (yfinance + IDX Stock Summary)

Prices and indicators come from yfinance. The top-10 ranking and the foreign
flow come from IDX's free daily **Stock Summary** file (Ringkasan Saham) when
you have supplied it; without it the ranking falls back to a yfinance estimate
and the foreign-flow columns stay empty. No Samuel credentials are needed.

## Daily routine

1. `run.sh` / `run.bat` first try `idx_download.py`, which asks IDX's JSON
   endpoint for the last 5 sessions and writes them into `downloads/` as CSV.
   Nothing to do by hand when that works.
   If IDX refuses (bot protection), the log says which dates were refused:
   download those from the **Stock Summary** page (Market Data -> Trading
   Summary -> Stock Summary) and save them into `downloads/`, keeping the date
   in the filename (`Stock Summary-20260922.xlsx` is fine as IDX names it).
   Files already present are never re-downloaded; `--force` overrides that.
   The more days you keep, the further the 3-day and 5-day foreign-flow sums
   reach back.
2. Run `./run.sh` (macOS) or `run.bat` (Windows). That runs
   `idx_download.py` -> `idx_summary.py` -> `pipeline.py` -> `news.py`.
   A failed download never stops the rest.
3. Open `reports/dashboard.html`.

Run after **17:00 WIB**. A bar dated today is ignored before then, because
Yahoo's current-day bar updates during trading; the report always covers the
last completed session.

Run `python idx_summary.py --show-headers` to see which columns and foreign-flow
units were detected. Explicit volume/share headers are stored as shares and
converted to estimated rupiah with VWAP; explicit value/IDR headers are stored
directly as rupiah. Generic `Foreign Buy` / `Foreign Sell` headers are accepted
as shares only while every populated row passes strict volume checks. If the
format changes or the units are ambiguous, import fails instead of publishing
silently incorrect foreign-flow figures.

## Which questions this answers

| Question | Answered by |
| --- | --- |
| Top 10 by transaction value | IDX Stock Summary official value; yfinance estimate (raw close x volume, 20-session value floor) as fallback |
| Price performance 1d / 3d / 5d | yfinance adjusted closes |
| Foreign flow 1d / 3d / 5d net | IDX Stock Summary foreign buy/sell, summed over real sessions; blank unless every session in the window is present |
| Largest buying/selling brokers | **Not available.** No free source publishes per-stock broker flow; it needs a broker terminal |
| Technical entry/exit levels | Raw-price MA20/MA50, RSI(14), ATR(14), prior-20-session support/resistance, trend-gated breakout scenario with risk and reward-to-risk |
| News and corporate developments | Google News RSS per stock, with corporate-action keywords flagged; rumors remain manual |

## Methodology notes

- **Returns** use adjusted closes (dividends and splits removed).
  **Levels and indicators** use raw prices, so entry/stop/target are prices you
  could actually trade.
- **Support and resistance** both use the 20 sessions *before* the report date,
  so the two are measured over the same window.
- **Trade scenarios follow the setup the chart presents**, and never a long in a
  downtrend:
  - *Breakout long* when price is at or within 3% of the prior-20-session high;
    the former resistance anchors the stop and a 2R extension sets the target.
  - Pullbacks require MA20 above MA50, a rising MA20, and price above MA50.
    A price within 0.5 ATR of MA20 is labelled either a test from above or a
    pending reclaim from below.
  - A support test must sit within 0.5 ATR above support. Pullback stops sit
    0.5 ATR below support and targets are capped at prior resistance.
  - When the setup rules fail, the row is marked **Watch only** and the reason
    is shown (downtrend, below support, unconfirmed regime, insufficient room
    to resistance, or mid-range with no nearby trigger). Reference
    entry/stop/target levels remain visible, but are not active signals.
  Each scenario carries distance to entry, risk per share, risk as a percent of
  entry, and reward-to-risk after valid-tick rounding. Scenarios below 1.5R are
  rejected. IDX auto-rejection limits cap how far price can travel in one
  session, so a far target is not a one-day target.
- **Liquidity floor:** the yfinance fallback ranking skips stocks whose
  20-session average value is under Rp 5bn (`MIN_AVG_VALUE_20D`).
- **Ranking universe:** the fallback ranks only the stocks in `companies.csv`,
  so it is "top 10 of that list", not of the whole exchange. The IDX file has
  no such limit. Add rows to `companies.csv` for names you follow; the news
  step uses the same file for company names.
- A one-time migration clears `stock_prices` the first time you run this
  version, because older rows stored adjusted closes in the `close` column.
  Prices refetch from Yahoo automatically.

## What it creates

- `data/market_data.db`: prices, rankings, IDX summary rows, news, run log.
- `downloads/`: IDX files you saved (inputs, kept).
- `raw/YYYY-MM-DD/top_stocks.json`: the ranking inputs and source for that day.
- `reports/latest_report.csv`, `reports/top10_report_YYYY-MM-DD.csv`.
- `reports/latest_news.csv`, `reports/news_YYYY-MM-DD.csv`.
- `reports/dashboard.html`: table, method/limits box, and the news section.
- `logs/pipeline.log`, `logs/idx_summary.log`, `logs/news.log`.

## macOS setup

```bash
git clone https://github.com/kevinnishitoyo/idx-top10-dashboard.git
cd idx-top10-dashboard
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
./run.sh
open reports/dashboard.html
```

You can also run `python pipeline.py` and `python news.py` separately while the
virtual environment is active.

## Windows setup

Copy this folder without a Mac `.venv` directory. In Command Prompt:

```cmd
git clone https://github.com/kevinnishitoyo/idx-top10-dashboard.git
cd idx-top10-dashboard
py -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
run.bat
```

## Daily behavior

1. Downloads recent daily bars for every code in `companies.csv`.
2. Uses the newest common market date and ranks stocks by close × volume.
3. Selects the top 10 and refreshes about 550 calendar days of price history so
   dividend and split adjustments can revise older adjusted closes.
4. Calculates 1/3/5-session returns, MA20, MA50, RSI(14), ATR(14), volume versus
   its 20-session average, and 20-session support/resistance.
5. Saves the SQLite history, CSV reports, dashboard, and raw ranking snapshot.
6. `news.py` retrieves recent Google News RSS headlines for the exact date and
   ordered stock list in `reports/latest_report.csv`.

SQLite upserts prevent duplicate rows. Yahoo data can be delayed, revised,
missing, or rate-limited, so verify important information against IDX or your
licensed market-data provider.

## News headlines

Run after `pipeline.py`:

```bash
python news.py
python news.py --date 2026-09-22
```

With no `--date`, the news step follows `reports/latest_report.csv`, ensuring
the news cards match the ten stocks shown in the dashboard table. Passing
`--date` explicitly selects that historical ranking from the database.

News comes from Google News RSS and does not use Samuel. Headlines are leads,
not verified facts; confirm corporate actions on IDX disclosures.

## Public daily website with GitHub Pages

The included `.github/workflows/daily-dashboard.yml` refreshes and publishes the
dashboard at 18:15 WIB every Monday-Friday. It can also be started manually from
the repository's Actions page.

The lighter `.github/workflows/refresh-news.yml` updates only the matching news
headlines every hour from 08:23 through 21:23 WIB on weekdays. It republishes
the same dashboard URL without recalculating the market table. A browser page
that is already open must be reloaded to display the newly published headlines.

1. Push this project to a public GitHub repository.
2. Open **Settings → Pages** in that repository.
3. Under **Build and deployment**, choose **GitHub Actions** as the source.
4. Open **Actions → Refresh and publish dashboard → Run workflow**.
5. When the run succeeds, open the URL shown in the deployment summary.

The workflow downloads available IDX data, imports it, rebuilds the market and
news reports, validates that the table and news contain the same ten stocks,
and publishes `reports/dashboard.html` as the site's `index.html`. Updated
database and report files are committed back to the repository so rank changes
and multi-session history persist between cloud runs.

The published URL is public. Do not add passwords, tokens, client information,
or other confidential material to this repository.
