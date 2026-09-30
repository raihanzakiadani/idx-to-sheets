"""
Intraday OHLCV + ~180 technical indicators, sumber: GOOGLE FINANCE (fungsi GOOGLEFINANCE).

PENTING - batasan Google Finance yang membentuk desain ini:
  * GOOGLEFINANCE hanya bisa memberi data historis DAILY/WEEKLY. Tidak ada histori
    intraday (menit/jam) yang bisa diminta mundur. Yang tersedia hanya kuotasi
    "sekarang" (tertunda s.d. ~20 menit): price, priceopen, high, low, volume, tradetime.
  * Karena itu bar OHLC intraday DIBANGUN SENDIRI dari snapshot berkala
    (tiap run workflow). Histori intraday terkumpul mulai run pertama; tidak bisa
    di-backfill ke belakang dari Google Finance.
  * Python tidak bisa memanggil GOOGLEFINANCE langsung, jadi script menulis rumus
    GOOGLEFINANCE ke tab helper `_GF_Live`, menunggu Google Sheets menghitung, lalu
    membaca hasilnya.

Tab output:
  Watchlist            - daftar ticker (kolom A). Edit bebas; dibuat otomatis jika belum ada.
  Intraday_Ticks       - snapshot mentah (append; key Ticker+TradeTime)
  Intraday_Bars        - bar OHLCV N-menit hasil rakitan dari ticks (di-overwrite tiap run)
  Intraday_Indicators  - 1 baris per ticker: bar terakhir + ~180 indikator (di-overwrite)
  _GF_Live             - tab kerja untuk rumus GOOGLEFINANCE (jangan diedit)

Env opsional:
  INTRADAY_TICKERS       - "BBCA,BBRI,TLKM" (menimpa tab Watchlist)
  INTRADAY_INTERVAL_MIN  - lebar bar dalam menit (default 5). Tidak bisa lebih rapat
                           dari jarak antar run workflow.
  INTRADAY_KEEP_DAYS     - berapa hari ticks/bars disimpan (default 30)
  INTRADAY_MIN_BARS      - minimum bar per ticker untuk dihitung indikatornya (default 20)
  INTRADAY_FORCE         - "true" = jalan walau di luar jam bursa
  INTRADAY_WAIT_SEC      - maks tunggu Sheets menghitung rumus (default 90)

Ukuran: batas Google Sheet 10 juta sel per spreadsheet (semua tab). Ticks 8 kolom;
50 ticker x ~85 tick/hari x 30 hari ~ 1 juta sel. Menaikkan jumlah ticker ke ratusan
atau KEEP_DAYS ke ratusan akan cepat memenuhi batas itu.
"""

import datetime
import os
import re
import time
from zoneinfo import ZoneInfo

import pandas as pd

from indicators import compute_indicators
from sheets_util import (get_or_create_worksheet, overwrite_snapshot,
                         read_header, read_sheet_df, upsert_timeseries)

WIB = ZoneInfo("Asia/Jakarta")
GF_SHEET = "_GF_Live"
DEFAULT_TICKERS = ["BBCA", "BBRI", "BMRI", "BBNI", "TLKM", "ASII", "UNVR", "ICBP", "INDF", "ADRO",
                   "ANTM", "GOTO", "BRPT", "MDKA", "AMRT", "CPIN", "KLBF", "PGAS", "PTBA", "SMGR"]
TICK_COLS = ["Ticker", "TradeTime", "Price", "DayOpen", "DayHigh", "DayLow", "CumVolume", "FetchedAt"]


# ------------------------------------------------------------------ util
def market_is_open(now=None):
    """Jendela longgar sesi IDX (Senin-Jumat 08:55-16:20 WIB). Libur bursa tidak
    dicek - tick duplikat (TradeTime sama) otomatis dibuang."""
    now = now or datetime.datetime.now(WIB)
    if now.weekday() >= 5:
        return False
    return datetime.time(8, 55) <= now.time() <= datetime.time(16, 20)


def normalize_ticker(t):
    t = str(t).strip().upper()
    t = re.sub(r"^IDX:", "", t)
    t = re.sub(r"\.JK$", "", t)
    return t if re.fullmatch(r"[A-Z0-9]{3,6}", t) else None


def load_tickers(spreadsheet):
    env = os.environ.get("INTRADAY_TICKERS", "").strip()
    if env:
        raw = re.split(r"[,\s]+", env)
    else:
        ws, existed = get_or_create_worksheet(spreadsheet, "Watchlist", 1, nrows=200)
        vals = [r[0] for r in ws.get_all_values() if r and r[0].strip()] if existed else []
        if len(vals) <= 1:  # kosong / hanya header
            ws.clear()
            ws.update([["Ticker"]] + [[t] for t in DEFAULT_TICKERS], range_name="A1", value_input_option="RAW")
            print(f"[Watchlist] tab dibuat dengan {len(DEFAULT_TICKERS)} ticker contoh - edit sesuai kebutuhan")
            raw = DEFAULT_TICKERS
        else:
            raw = vals[1:] if vals[0].strip().lower() == "ticker" else vals
    out, seen = [], set()
    for t in raw:
        n = normalize_ticker(t)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


# ------------------------------------------------------------------ snapshot GOOGLEFINANCE
def snapshot_google_finance(spreadsheet, tickers, wait_sec=90):
    """Tulis rumus GOOGLEFINANCE, tunggu terhitung, kembalikan DataFrame ticks."""
    try:  # supaya 'tradetime' terbaca dalam WIB
        spreadsheet.batch_update({"requests": [{"updateSpreadsheetProperties": {
            "properties": {"timeZone": "Asia/Jakarta"}, "fields": "timeZone"}}]})
    except Exception as e:
        print(f"  -> gagal set timezone spreadsheet (lanjut): {e}")

    n = len(tickers)
    ws, _ = get_or_create_worksheet(spreadsheet, GF_SHEET, 8, nrows=max(n + 5, 50))
    if ws.row_count < n + 2:
        ws.resize(rows=n + 5)
    nonce = int(time.time())  # teks rumus selalu baru -> Sheets pasti menghitung ulang
    attrs = ["price", "priceopen", "high", "low", "volume"]
    rows = [["Ticker", "Price", "DayOpen", "DayHigh", "DayLow", "CumVolume", "TradeTime"]]
    for i, t in enumerate(tickers, start=2):
        r = [t]
        for a in attrs:
            r.append(f'=IFERROR(GOOGLEFINANCE("IDX:"&$A{i},"{a}")+0*N("{nonce}"),"")')
        r.append(f'=IFERROR(TEXT(GOOGLEFINANCE("IDX:"&$A{i},"tradetime")+0*N("{nonce}"),"yyyy-mm-dd hh:mm:ss"),"")')
        rows.append(r)
    ws.clear()
    ws.update(rows, range_name="A1", value_input_option="USER_ENTERED")

    deadline = time.time() + wait_sec
    vals = []
    while True:
        time.sleep(4)
        vals = ws.get_all_values()
        loading = sum(1 for row in vals[1:] for c in row if c.strip().lower().startswith("loading"))
        if loading == 0 or time.time() > deadline:
            break
    if loading:
        print(f"  -> {loading} sel masih 'Loading...' setelah {wait_sec}s; baris itu dilewati")

    fetched = datetime.datetime.now(WIB).strftime("%Y-%m-%d %H:%M:%S")
    recs = []
    for row in vals[1:]:
        row = row + [""] * (7 - len(row))
        t, price, o, h, l, vol, tt = row[:7]
        if not t or price == "" or tt == "" or price.startswith("#"):
            continue
        recs.append([t, tt, price, o, h, l, vol, fetched])
    df = pd.DataFrame(recs, columns=TICK_COLS)
    for c in ("Price", "DayOpen", "DayHigh", "DayLow", "CumVolume"):
        df[c] = pd.to_numeric(df[c].astype(str).str.replace(",", ""), errors="coerce")
    print(f"[Intraday] snapshot: {len(df)}/{n} ticker punya data")
    missing = sorted(set(tickers) - set(df["Ticker"]))
    if missing:
        print(f"  -> tanpa data (kode salah / tidak di-cover Google Finance / pasar tutup): {missing[:30]}")
    return df


# ------------------------------------------------------------------ ticks -> bars
def build_bars(ticks, interval_min=5):
    """Rakit bar OHLCV dari ticks (kolom TICK_COLS, angka sudah numerik).

    Open  = tick pertama di bucket (bar pertama hari itu: DayOpen)
    Close = tick terakhir
    High/Low = max/min dari harga tick, plus DayHigh/DayLow bila rekor harian baru
               tercipta di antara dua snapshot (artinya terjadi di bucket itu)
    Volume = selisih CumVolume antar snapshot
    Bucket tanpa tick tidak dibuatkan bar (tidak mengarang data)."""
    if ticks.empty:
        return pd.DataFrame(columns=["Ticker", "BarTime", "Open", "High", "Low", "Close", "Volume", "Ticks"])
    t = ticks.copy()
    t["TradeTime"] = pd.to_datetime(t["TradeTime"], errors="coerce")
    t = t.dropna(subset=["TradeTime", "Price"]).sort_values(["Ticker", "TradeTime"])
    t["Day"] = t["TradeTime"].dt.date
    t["Bucket"] = t["TradeTime"].dt.floor(f"{interval_min}min")

    g = t.groupby(["Ticker", "Day"], sort=False)
    prev_high, prev_low = g["DayHigh"].shift(1), g["DayLow"].shift(1)
    first = g.cumcount() == 0
    new_high = t["DayHigh"].where(first | (t["DayHigh"] > prev_high))
    new_low = t["DayLow"].where(first | (t["DayLow"] < prev_low))
    t["HighCand"] = pd.concat([t["Price"], new_high], axis=1).max(axis=1)
    t["LowCand"] = pd.concat([t["Price"], new_low], axis=1).min(axis=1)
    vol_delta = g["CumVolume"].diff()
    t["VolDelta"] = vol_delta.where(vol_delta >= 0).fillna(t["CumVolume"].where(first)).fillna(0)
    t["OpenCand"] = t["Price"].where(~first, t["DayOpen"].fillna(t["Price"]))

    bars = (t.groupby(["Ticker", "Bucket"], sort=True)
              .agg(Open=("OpenCand", "first"), High=("HighCand", "max"), Low=("LowCand", "min"),
                   Close=("Price", "last"), Volume=("VolDelta", "sum"), Ticks=("Price", "size"))
              .reset_index().rename(columns={"Bucket": "BarTime"}))
    bars["High"] = bars[["High", "Open", "Close"]].max(axis=1)
    bars["Low"] = bars[["Low", "Open", "Close"]].min(axis=1)
    return bars


def latest_indicators(bars, min_bars=20):
    """Per ticker: hitung ~180 indikator dari seluruh bar, ambil baris terakhir."""
    out = []
    for tk, g in bars.groupby("Ticker", sort=True):
        if len(g) < min_bars:
            print(f"  -> {tk}: baru {len(g)} bar (< {min_bars}), indikator dilewati")
            continue
        g = g.sort_values("BarTime").set_index("BarTime")
        ind = compute_indicators(g[["Open", "High", "Low", "Close", "Volume"]])
        last = pd.concat([g.iloc[[-1]][["Open", "High", "Low", "Close", "Volume"]], ind.iloc[[-1]]], axis=1)
        last.insert(0, "Bars", len(g))
        last = last.reset_index()
        last.insert(0, "Ticker", tk)
        out.append(last)
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame()


# ------------------------------------------------------------------ orchestrator
def run_intraday(spreadsheet):
    force = os.environ.get("INTRADAY_FORCE", "false").lower() == "true"
    if not force and not market_is_open():
        print("[Intraday] di luar jam bursa (Sen-Jum 08:55-16:20 WIB), dilewati. "
              "Set INTRADAY_FORCE=true untuk memaksa.")
        return
    interval = int(os.environ.get("INTRADAY_INTERVAL_MIN", "5"))
    keep_days = int(os.environ.get("INTRADAY_KEEP_DAYS", "30"))
    min_bars = int(os.environ.get("INTRADAY_MIN_BARS", "20"))
    wait_sec = int(os.environ.get("INTRADAY_WAIT_SEC", "90"))

    tickers = load_tickers(spreadsheet)
    if not tickers:
        print("[Intraday] watchlist kosong")
        return
    print(f"[Intraday] {len(tickers)} ticker, bar {interval} menit")

    snap = snapshot_google_finance(spreadsheet, tickers, wait_sec)
    if not snap.empty:
        upsert_timeseries(spreadsheet, "Intraday_Ticks", snap, ["Ticker", "TradeTime"])

    ws, existed = get_or_create_worksheet(spreadsheet, "Intraday_Ticks", len(TICK_COLS))
    if not existed or not read_header(ws):
        print("[Intraday] belum ada ticks tersimpan")
        return
    ticks = read_sheet_df(ws)
    for c in ("Price", "DayOpen", "DayHigh", "DayLow", "CumVolume"):
        ticks[c] = pd.to_numeric(ticks[c], errors="coerce")

    cutoff = (datetime.datetime.now(WIB) - datetime.timedelta(days=keep_days)).strftime("%Y-%m-%d")
    fresh = ticks[ticks["TradeTime"] >= cutoff]
    if len(fresh) < len(ticks):
        print(f"[Intraday_Ticks] pruning {len(ticks) - len(fresh)} tick lebih tua dari {keep_days} hari")
        overwrite_snapshot(spreadsheet, "Intraday_Ticks", fresh)
        ticks = fresh

    bars = build_bars(ticks, interval)
    if bars.empty:
        print("[Intraday] belum ada bar")
        return
    overwrite_snapshot(spreadsheet, "Intraday_Bars", bars)

    ind = latest_indicators(bars, min_bars)
    if not ind.empty:
        overwrite_snapshot(spreadsheet, "Intraday_Indicators", ind)
        print(f"[Intraday_Indicators] {len(ind)} ticker x {ind.shape[1] - 8} indikator")
