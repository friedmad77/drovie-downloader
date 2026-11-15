# Configuration reference

Drovie Downloader reads one settings file, `.putio_config`. It is a bash file
of plain `KEY="value"` lines, and the Downloader sources it on every run, so
changes apply from the next run. Nothing needs restarting.

- **Docker:** `/config/.putio_config` on the config volume. It is written on
  the first start, and each line falls back to the container's environment
  (`REMOTE="${REMOTE:-putio:Drovie/}"`). That means you can set anything as an
  environment variable or in `downloader.env`, or edit the file to pin a value.
- **Without Docker:** `~/.putio_config`. Start from
  [`server_config_template`](../server_config_template).

`drovie-downloader config list` shows every setting with secrets masked.
`drovie-downloader config set KEY VALUE` changes one setting. It backs the
file up to `.putio_config.bak` first, and refuses any edit that would not be
valid bash.

## Required

| Setting | What it is |
|---|---|
| `TMDB_API_KEY` | TMDB API key, used to identify and name each download. |
| `PLEX_TOKEN` | Plex token, used to trigger library scans. |
| put.io access | Docker: set `PUTIO_TOKEN` and the container creates the rclone remote on start. Without Docker: run `drovie-downloader setup-putio`, or set up a `putio` remote with `rclone config`. |

## put.io

| Setting | Default | Notes |
|---|---|---|
| `REMOTE` | `putio:Drovie/` | The rclone remote and folder to download from. Keep it the same as Drovie's put.io download folder (pick the `Drovie` folder with **Choose folder** on Drovie's put.io card). **`putio:` on its own means your whole account**, and every file in it will be downloaded and then deleted. |
| `DELETE_AFTER_DOWNLOAD` | `true` | Set to `false` to keep files on put.io, for example during a trial run. Junk (samples, ads, files under 1 MB) is removed either way. |
| `PUTIO_TOKEN` | | Docker only. Read once at container start to write `/config/rclone.conf`, then dropped from the environment. |

## Folders

| Setting | Default (Docker) | Notes |
|---|---|---|
| `LOCAL_DIR` | `/downloads` | Where downloads land before they are filed. It needs room for your largest download. |
| `MOVIE_LIBRARY` | `/media/Movies` | Your Plex Movies library folder. |
| `TV_LIBRARY` | `/media/TV` | Your Plex TV library folder. |
| `UNMATCHED_DIR` | `/media/Unmatched` | Files TMDB couldn't identify. Check it now and then. |
| `MIN_FREE_SPACE_GB` | `5` | No download starts while `LOCAL_DIR` has less free space than this. |
| `USE_TEMP_DIR` | `false` | When `true`, each file is downloaded to `STAGING_DIR` on local disk and then copied into the library. Turn it on if your library is on a network share that sometimes drops. |
| `STAGING_DIR` | `/downloads/.staging` | Used only with `USE_TEMP_DIR=true`. |

## Plex

| Setting | Default | Notes |
|---|---|---|
| `PLEX_URL` | `http://host.docker.internal:32400` (Docker), `http://localhost:32400` | The default in Docker reaches a Plex on the same machine. |
| `PLEX_MOVIE_LIBRARY_ID`, `PLEX_TV_LIBRARY_ID` | empty | Found automatically when empty. Set them only if you have more than one Movies or TV library. The id is the number after `/library/sections/` in Plex's URLs. |

## Drovie

| Setting | Default | Notes |
|---|---|---|
| `DROVIE_WEBHOOK_URL` | empty (`http://app:3001/api/webhooks/plex-check` in Drovie's compose) | Called after each Plex scan, so Drovie marks requests available straight away. Use Drovie's address plus `/api/webhooks/plex-check`. |
| `DROVIE_WEBHOOK_KEY` | empty | Drovie › Settings › Plex › Instant updates › Webhook key. |

Drovie reads the Downloader's progress from its status service on port 9494.
In Drovie, the address is set on the **Drovie Downloader** card under
**Settings › Download client** (**Status address**), or with the `PUTIO_STATUSD_URL`
environment variable. The saved setting wins.

## Schedule (Docker)

| Setting | Default | Notes |
|---|---|---|
| `SYNC_INTERVAL_MINUTES` | `5` | How often to check put.io, from 1 to 60. A run that is still busy when the next one is due is left to finish, and the new run is skipped. |
| `PUID`, `PGID` | `1000` | The user and group that own downloaded and filed files. Match them to the owner of your media folders, which is usually the user Plex runs as. |
| `TZ` | `UTC` | Time zone used for log timestamps and the schedule. |

Without Docker, cron does the scheduling. See [NATIVE.md](NATIVE.md).

## Status service

| Setting | Default | Notes |
|---|---|---|
| `STATUSD_BIND` | `0.0.0.0` in Docker, `127.0.0.1` otherwise | The address the status service listens on. It has no login, so only expose it on a network you trust. |
| `STATUSD_PORT` | `9494` | |

## Logs and state

| Setting | Default (Docker) | Notes |
|---|---|---|
| `LOG_FILE` | `/config/logs/drovie-downloader.log` | Rotated automatically. |
| `LOG_LEVEL` | `INFO` | `DEBUG` shows every step. An idle run writes a single line. |
| `PAUSE_FILE` | `/config/paused` | Written by `drovie-downloader pause`. |
| `LOCKFILE` | `/tmp/drovie-downloader.lock` | Stops two runs overlapping. |
| `PROGRESS_STATE_DIR` | `/tmp/drovie-downloader-progress` | Where live progress is published. Set it to empty to turn progress off. |

## Tuning

| Setting | Default | Notes |
|---|---|---|
| `RCLONE_MAX_CONCURRENT` | `1` | Files downloaded at once. |
| `RCLONE_TRANSFERS`, `RCLONE_CHECKERS`, `RCLONE_BUFFER_SIZE`, `RCLONE_MULTI_THREADS` | `4`, `8`, `64M`, `2` | Passed to rclone. |
| `RCLONE_BWLIMIT` | `off` | Bandwidth cap, for example `20M` for 20 MB/s. |
| `RCLONE_STATS_INTERVAL` | `5s` | How often the progress bars update. |
| `MAX_RETRIES`, `RETRY_DELAY` | `3`, `5` | Retries for downloads and API calls. The delay doubles on each retry. |
| `MAX_REQUESTS_PER_WINDOW`, `RATE_WINDOW_SECONDS`, `RATE_LIMIT_DELAY` | `35`, `10`, `1` | TMDB rate limiting. |

## macOS network shares (native installs only)

If your library is on an SMB share mounted on a Mac, the Downloader can
remount it when it drops: set `MEDIA_MOUNT_POINT` (for example
`/Volumes/Media`) and `MEDIA_MOUNT_URL` (`smb://nas/Media`). The `MOUNT_*`
settings in `server_config_template` tune how often it retries. Remounting is
macOS only. On Linux, use your system's own automounting, such as an fstab
entry or a systemd automount.
