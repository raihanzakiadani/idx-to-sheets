"""Uji alur run_intraday (ticks -> bars -> indikator) dengan Sheets palsu.
Langkah GOOGLEFINANCE diganti data sintetis (rumus hanya bisa dihitung Google Sheets)."""
import datetime
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import intraday  # noqa: E402
from tests.fake_sheets import FakeSpreadsheet  # noqa: E402

os.environ.update(INTRADAY_FORCE="true", INTRADAY_TICKERS="BBCA,BBRI", INTRADAY_MIN_BARS="20")
day = datetime.datetime.now(intraday.WIB).strftime("%Y-%m-%d")
rng = np.random.default_rng(7)
state = {}


def fake_snapshot(ss, tickers, wait_sec):
    """Tiap panggilan maju 5 menit, seperti satu run workflow."""
    state["i"] = state.get("i", -1) + 1
    t = pd.Timestamp(f"{day} 09:00") + pd.Timedelta(minutes=5 * state["i"])
    rows = []
    for tk in tickers:
        st = state.setdefault(tk, {"p": 1000.0, "cum": 0, "hi": 0.0, "lo": 1e9, "op": None})
        st["p"] += rng.normal(0, 3)
        st["op"] = st["op"] or st["p"]
        st["hi"], st["lo"] = max(st["hi"], st["p"]), min(st["lo"], st["p"])
        st["cum"] += int(rng.integers(1000, 9000))
        rows.append([tk, t.strftime("%Y-%m-%d %H:%M:%S"), st["p"], st["op"], st["hi"], st["lo"], st["cum"], "x"])
    return pd.DataFrame(rows, columns=intraday.TICK_COLS)


intraday.snapshot_google_finance = fake_snapshot
ss = FakeSpreadsheet()
for _ in range(30):
    intraday.run_intraday(ss)
print({k: len(v.grid) - 1 for k, v in ss.sheets.items()})
ind = ss.sheets["Intraday_Indicators"].grid
print("kolom indikator:", len(ind[0]) - 8, "| contoh:", dict(zip(ind[0][:6], ind[1][:6])))
assert len(ss.sheets["Intraday_Ticks"].grid) - 1 == 60           # 30 run x 2 ticker
assert len(ss.sheets["Intraday_Bars"].grid) - 1 == 60
assert len(ind) - 1 == 2 and len(ind[0]) >= 189
# run ulang dengan snapshot identik (pasar tutup / tradetime sama) tidak menambah tick
state["i"] -= 1
n = len(ss.sheets["Intraday_Ticks"].grid)
intraday.run_intraday(ss)
assert len(ss.sheets["Intraday_Ticks"].grid) == n
print("OK")
