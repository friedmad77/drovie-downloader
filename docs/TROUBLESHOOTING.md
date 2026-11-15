# Troubleshooting

Start with these three commands (in Docker, prefix each with
`docker compose exec downloader`):

```sh
drovie-downloader status          # what it is doing, and when it runs next
drovie-downloader logs --errors   # recent problems only
drovie-downloader config list     # the settings it actually sees
```

## Nothing is downloading

- **Wrong folder.** The Downloader only looks in `REMOTE` (default
  `putio:Drovie/`). Check that Drovie sends transfers there: in Drovie, open
  Settings › Download client › put.io, press **Choose folder** and pick (or
  create) the `Drovie` folder at the top level, then save.
  Transfers already finished in another folder won't be picked up; move them
  into `Drovie` on put.io.
- **Paused.** `status` says so. Run `drovie-downloader resume`.
- **Not configured.** At startup the container log lists anything missing
  (`docker compose logs downloader`), such as
  `Not configured yet — set: TMDB_API_KEY`.
- **put.io token rejected.** In Docker, put a fresh token in `PUTIO_TOKEN` and
  restart the container. Without Docker, run `drovie-downloader setup-putio`.
- **Low disk space.** No download starts while `LOCAL_DIR` has less than
  `MIN_FREE_SPACE_GB` free. `status` shows the free space.

## Files end up in Unmatched

TMDB couldn't identify the name. Rename the file in `UNMATCHED_DIR` and move
it into your library yourself, or leave it on put.io under a clearer name and
let the next run try again.

When TMDB itself is unreachable, or rejects the key, files are **not** sent to
Unmatched. They stay in the download folder, the log shows an ERROR naming the
cause, and put.io keeps its copy until a later run can file them.

## Plex doesn't show new files

- Check `PLEX_URL` from inside the container:
  `docker compose exec downloader curl -s "$PLEX_URL/identity"` (any XML reply
  is fine). If Plex runs on the same machine, the compose files map
  `host.docker.internal` to it.
- A 401 in the log means `PLEX_TOKEN` is wrong.
- Plex must see the same folders. If the Downloader writes to `/media/Movies`
  inside the container, Plex's Movies library has to point at the host folder
  you mounted there.
- If you have more than one Movies or TV library, set `PLEX_MOVIE_LIBRARY_ID`
  and `PLEX_TV_LIBRARY_ID`.

## Drovie says it can't reach Drovie Downloader

In Drovie, open Settings › Download client and look at the **Drovie
Downloader** card. It says **Not connected** with the address it tried; fix the
address and press **Test connection**.

- **Same compose file:** the address is `http://downloader:9494`. Check that
  the `downloader` profile is running (`docker compose ps`).
- **Another machine:** publish port 9494 on the Downloader and set Drovie's
  status address to `http://<that-machine>:9494`.
- **Without Docker, on the same machine as a Drovie container:** start
  `putio-statusd --bind 0.0.0.0`. By default it listens on loopback only, and
  Docker on Linux can't reach that.

Drovie still works without the status service. Requests are marked available
when put.io finishes rather than when the file reaches your library, and the
progress rings skip the copy and Plex-scan steps.

## Permission denied writing to the library

The container runs as `PUID:PGID` (1000:1000 by default). Set both to the
owner of your media folders (`ls -n /srv/media`), then
`docker compose up -d`.

## A run seems stuck

`drovie-downloader progress -w` shows each transfer live. To end a run, use
`drovie-downloader stop`. It stops rclone too, and the next run carries on
from what is still on put.io. If a crash left the lock behind, the next run
detects that the owning process is gone and takes over.

## Reporting a bug

Run `drovie-downloader logs -n 200` and check the output for anything private.
Tokens and keys are already redacted in the log. Then open an issue with the
log and the output of `drovie-downloader --version`.
