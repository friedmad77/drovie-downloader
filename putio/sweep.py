"""The startup sweeps — what the last run left behind.

These run once at the top of a run, before any work, and every one of them
exists because a run that is *killed* never reaches its cleanup.  That is not
a defect to design away: crash-only operation means recovery is "start again
and look at the world", and these are the looking.

Four sweeps, and they are transcriptions of ``progress_sweep``,
``staging_sweep``, ``cleanup_smbdelete_tombstones`` and the marker half of
``cleanup_old_markers``.

**The boot-time guard is the subtle one.**  Both the progress and staging
sweeps decide whether an entry still has an owner by parsing a pid out of its
name and asking whether that pid is alive.  A pid only identifies a process
*within its own boot* — after a restart the kernel reissues the same numbers,
so ``kill -0`` reports a recycled pid as alive and the entry is then skipped by
its sweep **forever**.  So an entry also has to have been touched since the
current boot; anything older has no owner whatever its pid now points at.

That matters most for staging, where the leak is a full-size media file in the
very space the disk check measures — a few interrupted large downloads and the
queue stops with "Insufficient disk space" and nothing saying why.

The boot time is read **once**, at import of the run, for the same reason bash
resolves it into a variable rather than calling a function: a ``$(boot_epoch)``
call site runs in a subshell, so a cache assigned inside the function never
reached the parent and both sweeps spawned a ``sysctl`` and a ``sed`` for every
entry they looked at — the per-item subprocess pattern that once cost 36s of an
idle run.
"""

from __future__ import annotations

import errno
import os
import re
import subprocess
from typing import Callable, List, Optional, Sequence

from . import archives

__all__ = [
    "boot_epoch",
    "owner_is_live",
    "sweep_progress",
    "sweep_staging",
    "sweep_tombstones",
    "sweep_markers",
]

#: ``{ sec = 1755950000, usec = 0 } Fri Aug 22 09:33:20 2026`` — the *first*
#: number on the line.  Matching ``sec = `` instead lets a greedy prefix run on
#: and capture the ``usec`` field, which is the bug the bash comment warns
#: about; anchoring on the first digit run cannot.
_BOOTTIME = re.compile(r"[0-9]+")

_TOMBSTONE_TIMEOUT = 30

_cached_boot: Optional[int] = None


def boot_epoch(refresh: bool = False) -> int:
    """When this machine last booted, in epoch seconds; 0 if unknown.

    0 puts every mtime "since boot", which makes the pid check decide on its
    own exactly as it did before this guard existed — the guard only ever adds
    a reason to sweep, never a reason to keep.
    """
    global _cached_boot
    if _cached_boot is not None and not refresh:
        return _cached_boot

    value = 0
    try:
        out = subprocess.run(
            ["sysctl", "-n", "kern.boottime"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).stdout.decode("ascii", "replace")
        match = _BOOTTIME.search(out)
        if match:
            value = int(match.group(0))
    except (OSError, ValueError, subprocess.SubprocessError):
        value = 0

    if not value:
        # Linux, for running the harness off a Mac.
        try:
            with open("/proc/stat", "r") as handle:
                for line in handle:
                    if line.startswith("btime "):
                        value = int(line.split()[1])
                        break
        except (OSError, ValueError, IndexError):
            value = 0

    _cached_boot = value
    return value


def _mtime(path: str) -> Optional[float]:
    try:
        return os.lstat(path).st_mtime
    except OSError:
        return None


def _is_live(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


def owner_is_live(path: str, pid_text: str) -> bool:
    """Can the pid in ``path``'s name still own it?

    Only if the entry has been touched since boot *and* that pid is alive.  A
    directory's mtime moves when an entry is added, so a live transfer's
    staging directory is always post-boot even though rclone growing the file
    inside it never touches the directory again.
    """
    if not pid_text.isdigit():
        return False
    mtime = _mtime(path)
    # An mtime we could not read falls through to the pid check alone, the
    # same as an unknown boot time.
    if mtime is not None and mtime < boot_epoch():
        return False
    return _is_live(int(pid_text))


def _remove_file(path: str) -> bool:
    try:
        os.unlink(path)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# progress
# ---------------------------------------------------------------------------
def sweep_progress(state_dir: Optional[str]) -> int:
    """``progress_sweep`` — drop bars whose run is gone.

    Nothing else could tell a live transfer from one a killed run left frozen
    on screen, which is why the run's pid is in the filename at all.
    """
    if not state_dir or not os.path.isdir(state_dir):
        return 0
    removed = 0
    try:
        entries = os.listdir(state_dir)
    except OSError:
        return 0
    for entry in entries:
        if not entry.startswith("dl."):
            continue
        path = os.path.join(state_dir, entry)
        # dl.<runpid>.<jobid>.meta — `cut -d. -f2`.
        parts = entry.split(".")
        pid_text = parts[1] if len(parts) > 1 else ""
        if owner_is_live(path, pid_text):
            continue
        if _remove_file(path):
            removed += 1
    return removed


# ---------------------------------------------------------------------------
# staging
# ---------------------------------------------------------------------------
def sweep_staging(staging_root: str, log: Callable[[str, str], None]) -> int:
    """``staging_sweep`` — reclaim the space a killed transfer is still holding.

    The ``mktemp`` name is never reused, so without this nothing would ever
    remove these, and they are full-size media files in the space the disk
    check measures.
    """
    if not staging_root or not os.path.isdir(staging_root):
        return 0
    removed = 0
    try:
        entries = os.listdir(staging_root)
    except OSError:
        return 0
    for entry in entries:
        if not entry.startswith("dl_"):
            continue
        path = os.path.join(staging_root, entry)
        if not os.path.isdir(path):
            continue
        # dl_<pid>_XXXXXX — `cut -d_ -f2`.
        parts = entry.split("_")
        pid_text = parts[1] if len(parts) > 1 else ""
        if owner_is_live(path, pid_text):
            continue
        if _rmtree(path):
            removed += 1
    if removed:
        log(
            "INFO",
            "[CLEANUP] Removed {0} abandoned staging dir(s) from {1}".format(
                removed, staging_root
            ),
        )
    return removed


def _rmtree(path: str) -> bool:
    import shutil

    try:
        shutil.rmtree(path)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# .smbdelete tombstones
# ---------------------------------------------------------------------------
def _human_bytes(count: int) -> str:
    """``numfmt --to=iec-i --suffix=B``, which is not on a stock macOS.

    Bash fell back to printing the raw byte count when numfmt was missing, so
    on this machine the message has always been the plain number.  Rendering it
    properly is a message change, and a small one, but it is a change: noted
    here so the difference in the log is not mistaken for a bug.
    """
    if count < 1024:
        return "{0} bytes".format(count)
    value = float(count)
    for suffix in ("KiB", "MiB", "GiB", "TiB", "PiB"):
        value /= 1024.0
        if value < 1024.0:
            return "{0:.1f}{1}".format(value, suffix)
    return "{0:.1f}EiB".format(value)


def sweep_tombstones(local_dir: str, log: Callable[[str, str], None]) -> int:
    """``cleanup_smbdelete_tombstones`` — finish deletes macOS gave up on.

    macOS renames a file to ``.smbdeleteXXXX`` when an SMB unlink cannot
    complete — something still has it open, the Plex scanner walking this
    folder being the likely one — and retries later.  That retry only lives as
    long as the mount session, so a remount or a reboot orphans the tombstone
    permanently: a full-size file nothing will ever remove.  42 of them
    totalling 31GB had collected here over four months before anyone noticed.

    No age guard, because none is possible: renaming on this share leaves both
    mtime and ctime at the original file's values, so a tombstone made a minute
    ago can look months old.  None is needed either — a tombstone is a deletion
    the OS already committed to.

    The removals are logged at INFO and the *failures* at WARN every run, on
    purpose: a count that keeps growing means deletes are racing something that
    holds files open, and that is worth seeing rather than tidying away.
    """
    if not local_dir or not os.path.isdir(local_dir):
        return 0

    removed = 0
    stuck = 0
    total = 0
    try:
        entries = sorted(os.listdir(local_dir))
    except OSError:
        return 0

    for entry in entries:
        if not entry.startswith(".smbdelete"):
            continue
        path = os.path.join(local_dir, entry)
        try:
            stat = os.lstat(path)
        except OSError:
            continue
        if not os.path.isfile(path):
            continue
        size = stat.st_size

        if _unlink_with_timeout(path):
            removed += 1
            total += size
        else:
            stuck += 1
            log("DEBUG", "[SMBDELETE] Could not remove {0}".format(entry))

    if removed:
        log(
            "INFO",
            "[SMBDELETE] Removed {0} delete-pending file(s) macOS left behind, {1}".format(
                removed, _human_bytes(total)
            ),
        )
    if stuck:
        log(
            "WARN",
            "[SMBDELETE] {0} delete-pending file(s) could not be removed — "
            "something still holds them open".format(stuck),
        )
    if not removed and not stuck:
        log("DEBUG", "[SMBDELETE] No delete-pending files left behind")
    return removed


def _unlink_with_timeout(path: str) -> bool:
    """``with_timeout 30 rm -f`` — an unlink on a dropped share never returns.

    This is the one place in the package where a plain ``os.unlink`` is not
    good enough: the call sits in the kernel uninterruptibly when the NAS goes
    away, and a delete that hangs must not take the whole run with it.  There
    is no way to time out a syscall in-process, so the unlink happens in a
    child that can be killed — the same trade bash made by shelling to ``rm``
    under ``with_timeout``.
    """
    try:
        proc = subprocess.Popen(
            ["/bin/rm", "-f", path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    try:
        return proc.wait(timeout=_TOMBSTONE_TIMEOUT) == 0
    except subprocess.TimeoutExpired:
        # Wedged in the kernel: SIGKILL will not land until the I/O returns,
        # so it is sent and the child abandoned rather than waited on.  Waiting
        # on something in uninterruptible sleep is the wedge being avoided —
        # the same rule putio-statusd follows for its timed-out putioctl.
        try:
            proc.kill()
        except OSError:
            pass
        return False


# ---------------------------------------------------------------------------
# extraction markers
# ---------------------------------------------------------------------------
def sweep_markers(local_dir: str, log: Callable[[str, str], None]) -> int:
    """``cleanup_old_markers`` — drop markers whose archive is gone.

    The rules live in :mod:`putio.archives`; this is the pass over the
    directory.  One listing for the whole directory rather than bash's
    re-glob per marker.
    """
    if not local_dir:
        return 0
    try:
        entries = os.listdir(local_dir)
    except OSError as exc:
        # A dropped SMB share reaches here.  Bash's glob would simply match
        # nothing and it would sweep nothing; say so rather than claiming a
        # clean sweep of a directory that could not be read.
        log("DEBUG", "[MARKER] Could not read {0}: {1}".format(local_dir, exc))
        return 0

    markers = [e for e in entries if e.startswith(archives.MARKER_PREFIX)]
    orphans = archives.orphaned_markers(entries)

    cleaned = 0
    for marker in orphans:
        try:
            os.remove(os.path.join(local_dir, marker))
        except OSError as exc:
            log("DEBUG", "[MARKER] Could not remove {0}: {1}".format(marker, exc))
            continue
        cleaned += 1
        log(
            "DEBUG",
            "[MARKER] Removed orphaned marker: {0}".format(
                marker[len(archives.MARKER_PREFIX):]
            ),
        )

    # Bash's `if [ $marker_count -gt 0 ]`: silent when there was nothing to
    # sweep, which is what keeps an idle run to one line.
    if markers:
        log(
            "INFO",
            "[MARKER] Cleaned up {0} of {1} extraction markers".format(
                cleaned, len(markers)
            ),
        )
    return cleaned


def sweep_all(
    config,
    log: Callable[[str, str], None],
    staging_root: Optional[str] = None,
) -> None:
    """Every startup sweep, in the order the processor ran them."""
    sweep_markers(config.local_dir, log)
    sweep_progress(config.progress_state_dir)
    sweep_staging(
        staging_root or config.staging_dir or os.path.expanduser("~/.putio_staging"),
        log,
    )
    sweep_tombstones(config.local_dir, log)
