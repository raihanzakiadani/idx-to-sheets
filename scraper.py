"""
IDX -> Google Sheets scraper.

Pulls from IDX's public (but bot-protected) JSON endpoints using curl_cffi
(browser TLS/JA3 impersonation, needed to get past Cloudflare) and writes
the results into tabs of a single Google Sheet via a service account.

Meant to be run on a schedule by GitHub Actions (see
.github/workflows/idx_scraper.yml) - no data is stored in the repo itself,
only the code and the schedule. All data lands in Google Sheets.

Required environment variables (set as GitHub Actions secrets):
    GOOGLE_SERVICE_ACCOUNT_JSON - full contents of the service account JSON key
    SPREADSHEET_ID              - the target Google Sheet's ID (from its URL)

Optional:
    SCRAPE_DATE      - YYYYMMDD, defaults to today (useful for backfilling/testing)
    FUND_QUARTER     - fundamentals quarter (1-4), default 4
    FUND_YEAR        - fundamentals fiscal year, default (current year - 1)
    PROXY_URL        - e.g. http://user:pass@host:port - route all requests
                       through this proxy.
    FLARESOLVERR_URL - e.g. http://localhost:8191/v1 - a running FlareSolverr
                       instance used to solve IDX's Cloudflare JS challenge
                       once per run. Defaults to http://localhost:8191/v1,
                       assuming FlareSolverr is running as a local Docker
                       container on the same machine as this script (true
                       for the self-hosted-runner setup this repo uses).
                       Set to an empty string to disable and fall back to
                       the plain curl_cffi warm-up (won't work if IDX is
                       showing a JS challenge rather than a flat IP block).

NOTE on 403 errors:
    - A 403 with a generic/plain body usually means IDX's WAF is blocking
      the request's IP or fingerprint outright.
    - A 403 whose body contains "Just a moment..." is Cloudflare's actual
      JS challenge page - it requires running real JavaScript in a real
      browser to solve, which no amount of header/TLS-fingerprint tuning
      can fake. That's what FLARESOLVERR_URL is for: it points at a local
      FlareSolverr instance (a headless-browser-backed solver) that solves
      the challenge once and hands back the resulting cookies, which this
      script then reuses for the actual API calls.
"""

import json
import os
import sys
import datetime

import pandas as pd
import gspread
from curl_cffi import requests
from google.oauth2.service_account import Credentials

BASE_URL = "https://www.idx.co.id/primary"

PAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Dest": "document",
}

API_HEADERS = {
    "User-Agent": PAGE_HEADERS["User-Agent"],
    "Referer": "https://www.idx.co.id/id/data-pasar/ringkasan-perdagangan/ringkasan-saham",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "X-Requested-With": "XMLHttpRequest",
    "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
}

# Bump this if curl_cffi releases newer profiles later (chrome131 is the
# latest that ships in curl_cffi 0.7.x/0.8.x at time of writing).
IMPERSONATE = "chrome131"

PROXY_URL = os.environ.get("PROXY_URL")  # e.g. http://user:pass@host:port
PROXIES = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None

FLARESOLVERR_URL = os.environ.get("FLARESOLVERR_URL", "http://localhost:8191/v1")
WARMUP_URL = "https://www.idx.co.id/id/data-pasar/ringkasan-perdagangan/ringkasan-saham"

_session = None


def solve_with_flaresolverr(url):
    """Ask a local FlareSolverr instance to load `url` in a real headless
    browser and solve Cloudflare's JS challenge if one is shown. Returns
    (cookies_dict, user_agent) so the caller can replay them on a normal
    HTTP client instead of paying the browser-launch cost on every request."""
    payload = {"cmd": "request.get", "url": url, "maxTimeout": 60000}
    resp = requests.post(
        FLARESOLVERR_URL,
        json=payload,
        timeout=70,
    )
    data = resp.json()
    if data.get("status") != "ok":
        raise RuntimeError(f"FlareSolverr did not solve the challenge: {data}")

    solution = data["solution"]
    cookies = {c["name"]: c["value"] for c in solution.get("cookies", [])}
    user_agent = solution.get("userAgent") or PAGE_HEADERS["User-Agent"]
    print(
        f"FlareSolverr solved {url} -> status {solution.get('status')}, "
        f"{len(cookies)} cookie(s) captured"
    )
    return cookies, user_agent


def get_session():
    """Reuse one session so cookies picked up from the homepage carry over
    into the API calls - many WAFs require this "warm-up" before they'll
    accept API requests."""
    global _session
    if _session is None:
        _session = requests.Session(impersonate=IMPERSONATE, proxies=PROXIES)

        if FLARESOLVERR_URL:
            try:
                cookies, user_agent = solve_with_flaresolverr(WARMUP_URL)
                for name, value in cookies.items():
                    _session.cookies.set(name, value, domain=".idx.co.id")
                # Keep the UA consistent with whatever browser FlareSolverr
                # actually used to solve the challenge - a mismatched UA vs.
                # the cookie's originating fingerprint is itself a bot signal.
                PAGE_HEADERS["User-Agent"] = user_agent
                API_HEADERS["User-Agent"] = user_agent
                print("Warm-up via FlareSolverr: cookies applied to session")
                return _session
            except Exception as e:
                print(
                    f"FlareSolverr warm-up failed ({e}); falling back to plain "
                    "curl_cffi warm-up, which likely won't pass a JS challenge."
                )

        try:
            warmup = _session.get(WARMUP_URL, headers=PAGE_HEADERS, timeout=30)
            print(f"Warm-up request status: {warmup.status_code}")
            if warmup.status_code == 403:
                print(
                    "  -> 403 on the plain warm-up page (not the API) usually means "
                    "either the IP is blocked, or IDX is showing a Cloudflare JS "
                    "challenge that only a real browser (e.g. via FlareSolverr) "
                    "can solve."
                )
        except Exception as e:
            print(f"Warm-up request failed (continuing anyway): {e}")
    return _session


def fetch(path, params):
    session = get_session()
    url = f"{BASE_URL}{path}"
    resp = session.get(
        url,
        params=params,
        headers=API_HEADERS,
        impersonate=IMPERSONATE,
        proxies=PROXIES,
        timeout=30,
    )
    if resp.status_code != 200:
        print(f"  -> {url} returned {resp.status_code}")
        print(f"  -> body preview: {resp.text[:300]!r}")
    resp.raise_for_status()
    return resp.json()


def default_scrape_date():
    """Most recent day that's definitely a finished trading day: yesterday,
    or the Friday before if today is a Monday (so weekends are skipped).
    Doesn't know about Indonesian public holidays, so on a run right after
    a holiday this may still land on a non-trading day and come back
    empty - if that happens often enough to matter, this is the place to
    plug in an actual IDX trading-calendar check."""
    d = datetime.date.today() - datetime.timedelta(days=1)
    while d.weekday() >= 5:  # Saturday=5, Sunday=6
        d -= datetime.timedelta(days=1)
    return d.strftime("%Y%m%d")


def today_str():
    return datetime.date.today().strftime("%Y%m%d")


def get_stock_summary(date_str):
    data = fetch(
        "/TradingSummary/GetStockSummary",
        {"date": date_str, "start": 0, "length": 9999},
    )
    return pd.DataFrame(data.get("data", []))


def get_index_summary(date_str):
    data = fetch(
        "/TradingSummary/GetIndexSummary",
        {"date": date_str, "start": 0, "length": 9999},
    )
    return pd.DataFrame(data.get("data", []))


def get_broker_summary(date_str):
    data = fetch(
        "/TradingSummary/GetBrokerSummary",
        {"date": date_str, "start": 0, "length": 9999},
    )
    return pd.DataFrame(data.get("data", []))


def get_fundamentals(quarter, year):
    data = fetch(
        "/DigitalStatistic/GetApiDataPaginated",
        {
            "urlName": "LINK_FINANCIAL_DATA_RATIO",
            "periodQuarter": quarter,
            "periodYear": year,
            "type": "yearly",
            "pageSize": 2000,
            "pageNumber": 1,
        },
    )
    items = data.get("data") or data.get("Items") or []
    return pd.DataFrame(items)


def connect_sheet():
    creds_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    creds_dict = json.loads(creds_json)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    sheet_id = os.environ["SPREADSHEET_ID"]
    return gc.open_by_key(sheet_id)


def clean_for_sheets(df):
    # gspread/Sheets can't take NaN/NaT - blank them out.
    # astype(object) first is required: on a numeric-dtype column, pandas
    # silently converts None back to NaN when you .where() into it, since
    # a float64 column can't hold a real None. Casting to object dtype
    # first makes the None actually stick, which is what avoids the
    # downstream "Out of range float values are not JSON compliant: nan"
    # error when gspread JSON-encodes the values for the Sheets API.
    return df.astype(object).where(pd.notnull(df), None)


def get_or_create_worksheet(spreadsheet, title, ncols):
    try:
        return spreadsheet.worksheet(title), True
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=title, rows=100, cols=max(ncols, 10))
        return ws, False


def upsert_timeseries(spreadsheet, title, df, key_cols):
    """Append only rows whose key_cols combination isn't already present."""
    if df.empty:
        print(f"[{title}] nothing fetched, skipping")
        return

    df = clean_for_sheets(df)
    ws, existed = get_or_create_worksheet(spreadsheet, title, len(df.columns))

    if existed:
        existing_records = ws.get_all_records()
        existing_df = pd.DataFrame(existing_records)
    else:
        existing_df = pd.DataFrame()

    if not existing_df.empty and all(k in existing_df.columns for k in key_cols):
        existing_keys = set(
            existing_df[key_cols].astype(str).agg("|".join, axis=1)
        )
        new_keys = df[key_cols].astype(str).agg("|".join, axis=1)
        df = df[~new_keys.isin(existing_keys)]

    if df.empty:
        print(f"[{title}] no new rows (already up to date)")
        return

    if existing_df.empty:
        ws.update([df.columns.tolist()] + df.values.tolist())
    else:
        ws.append_rows(df.values.tolist(), value_input_option="RAW")

    print(f"[{title}] wrote {len(df)} new row(s)")


def overwrite_snapshot(spreadsheet, title, df):
    """Fully replace a tab's contents - for slow-changing data like fundamentals."""
    if df.empty:
        print(f"[{title}] nothing fetched, skipping")
        return

    df = clean_for_sheets(df)
    ws, _ = get_or_create_worksheet(spreadsheet, title, len(df.columns))
    ws.clear()
    ws.update([df.columns.tolist()] + df.values.tolist())
    print(f"[{title}] overwritten with {len(df)} row(s)")


def main():
    date_str = os.environ.get("SCRAPE_DATE", default_scrape_date())
    fund_quarter = os.environ.get("FUND_QUARTER", "4")
    fund_year = os.environ.get("FUND_YEAR", str(datetime.date.today().year - 1))

    spreadsheet = connect_sheet()

    print(f"Fetching IDX data for {date_str} ...")

    try:
        stock_df = get_stock_summary(date_str)
        upsert_timeseries(spreadsheet, "OHLCV", stock_df, ["Date", "StockCode"])
    except Exception as e:
        print(f"OHLCV fetch failed: {e}", file=sys.stderr)

    try:
        index_df = get_index_summary(date_str)
        upsert_timeseries(spreadsheet, "IndexSummary", index_df, ["Date", "IndexCode"])
    except Exception as e:
        print(f"IndexSummary fetch failed: {e}", file=sys.stderr)

    try:
        broker_df = get_broker_summary(date_str)
        upsert_timeseries(spreadsheet, "BrokerSummary", broker_df, ["Date", "IDFirm"])
    except Exception as e:
        print(f"BrokerSummary fetch failed: {e}", file=sys.stderr)

    try:
        fund_df = get_fundamentals(fund_quarter, fund_year)
        overwrite_snapshot(spreadsheet, "Fundamentals", fund_df)
    except Exception as e:
        print(f"Fundamentals fetch failed: {e}", file=sys.stderr)

    print("Done.")


if __name__ == "__main__":
    main()
