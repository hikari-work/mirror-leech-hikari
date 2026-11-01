# Plan Migrasi: Python (kurigram) → Rust (grammers)

**Deliverable turn ini: dokumen ini saja. Tidak ada satu baris code pun yang diubah.**

Dibuat: 2026-09-22 · Baseline commit: `c27502f` · Diukur pada working tree saat itu.

Angka di bawah hasil hitung, bukan kira-kira. Ukur ulang sebelum memakai:

```bash
find bot web -name '*.py' | xargs wc -l | tail -1
```

---

## 0. Ringkasan keputusan — baca ini dulu

Tiga temuan yang mengubah bentuk rencana:

1. **Lapisan Telegram cuma 17% dari code.** 4.831 dari 28.677 baris. Sisanya
   adalah orkestrasi download, scraper, state machine task, UI settings, dan DB
   — bagian yang grammers sama sekali tidak sentuh. Mengganti kurigram dengan
   grammers **bukan** migrasi ini; itu satu fase dari dua belas.

2. **Ada empat ganjalan yang Rust tidak selesaikan** (§3), dan satu di antaranya
   — yt-dlp — secara praktis tidak akan pernah hilang. Rencana ini menerima
   kenyataan itu dan menjadikannya subprocess permanen, bukan menunda-nunda
   keputusannya.

3. **grammers 0.10.0 tidak punya progress callback sama sekali.** Diverifikasi
   di source (`files.rs`): tidak ada parameter progress di `upload_file`,
   `upload_stream`, `download_media`, maupun `iter_download`. Padahal seluruh
   status bot ini — `TelegramStatus`, `update_status_message`, `-su` streaming —
   digantung di situ. Ini pekerjaan yang harus ditulis sendiri, bukan dipetakan.

**Rekomendasi: strangler-fig, bukan big-bang rewrite.** Dan sebelum apa pun,
kerjakan Fase 0 — ada satu pertanyaan yang, kalau jawabannya "tidak", membatalkan
seluruh strategi strangler (§5, Fase 0, item S6).

---

## 1. Inventaris terukur

### 1.1 Bobot per lapisan

| Lapisan | Baris | File kunci | Nasib di Rust |
|---|---:|---|---|
| Telegram transport | 4.831 | `core/telegram_manager.py`, `core/handlers.py`, `helper/telegram/*`, `helper/upload/*`, `helper/download/telegram_download.py` | **Ditulis ulang** dengan grammers |
| Modul perintah / UI | 6.488 | `modules/*` (settings, help, status, rss) | Ditulis ulang, murni glue |
| Scraper direct-link | 3.969 | `helper/download/direct_link_generators/hosts/*` (20 host) | **Dipertahankan di Python** (§3.2) |
| Downloader engine lain | 4.092 | `aria2_download`, `qbit_download`, `mega_download`, `yt_dlp_download`, `vidara`, `pornhub` | Campur: RPC porting + subprocess |
| Task engine | 3.394 | `helper/task/*`, `helper/listeners/*` | Ditulis ulang — **inti nilai migrasi** |
| Util | 3.192 | `helper/util/*` | Ditulis ulang sebagian, sebagian mati sendiri |
| Storage / DB | 1.520 | `helper/storage/db_handler.py` (1.055) | Ditulis ulang dengan `sqlx` |
| Progress / status | 665 | `helper/progress/*` | Ditulis ulang, terikat §2.2 |
| Web server | 464 | `web/wserver.py`, `web/nodes.py` | Ditulis ulang dengan `axum` |
| **Total produksi** | **28.677** | | |
| Test | 19.521 | 61 file di `tests/` | §7 |

### 1.2 Permukaan API Telegram yang benar-benar dipakai

Bagus: **nol pemakaian `raw.functions` / `raw.types`.** Semua lewat API level
tinggi kurigram. Artinya tidak ada utang MTProto mentah yang harus dibawa.

Simbol yang diimpor (47 file menyentuh `pyrogram`):

```
Client(8)  FloodWait(7)  FloodPremiumWait(7)  Message(5)  User(4)  Chat(3)
CallbackQuery(3)  InlineKeyboardMarkup(2)  InputMediaPhoto(2)  MessageHandler(2)
enums(2)  filters.regex(2)  filters.command  CallbackQueryHandler  BotCommand
InputMediaVideo  InputMediaDocument  ReplyParameters  LinkPreviewOptions
ChatAction  ChatMember  RPCError  BadRequest  Str
```

Method client yang dipanggil: `add_handler`, `remove_handler`, `start`, `stop`,
`restart`, `get_messages`, `send_message`, `edit_message_text`,
`delete_messages`, `send_media_group`, `send_chat_action`, `set_bot_commands`,
`get_users`, `get_chat`, `get_chat_member`, `copy_message`, `copy_media_group`,
`stream`, `stop_transmission`, `log_out`, plus `send_document` / `send_video` /
`send_audio` / `send_photo` dengan `progress=` di `telegram_uploader.py`.

Permukaan ini **kecil dan tertutup** — itu kabar baik terbesar dalam dokumen ini.
Seluruhnya muat dalam satu crate adaptor (§4.2).

---

## 2. grammers 0.10.0 — apa yang ada, apa yang tidak

Diverifikasi terhadap docs.rs 0.10.0 dan source `client/files.rs`, bukan ingatan.

- Versi terbaru **0.10.0**, rilis **2026-07-02**. Repo pindah ke
  `codeberg.org/Lonami/grammers`. 182k unduhan total.
- Masih **0.x**: 0.8.0 (Okt 2025) → 0.9.0 (Feb 2026) → 0.10.0 (Jul 2026). Tiga
  rilis breaking dalam sembilan bulan. Pin versi keras dan anggarkan biaya
  upgrade tiap ~6 bulan. Dokumentasi tercatat **62,94%** — sisanya baca source.

### 2.1 Yang dipetakan langsung

| Butuh | grammers 0.10 |
|---|---|
| Client + login bot | `Client::bot_sign_in(token, api_hash)` |
| Login user + 2FA | `request_login_code` → `sign_in` → `check_password` |
| Kirim / edit / hapus pesan | `send_message`, `edit_message`, `delete_messages` |
| Album | `send_album(peer, Vec<InputMedia>)` |
| Tombol inline | `message::Button`, `message::ReplyMarkup` di `InputMessage` |
| Callback query | `update::CallbackQuery` |
| Ambil pesan by id | `get_messages_by_id` |
| Parse mode HTML | `InputMessage::html()` (butuh feature `html5ever`) |
| Chat action (typing/upload) | `Client::action(peer).repeat(..)` |
| FloodWait otomatis | `ClientConfiguration` + `AutoSleep` / `RetryPolicy` |
| Atribut media | `media::Attribute` (durasi, w/h, judul, performer) |
| Panggilan MTProto mentah | `Client::invoke(&request)` — di luar jaminan semver |

### 2.2 Yang TIDAK ada — harus ditulis sendiri

Ini daftar pekerjaannya, bukan daftar keluhan. Tiap baris punya jalan keluar.

| Hilang | Dipakai di | Jalan keluar |
|---|---|---|
| **Progress callback upload/download** | seluruh `helper/progress/`, `telegram_uploader`, `telegram_download` | Bungkus `AsyncRead` dengan counting reader lalu `upload_stream`; untuk download pakai `iter_download` dan hitung chunk sendiri. **Catatan akurasi:** upload dibaca 4 worker × 512 KiB di depan, jadi byte terbaca melebihi byte ter-ACK sampai ~2 MiB. Untuk progress bar tidak masalah; untuk akuntansi jangan. |
| **`stop_transmission()`** | `telegram_download.py:68` (batal task) | Batalkan lewat `CancellationToken` di reader/stream, atau drop future-nya. Perlu didesain eksplisit, tidak gratis. |
| **`set_bot_commands`** | `core/startup.py` | `invoke(tl::functions::bots::SetBotCommands)` mentah. |
| **`copy_message` / `copy_media_group`** | `modules/copy.py` | Tidak ada padanan. Implementasi: baca media sumber → kirim ulang sebagai `InputMedia` referensi (tanpa unduh), atau `forward_messages` kalau perilaku forward diterima. **Perubahan perilaku — konfirmasi ke pemilik produk.** |
| **Dispatcher / filter** | `core/handlers.py` (table-driven) | Tidak ada. `stream_updates()` memberi `UpdateStream` mentah; router ditulis sendiri. Kabar baik: `handlers.py` sudah table-driven, jadi tabelnya tinggal dipindah bentuk. |
| **`Client.stream()`** (baca media jadi iterator) | `telegram_download.py` | `iter_download` sudah cukup. |
| **Cache peer otomatis** | implisit di mana-mana | grammers menuntut peer sudah ter-cache sebelum dipakai; dokumen resminya menyarankan `iter_dialogs` sekali setelah login. **Bot tidak punya dialog** (`iter_dialogs` error untuk bot) — jadi setiap `chat_id` harus di-resolve dan disimpan sendiri. Ini sumber bug "Could not find input entity" klasik. Anggarkan satu modul khusus. |

### 2.3 Performa transfer — angka, bukan asumsi

Dari source `grammers-client-0.10.0/src/client/files.rs`:

```rust
pub const MAX_CHUNK_SIZE: i32 = 512 * 1024;
const BIG_FILE_SIZE: usize = 10 * 1024 * 1024;
const WORKER_COUNT: usize = 4;
```

- File > 10 MiB → `SaveBigFilePart`, **4 worker konkuren** lewat
  `FuturesUnordered` (bukan `tokio::spawn`), part 512 KiB, tanpa MD5.
- File ≤ 10 MiB → loop sekuensial + MD5.
- Download > 10 MiB → 4 worker, kali ini `tokio::task::spawn` betulan.

Bandingkan: `telegram_manager.py` memakai `max_concurrent_transmissions=10`.
Angkanya beda semantik (kurigram: transfer file konkuren; grammers: part
konkuren dalam satu file), jadi **tidak bisa disimpulkan mana yang lebih cepat
dari membaca code.** Fase 0 harus mengukurnya (item S4). `WORKER_COUNT` di
grammers adalah konstanta privat — kalau 4 kurang, jalannya fork atau tulis
sendiri di atas `invoke_in_dc`.

---

## 3. Empat ganjalan yang Rust tidak selesaikan

### 3.1 yt-dlp — permanen, terima saja

Dipakai di 5 file produksi (`yt_dlp_download.py`, `yt_dlp_hooks.py`,
`pornhub_download.py`, `vidara_download.py`, `modules/ytdlp.py`). Tidak ada
padanan Rust dan tidak akan ada — nilainya ada di ~15.000 baris extractor yang
diperbarui tiap minggu.

**Keputusan: yt-dlp tetap binary subprocess, selamanya.** Konsekuensi konkret:

- `progress_hooks` in-process **hilang**. Ganti: `--newline` +
  `--progress-template '%(progress)j'` lalu parse JSON per baris dari stdout.
- `YoutubeDL().extract_info()` untuk pemilihan format (`modules/ytdlp.py`)
  ganti `yt-dlp --dump-single-json`, lalu pilih format di Rust.
- Pembatalan: kirim SIGTERM ke child, jangan andalkan exception.
- `cookies.txt` dan opsi `-opt` user diteruskan sebagai argumen CLI — perlu
  **sanitasi ketat**, karena sekarang opsi itu berasal dari input user
  (`S307`/`eval` di `ytdlp.py` sudah jadi utang lint; jangan pindahkan bentuk
  bahayanya ke argumen shell).

### 3.2 cloudscraper — pertahankan Python

20 host scraper (3.969 baris) bergantung pada `cloudscraper` + `lxml` untuk
menembus Cloudflare. Rust tidak punya padanan sematang itu. `rquest` (impersonasi
TLS/JA3) ada tapi menutup sebagian kasus saja.

**Keputusan: `direct_link_generators/` tetap Python, jadi sidecar.** Alasannya
bukan cuma cloudscraper: ini code dengan churn tertinggi dan nilai-per-baris
terendah di repo — dia berubah karena situs orang lain berubah, bukan karena
arsitektur bot. Menulis ulang 20 scraper ke Rust berarti membayar ulang seluruh
sejarah bug-fix `bunkr.py`, `terabox.py`, `vidara.py` tanpa dapat apa-apa.

Bentuk sidecar: proses Python kecil, HTTP lokal atau unix socket, satu endpoint
`POST /resolve {link}` → `{direct_url, headers, filename, size}`. Kontraknya
sudah ada di `registry.py` (`resolve(domain, link)`), jadi seam-nya bersih.

> Catatan: pola ini sudah dipakai di repo. `helper/net/mega_client.py` bicara ke
> gateway HTTP, bukan SDK Mega in-process. Jadi Mega **sudah portabel** — tinggal
> ganti klien HTTP-nya. Satu ganjalan yang ternyata bukan ganjalan.

### 3.3 aria2 & qBittorrent — porting RPC, bukan ganjalan

`aioaria2` (JSON-RPC via websocket) dan `aioqbt` (Web API) dipakai di
`core/torrent_manager.py`, `listeners/*`, `download/*`. Keduanya protokol biasa
— porting ke `tokio-tungstenite` + `reqwest` itu pekerjaan lurus, bukan riset.
Jangan pakai crate pihak ketiga yang tidak terawat; permukaan yang dipakai bot
ini sempit, tulis klien tipis sendiri.

### 3.4 Ekosistem kecil yang butuh padanan

| Python | Rust | Catatan |
|---|---|---|
| `psycopg` + pool | `sqlx` (postgres, async) | Query sudah raw SQL tanpa ORM — porting relatif lurus |
| `fastapi` + `gunicorn`/`uvicorn` | `axum` + `tokio` | 464 baris, paling mudah |
| `apscheduler` | `tokio-cron-scheduler` atau loop manual | Pemakaiannya tipis |
| `Pillow` | `image` crate, atau serahkan ke ffmpeg | Cuma untuk thumbnail |
| `boto3` | `aws-sdk-s3` | `s3_uploader.py` 350 baris |
| `telegraph` | HTTP biasa (`reqwest`) | API-nya sederhana |
| `natsort` | `natord` / komparator sendiri | Urutan **harus** identik — kunci test |
| `python-magic` | `infer` / `tree_magic_mini` | Deteksi tipe file |
| ffmpeg / 7z / ffprobe | tetap subprocess | Sudah subprocess sekarang |

---

## 4. Strategi

### 4.1 Kenapa strangler, bukan big-bang

Big-bang berarti: 28.677 baris produksi + 19.521 baris test ditulis ulang
sebelum satu pun user merasakan hasilnya, dengan bot produksi (dua VPS, lihat
memory) berjalan di code yang membeku selama itu. Repo ini punya disiplin tiga
gerbang di `AGENTS.md`; big-bang membuang gerbang itu justru saat paling
dibutuhkan.

Strangler memberi: tiap fase bisa dirilis, di-rollback, dan diukur.

### 4.2 Arsitektur target

```
┌──────────────────────── mltb-rs (workspace Cargo) ────────────────────────┐
│                                                                           │
│  mltb-bin        entrypoint, config, logging, shutdown                    │
│  mltb-tg         adaptor grammers: client, session, router, progress,     │
│                    flood, peer-cache, button   ← seluruh §1.2 di sini     │
│  mltb-task       task engine, listener, queue, status (inti nilai)        │
│  mltb-transfer   uploader, downloader, splitter, media pipeline           │
│  mltb-engine     aria2 RPC, qbit API, yt-dlp subprocess, ffmpeg, mega     │
│  mltb-store      sqlx + Postgres, blob crypto                             │
│  mltb-web        axum (file selector)                                     │
│  mltb-ui         modul perintah, settings, help                           │
└───────────────────────────────────────────────────────────────────────────┘
             │                                        │
             │ unix socket / HTTP lokal               │ subprocess
             ▼                                        ▼
   ┌──────────────────────┐              ┌────────────────────────┐
   │ resolver-py (sidecar)│              │ yt-dlp, ffmpeg, 7z,    │
   │ direct_link_gens +   │              │ aria2c, qbittorrent-nox│
   │ cloudscraper         │              └────────────────────────┘
   └──────────────────────┘
```

Batas `mltb-tg` itu penting: **tidak ada crate lain yang boleh `use grammers_*`
langsung.** Karena grammers masih 0.x dan breaking tiap ~6 bulan, upgrade harus
menyentuh satu crate, bukan dua belas.

### 4.3 Urutan fase, dan alasannya

Diurutkan **risiko rendah + nilai tinggi → risiko tinggi**, sama seperti
`REFACTOR_PLAN.md`. Yang dipindah duluan adalah yang paling diuntungkan Rust
(jalur transfer: CPU, memori, konkurensi) dan paling stabil spesifikasinya.
Yang dipindah terakhir adalah glue UI — banyak baris, nilai Rust hampir nol.

---

## 5. Fase

### Fase 0 — Spike. **Gerbang go/no-go. Jangan lewati.**

Target: satu binary Rust buangan, bukan bagian dari workspace final. Tidak ada
code produksi yang disentuh. Selesai dalam hitungan hari, bukan minggu.

| # | Yang dibuktikan | Gagal artinya |
|---|---|---|
| S1 | Konversi session kurigram → grammers **tanpa login ulang** (§5.1) | Semua user harus `/login` ulang. Pahit tapi bukan pembatal. |
| S2 | Kirim pesan HTML + tombol inline; terima `CallbackQuery`; jawab callback | Pembatal — separuh UI bot ini tombol |
| S3 | Upload file 2 GB dengan progress dari counting reader; batalkan di tengah jalan | Pembatal untuk Fase 4 |
| S4 | **Benchmark**: upload & download 1 GB, grammers vs kurigram saat ini, di VPS produksi | Kalau grammers lebih lambat signifikan, seluruh premis performa gugur |
| S5 | `set_bot_commands` lewat `invoke()` mentah | Kecil, ada jalan lain |
| S6 | **Dua sesi MTProto pada bot token yang sama — apakah update terkirim ke keduanya, atau terbagi?** | **Pembatal strategi strangler** — lihat bawah |

**S6 adalah item terpenting di dokumen ini.** Seluruh rencana strangler
mengandaikan proses Rust dan proses Python bisa hidup berdampingan selama
migrasi. Kalau dua sesi pada bot yang sama membuat update **terbagi** di antara
keduanya, tiap perintah user punya peluang nyasar ke proses yang belum siap
menanganinya — dan itu tidak bisa ditambal.

Mitigasi kalau S6 jelek: **Python tetap satu-satunya dispatcher update.** Rust
masuk sebagai *worker tanpa update* (jangan panggil `stream_updates`), dipanggil
lewat IPC untuk kerja transfer saja. Bentuk ini tetap memberi sebagian besar
nilai (Fase 4–5) dan menunda pemindahan router ke Fase 10.

**Gerbang keluar Fase 0:** tulis hasil enam item di atas ke dokumen ini sebagai
§5.0-hasil, lengkap dengan angka benchmark. Baru lanjut.

#### 5.1 Detail konversi session (S1)

Sudah diverifikasi dari `.venv/lib/python3.14/site-packages/pyrogram/storage/storage.py:39`:

```python
SESSION_STRING_FORMAT = ">BI?256sQ?"
#                        │ │ │ │  │ └─ is_bot   (bool)
#                        │ │ │ │  └──── user_id  (u64)
#                        │ │ │ └─────── auth_key (256 byte)
#                        │ │ └───────── test_mode(bool)
#                        │ └─────────── api_id   (u32)
#                        └───────────── dc_id    (u8)
```

Semua yang grammers butuh ada di situ: `dc_id` + `auth_key` + `user_id`. Di sisi
grammers 0.10, `Session` sudah jadi **trait** (bukan struct seperti versi lama),
dengan `MemorySession` / `SqliteSession` sebagai storage dan `SessionData`,
`types::DcOption`, `types::PeerAuth` sebagai isinya. Bentuk API persisnya
**harus dibaca dari source** — ini yang berubah antar rilis 0.x.

Cakupannya bukan satu session: `USER_SESSION_STRING` disimpan **per user** di
Postgres (`modules/user_login.py:152`, `helper/util/user_sessions.py`). Jadi
konverter harus jalan batch atas seluruh baris user, dan idealnya menyimpan
kedua format berdampingan sampai cutover selesai.

---

### Fase 1 — Fondasi workspace

- `cargo new --lib` per crate di §4.2; pin `grammers-client = "=0.10.0"`.
- Port `core/config_manager.py` → 65 kunci konfigurasi, dengan **validasi saat
  start** (sekarang sebagian gagal diam-diam di runtime).
- Logging (`tracing`), error type (`thiserror`), shutdown rapi.
- CI: `cargo clippy -- -D warnings`, `cargo fmt --check`, `cargo test`, `cargo deny`.
  Ini padanan tiga gerbang `AGENTS.md`; tegakkan dari commit pertama, jangan
  ditambahkan belakangan.
- Dockerfile multi-stage; base `anasty17/mltb` masih dibutuhkan untuk ffmpeg,
  aria2c, qbittorrent-nox, yt-dlp, dan sidecar Python.

**Verifikasi:** binary jalan, baca config, connect DB, exit bersih. Belum ada Telegram.

---

### Fase 2 — `mltb-tg`: session & lifecycle

Target: `core/telegram_manager.py` (155 baris) + `generate_string_session.py`.

- `TgClient` → struct Rust: klien bot, klien user opsional, peta klien per-user
  dengan lock per-user (pola `get_user_client` sekarang sudah benar, pertahankan).
- Konverter session dari Fase 0 jadi code produksi.
- Deteksi premium → `MAX_SPLIT_SIZE` 2 GB / 4 GB.
- **Peer cache** (§2.2) — modul tersendiri, dengan persistensi ke Postgres.

**Verifikasi:** bot login, `get_me` benar, klien user start, premium terdeteksi.

---

### Fase 3 — `mltb-tg`: router, pesan, flood

Target: `core/handlers.py`, `helper/telegram/*` (message_utils, button_build,
filters, flood, conversation, dest_chat, bot_commands).

- `stream_updates()` → router tabel. `handlers.py` sudah table-driven
  (`NamedTuple` per baris), jadi ini pemindahan bentuk, bukan perancangan ulang.
  **Pertahankan urutan dalam tiap jenis handler** — komentar di `handlers.py`
  menjelaskan kenapa: dispatcher berhenti di handler pertama yang cocok.
- `send_message` / `edit_message` / `delete_message` + auto-delete.
- `FloodWait` / `FloodPremiumWait` → `RetryPolicy` + `AutoSleep`. Perhatikan:
  bot pakai `sleep_threshold=0` (bot) dan `60` (user) — semantik ini harus
  direproduksi, bukan diambil default.
- `button_build.py` → `message::Button` / `ReplyMarkup`.
- `test_handlers_table.py` (9,1 KB) adalah spesifikasi routing — terjemahkan
  dulu ke test Rust, baru tulis router-nya.

**Verifikasi:** `/start`, `/help`, satu menu settings bertombol, callback ditangani.

---

### Fase 4 — `mltb-transfer`: uploader ← **nilai tertinggi**

Target: `helper/upload/` (869 + 350 + batcher + flood_pacer + stream_uploader).

- Progress dari counting reader (§2.2), dengan batas akurasi didokumentasikan.
- `send_document` / `send_video` / `send_audio` / `send_photo` → `InputMessage`
  + `media::Attribute` yang tepat. Salah atribut = video jadi dokumen.
- `media_group_batcher.py` → `send_album`.
- `flood_pacer.py` — logika pacing burst; pertahankan apa adanya, sudah teruji.
- `stream_uploader.py` (`-su`) — **hati-hati.** Dokumentasi di file itu
  menjelaskan uploader tidak aman-konkuren dan dilindungi satu coroutine
  konsumen + queue berbatas. Di Rust, ownership justru membuat invariant ini
  bisa **dipaksakan compiler**, bukan didokumentasikan. Ini contoh nyata di mana
  Rust membayar dirinya sendiri di repo ini.
- Pembatalan transfer via `CancellationToken`.

**Verifikasi:** `test_telegram_uploader_album.py` (31 KB),
`test_telegram_uploader_helpers.py` (21 KB), `test_stream_uploader.py`,
`test_media_group_batcher.py` (20 KB) — diterjemahkan, bukan diringkas.

---

### Fase 5 — `mltb-transfer`: downloader Telegram & status

Target: `helper/download/telegram_download.py` (221), `helper/progress/` (665).

- `iter_download` + hitung chunk → progress.
- `get_tg_link_message` (parsing link t.me, `message_utils.py:148`).
- Sembilan kelas status (`aria2_status`, `qbit_status`, `telegram_status`, …)
  punya antarmuka sama → **trait `Status`** di Rust.
- `update_status_message` + interval refresh.

**Verifikasi:** `test_telegram_download.py`, `test_status_view.py`,
`test_status_gid_lookup.py`, `test_torrent_status.py`.

---

### Fase 6 — `mltb-store`

Target: `helper/storage/` (1.520, `db_handler.py` 1.055).

- `psycopg` → `sqlx`. Query sudah raw SQL tanpa ORM, jadi lurus.
- `blob_crypto.py` — **`DB_ENCRYPTION_KEY` tidak boleh berubah.** Kompatibilitas
  ciphertext lama wajib; buktikan dengan test yang mendekripsi blob produksi.
- Skema dibagi dengan proses Python selama transisi — **jangan ubah skema di
  fase ini.**

**Verifikasi:** `test_pg_handler.py` (20 KB), `test_pg_integration.py` (21 KB),
`test_startup_restore.py`. Round-trip enkripsi harus lulus di kedua bahasa.

---

### Fase 7 — `mltb-task`: engine & listener

Target: `helper/task/` + `helper/listeners/` (3.394).

Ini jantungnya: `task_listener.py` (686) punya `on_download_complete`,
`on_upload_complete`, `on_download_error`, `on_upload_error` — tiga di antaranya
masih ber-utang `C901` di `pyproject.toml`. **Jangan pindahkan kompleksitasnya
apa adanya;** state machine task adalah kandidat paling jelas untuk enum + match
di Rust, dan ini satu-satunya fase di mana menulis ulang lebih murah daripada
menerjemahkan.

- Queue (`queued_dl`/`queued_up`, `non_queued_*`) → struktur ber-lock yang
  eksplisit. Global mutable dict di `bot/__init__.py` tidak punya padanan langsung
  dan memang tidak seharusnya punya.
- `multi_link.py`, `batch_tracker.py`, `media_pipeline.py`, `settings_resolver.py`.

**Verifikasi:** `test_settings_resolver.py` (19 KB),
`test_stream_queue_bookkeeping.py` (13 KB), `test_bulk_same_dir.py` (18 KB).

---

### Fase 8 — `mltb-engine`: mesin eksternal

- aria2 JSON-RPC (websocket) dan qBittorrent Web API — klien tipis sendiri (§3.3).
- yt-dlp subprocess + parser `--progress-template` (§3.1).
- ffmpeg / ffprobe / 7z subprocess — `helper/util/media_utils.py` (729) dan
  `subproc_runner.py`.
- Mega lewat gateway HTTP — port klien saja, protokolnya tetap.
- S3 (`aws-sdk-s3`).

**Verifikasi:** `test_qbit_metadata_wait.py`, `test_mega_*.py` (4 file),
`test_s3_uploader.py` (17 KB), `test_ytdlp_*.py` (3 file),
`test_media_probe_cache.py`.

---

### Fase 9 — Sidecar resolver Python

Target: bungkus `direct_link_generators/` (3.969) + `alldebrid_resolver.py`,
`torbox_resolver.py`, `bypass_dispatcher.py`, `url_shortener_bypass.py` jadi
layanan.

- Server kecil (FastAPI sudah ada di repo) di unix socket.
- Kontrak: `POST /resolve` → `{direct_url, headers, filename, size}`, plus
  `/health`. Versikan kontraknya sejak hari pertama.
- Supervisi proses: sidecar mati ≠ bot mati; restart + backoff.
- Test resolver yang ada (`test_direct_link_registry.py` 15 KB,
  `test_bunkr_resolve.py`, `test_vidara_*.py` 3 file, `test_torbox_resolver.py`)
  **tetap jalan apa adanya** — ini keuntungan langsung keputusan §3.2.

---

### Fase 10 — `mltb-ui`: modul perintah

Target: `modules/` (6.488). Fase terbesar per baris, terendah per nilai.

`bot_settings.py` (790) + `users_settings.py` (668) + `settings/` adalah menu
tombol bertingkat — murni glue. `rss/` (7 file) punya `feedparser` yang butuh
padanan (`feed-rs`). `exec.py` dan `shell.py` mengeksekusi code arbitrer;
di Rust `exec.py` **tidak punya padanan dan sebaiknya dihapus**, bukan dipaksakan.

Kalau S6 (Fase 0) jelek, inilah fase saat router akhirnya pindah dari Python.

---

### Fase 11 — `mltb-web`

`web/wserver.py` + `web/nodes.py` (464) → `axum`. File selector torrent. Paling
mudah, ditaruh belakangan karena tidak memblokir apa pun.

---

### Fase 12 — Cutover & pembongkaran

- Jalankan paralel di VPS non-produksi dengan trafik nyata (shadow), bandingkan
  hasil.
- Cutover per-fitur lewat feature flag, bukan sekali sakelar.
- Rencana rollback: image Python lama tetap bisa di-deploy sampai ≥1 bulan
  setelah cutover penuh. **Ingat `update.py` me-reset `/app` ke `origin/master`
  tiap start** — rollback berarti revert di remote, bukan cuma restart.
- Hapus code Python hanya setelah padanan Rust-nya jalan di produksi ≥2 minggu.
- Yang **tetap** Python selamanya: sidecar resolver (§3.2).

---

## 6. Strategi test — 19.521 baris tidak boleh hilang

Test di repo ini bukan pelengkap; `AGENTS.md` mensyaratkan tiap commit membawa
test yang **sudah dibuktikan bisa gagal**. Migrasi yang membuang itu membuang
satu-satunya bukti bahwa perilaku tidak berubah.

Tiga lapis:

1. **Golden/conformance (paling penting).** Sebelum menulis code Rust untuk
   suatu fase, rekam input→output fungsi Python terkait sebagai fixture JSON.
   Rust harus mereproduksi byte-per-byte. Cocok untuk: rendering status,
   `task_args` parsing, `bulk_links`, `links_utils`, urutan `natsort`, nama file
   hasil split.
2. **Unit Rust** (`#[cfg(test)]`) — terjemahan test per-fase.
3. **Integrasi end-to-end** dengan akun Telegram uji: leech satu file, satu
   album, satu torrent, satu link yt-dlp, batalkan di tengah.

Aturan mengikat: **fase tidak selesai sebelum test padanannya hijau di Rust.**
Sama seperti ledger utang lint di `pyproject.toml` — ketat, per-fase, dicabut
saat beres.

Perhatikan: `test_module_imports.py` dan `test_handlers_table.py` menguji
*struktur*, bukan perilaku. Keduanya tidak punya padanan langsung — di Rust,
compiler yang menjawab pertanyaan itu.

---

## 7. Daftar risiko

| Risiko | Dampak | Mitigasi |
|---|---|---|
| **S6 gagal** (dua sesi berebut update) | Strangler batal | Fase 0 duluan; fallback worker-tanpa-update (§5, Fase 0) |
| grammers 0.x breaking tiap ~6 bulan | Kerja upgrade berulang | Pin `=0.10.0`; kurung di `mltb-tg`; anggarkan upgrade rutin |
| grammers lebih lambat dari kurigram | Premis migrasi gugur | Benchmark S4 **sebelum** menulis code produksi |
| Doc coverage 62,94% | Banyak baca source | Terima; anggarkan waktu baca source, jangan tebak |
| Progress tidak akurat (over-read ~2 MiB) | Status menyesatkan | Dokumentasikan batasnya; jangan pakai untuk akuntansi |
| Peer cache salah | "Could not find input entity" di produksi | Modul khusus + persistensi + test |
| `copy_message` tak ada padanan | Perilaku `/copy` berubah | Konfirmasi ke pemilik produk **sebelum** Fase 10 |
| Migrasi session gagal | Semua user `/login` ulang | Fase 0 S1; simpan dua format berdampingan |
| Kompatibilitas blob terenkripsi | Data lama tak terbaca | `DB_ENCRYPTION_KEY` tetap; test round-trip lintas bahasa |
| Bot produksi membeku lama | Kerugian nyata | Fase bisa dirilis satu per satu; Python tetap jalan |
| Scope creep "sekalian perbaiki" | Migrasi tak selesai | Perbaikan perilaku = commit terpisah, di luar fase |

---

## 8. Estimasi

Sengaja dalam rentang, dan sengaja tidak dibuat presisi palsu. Asumsi: satu
orang, part-time, sudah paham domainnya (repo sendiri), **belum** ahli Rust.

| Fase | Beban |
|---|---|
| 0 — Spike | Hari, bukan minggu. **Wajib.** |
| 1–3 — Fondasi + transport | Bagian terbesar dari kurva belajar Rust |
| 4–5 — Transfer + status | Paling bernilai, paling banyak test diterjemahkan |
| 6–8 — Store + task + engine | Volume terbesar |
| 9 — Sidecar | Kecil — itu intinya |
| 10–11 — UI + web | Banyak baris, sedikit pikiran |
| 12 — Cutover | Kalender, bukan jam kerja: butuh waktu jalan di produksi |

Kalibrasi jujur: 28.677 baris Python biasanya jadi **40.000–60.000 baris Rust**
(error handling eksplisit, tipe, tanpa duck typing). Kalau Rust-nya masih baru,
Fase 1–3 akan terasa jauh lebih lambat dari sisanya — itu normal dan bukan
tanda rencananya salah.

---

## 9. Yang harus diputuskan sebelum Fase 1

1. **Hasil Fase 0**, terutama S4 (benchmark) dan S6 (update dua sesi).
2. **`/copy` (§2.2)** — terima perubahan perilaku, atau fitur dicoret?
3. **`exec.py` / `shell.py`** — dihapus, atau tetap di sidecar Python?
4. **Batas akhir sidecar** — permanen (rekomendasi) atau ada rencana porting?
5. **Siapa yang merawat** dua bahasa selama transisi berbulan-bulan?

Pertanyaan 5 yang paling sering membunuh migrasi seperti ini, dan paling jarang
ditanyakan di awal.
