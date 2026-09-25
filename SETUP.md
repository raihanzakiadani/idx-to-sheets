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
