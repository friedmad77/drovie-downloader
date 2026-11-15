# Drovie Downloader — downloads finished put.io transfers into a Plex library.
#
#   docker build -t drovie-downloader .
#
# Configuration is the container's environment (see docker/drovie-downloader.conf
# for every setting and its default). The first start writes that file to
# /config/.putio_config; edit it there to override anything, or keep using env.

# rclone from its own image: Debian's package is too old for --inplace (1.63+),
# which the direct-download path passes.
ARG RCLONE_VERSION=1.68.2
FROM rclone/rclone:${RCLONE_VERSION} AS rclone

FROM python:3.13-slim-bookworm

# jq: putioctl parses rclone's JSON log with it. unzip + unar: archives.
# procps: pgrep/ps for `stop` and status. tini: PID 1 that reaps and forwards
# signals, so `docker stop` reaches the run's SIGTERM handler.
RUN apt-get update \
 && apt-get install -y --no-install-recommends jq unzip unar procps tini ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone

WORKDIR /app
COPY putio_download_and_delete.sh putioctl putioctl_tui.py putio-statusd ./
COPY putio/ ./putio/
COPY docker/entrypoint.sh docker/drovie-downloader.conf docker/cli.sh /app/docker/
RUN ln -s putioctl /app/drovie-downloader \
 && ln -s /app/docker/cli.sh /usr/local/bin/drovie-downloader \
 && ln -s /app/docker/cli.sh /usr/local/bin/putioctl \
 && chmod +x /app/docker/entrypoint.sh /app/docker/cli.sh \
 && rm -rf /app/putio/tests

# HOME=/config puts the config, pause marker and rclone.conf on the volume.
# PUTIO_SCRIPT tells putioctl where the processor is (not ~/Scripts here).
# STATUSD_BIND: Drovie runs in another container, so loopback won't do; the
# port is only reachable on the compose network unless you publish it.
ENV DROVIE_DOWNLOADER_CONTAINER=1 \
    HOME=/config \
    PUTIO_SCRIPT=/app/putio_download_and_delete.sh \
    PYTHON_BIN=/usr/local/bin/python3 \
    RCLONE_CONFIG=/config/rclone.conf \
    STATUSD_BIND=0.0.0.0 \
    STATUSD_PORT=9494 \
    SYNC_INTERVAL_MINUTES=5 \
    PUID=1000 \
    PGID=1000 \
    TZ=UTC

VOLUME ["/config"]
EXPOSE 9494

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python3 -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('STATUSD_PORT','9494'), timeout=4)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
