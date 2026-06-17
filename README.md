# New Spirit WhatsApp Worker

Worker ini membaca pesan operasional dari grup WhatsApp `New Spirit`, menyimpan pesan mentah ke SQLite, melakukan parsing awal untuk format MVT departure/arrival, lalu mengirim hasilnya ke Google Sheets.

Dokumen ini sekaligus menjadi README teknis dan PRD sederhana untuk target berikutnya: dataset `RAW`, `FLIGHT_RAW`, dan `FLIGHT_OPS` dengan pembersihan mendalam memakai AI.

## Status Saat Ini

Yang sudah ada di repo:

- Listener WhatsApp berbasis Baileys.
- Ingest service Python untuk menerima dan menyimpan pesan.
- SQLite sebagai penyimpanan lokal.
- Parser berbasis aturan untuk pesan `MVT Dept` dan `Mvt Arrival`.
- Sinkronisasi Google Sheets ke tab bronze/silver: `RAW` dan `FLIGHT_RAW`.
- Script migrasi data operasional lokal.

Yang belum menjadi implementasi penuh:

- Tab gold `FLIGHT_OPS`.
- Worker pembersihan mendalam AI setiap 15 menit.
- Tombol atau kontrol pemeriksaan ulang paksa per baris di Google Sheets.
- Prompt endpoint produksi untuk membersihkan data.

Bagian PRD di bawah menjelaskan arah produk berikutnya.

## Alur E2E Saat Ini

```text
Grup WhatsApp New Spirit
  -> Baileys listener
  -> Python ingest service
  -> SQLite raw_messages / flight_movements
  -> Google Sheets tab RAW / FLIGHT_RAW
```

`RAW` adalah tab bronze untuk pesan mentah. `FLIGHT_RAW` adalah tab silver untuk hasil parser berbasis aturan. `Movements_Internal` adalah tab legacy dan tidak dipakai lagi.

## PRD: Dataset Flight Ops Hasil Pembersihan AI

### Tujuan Produk

Tujuan produk ini adalah mengubah pesan WhatsApp operasional yang formatnya tidak selalu konsisten menjadi dataset operasional penerbangan yang rapi, dapat diaudit, dan siap dipakai oleh tim Ops.

Sistem harus:

- Menyimpan pesan WhatsApp mentah sebagai sumber kebenaran.
- Menyimpan hasil parser berbasis aturan sebagai lapisan antara yang bisa diaudit.
- Menghasilkan dataset final yang sudah melewati pembersihan AI dan gerbang validasi.
- Tidak memproses ulang baris yang sudah bersih, kecuali pengguna meminta pemeriksaan ulang paksa.

### Lapisan Dataset

Target Google Sheets berikutnya berisi tiga tab utama:

```text
RAW         dataset bronze: pesan WhatsApp mentah
FLIGHT_RAW  dataset silver: hasil parser berbasis aturan
FLIGHT_OPS  dataset gold: hasil pembersihan AI dan validasi
```

`RAW` hanya boleh ditambah. Isi tab ini tidak boleh diedit manual karena menjadi sumber kebenaran.

`FLIGHT_RAW` adalah hasil parsing awal. Data di sini bisa belum sempurna, tetapi setiap baris harus tetap bisa ditelusuri ke `raw_message_id`, `movement_id`, dan versi parser.

`FLIGHT_OPS` adalah dataset final untuk kebutuhan operasional. Baris hanya boleh masuk ke sini setelah melewati pembersihan AI dan gerbang validasi.

### Alur E2E Target

```text
1. Hubungkan, baca, simpan
   grup WhatsApp -> tabel raw_messages -> tab RAW

2. Parse dan normalisasi
   tabel raw_messages -> tabel flight_movements -> tab FLIGHT_RAW

3. Pembersihan AI
   baris pending di FLIGHT_RAW -> endpoint prompt -> gerbang validasi -> tab FLIGHT_OPS
```

Pesan mentah harus selalu disimpan agar parser dan proses pembersihan AI bisa dijalankan ulang atau diaudit di kemudian hari.

### Jadwal Pembersihan AI

Worker pembersihan AI berjalan default setiap 15 menit.

Worker juga perlu mendukung pemicu manual:

- Bersihkan semua baris yang memenuhi syarat.
- Bersihkan satu baris tertentu.
- Pemeriksaan ulang paksa untuk baris yang sebelumnya sudah `cleaned`.

Aturan utama:

- Baris dengan status `cleaned` tidak diproses ulang oleh jadwal otomatis normal.
- Baris `cleaned` hanya diproses ulang jika `deepclean_force_check = TRUE`, ada proses migrasi, atau ada intervensi admin.
- Setelah pemeriksaan ulang paksa berhasil, worker harus mengembalikan `deepclean_force_check = FALSE`.

Untuk tahap pengembangan awal, proses pembersihan AI boleh dilakukan dengan bantuan Codex secara manual. Setelah prompt, skema, dan gerbang validasi stabil, kontrak yang sama dipindahkan ke worker khusus yang memanggil endpoint prompt.

### Status Pembersihan Mendalam

`FLIGHT_RAW` perlu memiliki kolom kontrol:

```text
deepclean_status
deepclean_force_check
deepclean_requested_at
deepcleaned_at
deepclean_prompt_version
deepclean_model
deepclean_error
flight_ops_id
```

Status yang direkomendasikan:

```text
pending
cleaning
cleaned
failed
needs_review
skipped
```

Makna status:

- `pending`: siap diproses oleh jadwal otomatis berikutnya.
- `cleaning`: sedang diproses oleh worker.
- `cleaned`: sudah berhasil masuk dataset gold dan tidak diproses ulang normal.
- `failed`: proses AI atau validasi gagal, bisa dicoba ulang dengan batas percobaan ulang.
- `needs_review`: butuh review manusia sebelum masuk `FLIGHT_OPS`.
- `skipped`: sengaja dilewati karena tidak memenuhi kriteria.

### Pemeriksaan Ulang Paksa di Google Sheets

Mekanisme paling sederhana untuk tim Ops adalah checkbox di tab `FLIGHT_RAW`:

```text
deepclean_force_check
```

Jika pengguna mencentang checkbox ini, jadwal otomatis berikutnya atau pemicu manual akan memproses ulang baris tersebut walaupun statusnya sudah `cleaned`.

Audit metadata tetap wajib disimpan:

- versi prompt
- model
- waktu pembersihan terakhir
- error terakhir jika ada
- referensi ke `FLIGHT_OPS`

### Input Pembersihan AI

Prompt endpoint harus menerima konteks yang cukup:

```text
raw_message_id
movement_id
source_text
rule_based_fields
airport_master_context
previous_flight_ops_row, jika pemeriksaan ulang paksa dilakukan atas baris gold yang sudah ada
prompt_version
```

AI harus memperlakukan `source_text` sebagai sumber utama. Hasil parser berbasis aturan hanya menjadi petunjuk, bukan kebenaran absolut.

### Output Pembersihan AI

Endpoint prompt harus mengembalikan JSON ketat. Respons berupa paragraf atau prosa tidak boleh dianggap sukses.

Field dataset gold yang direkomendasikan:

```text
operation_date
movement_type
registration
aircraft_type
flight_seq
leg_origin_code
leg_destination_code
route_full
takeoff_time
eta_time
ata_time
pax
pax_weight_kg
baggage_kg
cargo_kg
total_load_kg
remark
ops_status
ai_confidence
review_notes
```

Skema boleh berkembang, tetapi setiap perubahan skema harus memiliki versi yang eksplisit.

### Gerbang Validasi

Baris hanya boleh masuk ke `FLIGHT_OPS` jika semua gerbang berikut lolos:

- Respons AI valid JSON.
- Kolom wajib sesuai versi skema.
- Format registration valid, contoh `PK-SNW`.
- Jenis movement dikenali, contoh `departure` atau `arrival`.
- Kode airport cocok dengan internal airport master atau ditandai eksplisit sebagai belum terpetakan.
- Kolom waktu dinormalisasi konsisten.
- Kolom load numerik dikonversi ke angka jika memungkinkan.
- Confidence AI di atas ambang batas yang ditentukan.
- `raw_message_id` dan `movement_id` tetap tersimpan.

Jika gerbang validasi gagal, baris masuk `failed` atau `needs_review`, bukan langsung masuk `FLIGHT_OPS`.

### Perilaku Google Sheets Target

Buku kerja Google Sheets target berisi:

- `RAW`: pesan WhatsApp mentah.
- `FLIGHT_RAW`: hasil parser plus status pembersihan mendalam.
- `FLIGHT_OPS`: data gold hasil pembersihan AI.

Worker terjadwal harus menambahkan atau memperbarui tab-tab tersebut tanpa copy-paste manual.

Edit manual sebaiknya dibatasi hanya untuk kolom kontrol:

- `deepclean_force_check`
- kolom review manusia
- catatan Ops

Teks mentah dan kolom hasil parser tetap dimiliki sistem.

### Aturan Reprocessing

Kondisi default yang dilewati:

```text
deepclean_status = cleaned
deepclean_force_check = FALSE
```

Baris boleh diproses ulang hanya jika:

- `deepclean_force_check = TRUE`
- ada migrasi versi prompt
- ada migrasi skema
- ada intervensi admin

Pemrosesan ulang tidak boleh menghapus riwayat audit. Minimal simpan versi prompt, model, timestamp, dan status terbaru. Idealnya, simpan riwayat output AI sebelumnya di tabel terpisah.

### Bukan Tujuan Versi Awal

Versi awal tidak perlu:

- Menghapus parser berbasis aturan.
- Mengedit pesan WhatsApp.
- Menebak data yang tidak didukung `source_text` atau airport master.
- Menghapus raw message otomatis.
- Membuka endpoint publik tanpa token.

### Kriteria Penerimaan

Produk siap dipakai awal jika:

- Pesan WhatsApp baru muncul di `RAW`.
- Hasil parser berbasis aturan muncul di `FLIGHT_RAW`.
- Baris yang memenuhi syarat di `FLIGHT_RAW` diproses pembersihan AI maksimal 15 menit.
- Baris yang lolos pembersihan AI muncul di `FLIGHT_OPS`.
- Baris yang sudah `cleaned` dilewati secara default.
- Checkbox `deepclean_force_check` memicu pemrosesan ulang baris tersebut.
- Respons AI yang gagal tidak mencemari `FLIGHT_OPS`.
- Setiap baris `FLIGHT_OPS` bisa ditelusuri ke `movement_id`, `raw_message_id`, dan `source_text`.
- Versi prompt dan model AI tercatat di setiap baris gold.

## Struktur Repo

```text
src/                         Worker Node/Baileys untuk WhatsApp
app/                         Service Python, parser, dan Google Sheets sync
bin/                         Script runner berulang dan migrasi data lokal
config/airport_mappings.json Mapping airport cadangan
config/google-sheets.env.example
integrations/google-sheets-webhook.gs
```

Data operasional lokal, credential WhatsApp, pesan, database, log, PID, dan master data internal tidak ikut git.

## Prasyarat

- Node.js 22 atau lebih baru.
- Python 3.12 atau lebih baru.
- Nomor WhatsApp yang boleh join grup target.
- Google Sheet dengan deployment Apps Script Web App.

Pasang dependency Node:

```bash
npm install
```

## Setup Google Sheets

Tujuan sinkronisasi yang sudah diimplementasikan saat ini:

```text
RAW
FLIGHT_RAW
```

Target PRD berikutnya akan menambahkan tab gold:

```text
FLIGHT_OPS
```

Langkah setup Apps Script:

1. Buka Google Sheet.
2. Pilih `Extensions` -> `Apps Script`.
3. Paste isi `integrations/google-sheets-webhook.gs`.
4. Tambahkan Script Properties:
   - `TOKEN`: token random privat.
   - `SPREADSHEET_ID`: ID Google Sheet tujuan. Ini tetap direkomendasikan walaupun worker juga bisa mengirim `GOOGLE_SHEETS_SPREADSHEET_ID` dari config lokal.
5. Deploy sebagai Web App.
6. Atur `Execute as` ke `Me`.
7. Atur access ke `Anyone`.
8. Salin URL Web App yang berakhiran `/exec`.

Buat config lokal:

```bash
cp config/google-sheets.env.example config/google-sheets.env
```

Isi file tersebut:

```bash
GOOGLE_SHEETS_WEBHOOK_URL=https://script.google.com/macros/s/YOUR_DEPLOYMENT_ID/exec
GOOGLE_SHEETS_WEBHOOK_TOKEN=token-yang-sama-dengan-apps-script
GOOGLE_SHEETS_SPREADSHEET_ID=id-google-sheet-tujuan
GOOGLE_SHEETS_RAW_TAB=RAW
GOOGLE_SHEETS_FLIGHT_RAW_TAB=FLIGHT_RAW
```

File `config/google-sheets.env` tidak boleh di-commit.

Buat atau pastikan tab `RAW` dan `FLIGHT_RAW` tersedia di Google Sheets:

```bash
npm run sheets:ensure
```

Jika Sheet lama masih punya tab legacy, hapus hanya setelah `RAW` dan `FLIGHT_RAW` sudah benar:

```bash
npm run sheets:delete-legacy
```

Command ini mencoba menghapus tab legacy berikut:

```text
Movements_Internal
Movements
Schedules
```

## Menjalankan Listener WhatsApp

Gunakan nomor WhatsApp khusus worker. Jangan gabungkan sesi perangkat tertaut worker ini dengan WhatsApp Web lain yang aktif untuk kebutuhan berbeda.

Cara paling sederhana:

```bash
./connect.sh
```

Script ini akan menampilkan QR di terminal. Scan dengan WhatsApp sampai worker masuk status `listening`.
Jika setelah scan muncul `Stream Errored (restart required)`, biarkan script berjalan. `connect.sh` akan restart otomatis dan memakai sesi yang baru discan.

Jalankan sekali:

```bash
npm run listen
```

Saat pertama kali jalan, scan QR dengan WhatsApp:

```text
WhatsApp -> Linked devices -> Link a device
```

Jika terminal tidak bisa menampilkan QR dengan utuh, gunakan pairing code:

```bash
npm run listen:pair -- 628123456789
```

Masukkan nomor dalam format kode negara tanpa tanda `+`. Di HP, buka:

```text
WhatsApp -> Linked devices -> Link a device -> Link with phone number instead
```

Biarkan terminal tetap menyala sampai muncul status `listening`. Jika pairing gagal dengan error seperti `QR refs attempts ended`, reset sesi listener lalu coba lagi:

```bash
rm -rf .runtime-auth/listener data/listener-status.json data/listener-qr.txt data/listener-qr.png
npm run listen:pair -- 628123456789
```

Jika kode random sulit diketik, gunakan custom code 8 karakter:

```bash
rm -rf .runtime-auth/listener data/listener-status.json data/listener-qr.txt data/listener-qr.png
npm run listen:pair -- 628123456789 --pair-code 12345678
```

Untuk operasi jangka panjang, gunakan loop runner:

```bash
npm run listen:loop
```

Listener menulis pesan grup mentah ke:

```text
data/live-messages.jsonl
```

## Menjalankan Service Ingest

Jalankan service HTTP lokal:

```bash
npm run ingest
```

Untuk operasi jangka panjang:

```bash
npm run ingest:loop
```

Cek lokal:

```bash
curl http://127.0.0.1:8088/health
curl "http://127.0.0.1:8088/messages?limit=10"
curl "http://127.0.0.1:8088/movements?limit=10"
```

Database SQLite tersimpan di:

```text
data/ops_messages.sqlite3
```

## Parser Pergerakan

Parser ada di:

```text
app/movement_parser.py
```

Format yang sudah didukung:

- `MVT Dept`
- `Mvt Arrival`
- registrasi pesawat, contoh `PK-SNW`
- tipe pesawat, contoh `C208B-EX`
- urutan penerbangan harian, contoh `Flight 04`
- rute multi-leg, contoh `AAP-RTU-AAP`
- `Engine Start`, `Take Off`, `ETA`, `ATA`
- pax, pax weight, baggage, cargo, total load

Rute departure dipecah menjadi satu baris per leg. Contoh:

```text
AAP-RTU-AAP
```

menjadi:

```text
AAP -> RTU
RTU -> AAP
```

Buat ulang hasil parser dari raw message yang sudah ada:

```bash
npm run movements:rebuild
```

## Master Data Airport

Jika tersedia, letakkan master airport internal di:

```text
data/reference/master_iata.json
```

Field yang diharapkan:

```text
code
icao_code
airport_name
location
province_name
timezone
```

Prioritas mapping:

1. `data/reference/master_iata.json`
2. `config/airport_mappings.json`
3. fallback belum terpetakan atau teks bebas

Karena folder `data/` di-ignore, master internal tidak ikut ke GitHub.

## Sinkronisasi ke Google Sheets

Pastikan tab `RAW` dan `FLIGHT_RAW` sudah ada:

```bash
npm run sheets:ensure
```

Kirim pesan mentah dan movement rows yang belum tersinkron satu kali:

```bash
npm run sheets:sync
```

Jalankan sinkronisasi terus-menerus:

```bash
npm run sheets:sync:loop
```

Status sinkronisasi disimpan di:

```text
data/google-sheets-movement-sync-state.json
```

Hapus file status ini hanya jika memang ingin menambahkan ulang semua row `RAW` dan `FLIGHT_RAW`.

## Menjalankan Semua Worker

Jalankan di terminal terpisah:

```bash
npm run ingest:loop
npm run listen:loop
npm run sheets:sync:loop
```

Jika dijalankan di background atau secara manual, log dan PID biasanya ditulis ke folder `data/`.

## Migrasi Data Operasional Lokal

Untuk pindah server, jangan commit data WhatsApp mentah ke repo GitHub publik. SQLite dapat berisi isi pesan mentah, sender ID, timestamp, dan detail operasional.

Gunakan arsip lokal.

Ekspor data operasional lokal:

```bash
bin/export-migration.sh
```

Yang ikut jika tersedia:

- `data/ops_messages.sqlite3`
- `data/reference/master_iata.json`
- `data/google-sheets-movement-sync-state.json`

Ekspor dengan enkripsi:

```bash
MIGRATION_PASSPHRASE='gunakan-password-panjang' bin/export-migration.sh
```

Impor di server baru:

```bash
MIGRATION_PASSPHRASE='gunakan-password-panjang' bin/import-migration.sh /path/to/new-spirit-runtime-YYYYMMDDTHHMMSSZ.tar.gz.enc
```

Jika file env lokal juga perlu dibawa:

```bash
INCLUDE_LOCAL_ENV=1 MIGRATION_PASSPHRASE='gunakan-password-panjang' bin/export-migration.sh
```

Credential perangkat tertaut WhatsApp di `.runtime-auth/` tidak ikut script migrasi ini. Di mesin baru, worker perlu scan QR lagi.

## Parser Ekspor Manual

Untuk ekspor chat WhatsApp manual:

```bash
npm run parse-export -- --input /path/to/chat.txt --out data/export.jsonl --csv data/export.csv
```

## Sinkronisasi History Opsional

`src/scrape-history.js` masih eksperimental. WhatsApp menentukan sendiri seberapa banyak history lama yang diberikan ke perangkat tertaut. Untuk arsip historis yang lebih andal, ekspor manual masih lebih aman.

Jika ingin memakai credential dari integrasi WhatsApp lain, set env berikut:

```bash
OPENCLAW_WHATSAPP_AUTH_DIR=/path/to/source/auth
```

Proyek akan menyalin credential ke `.runtime-auth/` dan tidak perlu mengubah folder credential sumber.

## Kebersihan GitHub

Jangan di-commit:

- `.runtime-auth/`
- `data/`
- `config/google-sheets.env`
- database SQLite
- ekspor JSONL WhatsApp
- log dan PID file
- `node_modules/`
- arsip migrasi

Item di atas sudah tercakup oleh `.gitignore`.

## Catatan Operasional

- Raw message adalah sumber kebenaran. Jangan edit manual.
- Parser berbasis aturan tetap berguna walaupun nantinya ada pembersihan AI.
- Pembersihan AI harus punya skema dan gerbang validasi yang ketat.
- `FLIGHT_OPS` hanya boleh berisi baris yang sudah lolos validasi.
- Data operasional lokal boleh dimigrasikan antar server, tetapi jangan dipublikasikan ke repo publik.
