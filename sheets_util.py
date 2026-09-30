"""
Helper Google Sheets (dipakai bersama oleh scraper.py, ownership.py, intraday.py).

Dipisah dari scraper.py supaya modul baru bisa memakainya tanpa circular import.
Semua penulisan memakai value_input_option="RAW" -> teks tetap teks (mis. tanggal
"2026-03-31" tidak diubah Sheets menjadi date serial), sehingga de-dupe key stabil.
"""

import json
import os

import gspread
import pandas as pd
from google.oauth2.service_account import Credentials

CHUNK_ROWS = 5000


def connect_sheet():
    creds_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    creds_dict = json.loads(creds_json)
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(os.environ["SPREADSHEET_ID"])


def flatten_nested_columns(df):
    """Kolom berisi list/dict di-JSON-kan (sel Sheets hanya bisa scalar)."""
    df = df.copy()
    for col in df.columns:
        if df[col].apply(lambda v: isinstance(v, (list, dict))).any():
            df[col] = df[col].apply(
                lambda v: json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v
            )
    return df


def clean_for_sheets(df):
    """NaN/NaT/inf -> None, datetime -> string ISO, numpy scalar -> python scalar.
    astype(object) dulu wajib: kolom float64 tidak bisa menyimpan None asli,
    sehingga NaN lolos dan gspread error 'Out of range float values ...'."""
    df = flatten_nested_columns(df).copy()
    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            df[col] = df[col].dt.strftime("%Y-%m-%d %H:%M:%S")
    df = df.replace([float("inf"), float("-inf")], pd.NA)
    return df.astype(object).where(pd.notnull(df), None)


def get_or_create_worksheet(spreadsheet, title, ncols, nrows=1000):
    try:
        return spreadsheet.worksheet(title), True
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=title, rows=nrows, cols=max(ncols, 10))
        return ws, False


def _ensure_size(ws, rows, cols):
    if ws.row_count < rows or ws.col_count < cols:
        ws.resize(rows=max(ws.row_count, rows), cols=max(ws.col_count, cols))


def _write_block(ws, values):
    """Tulis matriks mulai A1 (header + data) per chunk, RAW."""
    _ensure_size(ws, len(values), len(values[0]) if values else 1)
    for start in range(0, len(values), CHUNK_ROWS):
        chunk = values[start:start + CHUNK_ROWS]
        ws.update(chunk, range_name=f"A{start + 1}", value_input_option="RAW")


def read_header(ws):
    try:
        return [h for h in ws.row_values(1) if h != ""]
    except Exception:
        return []


def read_key_set(ws, header, key_cols):
    """Ambil hanya kolom-kunci dari sheet (jauh lebih ringan dari get_all_records)."""
    cols = []
    for k in key_cols:
        idx = header.index(k) + 1
        cols.append(ws.col_values(idx)[1:])  # buang header
    n = min(len(c) for c in cols) if cols else 0
    return {"|".join(str(c[i]) for c in cols) for i in range(n)}


def read_sheet_df(ws):
    values = ws.get_all_values()
    if len(values) < 2:
        return pd.DataFrame(columns=values[0] if values else [])
    return pd.DataFrame(values[1:], columns=values[0])


def upsert_timeseries(spreadsheet, title, df, key_cols):
    """Append hanya baris yang kombinasi key_cols-nya belum ada di sheet."""
    if df.empty:
        print(f"[{title}] nothing fetched, skipping")
        return 0

    df = clean_for_sheets(df)
    ws, existed = get_or_create_worksheet(spreadsheet, title, len(df.columns))
    header = read_header(ws) if existed else []

    if header:
        missing = [c for c in header if c not in df.columns]
        extra = [c for c in df.columns if c not in header]
        if extra:
            print(f"[{title}] kolom baru diabaikan (tidak ada di header sheet): {extra}")
        if missing:
            print(f"[{title}] kolom di sheet tapi tidak ada di data baru (dikosongkan): {missing}")
        df = df.reindex(columns=header).astype(object)
        df = df.where(pd.notnull(df), None)

        if all(k in header for k in key_cols):
            existing = read_key_set(ws, header, key_cols)
            new_keys = df[key_cols].astype(str).agg("|".join, axis=1)
            df = df[~new_keys.isin(existing)]

    if df.empty:
        print(f"[{title}] no new rows (already up to date)")
        return 0

    if not header:
        _write_block(ws, [df.columns.tolist()] + df.values.tolist())
    else:
        rows = df.values.tolist()
        for start in range(0, len(rows), CHUNK_ROWS):
            ws.append_rows(rows[start:start + CHUNK_ROWS],
                           value_input_option="RAW", table_range="A1")

    print(f"[{title}] wrote {len(df)} new row(s)")
    return len(df)


def overwrite_snapshot(spreadsheet, title, df):
    """Ganti seluruh isi tab (untuk data yang berubah pelan / snapshot terbaru)."""
    if df.empty:
        print(f"[{title}] nothing fetched, skipping")
        return
    df = clean_for_sheets(df)
    ws, _ = get_or_create_worksheet(spreadsheet, title, len(df.columns))
    ws.clear()
    _write_block(ws, [df.columns.tolist()] + df.values.tolist())
    print(f"[{title}] overwritten with {len(df)} row(s)")
