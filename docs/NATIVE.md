# Running without Docker

Drovie Downloader runs on Linux or macOS with cron. You need bash, Python 3.9
or newer (standard library only, nothing to `pip install`), rclone 1.63 or
newer, `jq`, `unzip`, and `unar` or `unrar`.

```sh
# Debian / Ubuntu
sudo apt install python3 jq unzip unar curl
curl https://rclone.org/install.sh | sudo bash

# macOS (Homebrew)
brew install python@3.13 rclone jq unar
```

## Install

```sh
git clone https://github.com/friedmad77/drovie-downloader.git
cd drovie-downloader
./putioctl deploy                       # copies everything into ~/Scripts
cp server_config_template ~/.putio_config
chmod 600 ~/.putio_config
$EDITOR ~/.putio_config                 # TMDB_API_KEY, PLEX_TOKEN, the folders
~/Scripts/drovie-downloader setup-putio # asks for your put.io OAuth token
```

`setup-putio` creates the rclone remote, checks it, and creates the `Drovie`
folder on put.io if it isn't there. To stop the binaries cron finds from
differing from your shell's, pin them in the config:
`RCLONE_CMD`, `JQ_CMD` and `PYTHON_BIN`.

Do a trial run with nothing deleted from put.io:

```sh
~/Scripts/drovie-downloader config set DELETE_AFTER_DOWNLOAD false
~/Scripts/drovie-downloader sync -f      # -f follows the log
```

## Schedule it

```sh
crontab -e
*/5 * * * * $HOME/Scripts/putio_download_and_delete.sh >/dev/null 2>&1
```

`drovie-downloader status` reads the crontab to show the next run. To pause,
use `drovie-downloader pause`; there's no need to edit the crontab.

## The status service (for Drovie)

`putio-statusd` serves the Downloader's status on `127.0.0.1:9494`.

- **Linux (systemd, user service):**

  ```ini
  # ~/.config/systemd/user/drovie-downloader-statusd.service
  [Unit]
  Description=Drovie Downloader status service

  [Service]
  ExecStart=%h/Scripts/putio-statusd --putioctl %h/Scripts/putioctl --script %h/Scripts/putio_download_and_delete.sh
  Restart=always

  [Install]
  WantedBy=default.target
  ```

  ```sh
  systemctl --user enable --now drovie-downloader-statusd
  loginctl enable-linger "$USER"   # keep it running after you log out
  ```

- **macOS (launchd):** from the repo folder, `./putioctl statusd reinstall` (installs `app.drovie.downloader.statusd.plist` and starts it)

If Drovie runs in Docker on the same Linux machine, it can't reach loopback.
Add `--bind 0.0.0.0` (or set `STATUSD_BIND=0.0.0.0`), and set Drovie's status
address to `http://<this-machine>:9494`. The service has no login, so keep
port 9494 off the internet.

## Upgrading

```sh
git pull && ./putioctl deploy
```

`deploy` never overwrites `~/.putio_config`, and it waits if a sync is
running. Restart the status service afterwards:
`systemctl --user restart drovie-downloader-statusd`, or on macOS
`launchctl kickstart -k gui/$UID/app.drovie.downloader.statusd`.
