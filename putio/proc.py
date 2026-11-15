"""External commands, with bash's retry ladder and bash's log lines.

Everything the download path shells out to — rclone, unzip, unar, numfmt, the
mount-gate helper — goes through here, so there is exactly one place that
decides how a failing command is retried and how the attempt reads in the log.

**The wording is bash's, deliberately.**  ``retry_command`` emits five distinct
messages and a run's log has been read against them for years; a port that
paraphrased them would make every existing eye-check and every ``grep`` in
``putioctl logs`` wrong at once.  :class:`Retrier` reproduces all five,
including the one that only fires from the second attempt onwards.

Two things here are *not* bash, both of them consequences of no longer being
in a shell:

* ``argv`` is a list and is never re-split.  Bash runs ``$RCLONE_CMD`` unquoted
  precisely so a value with a space would word-split, which is a hazard, not a
  feature: a pinned ``RCLONE_CMD`` under ``/Volumes/My Disk/`` splits into two
  words and the run dies with "command not found" naming only the first.
* stderr is captured in memory rather than through a temp file named after the
  job.  Bash needs the file because ``$$`` is the same in every download
  subshell; there are no subshells left to collide.
"""

from __future__ import annotations

import subprocess
from typing import Callable, List, Optional, Sequence

__all__ = ["CommandResult", "Retrier", "run", "which"]


class CommandResult:
    """What a finished command left behind."""

    __slots__ = ("argv", "returncode", "stdout", "stderr")

    def __init__(
        self,
        argv: Sequence[str],
        returncode: int,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self.argv = list(argv)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "CommandResult(argv={0!r}, returncode={1!r})".format(
            self.argv, self.returncode
        )


def which(name: str) -> Optional[str]:
    """``command -v``, without importing shutil's PATH quirks into a caller."""
    import shutil

    return shutil.which(name)


def run(
    argv: Sequence[str],
    stdin_text: Optional[str] = None,
    capture: bool = True,
    cwd: Optional[str] = None,
    timeout: Optional[float] = None,
) -> CommandResult:
    """Run ``argv`` and come back with a :class:`CommandResult`.

    Never raises for a non-zero exit — the caller decides what that means.  A
    binary that does not exist comes back as 127, which is the shell's own code
    for it and what the rest of this package already reserves for "missing".

    ``timeout`` is deliberately unset by every download call site: rclone is
    given ``--timeout``/``--contimeout`` of its own, and a wall-clock cap on a
    multi-gigabyte transfer would be a new way for a working download to fail.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - argv is built, never a string
            list(argv),
            input=stdin_text.encode("utf-8", "surrogateescape") if stdin_text else None,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            cwd=cwd,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        return CommandResult(argv, 127, "", str(exc))
    except PermissionError as exc:
        return CommandResult(argv, 126, "", str(exc))
    except subprocess.TimeoutExpired as exc:
        # 124 is what `timeout(1)` uses, and what the processor's own
        # `with_timeout` returns, so a caller that cares can tell the two apart.
        return CommandResult(argv, 124, "", str(exc))

    def _text(raw: Optional[bytes]) -> str:
        if not raw:
            return ""
        return raw.decode("utf-8", "surrogateescape")

    return CommandResult(
        argv, completed.returncode, _text(completed.stdout), _text(completed.stderr)
    )


class Retrier:
    """``retry_command``: N attempts, 5s -> 10s -> 20s, bash's exact messages.

    :param max_attempts: ``MAX_RETRIES``.  **This has no default in the
        processor** — it is read straight from ``.putio_config`` — so a config
        that omits it makes bash's ``while [ $attempt -le $max_attempts ]`` a
        syntax error and *every* retried command fails immediately with
        ``[: unary operator expected`` and nothing naming the cause.  Every
        download, every delete, every API call.  ``putio.config`` defaults it;
        the bash is unchanged, and that gap is on the record in CLAUDE.md.
    :param delay: ``RETRY_DELAY``, doubled after each failed attempt.
    :param on_log: ``(level, message)``, replayed by bash through ``log``.
    :param sleep: injected so the tests do not actually wait.
    """

    def __init__(
        self,
        max_attempts: int = 3,
        delay: int = 5,
        on_log: Optional[Callable[[str, str], None]] = None,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> None:
        # A config that says 0 would run nothing at all and report failure with
        # no attempt in the log; bash's `while` reaches the same place, but
        # silently.  One attempt is the smallest honest reading of "retry".
        self.max_attempts = max(1, int(max_attempts))
        self.delay = int(delay)
        self._on_log = on_log
        if sleep is None:
            import time

            sleep = time.sleep
        self._sleep = sleep

    def _log(self, level: str, message: str) -> None:
        if self._on_log is not None:
            self._on_log(level, message)

    def run(self, description: str, argv: Sequence[str], **kwargs) -> CommandResult:
        """Run ``argv`` until it succeeds or the ladder runs out.

        Returns the *last* result, so a caller can still read the exit code and
        stderr of the final attempt.
        """
        delay = self.delay
        result = CommandResult(argv, 1)

        for attempt in range(1, self.max_attempts + 1):
            if attempt > 1:
                self._log(
                    "WARN",
                    "[RETRY] Attempt {0}/{1}: {2}".format(
                        attempt, self.max_attempts, description
                    ),
                )

            result = run(argv, **kwargs)
            if result.ok:
                if attempt > 1:
                    self._log(
                        "INFO",
                        "[RETRY] Success on attempt {0}: {1}".format(
                            attempt, description
                        ),
                    )
                return result

            # Bash logs the captured stderr only when the file is non-empty.
            # rclone's is empty on the download path because --log-file takes
            # everything, which is why progress_error_tail exists at all.
            if result.stderr.strip():
                self._log("ERROR", "[RETRY] Error output: " + result.stderr)

            if attempt < self.max_attempts:
                self._log(
                    "WARN",
                    "[RETRY] Attempt {0} failed: {1}. Retrying in {2}s...".format(
                        attempt, description, delay
                    ),
                )
                if delay > 0:
                    self._sleep(delay)
                delay *= 2
            else:
                self._log(
                    "ERROR",
                    "[RETRY] All {0} attempts failed: {1}".format(
                        self.max_attempts, description
                    ),
                )

        return result
