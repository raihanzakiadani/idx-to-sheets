"""
Kepemilikan pemegang saham >1% (bulanan) dan >5% (harian) dari file EXCEL (.xlsx)
di https://www.idx.co.id/id/perusahaan-tercatat/data-kepemilikan-saham/  (bukan PDF).

Alur:
 1. Ambil HTML halaman -> ekstrak daftar file (judul + URL .xlsx) dari state halaman.
 2. Untuk setiap file yang belum tercatat di tab `OwnershipLog`: download, parse, tulis.
 3. Backfill otomatis: semua file yang tampil di halaman diproses (urut tanggal),
    run berikutnya hanya memproses file baru.

Tab output:
  Holders1pct       - semua baris file >1% (snapshot bulanan, di-append, tanpa duplikat)
  Holders5pct       - LOG PERUBAHAN >5% (BASELINE di file pertama, lalu NEW/EXIT/CHANGE)
  Holders5pct_Latest- snapshot penuh terbaru >5% (di-overwrite tiap ada file baru)
  OwnershipLog      - file mana saja yang sudah diproses

Kenapa >5% disimpan sebagai perubahan, bukan snapshot penuh per hari?
  Satu file >5% berisi ~1.900 investor. Snapshot harian penuh = ~30rb sel/hari
  -> ~7 juta sel/tahun, padahal batas 1 Google Sheet = 10 juta sel (semua tab).
  Perubahan bersifat lossless (baseline + selisih) dan hanya puluhan baris/hari.
  Set HOLDERS5_STORE_FULL=true kalau tetap mau snapshot penuh tiap hari.

Env opsional:
  OWNERSHIP_EXTRA_URLS  - URL .xlsx tambahan (pisah koma/spasi/baris baru), untuk
                          backfill file yang tidak tampil di halaman (mis. 30 Jan 2026)
  OWNERSHIP_START_DATE  - YYYY-MM-DD, abaikan file sebelum tanggal ini (default 2026-01-30)
  HOLDERS5_STORE_FULL   - "true" -> Holders5pct berisi snapshot penuh tiap hari
"""

import datetime
import io
import os
import re

import numpy as np
import pandas as pd

PAGE_URL = "https://www.idx.co.id/id/perusahaan-tercatat/data-kepemilikan-saham/"

MONTHS = {
    "januari": 1, "jan": 1, "january": 1, "februari": 2, "feb": 2, "february": 2,
    "maret": 3, "mar": 3, "march": 3, "april": 4, "apr": 4,
    "mei": 5, "may": 5, "juni": 6, "jun": 6, "june": 6, "juli": 7, "jul": 7, "july": 7,
    "agustus": 8, "agu": 8, "agt": 8, "aug": 8, "august": 8,
    "september": 9, "sep": 9, "sept": 9, "oktober": 10, "okt": 10, "oct": 10, "october": 10,
    "november": 11, "nov": 11, "desember": 12, "des": 12, "dec": 12, "december": 12,
}


# ------------------------------------------------------------------ util
def parse_id_date(text):
    """'31 Agustus 2026' / '4 Aug 2026' / '04-AUG-2026' -> 'YYYY-MM-DD' (atau None)."""
    m = re.search(r"(\d{1,2})[\s\-/]+([A-Za-z]+)[\s\-/]+(\d{4})", str(text))
    if not m:
        return None
    mon = MONTHS.get(m.group(2).lower())
    if not mon:
        return None
    try:
        return datetime.date(int(m.group(3)), mon, int(m.group(1))).isoformat()
    except ValueError:
        return None


def to_num(v):
    """'3,200,142,830' -> 3200142830.0 ; '-' / '' / None -> NaN."""
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return np.nan
    if isinstance(v, (int, float, np.integer, np.floating)):
        return float(v)
    s = str(v).strip().replace(",", "").replace(" ", "")
    if s in ("", "-", "--", "N/A", "n/a"):
        return np.nan
    try:
        return float(s)
    except ValueError:
        return np.nan


def _code(v):
    """Kode saham. Ticker 'TRUE'/'FALSE' disimpan Excel sebagai boolean -> kembalikan ke teks."""
    if isinstance(v, (bool, np.bool_)):
        return "TRUE" if v else "FALSE"
    return str(v).strip().upper() if v is not None and str(v) != "nan" else None


def _clean_str(v):
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return None
    s = str(v).strip()
    return s or None


# ------------------------------------------------------------------ daftar file
def list_ownership_files(html):
    """Ekstrak file kepemilikan dari HTML halaman IDX.
    Daftar lengkap tertanam di state halaman (Description + Prospectus=URL xlsx),
    jadi tidak perlu klik pagination. Fallback: semua <a href> ke *.xlsx."""
    items = {}

    def add(title, url):
        url = (url.replace("\\u002F", "/").replace("\\/", "/").replace("&amp;", "&").strip())
        if not url.lower().endswith(".xlsx"):
            return
        if url.startswith("/"):
            url = "https://www.idx.co.id" + url
        low = (title + " " + url).lower()
        if "lima-persen" in low or "atas 5%" in low:
            kind = "5"
        elif "satu-persen" in low or "atas 1%" in low:
            kind = "1"
        else:
            return  # klasifikasi / tipe investor: bukan scope
        date = parse_id_date(title)
        if not date:
            m = re.search(r"peng-(\d{4})-(\d{2})-(\d{2})-", url)
            if m:
                date = "-".join(m.groups())
        if not date:
            return
        items[url] = {"kind": kind, "date": date, "url": url, "title": title.strip()}

    pat_state = re.compile(
        r"""["']?Description["']?\s*:\s*"((?:[^"\\]|\\.)*)"\s*,\s*["']?Prospectus["']?\s*:\s*"((?:[^"\\]|\\.)*)" """.strip()
    )
    for m in pat_state.finditer(html):
        add(m.group(1), m.group(2))

    if not items:  # fallback: anchor biasa
        for m in re.finditer(r"""href=["']([^"']+\.xlsx)["']""", html, flags=re.I):
            add("", m.group(1))

    return sorted(items.values(), key=lambda x: (x["date"], x["kind"]))


# ------------------------------------------------------------------ parser >5%
def _read_rows(data):
    return pd.read_excel(io.BytesIO(data), header=None, engine="openpyxl", dtype=object)


def parse_5pct(data, fallback_date=None):
    """Excel >5% (per SID) -> DataFrame level-INVESTOR (satu baris per pemegang saham).

    Struktur file: header 2 baris (No, Kode Efek, ... | Kepemilikan Per <prev> |
    Kepemilikan Per <cur> | Perubahan). Baris ber-'No' = investor (kolom 'Saham
    Gabungan' & '%' terisi), baris di bawahnya tanpa 'No' = sub-rekening investor itu.
    Angka bertipe TEKS dengan pemisah ribuan; '-' berarti tidak ada
    (investor baru di kolom prev / keluar di kolom cur).
    """
    raw = _read_rows(data)
    hdr = None
    for i in range(min(15, len(raw))):
        c1 = str(raw.iat[i, 1]).strip().lower() if raw.shape[1] > 1 else ""
        if c1 == "kode efek":
            hdr = i
            break
    if hdr is None or raw.shape[1] < 18:
        raise ValueError("format >5% tidak dikenali (header 'Kode Efek' / 18 kolom tidak ketemu)")

    title = str(raw.iat[0, 0])
    cur_date = parse_id_date(raw.iat[hdr, 14]) or parse_id_date(title) or fallback_date
    prev_date = parse_id_date(raw.iat[hdr, 11])

    body = raw.iloc[hdr + 2:, :18].copy()
    body.columns = ["No", "Kode", "Emiten", "PemegangRek", "PemegangSaham", "NamaRek", "Alamat",
                    "Alamat2", "Kebangsaan", "Domisili", "Status", "PrevJml", "PrevGab",
                    "PrevPct", "CurJml", "CurGab", "CurPct", "Perubahan"]
    body["Kode"] = body["Kode"].map(_code)
    body = body[body["Kode"].fillna("").str.fullmatch(r"[A-Z0-9]{3,6}")].reset_index(drop=True)

    is_investor = body["No"].notna()
    body["grp"] = is_investor.cumsum()
    body = body[body["grp"] > 0]

    rows = []
    for _, g in body.groupby("grp", sort=True):
        head = g.iloc[0]
        prev_gab, cur_gab = to_num(head["PrevGab"]), to_num(head["CurGab"])
        # jika 'saham gabungan' kosong, jumlahkan sub-rekening
        if np.isnan(prev_gab):
            s = pd.to_numeric(g["PrevJml"].map(to_num), errors="coerce").sum(min_count=1)
            prev_gab = s
        if np.isnan(cur_gab):
            s = pd.to_numeric(g["CurJml"].map(to_num), errors="coerce").sum(min_count=1)
            cur_gab = s
        custodians = sorted({_clean_str(x) for x in g["PemegangRek"] if _clean_str(x)})
        rows.append({
            "Date": cur_date,
            "PrevDate": prev_date,
            "Kode": head["Kode"],
            "Emiten": _clean_str(head["Emiten"]),
            "PemegangSaham": _clean_str(head["PemegangSaham"]),
            "Status": _clean_str(head["Status"]),
            "Kebangsaan": _clean_str(head["Kebangsaan"]),
            "Domisili": _clean_str(head["Domisili"]),
            "PrevShares": prev_gab,
            "CurShares": cur_gab,
            "PrevPct": to_num(head["PrevPct"]),
            "CurPct": to_num(head["CurPct"]),
            "PerubahanRaw": _clean_str(head["Perubahan"]),
            "JmlRekening": len(g),
            "Kustodian": "; ".join(custodians),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    # investor bernama sama tapi beda SID muncul sebagai baris terpisah -> nomor urut agar key unik
    df["Seq"] = df.groupby(["Date", "Kode", "PemegangSaham", "Status"], dropna=False).cumcount() + 1
    df["DeltaShares"] = df["CurShares"].fillna(0) - df["PrevShares"].fillna(0)
    df["DeltaPct"] = df["CurPct"].fillna(0) - df["PrevPct"].fillna(0)
    return df


def classify_5pct(df):
    """Tambah kolom RowType: NEW / EXIT / CHANGE / SAME."""
    if df.empty:
        return df.assign(RowType=pd.Series(dtype=str))
    df = df.copy()
    rt = np.where(df["PrevShares"].isna() | (df["PrevShares"] == 0), "NEW",
         np.where(df["CurShares"].isna() | (df["CurShares"] == 0), "EXIT",
         np.where((df["DeltaShares"] != 0) | (df["DeltaPct"].abs() > 1e-9), "CHANGE", "SAME")))
    df["RowType"] = rt
    return df


# ------------------------------------------------------------------ parser >1%
COLS_1PCT = ["DATE", "SHARE_CODE", "ISSUER_NAME", "INVESTOR_NAME", "INVESTOR_TYPE",
             "LOCAL_FOREIGN", "NATIONALITY", "DOMICILE", "HOLDINGS_SCRIPLESS",
             "HOLDINGS_SCRIP", "TOTAL_HOLDING_SHARES", "PERCENTAGE"]


def parse_1pct(data, source_name="", fallback_date=None):
    """Excel >1%: disclaimer panjang di atas, header di baris yang kolom-A nya 'DATE'."""
    raw = _read_rows(data)
    hdr = None
    for i in range(min(30, len(raw))):
        if str(raw.iat[i, 0]).strip().upper() == "DATE":
            hdr = i
            break
    if hdr is None:
        raise ValueError("format >1% tidak dikenali (header 'DATE' tidak ketemu)")
    names = [str(x).strip().upper() if x is not None and str(x) != "nan" else "" for x in raw.iloc[hdr]]
    body = raw.iloc[hdr + 1:].copy()
    body.columns = names
    body = body.loc[:, [c for c in COLS_1PCT if c in body.columns]]
    body["SHARE_CODE"] = body["SHARE_CODE"].map(_code)
    body = body[body["SHARE_CODE"].notna()].copy()

    def norm_date(v):
        if isinstance(v, (datetime.datetime, pd.Timestamp)):
            return v.strftime("%Y-%m-%d")
        return parse_id_date(v) or (str(v)[:10] if v is not None else fallback_date)

    body["DATE"] = body["DATE"].map(norm_date)
    for c in ("HOLDINGS_SCRIPLESS", "HOLDINGS_SCRIP", "TOTAL_HOLDING_SHARES", "PERCENTAGE"):
        if c in body:
            body[c] = body[c].map(to_num)
    for c in body.columns:
        if c not in ("DATE", "HOLDINGS_SCRIPLESS", "HOLDINGS_SCRIP", "TOTAL_HOLDING_SHARES", "PERCENTAGE"):
            body[c] = body[c].map(_clean_str)
    body["SourceFile"] = source_name
    return body.reset_index(drop=True)


# ------------------------------------------------------------------ orchestrator
def _log_df(rows):
    return pd.DataFrame(rows, columns=["Url", "Kind", "FileDate", "Rows", "IngestedAt", "Note"])


def run_ownership(spreadsheet, fetch_text, fetch_bytes):
    """fetch_text(url)->str, fetch_bytes(url)->bytes disediakan scraper.py
    (memakai session yang sudah lolos Cloudflare)."""
    from sheets_util import (get_or_create_worksheet, overwrite_snapshot, read_header,
                             read_sheet_df, upsert_timeseries)

    start_date = os.environ.get("OWNERSHIP_START_DATE", "2026-01-30")
    store_full = os.environ.get("HOLDERS5_STORE_FULL", "false").lower() == "true"

    html = fetch_text(PAGE_URL)
    files = list_ownership_files(html)
    for u in re.split(r"[,\s]+", os.environ.get("OWNERSHIP_EXTRA_URLS", "").strip()):
        if u:
            m = re.search(r"peng-(\d{4})-(\d{2})-(\d{2})-", u)
            kind = "5" if "lima-persen" in u else "1" if "satu-persen" in u else None
            if kind and m:
                files.append({"kind": kind, "date": "-".join(m.groups()), "url": u, "title": "(extra)"})
            else:
                print(f"  -> OWNERSHIP_EXTRA_URLS: {u} dilewati (nama file tidak dikenali; "
                      "beri format peng-YYYY-MM-DD-...-lima-persen.xlsx)")
    files = [f for f in {f["url"]: f for f in files}.values() if f["date"] >= start_date]
    files.sort(key=lambda x: (x["date"], x["kind"]))
    print(f"[Ownership] {len(files)} file ditemukan "
          f"(>5%: {sum(f['kind']=='5' for f in files)}, >1%: {sum(f['kind']=='1' for f in files)})")
    if not files:
        print("[Ownership] tidak ada file - cek apakah halaman IDX terblokir Cloudflare")
        return

    log_ws, existed = get_or_create_worksheet(spreadsheet, "OwnershipLog", 6)
    done = set(read_sheet_df(log_ws)["Url"]) if existed and read_header(log_ws) else set()
    pending = [f for f in files if f["url"] not in done]
    print(f"[Ownership] {len(pending)} file baru untuk diproses")
    if not pending:
        return

    # apakah baseline >5% sudah ada?
    h5_ws, h5_exists = get_or_create_worksheet(spreadsheet, "Holders5pct", 20)
    has_baseline = h5_exists and bool(read_header(h5_ws))

    log_rows, latest_5, latest_5_date = [], None, ""
    blocked_5 = False  # file >5% harus diproses berurutan (baseline -> selisih)
    for f in pending:
        if f["kind"] == "5" and blocked_5:
            continue
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        try:
            data = fetch_bytes(f["url"])
            fname = f["url"].rsplit("/", 1)[-1]
            if f["kind"] == "1":
                df = parse_1pct(data, fname, f["date"])
                upsert_timeseries(spreadsheet, "Holders1pct", df,
                                  ["DATE", "SHARE_CODE", "INVESTOR_NAME", "LOCAL_FOREIGN", "INVESTOR_TYPE"])
                log_rows.append([f["url"], "1%", f["date"], len(df), now, "ok"])
            else:
                snap = classify_5pct(parse_5pct(data, f["date"]))
                if snap.empty:
                    raise ValueError("0 baris ter-parse")
                snap["SourceFile"] = fname
                if store_full:
                    out = snap
                elif not has_baseline:
                    out = snap.assign(RowType="BASELINE")   # snapshot awal, lalu selisih saja
                    has_baseline = True
                else:
                    out = snap[snap["RowType"] != "SAME"]
                upsert_timeseries(spreadsheet, "Holders5pct", out,
                                  ["Date", "Kode", "PemegangSaham", "Status", "Seq", "RowType"])
                if snap["Date"].iat[0] >= latest_5_date:
                    latest_5, latest_5_date = snap.drop(columns=["RowType"]), snap["Date"].iat[0]
                log_rows.append([f["url"], "5%", f["date"], len(snap), now, f"ok, {len(out)} baris ditulis"])
        except Exception as e:
            # tidak di-log sebagai selesai -> dicoba lagi di run berikutnya
            print(f"  -> gagal memproses {f['url']}: {e}")
            if f["kind"] == "5":
                blocked_5 = True
                print("  -> file >5% berikutnya ditunda supaya urutan kronologis terjaga")

    if latest_5 is not None:
        overwrite_snapshot(spreadsheet, "Holders5pct_Latest", latest_5)
    if log_rows:
        upsert_timeseries(spreadsheet, "OwnershipLog", _log_df(log_rows), ["Url"])
