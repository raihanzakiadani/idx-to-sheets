"""Uji end-to-end run_ownership memakai file lokal (HTML + xlsx contoh) dan Sheets palsu.
Jalankan:  python -m tests.test_ownership_flow   (dengan UPLOADS=/path/ke/file-contoh)"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ownership import run_ownership  # noqa: E402
from tests.fake_sheets import FakeSpreadsheet  # noqa: E402

UP = os.environ.get("UPLOADS", "/mnt/user-data/uploads")
LOCAL = {"peng-03-00012-satu-persen.xlsx": "peng-03-00012-satu-persen.xlsx",
         "peng-2026-08-04-00046-lima-persen.xlsx": "peng-2026-08-04-00046-lima-persen.xlsx"}
html = open(os.path.join(UP, "PT_Bursa_Efek_Indonesia.html"), encoding="utf-8", errors="ignore").read()


def fetch_bytes(url):
    name = url.rsplit("/", 1)[-1]
    if name not in LOCAL:
        raise RuntimeError("404 (file contoh tidak tersedia di uji lokal)")
    return open(os.path.join(UP, LOCAL[name]), "rb").read()


def run(start):
    ss = FakeSpreadsheet()
    os.environ["OWNERSHIP_START_DATE"] = start
    run_ownership(ss, lambda u: html, fetch_bytes)
    return ss


# 1) >1% : mulai 2026-03-31 -> file Maret ada lokal, file bulan lain 'gagal' (tidak fatal)
ss = run("2026-03-31")
assert ss.sheets["Holders1pct"].grid[0][:3] == ["DATE", "SHARE_CODE", "ISSUER_NAME"]
assert len(ss.sheets["Holders1pct"].grid) - 1 == 7213
n1 = len(ss.sheets["Holders1pct"].grid)
run_ownership(ss, lambda u: html, fetch_bytes)          # run kedua: tidak menulis ulang
assert len(ss.sheets["Holders1pct"].grid) == n1
# 2) >5% : file 2026-08-04 ada lokal; mulai dari tanggal itu
ss5 = run("2026-08-04")
h5 = ss5.sheets["Holders5pct"].grid
assert len(ss5.sheets["Holders5pct_Latest"].grid) - 1 == 1907
assert {r[h5[0].index("RowType")] for r in h5[1:]} == {"BASELINE"}
print("log:", [(r[1], r[2], r[3]) for r in ss5.sheets["OwnershipLog"].grid[1:]])
# 3) file awal gagal -> file 5% berikutnya harus ditunda (tidak ada Holders5pct)
ss3 = run("2026-05-29")
assert "Holders5pct_Latest" not in ss3.sheets, "5% tidak boleh diproses jika file sebelumnya gagal"
print("OK")
