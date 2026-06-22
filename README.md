# New Spirit WhatsApp Worker

Worker ini membaca pesan operasional dari grup WhatsApp `New Spirit`, menyimpan pesan mentah ke SQLite, melakukan parsing awal untuk format MVT departure/arrival, lalu mengirim hasilnya ke Google Sheets.

Dokumen ini sekaligus menjadi README teknis dan PRD. Dataset utama terdiri dari `RAW` sebagai bronze, `FLIGHT_RAW` sebagai silver, dan `SORTIE_LOG` sebagai gold untuk dashboard produktivitas.

## Status Saat Ini

Yang sudah ada di repo:

- Listener WhatsApp berbasis Baileys.
- Ingest service Python untuk menerima dan menyimpan pesan.
- SQLite sebagai penyimpanan lokal.
- Parser berbasis aturan untuk pesan `MVT Dept` dan `Mvt Arrival`.
- Sinkronisasi Google Sheets ke tab bronze, silver, master, audit, dan gold.
- Builder `SORTIE_LOG` dengan grain satu baris per mission, dedup departure, matching arrival ACK, normalisasi pilot, dan quality gate.
- Tab `ARRIVAL_ACK_EXCEPTIONS` dan `ABNORMAL_EVIDENCE_AUDIT` untuk audit.
- Refresh otomatis `SORTIE_LOG` setiap satu jam.
- Script migrasi data operasional lokal.
- Command suite worker: `./connect.sh`, `./status.sh`, dan `./stop.sh`.
- Systemd user service untuk menjaga worker tetap hidup dan auto-start setelah reboot.

Yang belum menjadi implementasi penuh:

- Pembersihan AI lanjutan untuk memperbaiki field yang masih `MISSING`, `NEEDS_PARSE`, atau `NEEDS_REVIEW`.
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
  -> Google Sheets RAW / FLIGHT_RAW / FLIGHT_TIMELINE
  -> builder sortie + quality gate
  -> Google Sheets SORTIE_LOG / ARRIVAL_ACK_EXCEPTIONS / ABNORMAL_EVIDENCE_AUDIT
```

`RAW` adalah tab bronze untuk pesan mentah. `FLIGHT_RAW` adalah tab silver untuk hasil parser berbasis aturan. `Movements_Internal` adalah tab legacy dan tidak dipakai lagi.

## PRD: Lifecycle Worker WhatsApp

### Tujuan Operasional

Worker harus bisa dijalankan oleh tim Ops dengan command sederhana, tanpa perlu menjaga terminal tetap terbuka setelah koneksi WhatsApp berhasil.

Command utama:

```bash
./connect.sh
```

Requirement lifecycle:

- `./connect.sh` adalah entrypoint utama untuk koneksi dan start worker.
- Jika belum ada sesi WhatsApp valid, `./connect.sh` harus menampilkan QR di terminal.
- Setelah QR discan dan koneksi tervalidasi, proses operasional harus detach ke background.
- Worker yang harus berjalan adalah ingest service, Google Sheets sync, dan WhatsApp listener.
- Setiap pemanggilan `./connect.sh` harus stop/kill worker lama dari repo ini, lalu start ulang stack dari awal.
- Jika auth WhatsApp rusak atau session logout dari sisi WhatsApp, user menjalankan `./connect.sh --reset` untuk scan QR baru.
- Worker harus auto-restart jika crash atau koneksi network putus sementara.
- Worker harus tahan reboot server selama systemd user dan linger aktif.
- `./stop.sh` harus menghentikan semua worker dan disable auto-start.
- `./status.sh` harus menampilkan status systemd, proses fallback/manual, health ingest, status listener, dan status sync Google Sheets.

### Perilaku Command

`./connect.sh` melakukan urutan ini:

```text
1. Stop systemd service lama jika ada.
2. Kill proses manual/fallback lama dari repo ini.
3. Jika --reset, hapus auth WhatsApp listener dan QR lama.
4. Jalankan bootstrap WhatsApp di foreground agar QR terlihat di terminal.
5. Setelah koneksi valid, start semua worker di background.
6. Jika systemd user tersedia, tulis service, enable auto-start, dan start via systemd.
7. Jika systemd user tidak tersedia, gunakan fallback detached background.
```

Fallback detached background tetap tahan saat terminal ditutup, tetapi tidak tahan reboot. Untuk requirement tahan reboot, systemd user harus tersedia dan linger harus aktif. Script akan mencoba mengaktifkan linger otomatis. Jika gagal, jalankan sekali:

```bash
sudo loginctl enable-linger "$USER"
```

### Kriteria Penerimaan Lifecycle

Lifecycle worker dianggap benar jika:

- User bisa menjalankan `./connect.sh`, scan QR, lalu terminal bisa ditutup tanpa mematikan worker.
- Setelah reboot, worker hidup lagi otomatis tanpa scan QR selama session WhatsApp masih valid.
- Jika WhatsApp logout atau linked device dicabut, status menunjukkan perlu reconnect dan user bisa menjalankan `./connect.sh --reset`.
- `./status.sh` cukup untuk melihat apakah ingest, sync, dan listener sedang hidup.
- `./stop.sh` benar-benar menghentikan worker dan mencegah auto-start berikutnya sampai `./connect.sh` dijalankan lagi.

## PRD Lanjutan: Pembersihan AI per Movement

Bagian ini adalah rencana enrichment lanjutan, bukan kontrak gold dashboard saat ini. Kontrak final productivity berada pada bagian `SORTIE_LOG`; output AI nantinya harus memperbaiki field berkualitas rendah secara terkontrol dan memicu rebuild `SORTIE_LOG`.

### Tujuan Produk

Tujuan produk ini adalah mengubah pesan WhatsApp operasional yang formatnya tidak selalu konsisten menjadi dataset operasional penerbangan yang rapi, dapat diaudit, dan siap dipakai oleh tim Ops.

Sistem harus:

- Menyimpan pesan WhatsApp mentah sebagai sumber kebenaran.
- Menyimpan hasil parser berbasis aturan sebagai lapisan antara yang bisa diaudit.
- Menghasilkan kandidat enrichment yang sudah melewati pembersihan AI dan gerbang validasi.
- Tidak memproses ulang baris yang sudah bersih, kecuali pengguna meminta pemeriksaan ulang paksa.

### Lapisan Dataset

Lapisan utama Google Sheets:

```text
RAW         dataset bronze: pesan WhatsApp mentah
FLIGHT_RAW  dataset silver: hasil parser berbasis aturan
SORTIE_LOG  dataset gold: satu baris per sortie, siap dipakai dashboard productivity
```

Tab tambahan untuk konsumsi harian Ops:

```text
FLIGHT_TIMELINE  view rotasi pesawat yang sudah diurutkan untuk melihat kronologi per registration
FLIGHT_OPS       area eksperimen deepclean AI per movement; bukan source productivity utama
MASTER_IATA      master airport internal plus score traffic aktual dari flight movement
MAPPING_PILOT    master pilot dan kandidat alias/initial yang dipakai Ops di lapangan
ARRIVAL_ACK_EXCEPTIONS  arrival yang belum memiliki departure match
ABNORMAL_EVIDENCE_AUDIT pesan abnormal yang menjadi dasar status review
```

`RAW` hanya boleh ditambah. Isi tab ini tidak boleh diedit manual karena menjadi sumber kebenaran.

`FLIGHT_RAW` adalah hasil parsing awal. Data di sini bisa belum sempurna, tetapi setiap baris harus tetap bisa ditelusuri ke `raw_message_id`, `movement_id`, dan versi parser.

Untuk pesan arrival yang tidak membawa tanggal operasi di teks mentah, `operation_date` diisi dari tanggal pesan WhatsApp dalam zona waktu `Asia/Jakarta`. Kolom `chronology_sort_key` tersedia agar tim bisa filter `registration`, lalu sort ascending untuk melihat urutan movement per pesawat dengan lebih stabil.

`SORTIE_LOG` adalah dataset final untuk dashboard produktivitas. `FLIGHT_OPS` tetap disediakan sebagai area deepclean AI per movement, tetapi tidak dipakai untuk menghitung sortie agar route multi-leg tidak double count.

`FLIGHT_TIMELINE` adalah view turunan dari `FLIGHT_RAW`. Tab ini tidak menjadi sumber kebenaran baru; ia hanya memudahkan user melihat urutan terbang aktual pesawat per hari dan per registration. Untuk membaca kronologi, filter `operation_date` dan `registration`, lalu sort ascending kolom `timeline_sort_key`. Kolom yang paling enak dibaca manusia adalah `event_datetime_local`, sedangkan `event_time` tetap menyimpan waktu Z dari pesan sumber.

`MASTER_IATA` adalah master airport internal yang dinormalisasi dari source sheet master. Tab ini membawa `total_departure`, `total_arrival`, dan `total_flight` sebagai score popularity berdasarkan movement aktual yang sudah masuk SQLite.

`MAPPING_PILOT` adalah master pilot yang dinormalisasi dari master crew internal. Tab ini dipakai untuk menjembatani nama lengkap pilot dengan cara penulisan di grup Ops seperti `Capt.PFK`, `Capt. Oscar K`, `Fo. NRB`, atau `FO. Juven`.

Field crew yang dipakai di silver dan output enrichment:

```text
pic_name
sic_name
crew_text
```

`pic_name` diambil dari label `PIC`, `sic_name` dari label `SIC`, dan `crew_text` menyimpan ringkasan crew yang dekat dengan teks sumber. Field ini biasanya tersedia di pesan `MVT Dept`; pada pesan arrival field ini boleh kosong.

Pada tahap parser awal, `pic_name` dan `sic_name` tetap disimpan apa adanya dari pesan Ops. `SORTIE_LOG` sudah melakukan exact mapping konservatif ke master; AI hanya boleh menangani alias yang belum match atau ambigu dengan audit yang jelas.

### Alur E2E Target

```text
1. Hubungkan, baca, simpan
   grup WhatsApp -> tabel raw_messages -> tab RAW

2. Parse dan normalisasi
   tabel raw_messages -> tabel flight_movements -> tab FLIGHT_RAW

3. Bangun gold productivity
   MVT Dept + Takeoff -> dedup -> match Arrival ACK -> quality gate -> tab SORTIE_LOG

4. Pembersihan AI lanjutan
   field bermasalah -> endpoint prompt -> gerbang validasi -> patch gold yang dapat diaudit
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

Kontrak prompt awal disimpan di:

```text
prompts/flight_ops_deepclean_v1.md
```

File prompt ini harus dianggap sebagai sumber resmi untuk versi `flight_ops_deepclean_v1`. Jika strategi pembersihan berubah, buat versi prompt baru, jangan mengubah perilaku lama secara diam-diam.

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
- `cleaned`: sudah berhasil masuk area enrichment dan tidak diproses ulang normal.
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

Field kandidat enrichment yang direkomendasikan:

```text
schema_version
prompt_version
movement_id
raw_message_id
operation_date
movement_type
registration
aircraft_type
flight_seq
pic_name
sic_name
crew_text
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
source_trace
source_text
deepcleaned_at
deepclean_model
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
- `FLIGHT_OPS`: kandidat enrichment per movement hasil pembersihan AI.
- `FLIGHT_TIMELINE`: view kronologi actual departure/arrival.
- `MASTER_IATA`: master airport internal dan score traffic.
- `MAPPING_PILOT`: master pilot dan kandidat alias/initial crew.

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
- Versi prompt dan model AI tercatat di setiap baris enrichment.

## PRD: SORTIE_LOG Final Dataset

### Tujuan dan Grain

`SORTIE_LOG` adalah gold dataset utama untuk dashboard produktivitas operasional unscheduled/non-scheduled.

```text
1 row = 1 sortie / mission
1 MVT Dept dengan Takeoff = 1 sortie
```

Route `TIM-BEO-TIM` tetap dihitung satu sortie, walaupun memiliki dua leg. `FLIGHT_RAW` boleh tetap menyimpan dua leg untuk audit, tetapi payload, pax, cargo, dan total load hanya dihitung satu kali di `SORTIE_LOG`.

Dataset ini digunakan untuk menjawab:

- sortie per hari, aircraft, route, dan base;
- total pax, cargo, dan load;
- PIC dan SIC yang bertugas;
- mission dengan arrival ACK;
- mission yang diasumsikan selesai tanpa ACK;
- mission yang perlu review karena ada bukti abnormal.

### Sumber Kebenaran

Urutan otoritas data:

```text
1. MVT Dept + Takeoff      bukti utama mission terjadi
2. MVT Arrival            ACK atau konfirmasi tambahan
3. RAW WhatsApp message   sumber audit dan trace
```

Arrival tanpa departure match tidak membuat sortie baru. Row tersebut masuk `ARRIVAL_ACK_EXCEPTIONS`.

### Input

- tabel SQLite `raw_messages`;
- tabel SQLite `flight_movements` hasil parser;
- `data/reference/mapping_pilot.json` untuk normalisasi PIC/SIC;
- `data/reference/master_iata.json` untuk enrichment airport yang tersedia.

### Field Utama

```text
mission_id
operation_date
registration
aircraft_type
aircraft_type_quality
flight_seq
route_full
route_type
from
to
via
pic_raw
pic_full
sic_raw
sic_full
departure_time_z
departure_datetime_local
departure_datetime_utc
pax
pax_total
pax_weight_kg
baggage_kg
cargo_text
cargo_kg
cargo_ton
cargo_quality
total_load_kg
total_load_ton
arrival_ack_status
arrival_time_z
arrival_datetime_local
arrival_datetime_utc
mission_status
confidence_level
completion_basis
productivity_include_flag
load_quality
pax_quality
abnormal_flag_same_aircraft_date
abnormal_evidence_count
abnormal_evidence_sample
departure_raw_message_id
arrival_raw_message_id
departure_movement_id
arrival_movement_id
parse_confidence
issue_notes
```

### Status Mission

```text
completed_ack_received
  MVT Dept + Takeoff valid dan Arrival ACK berhasil dimatch.
  confidence_level = HIGH
  productivity_include_flag = TRUE

assumed_completed_no_arrival_ack
  MVT Dept + Takeoff valid, ACK tidak ada, dan bukti abnormal tidak ditemukan.
  confidence_level = HIGH_ASSUMED
  productivity_include_flag = TRUE

needs_review_departure_no_ack_abnormal_found
  MVT Dept + Takeoff valid, ACK tidak ada, dan ada bukti abnormal pada aircraft/date.
  confidence_level = REVIEW
  productivity_include_flag = FALSE
```

### Matching dan Dedup

Departure didedup dengan kunci:

```text
operation_date + registration + flight_seq + route_full + departure_time_z
```

Jika ada repost/revisi, builder mempertahankan row dengan kelengkapan field terbaik lalu `raw_message_id` terbaru sebagai tie-breaker.

Arrival ACK dimatch one-to-one berdasarkan:

```text
operation_date + registration + flight_seq + final destination route
```

Fallback terbatas:

- departure tanpa `flight_seq` memilih ACK terdekat dengan registration/date/final destination yang sama dan menulis alasan fallback ke `issue_notes`;
- selisih arrival hingga 30 menit sebelum departure masih diterima tetapi diberi `issue_notes`, karena ada data lapangan dengan timestamp typo;
- token route gabung enam karakter seperti `MGLTMH` dinormalisasi menjadi `MGL-TMH`.

Route mismatch tidak diterima hanya karena registration/date/flight sama.

### Aturan Produktivitas dan Quality

- `pax_total` menjumlahkan tiga angka utama dan angka FOC dalam catatan pax. Contoh `09/01/00(1 FOC SCA)` menjadi 11.
- `total_load_kg` memakai nilai total load dari departure raw. Nilai ini tidak dihitung ulang dari komponen agar payload tidak double count.
- `total_load_ton = total_load_kg / 1000`.
- `cargo_ton = cargo_kg / 1000`.
- `load_quality` bernilai `COMPLETE`, `PARTIAL`, atau `MISSING`.
- `pax_quality` bernilai `COMPLETE`, `MISSING`, atau `NEEDS_REVIEW`.
- `cargo_quality` bernilai `COMPLETE`, `MISSING`, atau `NEEDS_PARSE`.
- `aircraft_type_quality` bernilai `FROM_DEPARTURE_RAW` atau `MISSING`.

Field kosong tidak boleh ditebak tanpa bukti raw/master. Data tersebut tetap diberi quality flag untuk deepclean atau review berikutnya.

### Jadwal dan Command

Build lokal tanpa menulis Google Sheets:

```bash
npm run sortie:build
```

Validasi baseline 12-18 Juni 2026:

```bash
npm run sortie:check:12-18
```

Build dan replace tiga tab Google Sheets:

```bash
npm run sortie:sync
```

Saat service `new-spirit-sheets-sync.service` hidup, gold dataset di-refresh setiap satu jam. Konfigurasi:

```text
SORTIE_LOG_AUTO_SYNC=1
SORTIE_LOG_REFRESH_SECONDS=3600
SORTIE_LOG_FROM_DATE=2026-06-12
```

Output lokal audit berada di `data/derived/` dan tidak ikut GitHub.

### Validasi Baseline 12-18 Juni 2026

Hasil builder dari SQLite lokal saat implementasi:

```text
Total sortie                         210
Duplicate mission_id                  0
Completed dengan Arrival ACK        190
Assumed completed tanpa ACK          19
Needs review                          1
Productivity include                209
Pax total termasuk FOC              772
Row dengan total_load_kg             190
Total load yang terbaca          189.340 kg
Total cargo yang terbaca         116.343 kg
```

Angka status mission dan pax sesuai baseline PRD. Angka load/cargo lama `195.670 kg` dan `117.638 kg` tidak dapat direproduksi dari departure silver yang tersimpan saat ini. Pipeline tidak mengarang nilai untuk menutup selisih; row yang belum lengkap ditandai melalui quality flag dan exception audit.

### Kriteria Penerimaan

- semua sortie memiliki `departure_raw_message_id` dan Takeoff;
- tidak ada duplicate `mission_id`;
- field wajib registration, route, from, to, PIC raw, dan departure time terisi;
- arrival-only tidak membuat sortie baru;
- route multi-leg tetap satu row sehingga payload tidak double count;
- assumed completed selalu `HIGH_ASSUMED`;
- no-ACK dengan evidence abnormal selalu `REVIEW` dan dikeluarkan dari productivity;
- setiap row dapat ditelusuri kembali ke raw message dan movement source.

## Struktur Repo

```text
src/                         Worker Node/Baileys untuk WhatsApp
app/                         Service Python, parser, dan Google Sheets sync
bin/                         Script runner berulang dan migrasi data lokal
tests/                       Unit test pipeline gold SORTIE_LOG
config/airport_mappings.json Mapping airport cadangan
config/google-sheets.env.example
integrations/google-sheets-webhook.gs
```

Data operasional lokal, credential WhatsApp, pesan, database, log, dan PID tidak ikut git. Pengecualian khusus: `data/reference/master_iata.json` dan `data/reference/mapping_pilot.json` ikut git sebagai master referensi non-credential.

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
FLIGHT_OPS
FLIGHT_TIMELINE
SORTIE_LOG
ARRIVAL_ACK_EXCEPTIONS
ABNORMAL_EVIDENCE_AUDIT
MASTER_IATA
MAPPING_PILOT
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
GOOGLE_SHEETS_FLIGHT_OPS_TAB=FLIGHT_OPS
GOOGLE_SHEETS_FLIGHT_TIMELINE_TAB=FLIGHT_TIMELINE
GOOGLE_SHEETS_MASTER_IATA_TAB=MASTER_IATA
GOOGLE_SHEETS_MAPPING_PILOT_TAB=MAPPING_PILOT
GOOGLE_SHEETS_SORTIE_LOG_TAB=SORTIE_LOG
GOOGLE_SHEETS_ARRIVAL_EXCEPTION_TAB=ARRIVAL_ACK_EXCEPTIONS
GOOGLE_SHEETS_ABNORMAL_AUDIT_TAB=ABNORMAL_EVIDENCE_AUDIT
OPS_OPERATION_TIMEZONE=Asia/Jakarta
MASTER_IATA_AUTO_SYNC=1
MASTER_IATA_REFRESH_SECONDS=1800
MASTER_IATA_SOURCE_SPREADSHEET_ID=1nLd3kkkSWJFCUjjR3kph7Wjsv0v_gN8VvXWptGmS5NE
MASTER_IATA_SOURCE_GID=980038686
MAPPING_PILOT_AUTO_SYNC=1
MAPPING_PILOT_REFRESH_SECONDS=3600
MAPPING_PILOT_SOURCE_SPREADSHEET_ID=1fAUbyfFrMw5VPK2hb_Ocg3-xjLjr_x6p
OPS_PILOT_CALLSIGN_PATH=data/reference/pilot_callsigns.json
SORTIE_LOG_AUTO_SYNC=1
SORTIE_LOG_REFRESH_SECONDS=3600
SORTIE_LOG_FROM_DATE=2026-06-12
```

File `config/google-sheets.env` tidak boleh di-commit.

Buat atau pastikan tab utama tersedia di Google Sheets:

```bash
npm run sheets:ensure
npm run master:iata:sync
npm run mapping:pilot:sync
npm run sortie:sync
```

Jika Sheet lama masih punya tab legacy, hapus hanya setelah `RAW`, `FLIGHT_RAW`, `FLIGHT_OPS`, `FLIGHT_TIMELINE`, `MASTER_IATA`, dan `MAPPING_PILOT` sudah benar:

```bash
npm run sheets:delete-legacy
```

Command ini mencoba menghapus tab legacy berikut:

```text
Movements_Internal
Movements
Schedules
```

## Command Suite Worker

Gunakan nomor WhatsApp khusus worker. Jangan gabungkan sesi perangkat tertaut worker ini dengan WhatsApp Web lain yang aktif untuk kebutuhan berbeda.

Start atau reconnect semua worker:

```bash
./connect.sh
```

Command ini akan menghentikan worker lama, menampilkan QR jika diperlukan, lalu menjalankan semua proses di background. Setelah berhasil, terminal boleh ditutup.

Paksa scan QR ulang:

```bash
./connect.sh --reset
```

Cek status:

```bash
./status.sh
```

Stop semua worker dan disable auto-start:

```bash
./stop.sh
```

Command `./connect.sh` akan mencoba memakai systemd user agar worker tahan reboot. Jika systemd user tidak tersedia di session tersebut, script tetap menjalankan fallback detached background. Fallback ini cukup untuk terminal/Codex session mati, tetapi tidak hidup lagi otomatis setelah reboot.

Install atau update systemd user service tanpa start worker:

```bash
npm run workers:install-systemd
```

Command npm ekuivalen:

```bash
npm run workers:connect
npm run workers:status
npm run workers:stop
```

Status utama ada di:

```text
data/listener-status.json
data/google-sheets-movement-sync-state.json
```

Log fallback jika systemd user tidak tersedia:

```text
data/ingest-loop.log
data/sheets-sync-loop.log
data/listener-loop.log
```

Log systemd jika service aktif:

```bash
journalctl --user -u new-spirit-listener.service -f
journalctl --user -u new-spirit-ingest.service -f
journalctl --user -u new-spirit-sheets-sync.service -f
```

## Mode Debug Manual

Command di bawah hanya untuk debugging. Operasional harian harus memakai `./connect.sh`.

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
latitude_deg
longitude_deg
```

Prioritas mapping:

1. `data/reference/master_iata.json`
2. `config/airport_mappings.json`
3. fallback belum terpetakan atau teks bebas

File `data/reference/master_iata.json` dan `data/reference/mapping_pilot.json` sengaja dikecualikan dari ignore rule agar ikut ke GitHub sebagai master referensi. File runtime lain di folder `data/` tetap di-ignore.

Untuk membuat atau refresh tab `MASTER_IATA` di Google Sheets ops dari source sheet master:

```bash
npm run master:iata:sync
```

Command ini:

```text
1. Download CSV dari source Google Sheet master IATA
2. Normalisasi casing code, icao_code, airport_name, location, province_name
3. Simpan hasil lokal ke data/reference/master_iata.json
4. Replace tab MASTER_IATA di spreadsheet ops
```

Saat `npm run sheets:sync:loop` berjalan, `MASTER_IATA` ikut di-refresh otomatis secara periodik. Default interval:

```text
MASTER_IATA_REFRESH_SECONDS=1800
```

Artinya `MASTER_IATA` dihitung ulang setiap 30 menit. Refresh ini sengaja tidak dibuat trigger per message karena command akan menghitung ulang traffic dan replace seluruh tab master; update periodik lebih aman terhadap limit Google Sheets dan menghindari race dengan sync utama.

Untuk menonaktifkan auto-refresh master:

```bash
MASTER_IATA_AUTO_SYNC=0
```

Kolom koordinat `latitude_deg` dan `longitude_deg` disiapkan tetapi masih kosong sampai ada proses enrichment koordinat.

Tab `MASTER_IATA` juga membawa ringkasan traffic dari `FLIGHT_TIMELINE`/movement aktual:

```text
total_departure = jumlah actual_departure dengan origin code tersebut
total_arrival   = jumlah actual_arrival dengan destination code tersebut
total_flight    = total_departure + total_arrival
```

Metric ini dipakai sebagai score popularity airport/lokasi dalam seluruh mission yang sudah tersimpan di SQLite lokal.

Dedup yang diterapkan otomatis masih konservatif. Worker hanya menandai duplicate exact `code + icao_code`; tidak ada row yang dihapus. Kolom kontrol:

```text
canonical_airport_id
dedup_status
dedup_reason
dedup_group_key
```

Nilai `dedup_status`:

```text
unique     tidak masuk group duplicate exact
canonical  row utama untuk group duplicate exact
duplicate  row duplikat yang menunjuk ke canonical_airport_id
```

Pemilihan canonical memakai prioritas: `total_flight` tertinggi, nama yang lebih informatif, lalu `id` paling kecil sebagai tie-breaker.

## Master Pilot dan Mapping Initial

Master pilot internal disimpan sebagai referensi lokal:

```text
data/reference/mapping_pilot.json
data/reference/pilot_callsigns.json
```

Untuk membuat atau refresh tab `MAPPING_PILOT` dari master pilot internal:

```bash
npm run mapping:pilot:sync
```

Command ini:

```text
1. Download XLSX `master_pilot.xlsx` dari folder master Drive
2. Normalisasi casing nama pilot dan metadata rating
3. Merge call sign dan nomor CPL dari `CALL SIGN SCREW SCA.pdf`
4. Membuat kolom initial_1 sampai initial_4
5. Memasukkan call sign resmi ke match_keys untuk alias lapangan seperti EVT, MDK, YST, JNY, dan SMP
6. Menghitung raw crew value yang sudah pernah muncul di SQLite
7. Simpan hasil lokal ke data/reference/mapping_pilot.json
8. Replace tab MAPPING_PILOT di spreadsheet ops
```

Kolom inti:

```text
pilot_name
rank
position
rating
call_sign
cpl_number
callsign_match_method
callsign_source
initial_1
initial_2
initial_3
initial_4
match_keys
observed_raw_values
observed_raw_count
mapping_status
mapping_confidence
```

`call_sign` berasal dari dokumen resmi `CALL SIGN SCREW SCA.pdf` tanggal 6 Mei 2026. Sebanyak 94 call sign berhasil dimatch ke master pilot. Tiga nama, yaitu `JDH`, `JMR`, dan `DPP`, masih disimpan sebagai audit unmatched karena belum memiliki row yang dapat dipastikan di `master_pilot.xlsx`.

`initial_1` adalah initial hasil derivasi nama lengkap, sedangkan `call_sign` adalah alias operasional resmi. Keduanya dimasukkan ke `match_keys`. Jika alias Ops match unik tetapi rank pada pesan berbeda dari rank master, mapping tetap diperbolehkan karena captain dapat dilaporkan sebagai SIC; keunikan pilot tetap menjadi syarat.

Saat `npm run sheets:sync:loop` berjalan, `MAPPING_PILOT` ikut di-refresh otomatis secara periodik. Default interval:

```text
MAPPING_PILOT_REFRESH_SECONDS=3600
```

Artinya `MAPPING_PILOT` dihitung ulang setiap 1 jam. Refresh ini cukup periodik karena master pilot dan alias crew tidak perlu dihitung ulang setiap pesan masuk.

## Sinkronisasi ke Google Sheets

Pastikan tab bronze, silver, master, dan gold sudah ada:

```bash
npm run sheets:ensure
npm run master:iata:sync
npm run mapping:pilot:sync
npm run sortie:sync
```

Kirim pesan mentah dan movement rows yang belum tersinkron satu kali:

```bash
npm run sheets:sync
```

Jalankan sinkronisasi terus-menerus:

```bash
npm run sheets:sync:loop
```

Loop ini menjalankan empat proses dalam satu service:

```text
1. sync utama RAW / FLIGHT_RAW / FLIGHT_TIMELINE secara kontinu
2. refresh MASTER_IATA periodik sesuai MASTER_IATA_REFRESH_SECONDS
3. refresh MAPPING_PILOT periodik sesuai MAPPING_PILOT_REFRESH_SECONDS
4. rebuild SORTIE_LOG dan tab audit periodik sesuai SORTIE_LOG_REFRESH_SECONDS
```

Jika schema `FLIGHT_RAW` berubah dan tab perlu dibangun ulang dari SQLite lokal:

```bash
npm run sheets:replace-flight-raw
```

Command ini menghapus tab `FLIGHT_RAW`, membuatnya lagi dengan header terbaru, lalu mengisi ulang semua movement dari `data/ops_messages.sqlite3`. Jalankan saat sync loop sedang berhenti agar tidak ada dua proses yang menulis Google Sheet bersamaan. Gunakan hanya jika sudah yakin SQLite lokal adalah sumber data yang benar untuk silver dataset.

Untuk membangun ulang view rotasi pesawat:

```bash
npm run sheets:replace-flight-timeline
```

`FLIGHT_TIMELINE` memakai kolom `timeline_sort_key` sebagai urutan utama. Cara baca yang disarankan di Google Sheets:

```text
1. Filter operation_date
2. Filter registration
3. Sort timeline_sort_key ascending
```

Untuk membaca hasilnya, fokus ke kolom ini:

```text
event_datetime_local
timeline_kind
route_leg
event_time
event_time_source
```

`event_datetime_local` adalah waktu event aktual dalam timezone operasi. `event_time` adalah waktu mentah dari pesan ops, biasanya waktu Z, sehingga bisa terlihat mundur tanggal jika dibaca tanpa konteks timezone.

`timeline_kind = actual_departure` berasal dari pesan departure yang memiliki `takeoff_time`. `timeline_kind = actual_arrival` berasal dari pesan arrival yang memiliki `ata_time`. Planned return leg dari route seperti `MKQ-EWE-MKQ` tetap tersimpan di `FLIGHT_RAW`, tetapi tidak dimasukkan ke timeline utama selama belum ada event aktualnya.

Setelah tab dibuat, sync loop reguler ikut menambahkan row baru ke `FLIGHT_TIMELINE`. Jika urutan visual di Sheet terlihat berubah karena row baru di-append di bawah, sort ulang kolom `timeline_sort_key` ascending.

Jika tetap membaca dari `FLIGHT_RAW`, gunakan `chronology_sort_key` sebagai urutan utama setelah memfilter `registration`. Kolom ini memakai fallback `operation_date` dari tanggal pesan WA jika teks arrival tidak mencantumkan tanggal eksplisit.

Status sinkronisasi disimpan di:

```text
data/google-sheets-movement-sync-state.json
```

Hapus file status ini hanya jika memang ingin menambahkan ulang semua row `RAW`, `FLIGHT_RAW`, dan `FLIGHT_TIMELINE`.

## Menjalankan Semua Worker

Untuk operasi normal, gunakan:

```bash
./connect.sh
```

Cara lama di bawah hanya untuk debugging karena terminal harus tetap dijaga manual:

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
- `data/reference/mapping_pilot.json`
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
- `FLIGHT_OPS` hanya boleh berisi kandidat enrichment yang sudah lolos validasi dan tidak menjadi source count productivity.
- Data operasional lokal boleh dimigrasikan antar server, tetapi jangan dipublikasikan ke repo publik.
