"""Configuration, as handed over by the bash entry point.

The processor sources ``.putio_config`` and then runs Python with the resolved
values on its environment (see ``putio_python`` in
``putio_download_and_delete.sh``).  This module reads ``os.environ`` and
nothing else.

**There is deliberately no bash-config parser here.**  ``.putio_config`` is a
bash file that is *sourced*: values reference ``$HOME``, and nothing stops one
using command substitution or a conditional.  A Python reader for it would be a
second implementation of bash's semantics that has to agree with the first one
forever, and it would have to be written before anything else could be ported —
the riskiest possible place to start.  The shell that the file is written in
reads it, once, and passes the answers on.

The cost of that choice is a small asymmetry: a child process cannot tell a
variable that was unset from one that was set to the empty string.  So an empty
value here means "not configured" and the default below applies.  The one place
where empty is itself a setting is :attr:`Config.progress_state_dir`, and that
case is spelled out rather than defaulted.

Defaults must match the ones in the bash script.  Where they disagree the bash
script wins, because it is what actually runs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from typing import Any, ClassVar, Dict, Mapping, Optional, Tuple

__all__ = ["Config", "SECRET_FIELDS", "REDACTED"]

#: Fields whose values must never be printed, logged or returned in JSON.
SECRET_FIELDS = frozenset(
    {"tmdb_api_key", "plex_token", "drovie_webhook_key"}
)

REDACTED = "<redacted>"

#: Bash has no booleans, and this script does not test them one way.  Two
#: conventions are in use and they disagree, so each is mirrored exactly rather
#: than collapsed into one allowlist:
#:
#: * ``[ "${USE_TEMP_DIR:-false}" = "true" ]`` — only the literal ``true``
#:   enables it, so ``yes`` and ``1`` are *off*.
#: * ``[ "${DELETE_AFTER_DOWNLOAD}" != "false" ]`` — only the literal ``false``
#:   disables it, so anything else, including ``0``, is *on*.
#:
#: Both comparisons are byte-exact and case-sensitive in the shell, so neither
#: helper lowercases.  Guessing a friendlier reading here would make Python
#: stage to a volume bash never used, or keep a file bash would have deleted.
_TRUE_LITERAL = "true"
_FALSE_LITERAL = "false"


def _text(env: Mapping[str, str], name: str, default: str = "") -> str:
    value = env.get(name, "")
    return value if value else default


# The put.io folder Drovie adds transfers to, not the account root. Everything
# the processor deletes is under REMOTE, so a default of "putio:" would let a
# new install empty a whole account of files it never asked for.
DEFAULT_REMOTE = "putio:Drovie/"


def _remote(env: Mapping[str, str]) -> str:
    """REMOTE, made safe to concatenate.

    rclone.remote() joins REMOTE and a path the way bash's "$REMOTE$FILE" did,
    so "putio:Drovie" + "a.mkv" would name "putio:Droviea.mkv". The account
    root ("putio:") and an explicit trailing slash pass through unchanged;
    a bare folder gets its slash.
    """
    value = _text(env, "REMOTE", DEFAULT_REMOTE)
    if not value.endswith((":", "/")):
        value += "/"
    return value


def _exactly_true(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    """Mirror ``[ "${NAME:-<default>}" = "true" ]``."""
    value = env.get(name, "")
    if not value:
        return default
    return value == _TRUE_LITERAL


def _unless_false(env: Mapping[str, str], name: str) -> bool:
    """Mirror ``[ "${NAME}" != "false" ]``.

    No default parameter: bash's own default for the one setting that uses this
    form is ``true``, and empty already compares unequal to ``false``, so absent
    and "anything but false" are the same answer.
    """
    return env.get(name, "") != _FALSE_LITERAL


def _seconds(env: Mapping[str, str], name: str, default: float) -> float:
    """Like :func:`_number`, but fractional.

    Only for values bash hands to ``sleep``, which accepts ``0.2`` — parsing
    those with ``int()`` would reject a perfectly good setting and fall back to
    a default several times larger, with nothing in the log to say so.
    """
    value = env.get(name, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _number(env: Mapping[str, str], name: str, default: int) -> int:
    """An int, falling back rather than raising.

    A malformed number in the config must not be able to stop a run: the bash
    side treats these the same way (``RCLONE_STATS_INTERVAL`` warns and falls
    back rather than letting rclone fail at flag-parse time on every download).
    """
    value = env.get(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    """The settings the Python side is allowed to see.

    Every field here corresponds to a name in ``PUTIO_PY_VARS`` in the bash
    script; :meth:`env_names` is what the test harness compares against that
    list, so a field added on one side and forgotten on the other is caught
    rather than silently arriving empty.
    """

    # -- where things go ----------------------------------------------------
    remote: str = DEFAULT_REMOTE
    local_dir: str = ""
    staging_dir: str = ""
    use_temp_dir: bool = False
    movie_library: str = ""
    tv_library: str = ""
    unmatched_dir: str = ""
    media_mount_point: str = ""

    # -- run state ----------------------------------------------------------
    # ``$$`` of the bash process, not this interpreter's pid.  It names every
    # progress file, and readers call ``kill -0`` on it to tell a live transfer
    # from what a killed run left behind — so publishing under the child's pid
    # would make every bar vanish the moment the batch ended.  See
    # :mod:`putio.progress`.
    run_pid: str = ""
    # The processor itself, so the download loop can re-enter it for the one
    # thing that stays in bash: ``--mount-gate``.  See :mod:`putio.mount`.
    script_path: str = ""
    # Which ``.putio_config`` the shim actually sourced — beside the script or
    # in ``$HOME``.  Reported by the ``[ENV]`` banner and by ``config-dump``,
    # and it is the first thing to check when a deployed run and a hand-run one
    # disagree: they can legitimately be reading two different files.
    config_file: str = ""
    log_file: str = ""
    log_level: str = "INFO"
    lockfile: str = "/tmp/putio_download_and_delete.lock"
    # ``$HOME/.putio_paused``.  Deliberately not under /tmp: a pause should
    # survive a reboot rather than quietly lapsing.  The bash side applies this
    # default late — long after the handoff — so a Python component called
    # early would otherwise see an empty string here.
    pause_file: str = ""

    # -- transfer progress --------------------------------------------------
    # Empty is a real setting: it switches progress publishing off.  See
    # ``progress_enabled``.
    progress_state_dir: Optional[str] = "/tmp/putio_progress"
    rclone_stats_interval: str = "5s"

    # -- pinned binaries ----------------------------------------------------
    rclone_cmd: str = ""
    jq_cmd: str = ""
    python_bin: str = ""

    # -- rclone tuning ------------------------------------------------------
    rclone_transfers: int = 4
    rclone_checkers: int = 8
    rclone_buffer_size: str = "64M"
    rclone_multi_threads: int = 2
    rclone_max_concurrent: int = 1
    rclone_bwlimit: str = "off"

    # -- behaviour ----------------------------------------------------------
    delete_after_download: bool = True
    # server_config_template is the only place this has a default — the
    # processor never supplies one — so 5 is the documented value.
    min_free_space_gb: int = 5
    max_retries: int = 3
    retry_delay: int = 5

    # -- external services --------------------------------------------------
    tmdb_api_key: str = ""
    plex_url: str = "http://localhost:32400"
    plex_token: str = ""
    plex_movie_library_id: str = ""
    plex_tv_library_id: str = ""
    drovie_webhook_url: str = ""
    drovie_webhook_key: str = ""
    max_requests_per_window: int = 35
    rate_window_seconds: int = 10
    # RATE_LIMIT_FILE is gone as of phase 5, along with the file-backed
    # limiter: the run is one process, so the window is a variable.  It is
    # dropped from the handoff rather than kept and ignored, because a
    # ``config-dump`` that reports a setting doing nothing is worse than one
    # that does not mention it.  A .putio_config still setting it is harmless.
    # See :mod:`putio.ratelimit`.
    #
    # QUIRK — none of the remaining rate-limit settings had a default in the
    # processor.  They were read straight from .putio_config, so a config that
    # omitted RATE_LIMIT_DELAY made bash run `sleep ""` on every TMDb lookup,
    # and one that omitted MAX_REQUESTS_PER_WINDOW made `(( count >= ))`
    # compare against 0 and take the "limit reached" branch every time, with a
    # negative wait that `sleep` then rejected.  The defaults here are
    # server_config_template's, which is the only place they are written down.
    #
    # A float, not an int: bash hands this straight to `sleep`, which takes
    # `0.2`.  Reading it with int() would reject that and quietly pause five
    # times longer than the operator asked for.
    rate_limit_delay: float = 1.0

    # Field name -> environment name.  Kept explicit rather than derived by
    # upper-casing, so that renaming a Python attribute cannot silently change
    # the wire contract with bash.
    _FIELDS: ClassVar[Tuple[Tuple[str, str], ...]] = (
        ("remote", "REMOTE"),
        ("local_dir", "LOCAL_DIR"),
        ("staging_dir", "STAGING_DIR"),
        ("use_temp_dir", "USE_TEMP_DIR"),
        ("movie_library", "MOVIE_LIBRARY"),
        ("tv_library", "TV_LIBRARY"),
        ("unmatched_dir", "UNMATCHED_DIR"),
        ("media_mount_point", "MEDIA_MOUNT_POINT"),
        ("run_pid", "RUN_PID"),
        ("script_path", "SCRIPT_PATH"),
        ("config_file", "CONFIG_FILE"),
        ("log_file", "LOG_FILE"),
        ("log_level", "LOG_LEVEL"),
        ("lockfile", "LOCKFILE"),
        ("pause_file", "PAUSE_FILE"),
        ("progress_state_dir", "PROGRESS_STATE_DIR"),
        ("rclone_stats_interval", "RCLONE_STATS_INTERVAL"),
        ("rclone_cmd", "RCLONE_CMD"),
        ("jq_cmd", "JQ_CMD"),
        ("python_bin", "PYTHON_BIN"),
        ("rclone_transfers", "RCLONE_TRANSFERS"),
        ("rclone_checkers", "RCLONE_CHECKERS"),
        ("rclone_buffer_size", "RCLONE_BUFFER_SIZE"),
        ("rclone_multi_threads", "RCLONE_MULTI_THREADS"),
        ("rclone_max_concurrent", "RCLONE_MAX_CONCURRENT"),
        ("rclone_bwlimit", "RCLONE_BWLIMIT"),
        ("delete_after_download", "DELETE_AFTER_DOWNLOAD"),
        ("min_free_space_gb", "MIN_FREE_SPACE_GB"),
        ("max_retries", "MAX_RETRIES"),
        ("retry_delay", "RETRY_DELAY"),
        ("tmdb_api_key", "TMDB_API_KEY"),
        ("plex_url", "PLEX_URL"),
        ("plex_token", "PLEX_TOKEN"),
        ("plex_movie_library_id", "PLEX_MOVIE_LIBRARY_ID"),
        ("plex_tv_library_id", "PLEX_TV_LIBRARY_ID"),
        ("drovie_webhook_url", "DROVIE_WEBHOOK_URL"),
        ("drovie_webhook_key", "DROVIE_WEBHOOK_KEY"),
        ("max_requests_per_window", "MAX_REQUESTS_PER_WINDOW"),
        ("rate_window_seconds", "RATE_WINDOW_SECONDS"),
        ("rate_limit_delay", "RATE_LIMIT_DELAY"),
    )

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Config":
        """Build from ``os.environ`` (or any mapping, for tests)."""
        if env is None:
            env = os.environ

        # PROGRESS_STATE_DIR is the exception described in the module
        # docstring.  Absent means "the bash default applies"; present but
        # empty means the operator switched progress publishing off, and that
        # must survive the round trip rather than being defaulted back on.
        if "PROGRESS_STATE_DIR" in env:
            progress: Optional[str] = env["PROGRESS_STATE_DIR"] or None
        else:
            progress = "/tmp/putio_progress"

        return cls(
            remote=_remote(env),
            local_dir=_text(env, "LOCAL_DIR"),
            staging_dir=_text(env, "STAGING_DIR"),
            use_temp_dir=_exactly_true(env, "USE_TEMP_DIR", False),
            movie_library=_text(env, "MOVIE_LIBRARY"),
            tv_library=_text(env, "TV_LIBRARY"),
            unmatched_dir=_text(env, "UNMATCHED_DIR"),
            media_mount_point=_text(env, "MEDIA_MOUNT_POINT"),
            run_pid=_text(env, "RUN_PID"),
            script_path=_text(env, "SCRIPT_PATH"),
            config_file=_text(env, "CONFIG_FILE"),
            log_file=_text(env, "LOG_FILE"),
            log_level=_text(env, "LOG_LEVEL", "INFO").upper(),
            lockfile=_text(env, "LOCKFILE", "/tmp/putio_download_and_delete.lock"),
            pause_file=_text(env, "PAUSE_FILE", os.path.expanduser("~/.putio_paused")),
            progress_state_dir=progress,
            rclone_stats_interval=_text(env, "RCLONE_STATS_INTERVAL", "5s"),
            rclone_cmd=_text(env, "RCLONE_CMD"),
            jq_cmd=_text(env, "JQ_CMD"),
            python_bin=_text(env, "PYTHON_BIN"),
            rclone_transfers=_number(env, "RCLONE_TRANSFERS", 4),
            rclone_checkers=_number(env, "RCLONE_CHECKERS", 8),
            rclone_buffer_size=_text(env, "RCLONE_BUFFER_SIZE", "64M"),
            rclone_multi_threads=_number(env, "RCLONE_MULTI_THREADS", 2),
            rclone_max_concurrent=_number(env, "RCLONE_MAX_CONCURRENT", 1),
            rclone_bwlimit=_text(env, "RCLONE_BWLIMIT", "off"),
            delete_after_download=_unless_false(env, "DELETE_AFTER_DOWNLOAD"),
            min_free_space_gb=_number(env, "MIN_FREE_SPACE_GB", 5),
            max_retries=_number(env, "MAX_RETRIES", 3),
            retry_delay=_number(env, "RETRY_DELAY", 5),
            tmdb_api_key=_text(env, "TMDB_API_KEY"),
            plex_url=_text(env, "PLEX_URL", "http://localhost:32400"),
            plex_token=_text(env, "PLEX_TOKEN"),
            plex_movie_library_id=_text(env, "PLEX_MOVIE_LIBRARY_ID"),
            plex_tv_library_id=_text(env, "PLEX_TV_LIBRARY_ID"),
            # The PLEX_REQUEST_* names predate the rename and stay as aliases,
            # so an existing config keeps notifying Drovie. The shim resolves
            # the alias before the handoff; reading it here as well covers a
            # hand-run `python3 -m putio` with only the old name exported.
            drovie_webhook_url=_text(env, "DROVIE_WEBHOOK_URL")
            or _text(env, "PLEX_REQUEST_WEBHOOK_URL"),
            drovie_webhook_key=_text(env, "DROVIE_WEBHOOK_KEY")
            or _text(env, "PLEX_REQUEST_WEBHOOK_KEY"),
            max_requests_per_window=_number(env, "MAX_REQUESTS_PER_WINDOW", 35),
            rate_window_seconds=_number(env, "RATE_WINDOW_SECONDS", 10),
            rate_limit_delay=_seconds(env, "RATE_LIMIT_DELAY", 1.0),
        )

    # ------------------------------------------------------------------
    # accessors
    # ------------------------------------------------------------------
    @classmethod
    def env_names(cls) -> Tuple[str, ...]:
        """Environment names this module reads — the bash-side contract."""
        return tuple(name for _, name in cls._FIELDS)

    @property
    def progress_enabled(self) -> bool:
        return bool(self.progress_state_dir)

    def redacted(self) -> Dict[str, Any]:
        """A plain dict safe to print, log or serialise.

        Secrets are replaced rather than dropped: a caller debugging a run
        needs to see that a key is *set*, and an omitted entry reads as unset.
        """
        out: Dict[str, Any] = {}
        for field in fields(self):
            if field.name.startswith("_"):
                continue
            value = getattr(self, field.name)
            if field.name in SECRET_FIELDS:
                out[field.name] = REDACTED if value else ""
            else:
                out[field.name] = value
        return out
