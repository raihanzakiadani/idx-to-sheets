# Setup Guide: IDX -> Google Sheets -> Excel

## 1. Buat Google Sheet tujuan
1. Buat 1 Google Sheet baru (kosong), kasih nama misal "IDX Data".
2. Copy Spreadsheet ID dari URL-nya:
   `https://docs.google.com/spreadsheets/d/`**`INI_ID_NYA`**`/edit`

## 2. Buat Google Service Account
1. Ke https://console.cloud.google.com -> buat project baru (atau pakai yang ada).
2. Enable **Google Sheets API** (APIs & Services > Library > cari "Google Sheets API" > Enable).
3. APIs & Services > Credentials > Create Credentials > Service Account.
4. Setelah dibuat, buka service account itu > tab "Keys" > Add Key > Create New Key > JSON.
   File JSON akan otomatis terdownload - ini `GOOGLE_SERVICE_ACCOUNT_JSON` kamu nanti.
5. Di file JSON itu, cari field `"client_email"` (formatnya
   `xxxx@xxxx.iam.gserviceaccount.com`).

## 3. Share Google Sheet ke Service Account
1. Buka Google Sheet dari langkah 1 > tombol Share.
2. Paste `client_email` dari langkah 2, kasih akses **Editor**.
   (Tanpa ini, script akan gagal dengan error permission.)

## 4. Push folder ini ke GitHub repo baru
1. Buat repo baru di GitHub (boleh Private).
2. Upload semua file di folder ini (`scraper.py`, `requirements.txt`,
   `.github/workflows/idx_scraper.yml`), **pertahankan struktur foldernya**
   (folder `.github/workflows/` harus tetap ada apa adanya).

## 5. Tambahkan Secrets di repo
Repo > Settings > Secrets and variables > Actions > New repository secret:

| Name | Value |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | isi lengkap file JSON dari langkah 2 (copy-paste semuanya) |
| `SPREADSHEET_ID` | Spreadsheet ID dari langkah 1 |

## 6. Test manual run
1. Tab **Actions** di repo > pilih workflow "IDX to Google Sheets" > **Run workflow**.
2. Tunggu selesai (~1 menit), cek apakah muncul error di log.
3. Kalau sukses, buka Google Sheet-nya - harusnya sudah ada tab baru:
   `OHLCV`, `IndexSummary`, `BrokerSummary`, `Fundamentals`.

Kalau langsung ada data di hari itu, berarti curl_cffi berhasil menembus
proteksi IDX. Kalau gagal terus dengan error 403/CAPTCHA, kabari saya -
mungkin perlu ganti `impersonate` profile di `scraper.py` (misal ke
`"chrome120"` atau `"safari17_0"`) atau tambah rotasi User-Agent.

Setelah ini jalan, workflow otomatis jalan sendiri tiap hari kerja jam
17:30 WIB - kamu tidak perlu trigger manual lagi.

## 7. Publish tab yang dibutuhkan sebagai CSV
Untuk setiap tab yang mau ditarik ke Excel (misal `OHLCV`):
1. Di Google Sheets: File > Share > **Publish to web**.
2. Di dropdown pertama, pilih tab spesifik (misal "OHLCV") - jangan pilih
   "Entire Document".
3. Di dropdown format, pilih **Comma-separated values (.csv)**.
4. Klik Publish, copy link yang muncul.
5. Ulangi untuk tab lain yang kamu perlu (IndexSummary, Fundamentals, dst),
   masing-masing tab punya link CSV sendiri.

## 8. Sambungkan ke Excel via Power Query
Untuk setiap link CSV dari langkah 7:
1. Excel > Data > Get Data > From Web.
2. Paste link CSV-nya > OK > Load (atau Transform Data dulu kalau perlu
   bersih-bersih kolom).
3. Supaya auto-refresh tiap file dibuka: klik kanan query di panel
   "Queries & Connections" > Properties > centang "Refresh data when
   opening the file".

Setelah ini, data OHLCV/index/fundamental/broker flow di Excel kamu akan
otomatis ikut update setiap kali file dibuka (asal ada internet), tanpa
siapa pun yang buka file perlu install apapun.

---

# Tambahan: Kepemilikan Saham (>1% & >5%) dan Intraday Google Finance

## Struktur repo

```
scraper.py            # job harian IDX (sudah ada) + panggil ownership
sheets_util.py        # helper Google Sheets (dipindah dari scraper.py)
ownership.py          # NEW  kepemilikan >1% / >5% dari file Excel IDX
intraday.py           # NEW  snapshot Google Finance -> bar OHLCV -> indikator
indicators.py         # NEW  182 indikator teknikal dari OHLCV
intraday_job.py       # NEW  entrypoint workflow intraday
tests/                # uji lokal (tanpa jaringan)
.github/workflows/idx_scraper.yml   # harian 17:30 WIB (+ kepemilikan saham)
.github/workflows/intraday.yml      # NEW tiap 5 menit saat jam bursa
```
Push semua file ini, pertahankan folder `.github/workflows/`. Secrets tidak berubah
(`GOOGLE_SERVICE_ACCOUNT_JSON`, `SPREADSHEET_ID`). Setelah `pip install -r requirements.txt`
ada dua dependency baru: `openpyxl` dan `numpy`.

## A. Kepemilikan pemegang saham >1% dan >5%

Sumber: file **.xlsx** (bukan PDF) di halaman
`idx.co.id/id/perusahaan-tercatat/data-kepemilikan-saham/`. Daftar file dibaca dari
halaman itu sendiri, jadi file baru terdeteksi otomatis.

| Tab | Isi |
|---|---|
| `Holders1pct` | Semua baris file >1% (bulanan), tanpa duplikat |
| `Holders5pct` | File >5% (harian) sebagai **baseline + perubahan**: kolom `RowType` = `BASELINE` (file pertama), lalu `NEW` / `EXIT` / `CHANGE` |
| `Holders5pct_Latest` | Snapshot penuh >5% terbaru (di-overwrite) |
| `OwnershipLog` | File yang sudah diproses (dipakai supaya run berikutnya hanya file baru) |

Catatan penting:
- **Kenapa >5% berupa perubahan?** Satu file berisi ±1.900 investor; snapshot penuh tiap hari
  ≈ 7 juta sel/tahun, sedangkan batas satu Google Sheet 10 juta sel (semua tab). Baseline +
  perubahan tidak menghilangkan informasi. Kalau tetap mau snapshot penuh tiap hari:
  `HOLDERS5_STORE_FULL=true` (pantau jumlah sel).
- Kolom `Perubahan` bawaan IDX tidak konsisten (kadang ribuan saham, kadang saham penuh),
  jadi disimpan mentah di `PerubahanRaw` dan dihitung ulang sebagai `DeltaShares` / `DeltaPct`.
- Satu investor = satu baris (sub-rekening digabung; jumlahnya di `JmlRekening`,
  kustodiannya di `Kustodian`). Investor bernama sama dengan SID berbeda dibedakan `Seq`.
- Ticker **TRUE** disimpan Excel sebagai boolean; sudah ditangani.
- **Backfill 30 Januari 2026**: pada HTML yang Anda kirim, daftar file >1% dimulai 27 Feb 2026
  dan >5% dimulai 29 Mei 2026. Kalau file sebelum itu ada di IDX tetapi tidak muncul di
  halaman, isi `OWNERSHIP_EXTRA_URLS` (URL .xlsx, pisah koma/spasi) di workflow. Nama file
  harus tetap berbentuk `peng-YYYY-MM-DD-...-lima-persen.xlsx` untuk >5%, atau
  `...-satu-persen.xlsx` untuk >1%. Untuk >5%, masukkan mulai dari file paling awal
  supaya baseline terbentuk dari file yang benar.
- File >5% diproses berurutan; bila satu file gagal diunduh, file setelahnya ditunda dan
  dicoba lagi di run berikutnya (agar urutan baseline → perubahan tetap benar).

## B. Intraday OHLC + 180 indikator (Google Finance)

**Batasan Google Finance yang perlu Anda tahu:** `GOOGLEFINANCE` hanya menyediakan histori
**harian/mingguan**; tidak ada histori menit/jam yang bisa diminta mundur, dan kuotasi live
tertunda sampai ±20 menit. Jadi bar intraday **dirakit dari snapshot tiap 5 menit**, dan histori
intraday mulai terkumpul sejak run pertama (tidak bisa di-backfill ke belakang dari Google
Finance). Bar tidak bisa lebih rapat dari jarak antar run.

Cara kerja: script menulis rumus `GOOGLEFINANCE` ke tab `_GF_Live`, menunggu Sheets
menghitung, membaca hasilnya, lalu menyimpan:

| Tab | Isi |
|---|---|
| `Watchlist` | Daftar ticker (kolom A). Dibuat otomatis berisi 20 contoh; **edit sesuai kebutuhan** |
| `Intraday_Ticks` | Snapshot mentah (harga, open/high/low hari itu, volume kumulatif, waktu trade) |
| `Intraday_Bars` | Bar OHLCV 5 menit hasil rakitan (disimpan `INTRADAY_KEEP_DAYS`, default 30 hari) |
| `Intraday_Indicators` | 1 baris/ticker: bar terakhir + 182 indikator |

- **Indikator**: kode sebelumnya belum memuat daftar indikator, jadi `indicators.py` berisi
  set 182 kolom (MA/EMA/WMA/HMA/DEMA/TEMA, RSI, Stochastic, MACD, ROC, CCI, Williams %R,
  ATR, Bollinger, Keltner, Donchian, ADX, Aroon, Vortex, Ichimoku, PSAR, Supertrend, OBV, MFI,
  CMF, VWAP harian, statistik, return, pola candle). Kolom yang belum punya cukup bar
  (mis. `SMA_200`) kosong sampai bar-nya cukup. Untuk memakai daftar 180 versi Anda sendiri,
  edit fungsi `_build()`.
- **Ukuran**: skala aman ±20-100 ticker. Ratusan ticker atau simpan >100 hari akan cepat
  memenuhi 10 juta sel dan membuat GOOGLEFINANCE lambat/#N/A.
- **Runner**: workflow intraday memakai `self-hosted` (sama dengan workflow lama) karena
  ±100 run/hari akan menghabiskan menit GitHub-hosted untuk repo private. Cron GitHub tidak
  dijamin tepat waktu (sering telat beberapa menit saat ramai); tick yang terlewat
  menghasilkan bar yang lebih jarang, bukan data salah.
- Libur bursa tidak dicek; tanpa tick baru (`TradeTime` sama) script tidak menulis apa pun.
- Kode yang tidak dikenali Google Finance (`IDX:XXXX`) dilaporkan di log dan dilewati.

## C. Uji lokal (opsional)
```bash
pip install -r requirements.txt
UPLOADS=/folder/berisi/contoh python -m tests.test_ownership_flow
python -m tests.test_intraday_flow
```
