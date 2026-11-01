# AGENTS.md — cara mengubah code di repo ini

Satu aturan mengikat sisanya: **tiap commit lulus tiga gerbang, membawa test
yang sudah dibuktikan bisa gagal, dan code yang disentuhnya beranotasi tipe.**
Sisa dokumen ini perinciannya.

Angka yang dikutip di bawah adalah snapshot 2026-08-24 (`c4f47c5`). Ukur ulang,
jangan percaya angkanya — yang mengikat adalah perintah dan targetnya.

---

## 1. Interpreter — salah pilih, code yang sehat terlihat rusak

Tiga tool, tiga lokasi berbeda. Ini bukan detail kosmetik: `pytest` yang salah
melaporkan **6 collection error** `ModuleNotFoundError: No module named
'pyrogram'`, dan itu gampang dibaca sebagai "code-nya rusak" padahal cuma
interpreternya keliru.

| Tool | Jalankan dengan | Kenapa |
|---|---|---|
| pytest | `.venv/bin/python -m pytest` | Dependensi ada di `.venv` (`kurigram` → modul `pyrogram`). `pytest` di PATH (`~/.local/bin`) memakai site-packages sistem yang tidak punya itu. |
| ruff | `ruff check .` | Hanya ada di PATH (`~/.local/bin/ruff`), **tidak** di `.venv`. |
| pyrefly | `.venv/bin/pyrefly check` | Hanya ada di `.venv`, **tidak** di PATH. |

`.venv` dibuat tanpa system-site-packages, jadi `.venv/bin/python` adalah satu-satunya
interpreter yang melihat requirements lengkap.

**Kalau Anda agent yang jalan di bawah hook RTK:** hook itu memangkas output
ruff (43 baris mentah → 28 baris, baris terakhir terpotong `b...`). Untuk
angka dan daftar temuan yang utuh — yang jadi dasar diff di §2 — lewati
filternya:

```bash
rtk proxy 'ruff check . --output-format concise'
```

---

## 2. Tiga gerbang

Jalankan ketiganya sebelum commit. Bukan salah satu.

```bash
.venv/bin/python -m pytest -q                                # 1. test
ruff check .                                                 # 2. lint
.venv/bin/pyrefly check --baseline pyrefly-baseline.json      # 3. tipe
```

| Gerbang | Target | Snapshot 2026-08-24 |
|---|---|---|
| pytest | 0 failed | 872 passed, 0 failed (~24 s) |
| ruff | 0 temuan | **41 temuan** — lihat §5 |
| pyrefly | 0 errors | 0 errors (36 suppressed) |

**Soal 41 temuan ruff itu.** Semuanya utang yang masuk *setelah* refactor
Fase 10 selesai, dan tidak satu pun tercatat di ledger `per-file-ignores`
(§5) — jadi bukan utang yang disahkan, tapi regresi yang lolos. Sebarannya:
`bot/core/handlers.py` (18× E501), `hosts/vidara.py` (5), `pornhub_scraper.py`
(6), plus I001 tersebar dan 3 di `tests/`.

Konsekuensinya untuk pekerjaan Anda: **jangan pakai angka 41 sebagai izin.**
Bandingkan sebelum dan sesudah supaya temuan Anda tidak menyamar sebagai
temuan yang sudah ada:

```bash
ruff check . --output-format concise | sort > /tmp/before.txt   # sebelum menyentuh apa pun
# ... kerjakan perubahannya ...
ruff check . --output-format concise | sort > /tmp/after.txt
diff /tmp/before.txt /tmp/after.txt                            # harus kosong, atau hanya berisi baris yang hilang
```

File yang Anda sentuh idealnya keluar dari daftar itu sekalian. Kalau tidak,
minimal jangan menambah barisnya.

---

## 3. Type hints

`pyrefly` adalah pemeriksa tipenya; konfigurasi di `[tool.pyrefly]`
(`pyproject.toml`), cakupan `bot` + `web`.

**Aturannya:** tiap fungsi/method yang Anda tambah — atau yang signature-nya
Anda ubah — dapat anotasi parameter dan anotasi return.

Yang perlu diketahui: **ruff tidak akan menangkap hint yang hilang.** Aturan
`ANN` (flake8-annotations) sengaja tidak diaktifkan, karena code lama punya
2255 temuan ANN dan menyalakannya akan menenggelamkan yang baru. Artinya
kelengkapan anotasi ditegakkan oleh review, bukan oleh linter. Periksa diff
Anda sendiri — dan hanya baris yang Anda sentuh:

```bash
ruff check <file-yang-diubah> --select ANN
```

Gaya rumah, sesuai code yang sudah ada:

- PEP 604 untuk union: `FloodWait | FloodPremiumWait`, `str | None`. Bukan `Optional[...]`.
- `collections.abc` untuk `Callable`/`Iterable`, bukan `typing`.
- `from __future__ import annotations` ditambahkan **kalau butuh** forward
  reference atau ingin menjaga anotasi tetap murah — 23 dari 145 file di
  `bot`/`web` memakainya, jadi bukan kewajiban.
- Kalau banyak call site membaca atribut `self` yang di-set di file lain,
  tuliskan kontraknya sekali di satu class annotation-only yang inert.
  Presedennya `bot/helper/task/_host.py` (41 atribut, tanpa `__init__`,
  tanpa nilai — komposisinya tidak mengubah apa pun saat runtime).
- Sebab yang menyuapi banyak temuan sekaligus di-root-cause, bukan dianotasi
  satu-satu di tiap call site. Presedennya `flood_seconds()` di
  `bot/helper/telegram/flood.py`: satu fungsi menggantikan lima
  `sleep(f.value * slack)` yang berhitung di atas tipe yang mungkin `str`.

**Yang tidak boleh:**

- `# type: ignore` untuk melewati gerbang. Cari akarnya. Kalau temuannya
  memang bug pihak ketiga, baru baseline-kan, dengan komentar yang menjelaskan
  kenapa — 5 entri yang ada semuanya satu bug pyrogram (`thumb: Union[str,
  BinaryIO] = None` di signature-nya sendiri).
- `pyrefly check --update-baseline` untuk membungkam temuan baru. Perintah itu
  hanya untuk saat utang benar-benar dibereskan (entri hilang) atau sengaja
  diterima setelah dipikirkan.

Baseline dipilih alih-alih mematikan kode-error per file justru karena bedanya:
mematikan `bad-assignment` untuk satu modul juga menelan `bad-assignment`
*berikutnya* di modul itu; baseline mencatat temuan satu per satu, jadi yang
baru tetap dilaporkan meski jenis dan file-nya sudah pernah muncul.

---

## 4. Test

**Tiap perubahan perilaku membawa test, dan test itu sudah dibuktikan gagal
tanpa perubahannya.** Pembuktian itu ditulis di body commit — bukan diklaim,
tapi dilaporkan. Test yang tidak pernah dilihat gagal tidak menjamin apa pun.

Letak dan bentuk:

- `tests/test_<subjek>.py`. `asyncio_mode = auto`, jadi `async def test_...`
  tidak perlu marker. Marker yang dikenal cuma `slow`, dan `--strict-markers`
  aktif — marker yang salah tulis akan error, bukan diam.
- Mengimpor modul `bot` menarik seluruh rantainya (lxml, cloudscraper,
  pyrogram), dan `bot/__init__.py` punya efek samping saat import
  (`uvloop.install()`, bikin event loop, buka `log.txt`). Untuk menguji satu
  modul daun, **stub package-nya** alih-alih mengimpor rantai itu — preseden:
  fixture `bunkr` dan `vidara` di `tests/conftest.py` memuat satu file host
  lewat `spec_from_file_location` dengan `_common`/`registry` yang dipalsukan.
- `DOWNLOAD_DIR = "/app/downloads/"` hardcoded; test yang menyentuh path perlu
  monkeypatch.

**Untuk refactor yang menjanjikan perilaku tidak berubah**, dua test saja tidak
cukup — pola yang dipakai repo ini ada dua lapis:

1. **Differential harness** di `tools/archive/` — memuat modul versi pra-refactor
   langsung dari git di samping versi working tree, menjalankan keduanya lewat
   skenario yang sama, lalu membandingkan **panggilan** (method mana, argumen
   apa, urutan apa), **log**, **state**, dan **hasil** (nilai kembali atau
   exception). Preseden: `tools/archive/_phase11b_diff.py`, 103 skenario.
2. **Mutation check** — harness yang melaporkan "identical" belum berarti apa
   pun sampai terbukti bisa membedakan. Script mutasi merusak code baru satu
   perubahan kecil sekaligus dan menuntut harness-nya gagal di setiap mutasi.
   Preseden: `tools/archive/_phase11b_mutants.py`, 62 mutasi, 62 tertangkap.
   **Mutan yang lolos bukan lulus** — itu entah lubang di harness, entah baris
   yang tidak berpengaruh, dan mana dari keduanya harus dipastikan, bukan
   diasumsikan.

`tests/` dan `tools/` di luar cakupan pyrefly (164 temuan, akan menenggelamkan
temuan dari code yang ship), tapi **tetap** di dalam cakupan ruff.

---

## 5. Ledger lint — menyusut, tidak pernah bertambah

`[tool.ruff.lint.per-file-ignores]` di `pyproject.toml` adalah daftar utang
yang sudah ada sebelum refactor, dicabut per fase. Aturannya satu arah: **baris
dihapus saat file dibereskan; menambah baris baru adalah regresi.** Kalau Anda
menyentuh file yang punya entri, bereskan temuan yang tersisa dan hapus
barisnya.

Memastikan satu direktori benar-benar tidak punya utang — bukan sekadar
utangnya sedang disembunyikan:

```bash
ruff check bot/helper/upload/ --config 'lint.per-file-ignores = {}'
```

`upload/` sekarang bersih dengan perintah itu. Kalau nanti ada yang
menambahkan barisnya kembali, itu regresi, bukan penyesuaian.

Ambang yang berlaku: `line-length = 88`, `target-version = "py314"`,
`max-complexity = 10`. `.venv` dan `qBittorrent` dikecualikan.

Untuk menilai kapan sebuah baris ledger sudah boleh dicabut (dan untuk melihat
apakah perubahan Anda menambah kompleksitas):

```bash
.venv/bin/python tools/complexity_report.py --summary
.venv/bin/python tools/complexity_report.py --diff tools/baseline.txt
```

CX-nya dihitung dengan pendekatan yang sama dengan C901 ruff, jadi angkanya
sebanding. Catat bahwa target §5 `REFACTOR_PLAN.md` belum semuanya tercapai
(`cx_max` masih 69, target ≤ 15) — jangan tambah, dan kurangi kalau lewat.

---

## 6. Alur satu perubahan

1. **Baca dulu.** `REFACTOR_PLAN.md` mencatat kenapa banyak hal berbentuk
   seperti sekarang, termasuk asimetri yang disengaja. Melawan salah satunya
   tanpa tahu itu disengaja biasanya jadi bug.
2. **Rekam baseline ruff** (§2) sebelum menyentuh apa pun.
3. **Satu perubahan logis per commit.** Ekstraksi dan perbaikan perilaku tidak
   digabung — kalau refactor mengubah perilaku, perubahan perilakunya jadi
   commit `fix:`/`feat:` tersendiri (preseden: empat `eval()` di Fase 7).
4. **Tulis testnya, lihat gagal, baru perbaiki.**
5. **Anotasi apa pun yang Anda tulis** (§3).
6. **Jalankan tiga gerbang** (§2). Tidak ada yang di-skip karena "cuma ubah
   satu baris".
7. **Cabut baris ledger** kalau file yang disentuh sudah bersih (§5).
8. **Commit** (§7).

Selesai berarti: 0 test gagal, tidak ada temuan ruff baru, pyrefly 0 errors,
code baru beranotasi, dan test barunya sudah terbukti bisa gagal. Kalau ada
bagian yang tidak bisa dituntaskan, katakan bagian mana dan kenapa — jangan
diamkan.

---

## 7. Commit

Ringkasan: `<type>: <imperatif huruf kecil>` — `feat`, `fix`, `refactor`,
`perf`, `docs`. Body menjelaskan **kenapa**, bukan mengulang diff: masalah apa
yang ada sebelumnya, apa yang berubah secara perilaku, dan hasil verifikasinya
(jumlah test, skenario harness, mutasi tertangkap, hasil di lingkungan nyata
kalau ada). Akhiri dengan:

```
Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
```

**Bahasa.** Ini bukan preferensi, ini pola yang sudah konsisten di repo:

| Di mana | Bahasa |
|---|---|
| Commit message | Inggris |
| Docstring & komentar di `bot/`, `web/`, `tests/` | Inggris |
| `AGENTS.md` (terjemahan Inggris, dimuat otomatis saat sesi mulai) | Inggris |
| Dokumen proses (`AGENTS.id.md`, `REFACTOR_PLAN.md`), komentar `pyproject.toml`, docstring `tools/` | Indonesia |

Commit dan push hanya kalau diminta.

---

## 8. Dua remote — satu fork, satu repo personal

Checkout ini terpasang ke tiga remote. Ketiganya tidak bisa saling menggantikan:

| Remote | Repository | Peran |
|---|---|---|
| `upstream` | `anasty17/mirror-leech-telegram-bot` | Sumber kebenaran, read-only. Jangan pernah push ke sini. |
| `origin` | `hikari-work/mirror-leech-telegram-bot` | Fork GitHub. `master`-nya dijaga **identik byte-per-byte** dengan `upstream/master` supaya branch PR yang dipotong darinya hanya memuat commit PR itu. |
| `personal` | `hikari-work/mirror-leech-hikari` | Pohon yang divergen — semua commit fitur lokal ada di sini. `master` melacak remote ini. |

Kenapa dipisah: `master` di fork pernah membawa 161 commit fitur di atas
upstream. PR yang dibuka dari branch semacam itu menampilkan semua 161 commit,
bukan satu perubahan yang dimaksud. `master` fork sudah di-force-push kembali
ke `upstream/master`; pohon 162 commit-nya diawetkan di `personal/master`.

Aturan kerja:

- **`master` lokal melacak `personal/master`.** Jalankan gerbang (§2) di sini
  dan push pekerjaan fitur dengan `git push personal master`.
- **Berkontribusi ke upstream:** potong branch dari `upstream/master`, jangan
  pernah dari `master` lokal, lalu push branch itu ke fork.

  ```bash
  git fetch upstream
  git checkout -b fix-upstream-bug upstream/master
  # ... commit ...
  git push origin fix-upstream-bug
  gh pr create --repo anasty17/mirror-leech-telegram-bot --head hikari-work:fix-upstream-bug
  ```

- **Menarik perbaikan upstream** ke pohon personal:

  ```bash
  git fetch upstream
  git merge upstream/master        # di master lokal, lalu push personal
  ```

- **Jangan pernah** push commit fitur lokal ke `origin/master`. `master` fork
  harus sama dengan `upstream/master`; verifikasi dengan:

  ```bash
  git fetch upstream && git diff --stat upstream/master...origin/master   # harus kosong
  ```

### Dua worktree, satu `.git`

Kedua pohon berbagi satu object store, jadi tidak ada yang diklon dua kali:

| Direktori | Branch | Untuk |
|---|---|---|
| `~/mirror-leech-telegram-bot` | `master` → `personal/master` | kerja personal, dan deployment |
| `~/mirror-leech-contrib` | `contrib` → `upstream/master` | khusus kontribusi upstream |

Nama direktori **tidak** menunjukkan remote-nya -- pohon personal justru yang
bernama `mirror-leech-telegram-bot`, sementara fork dengan nama itu hanya ref
remote-tracking di dalamnya. Periksa `git branch -vv`, bukan `pwd`.

`contrib` adalah branch berumur panjang yang diparkir di `upstream/master`,
bukan branch per-PR. Mulai PR dengan bercabang darinya di dalam worktree:

```bash
cd ~/mirror-leech-contrib
git fetch upstream && git merge --ff-only upstream/master   # di contrib
git switch -c feat/some-fix                                 # baru bercabang dari sini
# ... commit ...
git push origin feat/some-fix
gh pr create --repo anasty17/mirror-leech-telegram-bot --head hikari-work:feat/some-fix
git switch contrib && git branch -D feat/some-fix           # setelah di-merge
```

Worktree itu tidak punya `.venv`, `config.py`, atau `.env`: tempatnya untuk
commit, bukan menjalankan bot. Jalankan gerbang (§2) dari pohon personal.

`git worktree list` menampilkan keduanya; `git worktree remove
~/mirror-leech-contrib` melepas yang kedua tanpa menyentuh checkout personal.

### Deployment harus menunjuk `personal`, jangan pernah ke fork

`update.py` menulis ulang checkout yang berjalan pada setiap boot: ia menghapus
`.git`, `git add .` seluruh pohon jadi satu commit sekali pakai, lalu
`git reset --hard origin/{UPSTREAM_BRANCH}`. Dua konsekuensinya, dan keduanya
sudah pernah memakan korban.

**1. `UPSTREAM_REPO` menentukan code mana yang dipasang restart berikutnya.**
Arahkan ke fork dan restart akan memasang upstream polos -- 162 commit fitur
hilang dari checkout, sementara `config.py`, session, dan download selamat,
jadi kerusakannya senyap sampai ada perintah yang sudah tidak ada dipanggil.
`UPSTREAM_REPO` sebuah deployment harus remote personal:

```
UPSTREAM_REPO   = 'https://github.com/hikari-work/mirror-leech-hikari.git'
UPSTREAM_BRANCH = 'master'
```

Nilainya tidak hanya dibaca dari `config.py`. `update.py` juga membaca
`settings_config.data` dari PostgreSQL untuk `bot_id` yang berjalan dan
membiarkannya menimpa file, dan bot sendiri membaca ulang `UPSTREAM_REPO` dari
baris yang sama saat runtime. **Setiap `bot_id` di tabel itu adalah senjata
terkokang**: menjalankan salah satunya menjalankan ulang `update.py` terhadap
repo tersimpannya, ke dalam `/app` yang **dipakai bersama**. Ubah satu baris
dan bot yang sedang jalan tetap memakai code-nya; lewatkan satu baris dan bot
berikutnya yang boot akan mengganti code di bawah semuanya. Audit seluruh
tabel, bukan hanya baris bot yang kebetulan sedang dilihat.

**2. `git add .` meng-commit file untracked, dan `reset --hard`
menghapusnya.** Apa pun yang tidak ditutup file ignore ikut ter-commit ke
commit sekali pakai itu lalu terhapus. Praktiknya itu adalah state
qBittorrent: `qBittorrent/data/nova3/**` (plugin pencarian),
`qBittorrent/data/GeoDB/*.mmdb`,
`qBittorrent/config/{categories,watched_folders}.json`, `rss/`, dan
`qBittorrent/config/qBittorrent.conf` yang sudah diedit -- yang membawa
`WebUI\Password_PBKDF2` dan pilihan port listen.

Sebagian beregenerasi sendiri (plugin dipasang ulang dari
`Config.SEARCH_PLUGINS`, GeoDB diunduh ulang), tapi password WebUI dan pilihan
port hanya hidup di file itu. Backup sebelum update apa pun yang menyentuh
checkout:

```bash
docker cp <container>:/app/qBittorrent/config/qBittorrent.conf ./qbit-backup.conf
```

**Update deployment dengan aman.** Restart hanya gratis kalau tidak ada yang
berjalan: `aria2.tellActive` kosong, `curl /api/v2/torrents/info` di WebUI
qBittorrent kosong, dan tidak ada anak `ffmpeg`/`7z`/`split`. Lalu pilih: biarkan
`/restart` menjalankan `update.py` setelah file di atas dibackup, atau lakukan
reset manual dan pertahankan edit lokal:

```bash
# di dalam container, sebelum reset
tar czf /tmp/qb.tgz \
  qBittorrent/config/qBittorrent.conf qBittorrent/config/qBittorrent-data.conf \
  qBittorrent/config/categories.json qBittorrent/config/watched_folders.json \
  qBittorrent/config/rss qBittorrent/data/GeoDB qBittorrent/data/nova3
git stash push -u -- qBittorrent/config/qBittorrent.conf aria-nox.sh

git merge --ff-only origin/master   # atau biarkan update.py saat boot berikutnya
tar xzf /tmp/qb.tgz                 # pulihkan yang terhapus oleh merge
```

Perhatikan bahwa `update.py` bukan opsional untuk sebuah *boot* -- `start.sh`
selalu menjalankannya -- jadi jalur amannya adalah membenarkan inputnya, bukan
menghindarinya.

Jangan menaruh hostname, path, atau kredensial deployment yang hidup di file
ini: repository-nya publik. Yang transferabel adalah invarian di atas.
