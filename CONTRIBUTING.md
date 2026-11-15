# Contributing

Thanks for helping. Bug reports with a log excerpt are the most useful thing
you can send (see [Troubleshooting](docs/TROUBLESHOOTING.md#reporting-a-bug)).

## Layout

- `putio_download_and_delete.sh`: a small bash entry point. It finds the
  binaries, sources `.putio_config`, and `exec`s `python3 -m putio run`. Mount
  recovery for macOS SMB shares lives here too.
- `putio/`: the downloader itself, in Python with the standard library only.
  Its tests are in `putio/tests/`.
- `putioctl`: the `drovie-downloader` command (bash). `putioctl_tui.py` is its
  interactive console.
- `putio-statusd`: the read-only status service Drovie polls
  ([STATUS_API.md](STATUS_API.md)).
- `docker/` and `Dockerfile`: the container image.

## Running the tests

```sh
python3 -m unittest discover          # the unit suite, no network needed
```

The suite uses recorded TMDB and Plex responses in `putio/tests/fixtures/`, so
it never opens a socket.

## Rules the code relies on

These are what make the Downloader safe to leave running. A change that bends
one needs a very good reason, stated in the pull request.

- **put.io is the work queue.** A file is deleted from put.io only after it
  has left the download folder and been filed. A run that dies at any point
  must leave the next run able to carry on from what is still on put.io.
- **No new state that a crash could leave inconsistent.** Locks, progress
  files and staging folders are all tied to the run's process id and swept
  against boot time.
- **The entry point must `exec`.** The run's process id identifies it to the
  lock file, `drovie-downloader stop` and the progress files.
- **Only bash reads `.putio_config`.** The shell passes values to Python
  through the environment. Any setting you add goes into `PUTIO_PY_VARS` in
  the entry point *and* `Config` in `putio/config.py`, or it arrives empty.
- **Standard library only**, in both the Downloader and the status service.
- **No subprocess per file.** Batch work and make one call for the whole list.
- **The status service's request handlers never touch the filesystem.** A
  dropped network share would block them in the kernel and hang Drovie.
- The log format `[YYYY-MM-DD HH:MM:SS] [LEVEL]` and the phrase
  `Script completed successfully` are read by other tools. Don't change them.

## Pull requests

Keep each pull request to one change, with tests, and explain the reason in
the description. By contributing you agree that your work is licensed under
the [AGPL-3.0](LICENSE).
