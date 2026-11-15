# Drovie Downloader for put.io

Drovie Downloader copies finished downloads from your [put.io](https://put.io)
account into your Plex library. It names and files each one with
[TMDB](https://www.themoviedb.org), asks Plex to scan, and then removes the
file from put.io.

It is the companion to [Drovie](https://github.com/friedmad77/drovie), the
request app for Plex. When Drovie downloads with put.io, Drovie sends each
request to put.io and Drovie Downloader brings the finished file home. Drovie
then shows the request's progress all the way to Plex: downloading, copying,
and waiting for the Plex scan.

> **Using Radarr or Sonarr?** You probably want
> [putioarr](https://github.com/wouterdebie/putioarr) instead. It makes put.io
> look like a download client to Radarr and Sonarr, which then do the filing.
> Drovie Downloader is for setups without them.

## What it does

Every few minutes (five by default) it:

1. Lists the put.io folder it watches (`putio:Drovie/` by default).
2. Skips junk such as samples, `.nfo` files and tracker ads.
3. Downloads each file, unpacking ZIP and RAR archives, multi-part sets included.
4. Looks the title up on TMDB and files it the way Plex expects:
   `Movies/Good Boy (2026)/Good Boy (2026).mkv` or
   `TV/Show/S01/Show - S01E02.mkv`. Anything it can't identify goes to an
   `Unmatched` folder.
5. Scans the matching Plex library, then tells Drovie (optional).
6. Deletes the file from put.io, only after it has left the download folder
   and is in your library. Until then, put.io keeps its copy.

If something fails partway through (a restart, a full disk, TMDB down), the
next run picks up from what is still on put.io. It never deletes anything it
hasn't filed.

## Quick start: Docker, alongside Drovie

You need Drovie's `docker-compose.yml` running, plus:

- a **put.io OAuth token**: put.io › Settings › API › Create new app, then copy
  the app's OAuth token.
- a **TMDB API key**: <https://www.themoviedb.org/settings/api> (the one Drovie
  uses is fine).
- your **Plex token**: <https://support.plex.tv/articles/204059436>.

1. In Drovie's folder, copy the settings file and fill in the three keys:

   ```sh
   cp downloader.env.example downloader.env
   chmod 600 downloader.env
   $EDITOR downloader.env
   ```

2. In Drovie's `.env`, add the downloader profile and the folders Plex uses:

   ```sh
   COMPOSE_PROFILES=downloader          # or jackett,downloader
   MEDIA_MOVIES_DIR=/srv/media/Movies
   MEDIA_TV_DIR=/srv/media/TV
   DOWNLOADER_DOWNLOADS_DIR=/srv/media/downloads
   ```

3. Start it:

   ```sh
   docker compose up -d
   docker compose logs -f downloader
   ```

4. In Drovie, open **Settings › Download client › put.io** and press
   **Use a Drovie folder**. This creates a `Drovie` folder on put.io and sends
   new downloads there, which is the folder Drovie Downloader watches. Then
   press **Test status address**. It should answer "Connected. Drovie
   Downloader is idle."

5. Optional: copy Drovie's webhook key (**Settings › Plex › Instant updates**)
   into `downloader.env` as `DROVIE_WEBHOOK_KEY`, then
   `docker compose up -d` again. Drovie then marks a request available as soon
   as Plex has the file, instead of at its next check.

To try it without deleting anything from put.io, add
`DELETE_AFTER_DOWNLOAD=false` to `downloader.env` for the first few runs.

## Quick start: Docker, on its own

To run it without Drovie, or on a different machine:

```sh
cp docker/docker-compose.example.yml docker-compose.yml   # set the folder paths
export PUTIO_TOKEN=… TMDB_API_KEY=… PLEX_TOKEN=…           # or put them in .env
docker compose up -d
```

To let a Drovie on another machine read its status, uncomment the `ports`
lines and set Drovie's **Drovie Downloader status address** to
`http://<this-machine>:9494`. The status service has no login of its own, so
only publish it on a network you trust.

## Everyday use

The `drovie-downloader` command controls a running Downloader. In Docker, run
it with `docker compose exec downloader drovie-downloader …`.

| Command | What it does |
|---|---|
| `status` | Paused or running, the next run, free space and transfers in flight |
| `sync` | Run now instead of waiting for the schedule |
| `pause 2h` / `resume` | Hold off scheduled runs (a bare `pause` lasts until you resume) |
| `stop` | Stop the current run |
| `progress -w` | Live progress bars for each download |
| `logs -f` | Follow the log (`--errors` for problems only) |
| `config list` / `config set KEY VALUE` | Read or change settings (secrets are masked) |

Bare `drovie-downloader` opens an interactive console with all of the above.
See [PUTIOCTL.md](PUTIOCTL.md) for every command.

## More

- [Configuration reference](docs/CONFIGURATION.md): every setting and its default.
- [Troubleshooting](docs/TROUBLESHOOTING.md): fixes for the common problems.
- [Running without Docker](docs/NATIVE.md): Linux or macOS with cron.
- [Status API](STATUS_API.md): the read-only JSON endpoint Drovie polls.
- [Contributing](CONTRIBUTING.md)

## License

[AGPL-3.0](LICENSE). © Lowcontrast LLC.
