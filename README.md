# mirror-leech-hikari

A fork of [anasty17/mirror-leech-telegram-bot](https://github.com/anasty17/mirror-leech-telegram-bot)
with one focus: **leeching files from the Internet to Telegram, with the strongest direct-link
support of any bot in this family**. Everything upstream carries that does not serve that focus —
Google Drive, rclone, JDownloader, Sabnzbd, gallery-dl — is gone; the effort went into the download
path instead. ~200 commits ahead of upstream, every one green on 1800+ pytest tests, ruff and pyrefly.

- **TELEGRAM CHANNEL:** https://t.me/mltb_official_channel
- **TELEGRAM GROUP:** https://t.me/mltb_official_support

<details open>
  <summary><h1>What this fork improves over upstream</h1></summary>

| Area | Upstream | This fork |
|---|---|---|
| Direct links | one 200-line if/elif chain, ~30 hosts | an **ordered handler registry** — every host its own module with its own tests (see next section) |
| Shortlinks | a separate bypass per service | one **unified gateway bypass endpoint**; nested shortlinks are followed to the final link automatically |
| Dead hosts | stale handlers that fail confusingly | **tombstones**: dead services (uptobox, zippyshare, …) are checked last and answer "this service is dead", never a wrong handler |
| Storage | MongoDB | **PostgreSQL** (`jsonb` + `bytea`), private files optionally encrypted at rest (`DB_ENCRYPTION_KEY`, env-only by design) |
| Upload destinations | Telegram only | **Telegram, S3-compatible bucket, or FTP** — per config, per task (`-s3` / `-tg`); `/mirror` always uploads to FTP |
| Torrent search | qBittorrent search plugins, results as text | **gateway-backed search**: provider list, results and magnets from one API, paged in the message keyboard, page-pinned buttons |
| Social media | x/twitter and instagram only, partial | **x.com, instagram, tiktok** posts resolved through the gateway — photos, videos, no-watermark MP4s, slideshow frames |
| Mega | single-threaded, quota kills the task | ranged parallel downloads with **WARP tunnel rotation** or Cloudflare Worker proxies when the per-IP quota is spent |
| Messages | plain text | one rich, consistent layout for status and results |
| PikPak | broken ranged reads, per-file chat spam | sequential share downloads, **CDN fetched through aria2-style ranged reads** |
| Self-update | restart refused while the branch moved | **update-watch**: restarts between tasks after a quiet window when the branch moves |
| New commands | — | `/pleech` (leech a private DM), `/copy` (re-send a finished task from the database), `/bypass` (`/bp`), `/mirror` |
| Retries | a failed scrape fails the task | failed gateway scrapes are **retried with the cache bypassed** (terabox, vidara, doodstream) |
| Bulk runs | gateway rate-limits large batches | `RESOLVE_CONCURRENCY` caps links resolved at once, so `-b` batches don't trip the gateway |

</details>

<details open>
  <summary><h1>Direct links &amp; handlers</h1></summary>

The old bot dispatched direct links through a single if/elif chain. This fork replaces it with an
ordered registry (`bot/helper/download/direct_link_generators/registry.py`):

- Matching is **substring against the hostname**, exactly like the old chain — `"racaty" in domain`,
  not an exact or suffix match, so legacy URLs keep working.
- Because matching is by substring, one hostname can satisfy several handlers
  (`racaty.mediafire.com` contains both). Each handler declares an explicit `order=` that
  reproduces the old chain's precedence — the ordering **is** behaviour, and it is pinned by
  `tests/test_direct_link_registry.py`, including a test that pins the sequence itself.
- Adding a host is one module, one export, one registry entry, one test — never another branch in
  a monolith.
- Dead services are kept as **tombstones**, checked last, so a failure names the truth.

Two kinds of resolution sit behind the registry:

1. **Local handlers** scrape or construct the link in the bot itself:
   mediafire (file/folders), gofile (file/folders), send.cm (file/folders), 1fichier, buzzheavier,
   devuploads, uploadhaven, lulacloud, fuckingfast, mediafile, hxfile (needs `hxfile.txt` cookies),
   racaty, solidfiles, krakenfiles, upload.ee, akmfiles, shrdsk.me, tmpsend, qiwi.gg, berkasdrive,
   transfer.it, easyupload.io, swisstransfer, linkbox (file/folders),
   yandex.disk, osdn, github, 1drv.ms, pixeldrain, pcloud, wetransfer,
   streamtape + mirrors, streamvid, streamhub, mp4upload, filelions (`FILELION_API`),
   streamwish (`STREAMWISH_API`), imgbb, bunkr (all TLDs), mega (folders/files, proxied),
   and a private **seedbox** (nginx listing behind HTTP basic auth — `SEEDBOX_HOSTS`,
   `SEEDBOX_USERNAME`, `SEEDBOX_PASSWORD`; one link per folder downloads every file the listing shows).

2. **Gateway-backed handlers** call the scraping gateway (`GATEWAY_URL`, default
   `https://api.piyann.my.id`, optional `GATEWAY_TOKEN`):
   DoodStream, Terabox (file/folders, videos), vidara, vidoy, shyav, kpop.xxx, Erome,
   x/twitter, instagram, tiktok — and the unified shortlink bypass.
   `shyav` and `kpop` resolve **lazily**: the page URL is signed into a CDN link only when the
   download actually starts (`LAZY_RESOLVERS`).

Any link no handler claims falls through to the gateway's shortlink bypass, which answers `None`
for anything it does not recognise — so `/bypass` (`/bp`) takes any of the above and answers with
the final direct link, following nested shortlinks to the end.

</details>

<details>
  <summary><h1>Everything else the fork keeps</h1></summary>

- **qBittorrent / aria2c** with web file selection, seeding to ratio/time, live global settings
- **yt-dlp** with quality buttons, custom options, cookies and netrc auth
- **Telegram leech** with split sizes, thumbnails and layouts, media groups, topics, bot/user
  hybrid transmission, restricted-message downloads, `LEECH_DUMP_CHAT`, `-b` bulk
- **Status pages** with paging, per-status buttons and steps, interval refresh
- **RSS** with title filters, per-feed editing while running, sudo control
- **Archives**: 7z extract (with password), zip with splits, join of `split` parts
- **Queueing** (`QUEUE_ALL` / `QUEUE_DOWNLOAD` / `QUEUE_UPLOAD`, per-chat upload limits)
- **Extras**: sample video & screenshots, ffmpeg post-commands (`-ff`), name substitution,
  convert-by-filter, force-start, cancel-all, shell/executor, sudo users, optional second
  api_id/api_hash pair (`USER_TELEGRAM_API` / `USER_TELEGRAM_HASH`) keeping `/login` sessions off
  the bot's own api_id
- Docker images for `amd64, arm64/v8, arm/v7`

Bot commands (`CMD_SUFFIX` appended where configured):

```
start - Start the bot
leech - or /l Upload to telegram
pleech - or /pl Leech a private chat's media
mirror - or /m Download and upload to the FTP server
qbleech - or /ql Leech torrent using qBittorrent
ytdlleech - or /yl Leech yt-dlp supported links
copy - Re-send a finished task to a copy preset
bypass - or /bp Bypass and get the direct link
usetting - or /us User settings
bsetting - or /bs Bot settings
status - Get Leech Status message
sel - Select files from torrent
rss - Rss menu
search - Search for torrents through the gateway
cancel - or /c Cancel a task
cancelall - Cancel all tasks
forcestart - or /fs to start task from queue
login - Login with your telegram account
logout - Revoke your saved session
users - Show users settings
addsudo - Add sudo user
rmsudo - Remove sudo user
log - Get the Bot Log
auth - Authorize user or chat
unauth - Unauthorize user or chat
shell - Run commands in Shell
aexec - Execute async function
exec - Execute sync function
clearlocals - Clear exec locals
restart - Restart the Bot
stats - Bot Usage Stats
ping - Ping the Bot
help - All cmds with description
```

</details>

<details>
  <summary><h1>Configuration</h1></summary>

```
cp config_sample.py config.py
```

`config_sample.py` documents every key inline. Required: `BOT_TOKEN`, `OWNER_ID`,
`TELEGRAM_API`, `TELEGRAM_HASH`. The keys this fork adds on top of upstream:

| Key | Purpose |
|---|---|
| `GATEWAY_URL` / `GATEWAY_TOKEN` | scraping gateway used by direct-link handlers, shortlink bypass and torrent search (default `https://api.piyann.my.id`) |
| `RESOLVE_CONCURRENCY` | links resolved at once; the knob for bulk `-b` runs (default `4`, `0` disables) |
| `UPLOAD_DESTINATION` | `"tg"`, `"s3"` or `"ftp"`; per-task override with `-s3` / `-tg`; `/mirror` always FTP |
| `S3_*` | any S3-compatible bucket (Cloudflare R2 in practice), `S3_BROWSER_URL` for the completion link |
| `FTP_*` | FTP server for `/mirror`, one folder per task, `FTP_TLS` for explicit TLS |
| `DB_ENCRYPTION_KEY` | env-only; encrypts private files (cookies, .netrc) stored in PostgreSQL |
| `USER_TELEGRAM_API` / `USER_TELEGRAM_HASH` | second api pair for user sessions |
| `WARP_ENABLED` / `WARP_PROXY_PORT` / `MEGA_PROXY_URL` / `MEGA_CONNECTIONS` / `MEGA_MAX_RESTARTS` | Mega parallel downloads and IP rotation |
| `SEEDBOX_HOSTS` / `SEEDBOX_USERNAME` / `SEEDBOX_PASSWORD` | private seedbox directory listings |
| `FILELION_API` / `STREAMWISH_API` / `ALLDEBRID_API_KEY` | handler credentials |

`UPSTREAM_REPO` must point **at this fork**, not upstream — every restart resets the checkout to
it, so pointing it at anasty17's repo replaces this fork's code with the plain upstream bot.

</details>

<details>
  <summary><h1>Deploy &amp; database</h1></summary>

Docker (compose recommended):

```
sudo docker compose up
```

Full build/stop/log instructions and the iptables flush notes are in `Dockerfile` /
`start.sh` and upstream's docs — nothing differs except that the repo to clone is
`hikari-work/mirror-leech-hikari`.

PostgreSQL replaces MongoDB. The schema is created automatically on first boot; only
`DATABASE_URL` is needed. Installations that ran on Mongo can be carried over with a one-shot,
idempotent migration:

```
pip install pymongo                      # migration-time only
python tools/migrate_mongo_to_pg.py \
    --mongo-uri "$MONGO_URL" --mongo-db mltb \
    --pg-url "$DATABASE_URL"
```

Boot the bot once against the empty PostgreSQL first so the schema exists, then migrate. PostgreSQL
installs predating the row-based copy storage can be brought up to date with
`tools/migrate_copy_records_to_rows.py` and `tools/migrate_copy_presets_to_rows.py`
(both idempotent, both need the schema from a first boot).

</details>

# Credits

Built on [anasty17/mirror-leech-telegram-bot](https://github.com/anasty17/mirror-leech-telegram-bot),
which in turn is based on [python-aria-mirror-bot](https://github.com/lzzy12/python-aria-mirror-bot).

<a href="https://github.com/anasty17/mirror-leech-telegram-bot/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=anasty17/mirror-leech-telegram-bot" />
</a>
