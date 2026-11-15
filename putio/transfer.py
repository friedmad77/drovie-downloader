"""One file, from put.io to ``LOCAL_DIR`` — ``download_file_run`` in Python.

The shape is bash's, step for step: size, disk check, publish the bar, copy,
and — when ``USE_TEMP_DIR`` is on — a second copy from the staging volume onto
the SMB share.  Every log line is bash's wording, because a run's log is read
against those strings.

Three things are deliberately different, all of them on failure paths:

* **The staged copy is attempted once.**  Bash's ``cp`` failure branch runs the
  same ``cp`` a *second* time purely to capture stderr for the message.  On a
  multi-gigabyte file that is minutes of re-copying after a failure, and if the
  retry happens to succeed the run still logs failure, still deletes the source
  and still keeps the put.io copy.  Here the error text comes from the
  exception the one attempt raised.
* **A staging directory is removed however the transfer ends.**  Bash removes
  it on each of its five exits and would leak it on a sixth; here it is a
  ``finally``.  ``staging_sweep`` reclaims a leak on the next run, but only
  after a full-size media file has sat in the very space ``check_disk_space``
  measures.
* **"Could not determine size" now fires when rclone actually failed.**  Bash
  tests the exit status of ``rclone lsjson | jq``, which is *jq's* — and jq
  exits 0 while printing nothing — so that branch is nearly unreachable and a
  broken ``RCLONE_CMD`` reports "Invalid size" instead.  The two causes are
  told apart here.

The wrapper/`finally` structure exists for the same reason
:func:`putio.progress.Job.__exit__` does: this function returns from a dozen
places and a missed cleanup leaves a finished download sitting at 90% in every
reader until the run exits.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from typing import Callable, Optional

from .proc import run, which

__all__ = ["Downloader", "free_bytes", "human_bytes"]

_GIB = 1024 * 1024 * 1024


def human_bytes(size: int) -> str:
    """What bash puts in the "Starting" line.

    ``numfmt --to=iec-i --suffix=B`` when it is installed and the raw byte
    count when it is not — which is bash's own fallback, and on a stock macOS
    without Homebrew's coreutils it is the branch that runs.  Shelling out
    rather than reimplementing GNU's rounding keeps the two identical on the
    machine that has it.
    """
    numfmt = which("numfmt")
    if numfmt:
        result = run([numfmt, "--to=iec-i", "--suffix=B", str(size)])
        if result.ok and result.stdout.strip():
            return result.stdout.strip()
    return "{0} bytes".format(size)


def _short_size(size: int) -> str:
    """``ls -lh``-style, for the DEBUG line that reports the staged file."""
    value = float(size)
    for suffix in ("B", "K", "M", "G", "T"):
        if value < 1024 or suffix == "T":
            if suffix == "B":
                return "{0}B".format(int(value))
            return "{0:.1f}{1}".format(value, suffix)
        value /= 1024
    return "{0}B".format(size)  # pragma: no cover - unreachable


def free_bytes(path: str) -> Optional[int]:
    """Free space on ``path``'s volume, or None if it cannot be measured.

    ``f_bavail`` — space available to an unprivileged process — which is the
    column ``df`` prints as "Avail" and the one bash reads.  Bash forces
    ``df -k`` because macOS defaults to 512-byte blocks and would otherwise
    report twice the real figure; there are no blocks to get wrong here.
    """
    try:
        stats = os.statvfs(path)
    except OSError:
        return None
    return stats.f_bavail * stats.f_frsize


class Downloader:
    """Transfers for one run.

    :param rclone: a :class:`putio.rclone.RcloneClient`.
    :param progress: a :class:`putio.progress.ProgressPublisher`.
    :param log: ``(level, message)``.
    :param on_complete: called with the basename of every file that landed —
        the ``note_download_done`` half, which the caller turns into a line in
        ``DOWNLOADS_DONE_FILE`` so the run summary can count what *arrived*
        rather than what the listing offered.
    """

    def __init__(
        self,
        config,
        rclone,
        progress,
        log: Callable[[str, str], None],
        on_complete: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._config = config
        self._rclone = rclone
        self._progress = progress
        self._log = log
        self._on_complete = on_complete

    # ------------------------------------------------------------------
    @property
    def staging_root(self) -> str:
        return self._config.staging_dir or os.path.expanduser("~/.putio_staging")

    def check_disk_space(self, required: int, file_name: str) -> bool:
        """``check_disk_space``, message for message.

        The volume checked is the one the bytes land on *first*: the staging
        volume when ``USE_TEMP_DIR`` is on, ``LOCAL_DIR`` otherwise.
        """
        config = self._config
        check_path = self.staging_root if config.use_temp_dir else config.local_dir

        available = free_bytes(check_path)
        if not available:
            self._log(
                "WARN", "[DISK] Could not determine available disk space, proceeding anyway"
            )
            return True

        required_gb = required // _GIB
        available_gb = available // _GIB
        min_free_bytes = config.min_free_space_gb * _GIB

        self._log(
            "DEBUG",
            "[DISK] Required: {0}GB, Available: {1}GB, Min free: {2}GB".format(
                required_gb, available_gb, config.min_free_space_gb
            ),
        )

        if available - required < min_free_bytes:
            self._log("ERROR", "[DISK] Insufficient disk space for " + file_name)
            self._log(
                "ERROR",
                "[DISK] Required: {0}GB + {1}GB buffer = {2}GB".format(
                    required_gb,
                    config.min_free_space_gb,
                    required_gb + config.min_free_space_gb,
                ),
            )
            self._log("ERROR", "[DISK] Available: {0}GB".format(available_gb))
            return False

        return True

    # ------------------------------------------------------------------
    def fetch(self, remote_file: str, local_dir: str) -> bool:
        """Bring ``remote_file`` down into ``local_dir``.  True if it landed."""
        file_name = os.path.basename(remote_file)

        size = self._rclone.size_of(remote_file)
        if size is None:
            self._log("ERROR", "[DOWNLOAD] Could not determine size for " + remote_file)
            return False
        if size == 0:
            self._log("ERROR", "[DOWNLOAD] Invalid size for " + remote_file)
            return False

        if not self.check_disk_space(size, file_name):
            return False

        self._log(
            "INFO",
            "[DOWNLOAD] Starting: {0} ({1})".format(remote_file, human_bytes(size)),
        )

        destination = os.path.join(local_dir, file_name)
        with self._progress.begin(remote_file, str(size), destination) as job:
            if self._config.use_temp_dir:
                return self._staged(remote_file, local_dir, file_name, job)
            return self._direct(remote_file, local_dir, file_name, job)

    # ------------------------------------------------------------------
    def _staged(self, remote_file: str, local_dir: str, file_name: str, job) -> bool:
        """Download to a local staging volume, then copy onto the share.

        This insulates an in-flight transfer from a share that drops: only the
        final copy has to survive the network.  ``STAGING_DIR`` must be on
        local disk — /tmp is volatile across reboots and fills the boot volume.
        """
        staging_root = self.staging_root
        try:
            os.makedirs(staging_root, exist_ok=True)
        except OSError:
            self._log(
                "ERROR", "[DOWNLOAD] Failed to create staging root: " + staging_root
            )
            return False

        try:
            # Per job, never per run: the name is swept against boot time by
            # `staging_sweep`, which reads the pid out of it, so the run's pid
            # is the one that belongs here — but the random tail is what keeps
            # two concurrent jobs from sharing a directory and deleting each
            # other's file mid-transfer.
            temp_dir = tempfile.mkdtemp(
                prefix="dl_{0}_".format(self._progress.run_pid), dir=staging_root
            )
        except OSError:
            self._log(
                "ERROR",
                "[DOWNLOAD] Failed to create staging dir under: " + staging_root,
            )
            return False

        try:
            result = self._rclone.copy(
                remote_file, temp_dir, job.log_path, inplace=False
            )
            if not result.ok:
                self._log(
                    "ERROR",
                    "[DOWNLOAD] Failed to download {0} after {1} attempts{2}".format(
                        file_name, self._config.max_retries, job.error_tail()
                    ),
                )
                return False

            staged = os.path.join(temp_dir, file_name)
            if not os.path.isfile(staged):
                self._log(
                    "ERROR",
                    "[DOWNLOAD] File not found in temp directory: " + staged,
                )
                self._log(
                    "DEBUG",
                    "[DOWNLOAD] Temp directory contents: " + _listing(temp_dir),
                )
                return False

            self._log(
                "DEBUG",
                "[DOWNLOAD] File downloaded to temp: {0} ({1})".format(
                    staged, _short_size(os.path.getsize(staged))
                ),
            )
            self._log("DEBUG", "[DOWNLOAD] Attempting to move to: " + local_dir + "/")

            final = os.path.join(local_dir, file_name)
            job.phase("copy", final)
            try:
                shutil.copyfile(staged, final)
            except (OSError, shutil.Error) as exc:
                self._log(
                    "ERROR",
                    "[DOWNLOAD] Failed to copy {0} from temp to final location: {1}".format(
                        file_name, exc
                    ),
                )
                self._log("DEBUG", "[DOWNLOAD] Source file: " + _describe(staged))
                self._log(
                    "DEBUG", "[DOWNLOAD] Destination dir: " + _describe(local_dir)
                )
                return False

            try:
                os.unlink(staged)
            except OSError:
                pass
            self._log("INFO", "[DOWNLOAD] Completed: " + file_name)
            self._note(file_name)
            return True
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    # ------------------------------------------------------------------
    def _direct(self, remote_file: str, local_dir: str, file_name: str, job) -> bool:
        self._log(
            "DEBUG",
            "[DOWNLOAD] Downloading directly to final destination: " + local_dir + "/",
        )
        result = self._rclone.copy(remote_file, local_dir, job.log_path, inplace=True)
        if not result.ok:
            self._log(
                "ERROR",
                "[DOWNLOAD] Failed to download {0} after {1} attempts{2}".format(
                    file_name, self._config.max_retries, job.error_tail()
                ),
            )
            return False

        final = os.path.join(local_dir, file_name)
        if os.path.isfile(final):
            self._log("INFO", "[DOWNLOAD] Completed: " + file_name)
            self._note(file_name)
            return True

        self._log("ERROR", "[DOWNLOAD] File not found after download: " + final)
        return False

    # ------------------------------------------------------------------
    def _note(self, file_name: str) -> None:
        if self._on_complete is not None:
            self._on_complete(file_name)


def _listing(path: str) -> str:
    try:
        return " ".join(sorted(os.listdir(path)))
    except OSError:
        return "directory not found"


def _describe(path: str) -> str:
    try:
        stats = os.stat(path)
    except OSError:
        return "not found"
    return "{0} bytes, mode {1:o}".format(stats.st_size, stats.st_mode & 0o7777)
