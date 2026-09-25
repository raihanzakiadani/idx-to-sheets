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
    SCRAPE_DATE           - YYYYMMDD, defaults to the last finished trading
                            day (yesterday, or Friday if today is Monday)
    FUND_QUARTER          - fundamentals quarter (1-4), default 4
    FUND_YEAR             - fundamentals fiscal year, default (current year - 1)
    PROXY_URL             - e.g. http://user:pass@host:port - route all
                            requests through this proxy.
    FLARESOLVERR_URL      - e.g. http://localhost:8191/v1 - a running
                            FlareSolverr instance used to solve IDX's
                            Cloudflare JS challenge once per run. Defaults
                            to http://localhost:8191/v1, assuming
                            FlareSolverr is running as a local Docker
                            container on the same machine as this script.
                            Set to an empty string to disable.
    FETCH_COMPANY_DETAILS - "true" to also pull per-company detail (board,
                            shareholders, subsidiaries) for every listed
                            company. This is ~962 individual requests, so
                            it's off by default - run it on its own
                            less-frequent schedule (e.g. weekly) rather
                            than every daily run.
    KSEI_MONTHS_BACK      - how many of the most recent months of KSEI
                            ownership data to (re-)fetch each run, default
                            3. Already-ingested months are skipped by the
                            de-dupe logic, so this just controls how far
                            back a run reaches - bump it way up (e.g. 60)
                            for a one-off historical backfill.

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
import re
import io
import zipfile
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


# All 13 corporate-action categories IDX's GetIssuedHistory recognizes.
# The endpoint is filtered per category (no "all" option), so a full
# corporate-actions pull means one request per category.
CORPORATE_ACTION_TYPES = [
    "BuybackSaham",
    "PrivatePlacement",
    "stockSplit",
    "reverseStock",
    "hmetd",
    "tanpaHmetd",
    "dividenSaham",
    "sahamBonus",
    "ipo",
    "waran",
    "gabungUsaha",
    "kurangModal",
    "konversiSaham",
]


def get_corporate_actions(date_from=None, date_to=None):
    """Corporate actions (buybacks, splits, rights issues, IPOs, etc.) across
    all categories. date_from/date_to are YYYY-MM-DD strings; leave both
    None to pull each category's full history (can be slow - the endpoint
    doesn't support an unfiltered "everything" query)."""
    frames = []
    for ca_type in CORPORATE_ACTION_TYPES:
        params = {"caType": ca_type, "start": 0, "length": 9999}
        if date_from:
            params["dateFrom"] = date_from
        if date_to:
            params["dateTo"] = date_to
        try:
            data = fetch("/ListingActivity/GetIssuedHistory", params)
            rows = data.get("data", [])
            if rows:
                frames.append(pd.DataFrame(rows))
        except Exception as e:
            print(f"  -> corporate actions fetch failed for caType={ca_type}: {e}")
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def get_company_profiles():
    """Full listed-company directory: ticker, sector, listing board, etc."""
    data = fetch("/ListedCompany/GetCompanyProfiles", {"start": 0, "length": 9999})
    return pd.DataFrame(data.get("data", []))


def flatten_nested_columns(df):
    """GetCompanyProfilesDetail returns nested lists/dicts per company
    (board members, shareholders, subsidiaries...). Sheets cells can only
    hold scalars, so JSON-encode any column that isn't already scalar."""
    df = df.copy()
    for col in df.columns:
        if df[col].apply(lambda v: isinstance(v, (list, dict))).any():
            df[col] = df[col].apply(
                lambda v: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v
            )
    return df


def get_company_profiles_detail(kode_emiten_list):
    """Per-company detail (board, shareholders, subsidiaries...). One
    request per ticker - with ~962 listed companies this is slow and is
    meant to be run occasionally (e.g. weekly), not on every daily run.
    See FETCH_COMPANY_DETAILS in main()."""
    rows = []
    total = len(kode_emiten_list)
    for i, code in enumerate(kode_emiten_list, 1):
        try:
            data = fetch(
                "/ListedCompany/GetCompanyProfilesDetail",
                {"KodeEmiten": code, "language": "id-id"},
            )
            row = data.get("data") if isinstance(data.get("data"), dict) else data
            if row:
                rows.append(row)
        except Exception as e:
            print(f"  -> company detail fetch failed for {code}: {e}")
        if i % 50 == 0 or i == total:
            print(f"  -> company details: {i}/{total} fetched")
    if not rows:
        return pd.DataFrame()
    return flatten_nested_columns(pd.DataFrame(rows))


def get_broker_directory():
    """Exchange member / broker directory - maps broker codes (AK, YP, ZP...)
    to firm names and license types. Reference data, changes rarely."""
    data = fetch(
        "/ExchangeMember/GetBrokerSearch",
        {"option": 0, "license": "", "start": 0, "length": 9999},
    )
    return pd.DataFrame(data.get("data", []))


def get_announcements(date_from, date_to, lang="id", page_size=100):
    """Company disclosures & PDF filings for a date range, paginated."""
    frames = []
    page = 1
    while True:
        data = fetch(
            "/NewsAnnouncement/GetAllAnnouncement",
            {
                "pageNumber": page,
                "pageSize": page_size,
                "lang": lang,
                "dateFrom": date_from,
                "dateTo": date_to,
            },
        )
        items = data.get("Items", [])
        if not items:
            break
        frames.append(pd.DataFrame(items))
        page_count = data.get("PageCount", page)
        if page >= page_count:
            break
        page += 1
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def get_news(page_size=100):
    """Latest market news headlines - just the first page (this is meant
    to reflect "recent news", not a full historical archive)."""
    data = fetch(
        "/NewsAnnouncement/GetNewsSearch",
        {"pageNumber": 1, "pageSize": page_size, "locale": "id-id"},
    )
    items = data.get("Items") or data.get("data") or []
    return pd.DataFrame(items)


# --- KSEI monthly securities ownership (Local/Foreign x Insurance, Mutual
# Fund, Pension Fund, Bank, Corporate, Individual, etc.) ------------------
# This is a *different site* (ksei.co.id, not idx.co.id) and, as far as we
# could confirm, isn't behind the same Cloudflare JS challenge - so it uses
# its own plain curl_cffi session rather than the FlareSolverr-solved one.
# We don't guess the monthly file's date (it's the last *trading* day of
# the month, which shifts around and isn't simple weekday math) - instead
# we scrape the actual download links off KSEI's own listing page.

KSEI_LISTING_URL = "https://www.ksei.co.id/en/publication/data-and-statistics/securities-ownership"
KSEI_ZIP_PATTERN = re.compile(r"BalanceposEfek(\d{8})\.zip")

_ksei_session = None


def get_ksei_session():
    global _ksei_session
    if _ksei_session is None:
        _ksei_session = requests.Session(impersonate=IMPERSONATE)
    return _ksei_session


def get_ksei_download_links():
    """Scrape (period_date, zip_url) pairs off KSEI's securities-ownership
    listing page. Only returns what's linked on the (first) page - if KSEI
    paginates further back than that, older months won't show up here."""
    session = get_ksei_session()
    resp = session.get(KSEI_LISTING_URL, headers=PAGE_HEADERS, timeout=30)
    resp.raise_for_status()
    links = []
    for match in KSEI_ZIP_PATTERN.finditer(resp.text):
        date_str = match.group(1)
        url = f"https://www.ksei.co.id/storage/Download/BalanceposEfek{date_str}.zip"
        links.append((date_str, url))
    # de-dupe while keeping order (the pattern can appear more than once per link)
    seen = set()
    unique_links = []
    for date_str, url in links:
        if date_str not in seen:
            seen.add(date_str)
            unique_links.append((date_str, url))
    return unique_links


def get_ksei_ownership_for_period(date_str, url):
    """Download+parse one month's ownership ZIP.

    Confirmed file format (pipe-delimited, verified from a real sample):
      Date|Code|Type|Sec. Num|Price|
      Local IS|Local CP|Local PF|Local IB|Local ID|Local MF|Local SC|Local FD|Local OT|Total|
      Foreign IS|Foreign CP|Foreign PF|Foreign IB|Foreign ID|Foreign MF|Foreign SC|Foreign FD|Foreign OT|Total
    Sector codes: IS=Insurance, CP=Corporate, PF=Pension Fund, IB=Financial
    Institution (bank), ID=Individual, MF=Mutual Fund, SC=Securities
    Company, FD=Foundation, OT=Other. The two "Total" columns (Local
    total, Foreign total) share the same header name, so they're renamed
    below to keep them distinguishable in the sheet.
    """
    session = get_ksei_session()
    resp = session.get(url, headers=PAGE_HEADERS, timeout=60)
    resp.raise_for_status()

    period_date = f"{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}"
    frames = []
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        for name in zf.namelist():
            if not name.lower().endswith((".csv", ".txt")):
                continue
            with zf.open(name) as f:
                raw = f.read()
            try:
                df = pd.read_csv(io.BytesIO(raw), sep="|")
            except Exception as e:
                print(f"  -> couldn't parse {name} in {url} as pipe-delimited: {e}")
                # fall back to sniffing, in case KSEI changes format later
                try:
                    df = pd.read_csv(io.BytesIO(raw), sep=None, engine="python")
                except Exception as e2:
                    print(f"  -> sniff fallback also failed: {e2}")
                    continue

            # pandas auto-suffixes the second duplicate "Total" column as
            # "Total.1" - rename both to something meaningful.
            cols = list(df.columns)
            for i, col in enumerate(cols):
                if col == "Total":
                    cols[i] = "Local Total"
                elif col == "Total.1":
                    cols[i] = "Foreign Total"
            df.columns = cols

            df["PeriodDate"] = period_date
            df["SourceFile"] = name
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def get_ksei_ownership(months_back=3):
    """Pulls the most recent `months_back` months of ownership data that
    are listed on KSEI's page. Safe to call every run - already-ingested
    months get skipped downstream by upsert_timeseries's dedupe, so this
    is how new months accumulate into history over time. Set
    KSEI_MONTHS_BACK higher (e.g. 60) for a one-off deep backfill of
    however much history KSEI's first listing page exposes."""
    try:
        links = get_ksei_download_links()
    except Exception as e:
        print(f"  -> couldn't load KSEI listing page: {e}")
        return pd.DataFrame()

    frames = []
    for date_str, url in links[:months_back]:
        try:
            df = get_ksei_ownership_for_period(date_str, url)
            if not df.empty:
                frames.append(df)
                print(f"  -> KSEI ownership {date_str}: {len(df)} row(s)")
        except Exception as e:
            print(f"  -> KSEI ownership fetch failed for {date_str}: {e}")
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def connect_sheet():
    creds_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    creds_dict = json.loads(creds_json)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    sheet_id = os.environ["SPREADSHEET_ID"]
    return gc.open_by_key(sheet_id)


def clean_for_sheets(df):
    # gspread/Sheets can't take NaN/NaT, or raw list/dict cells - flatten
    # and blank those out.
    # astype(object) first is required: on a numeric-dtype column, pandas
    # silently converts None back to NaN when you .where() into it, since
    # a float64 column can't hold a real None. Casting to object dtype
    # first makes the None actually stick, which is what avoids the
    # downstream "Out of range float values are not JSON compliant: nan"
    # error when gspread JSON-encodes the values for the Sheets API.
    df = flatten_nested_columns(df)
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

    try:
        ca_df = get_corporate_actions(date_from=None, date_to=None)
        upsert_timeseries(spreadsheet, "CorporateActions", ca_df, ["id"])
    except Exception as e:
        print(f"CorporateActions fetch failed: {e}", file=sys.stderr)

    try:
        company_df = get_company_profiles()
        overwrite_snapshot(spreadsheet, "CompanyProfiles", company_df)
    except Exception as e:
        print(f"CompanyProfiles fetch failed: {e}", file=sys.stderr)
        company_df = pd.DataFrame()

    try:
        broker_dir_df = get_broker_directory()
        overwrite_snapshot(spreadsheet, "BrokerDirectory", broker_dir_df)
    except Exception as e:
        print(f"BrokerDirectory fetch failed: {e}", file=sys.stderr)

    date_iso = f"{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}"
    try:
        announce_df = get_announcements(date_from=date_iso, date_to=date_iso)
        upsert_timeseries(spreadsheet, "Announcements", announce_df, ["Id"])
    except Exception as e:
        print(f"Announcements fetch failed: {e}", file=sys.stderr)

    try:
        news_df = get_news()
        overwrite_snapshot(spreadsheet, "News", news_df)
    except Exception as e:
        print(f"News fetch failed: {e}", file=sys.stderr)

    try:
        months_back = int(os.environ.get("KSEI_MONTHS_BACK", "3"))
        ksei_df = get_ksei_ownership(months_back=months_back)
        if not ksei_df.empty:
            # Column names come straight from KSEI's own CSV (see
            # get_ksei_ownership_for_period) - PeriodDate is ours, plus
            # whatever KSEI's first 2 columns are (typically an
            # identifier + type) form the de-dupe key.
            key_cols = ["PeriodDate"] + list(ksei_df.columns[:2])
            upsert_timeseries(spreadsheet, "KSEIOwnership", ksei_df, key_cols)
        else:
            print("[KSEIOwnership] nothing fetched, skipping")
    except Exception as e:
        print(f"KSEIOwnership fetch failed: {e}", file=sys.stderr)

    # Heavy: one request per listed company (~962 of them). Off by default -
    # set FETCH_COMPANY_DETAILS=true (e.g. on a separate weekly workflow)
    # to run this.
    if os.environ.get("FETCH_COMPANY_DETAILS", "false").lower() == "true":
        try:
            if company_df.empty:
                company_df = get_company_profiles()
            codes = company_df.get("KodeEmiten", pd.Series(dtype=str)).dropna().tolist()
            detail_df = get_company_profiles_detail(codes)
            overwrite_snapshot(spreadsheet, "CompanyDetails", detail_df)
        except Exception as e:
            print(f"CompanyDetails fetch failed: {e}", file=sys.stderr)

    print("Done.")


if __name__ == "__main__":
    main()
