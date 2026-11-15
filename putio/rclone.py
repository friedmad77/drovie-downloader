"""put.io, through rclone — listing, sizes, transfers and deletes.

rclone stays an external binary, as the plan says it should.  What moves here
is only *how it is called*, and two of those calls change shape:

* **Sizes are fetched once for the whole listing.**  Bash runs
  ``rclone lsjson`` twice per file — once inside ``is_junk_file`` to spot a
  sub-1MB decoy, and again inside ``download_file_run`` to size the transfer
  for the disk check — each piped through ``jq``.  That is two subprocess pairs
  per entry in the listing, which is precisely the shape the batching rule
  exists to forbid.  One ``lsjson --recursive`` answers both questions for
  every file, and a listing that fails to parse falls back to bash's per-file
  lookup rather than guessing.
* **The JSON is parsed with ``json``, not ``jq``.**  One fewer binary on the
  path, and one fewer place where cron's jq and an interactive shell's jq can
  disagree about the same bytes.

Everything else is bash's, flag for flag, including ``nice -n 10`` and the
``--retries 10 --low-level-retries 20 --timeout 5m --contimeout 1m`` block.
There is deliberately no wall-clock cap on a transfer: rclone's own timeouts
cover a stalled connection, and a ceiling on a multi-gigabyte copy over SMB
would be a new way for a working download to fail.
"""

from __future__ import annotations

import json
import os
import posixpath
import threading
from typing import Dict, List, Optional

from .proc import CommandResult, Retrier, run

__all__ = ["RcloneClient", "parse_lsjson"]


def parse_lsjson(text: str) -> Dict[str, int]:
    """``rclone lsjson --recursive`` -> ``{Path: Size}``.

    Only entries with a usable integer size are kept.  Directories carry
    ``IsDir`` and a size of -1; both are dropped, because a caller asking "how
    big is this" must get "I do not know" rather than a negative number that
    would pass a disk check.
    """
    try:
        entries = json.loads(text)
    except (ValueError, TypeError):
        return {}
    if not isinstance(entries, list):
        return {}

    sizes = {}  # type: Dict[str, int]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("IsDir"):
            continue
        path = entry.get("Path")
        size = entry.get("Size")
        if not isinstance(path, str) or not isinstance(size, int):
            continue
        if size < 0:
            continue
        sizes[path] = size
    return sizes


class RcloneClient:
    """Every rclone call the download path makes."""

    def __init__(self, config, retrier: Retrier, log=None) -> None:
        self._config = config
        self._retrier = retrier
        self._log = log if log is not None else (lambda level, message: None)
        self._sizes = None  # type: Optional[Dict[str, int]]
        # The listing is primed once before the pool starts, so in production
        # this lock is never contended.  It is here because "never contended"
        # depends on a call order two files apart: a caller that reached
        # size_of() first from a worker thread would otherwise have two threads
        # each spawn `rclone lsjson --recursive` over the whole remote.
        self._sizes_lock = threading.Lock()

    # ------------------------------------------------------------------
    # locations
    # ------------------------------------------------------------------
    def remote(self, path: str = "") -> str:
        """``REMOTE`` plus a path, concatenated the way bash does it.

        ``REMOTE`` carries its own trailing ``:`` or ``:/``, so this is a join
        only in the sense that ``"$REMOTE$FILE"`` is.
        """
        return self._config.remote + path

    # ------------------------------------------------------------------
    # reading
    # ------------------------------------------------------------------
    def list_files(self) -> Optional[List[str]]:
        """``lsf --files-only --recursive``, or None if rclone failed.

        Through the retry ladder, unlike bash's ``lsf … 2>/dev/null``: a put.io
        blip used to end the run with "Failed to list files" and nothing saying
        why, because rclone's stderr was thrown away.  It is a read, so retrying
        is free of side effects, and the ladder logs rclone's own message on
        every failed attempt.
        """
        result = self._retrier.run(
            "list files on put.io",
            [self._config.rclone_cmd, "lsf", "--files-only", "--recursive", self.remote()],
        )
        if not result.ok:
            return None
        return [line for line in result.stdout.splitlines() if line]

    def list_dir(self, subdir: str) -> Optional[List[str]]:
        """``lsf`` of one directory — what the archive-parts rules are given."""
        result = run([self._config.rclone_cmd, "lsf", self.remote(subdir)])
        if not result.ok:
            return None
        return [line for line in result.stdout.splitlines() if line]

    def list_dirs(self) -> Optional[List[str]]:
        """``lsf --dirs-only --recursive``, deepest first.

        The end-of-run empty-folder pass needs children before parents, or
        emptying a leaf leaves its now-empty parent for the *next* run.  Bash
        got that ordering by reversing rclone's output with an awk one-liner,
        which works because ``--recursive`` lists a parent before its children;
        this reverses the list for the same reason.
        """
        result = run(
            [self._config.rclone_cmd, "lsf", "--dirs-only", "--recursive", self.remote()]
        )
        if not result.ok:
            return None
        return [line for line in reversed(result.stdout.splitlines()) if line]

    def sizes(self) -> Dict[str, int]:
        """Every file's size, fetched once and cached for the run.

        An empty dict is a legitimate answer — an empty put.io, or an rclone
        that failed — and callers fall back to :meth:`size_of` for anything
        they cannot find here, so a failure costs a per-file lookup rather
        than a wrong disk check.
        """
        with self._sizes_lock:
            if self._sizes is None:
                result = run(
                    [self._config.rclone_cmd, "lsjson", "--recursive", self.remote()]
                )
                if result.ok:
                    self._sizes = parse_lsjson(result.stdout)
                else:
                    self._sizes = {}
                    self._log(
                        "DEBUG",
                        "[PUTIO] Could not list sizes in one call — falling back per file",
                    )
            return self._sizes

    def size_of(self, remote_file: str) -> Optional[int]:
        """One file's size: the cached listing first, then rclone.

        The fallback is bash's own call, minus the ``jq``.  ``None`` means "no
        answer", which the caller must not confuse with zero — bash conflates
        them into one ``[[ -z ... || ... -eq 0 ]]`` test and then logs the same
        "Invalid size" line for both.
        """
        cached = self.sizes().get(remote_file.lstrip("/"))
        if cached is not None:
            return cached

        result = run([self._config.rclone_cmd, "lsjson", self.remote(remote_file)])
        if not result.ok:
            return None
        try:
            entries = json.loads(result.stdout)
        except (ValueError, TypeError):
            return None
        if not isinstance(entries, list) or not entries:
            return None
        first = entries[0]
        if not isinstance(first, dict):
            return None
        size = first.get("Size")
        if not isinstance(size, int) or size < 0:
            return None
        return size

    # ------------------------------------------------------------------
    # writing
    # ------------------------------------------------------------------
    def copy(self, remote_file: str, destination: str, log_path: str, inplace: bool):
        """One ``rclone copy``, through the retry ladder.

        ``--inplace`` is bash's: set on the direct-to-share path and not on the
        staged one, where the file is already landing on local disk.
        """
        config = self._config
        argv = [
            "nice",
            "-n",
            "10",
            config.rclone_cmd,
            "copy",
            "--transfers={0}".format(config.rclone_transfers),
            "--checkers={0}".format(config.rclone_checkers),
            "--multi-thread-streams={0}".format(config.rclone_multi_threads),
            "--buffer-size={0}".format(config.rclone_buffer_size),
            "--bwlimit={0}".format(config.rclone_bwlimit),
            "--retries",
            "10",
            "--low-level-retries",
            "20",
            "--timeout",
            "5m",
            "--contimeout",
            "1m",
        ]
        if inplace:
            argv.append("--inplace")
        if log_path:
            # Only when progress is on: rclone rejects --log-file="" outright,
            # and without a log file there is nothing for --use-json-log to
            # write to.
            argv.extend(
                [
                    "--log-file={0}".format(log_path),
                    "--use-json-log",
                    "--stats={0}".format(config.rclone_stats_interval),
                    "--stats-log-level",
                    "NOTICE",
                ]
            )
        argv.extend([self.remote(remote_file), destination])

        return self._retrier.run(
            "download {0}".format(posixpath.basename(remote_file)), argv
        )

    def delete(self, remote_path: str, description: str) -> bool:
        """``rclone delete``, through the retry ladder.

        The binary is ``RCLONE_CMD``, never the bare word ``rclone``.  Four
        delete call sites in the processor used to run the bare word and so
        bypassed the pin that exists precisely so cron and an interactive shell
        cannot resolve different binaries; they were fixed in phase 3 and there
        is no way to reintroduce it from here.
        """
        result = self._retrier.run(
            description, [self._config.rclone_cmd, "delete", self.remote(remote_path)]
        )
        return result.ok

    def rmdir(self, remote_path: str, description: str) -> bool:
        result = self._retrier.run(
            description, [self._config.rclone_cmd, "rmdir", self.remote(remote_path)]
        )
        return result.ok
