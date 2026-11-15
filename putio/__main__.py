"""``python3 -m putio`` — the single entry point bash calls into.

One process per *invocation*, never one per item: subcommands take a whole
batch on stdin and answer with a whole batch on stdout.  See ``putio_python``
in the processor for the shell side of this.

Exit codes are the contract:

==  ===========================================================
0   the subcommand did what it was asked
1   the subcommand ran and failed
2   usage error — unknown subcommand, bad flags
==  ===========================================================

127 is reserved: the shell wrapper returns it when there is no interpreter at
all, so a caller can tell "Python is missing" from "Python said no".
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from typing import Callable, List, Optional, Sequence

from . import SCHEMA, __version__
from . import archives, organize, pipeline, sweep
from .config import Config
from .http import HttpClient, HttpError
from .media import classify, is_junk_name, sanitize_filename
from .plex import SECTION_TYPES, PlexClient
from .mount import MountGate
from .proc import Retrier
from .progress import ProgressPublisher
from .ratelimit import NullLimiter, TokenWindow
from .rclone import RcloneClient
from .transfer import Downloader
from .tmdb import TmdbClient

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

#: The oldest interpreter this package is written against.  Apple's
#: /usr/bin/python3 is 3.9.6 and past upstream EOL, but it is what a machine
#: without the Homebrew pin still has — so the syntax stays inside 3.9 until
#: PYTHON_BIN is pinned everywhere, and selftest reports rather than refuses.
MIN_PYTHON = (3, 9)

#: What the port is actually being moved onto.
WANTED_PYTHON = (3, 13)


def _cmd_run(_args: argparse.Namespace) -> int:
    """The run itself.  Imported lazily so the cheap subcommands stay cheap.

    ``putio.run`` pulls in the whole download stack — rclone, TMDb, Plex, the
    pool.  ``classify`` and ``junk`` need none of it, and an idle cron tick is
    measured in this project rather than assumed, so the import happens when
    the run happens and not at ``-m putio`` startup.
    """
    from . import run as run_module

    return run_module.main()


def _cmd_version(_args: argparse.Namespace) -> int:
    """Identify the code that is actually loaded, not the code you expected.

    ``package`` is here because for ``python -m``, sys.path[0] is the working
    directory and it beats PYTHONPATH — so "which copy did I just run?" is a
    real question with no other way to answer it.
    """
    import putio

    print(
        json.dumps(
            {
                "version": __version__,
                "schema": SCHEMA,
                "python": ".".join(str(n) for n in sys.version_info[:3]),
                "executable": sys.executable,
                "package": os.path.realpath(os.path.dirname(putio.__file__)),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return EXIT_OK


def _cmd_config_dump(args: argparse.Namespace) -> int:
    """Show what actually arrived from bash — the first thing to check when a
    ported component behaves differently under cron than by hand."""
    config = Config.from_env()
    data = config.redacted()
    if args.json:
        print(json.dumps(data, indent=2, sort_keys=True))
    else:
        width = max(len(k) for k in data)
        for key in sorted(data):
            print("{0:<{1}}  {2}".format(key, width, data[key]))
    return EXIT_OK


def _cmd_selftest(args: argparse.Namespace) -> int:
    """Prove the scaffolding is intact: interpreter, import path, handoff.

    Written to be run against a *deployed* tree, not just the checkout.  It is
    the only thing that can tell you the copy under ~/Scripts is the one that
    works, which matters because ``putioctl deploy`` replaces files one at a
    time and cron never reports an ImportError to anyone.
    """
    problems: List[str] = []
    notes: List[str] = []

    running = sys.version_info[:3]
    if running[:2] < MIN_PYTHON:
        problems.append(
            "interpreter is {0}, below the {1}.{2} minimum".format(
                ".".join(str(n) for n in running), *MIN_PYTHON
            )
        )
    elif running[:2] < WANTED_PYTHON:
        notes.append(
            "interpreter is {0} — pin PYTHON_BIN to a Homebrew python{1}.{2}".format(
                ".".join(str(n) for n in running), *WANTED_PYTHON
            )
        )

    config = Config.from_env()

    # An empty LOCAL_DIR means the environment did not come from the processor:
    # either putio_python was bypassed or PUTIO_PY_VARS lost a name.
    if not config.local_dir:
        problems.append("LOCAL_DIR is empty — the bash handoff did not happen")

    # Names the caller left empty.  Not a failure — most are optional, and the
    # shell cannot distinguish empty from unset anyway — but it is the fastest
    # way to spot a name that fell out of PUTIO_PY_VARS.
    empty = [name for name in Config.env_names() if not os.environ.get(name)]
    if empty:
        notes.append("empty in the environment: " + ", ".join(empty))


    for line in notes:
        print("note: " + line)
    for line in problems:
        print("FAIL: " + line, file=sys.stderr)

    if problems:
        return EXIT_FAIL
    if not args.quiet:
        print(
            "ok: putio {0} (schema {1}) on {2}".format(
                __version__, SCHEMA, ".".join(str(n) for n in running)
            )
        )
    return EXIT_OK


# ---------------------------------------------------------------------------
# batched filename subcommands
# ---------------------------------------------------------------------------
#
# All three take a whole list on stdin and answer with a whole list on stdout.
# There is no single-name form on purpose: the moment one exists, a caller in
# bash will put it inside a `while read` loop, and a ~30ms interpreter start
# per file is exactly the mistake rotate_log already paid 36 seconds a run for.
#
# Names are carried as bytes, decoded with surrogateescape and encoded back the
# same way, so a filename that is not valid UTF-8 survives the round trip
# unchanged instead of raising.  put.io has produced such names.


def _read_names(delimiter: bytes) -> List[str]:
    """Split stdin on ``delimiter``, dropping a single trailing empty field.

    ``rclone lsf`` ends its output with a newline and bash's ``printf '%s\\0'``
    ends with a NUL, so the last field is always empty and never a real name.
    Empty fields elsewhere are dropped too: bash's own loops skip a blank line
    (``[ -z "$FILE" ] && continue``) rather than classifying it.
    """
    raw = sys.stdin.buffer.read()
    if not raw:
        return []
    return [
        chunk.decode("utf-8", "surrogateescape")
        for chunk in raw.split(delimiter)
        if chunk
    ]


def _write(text: str, delimiter: bytes) -> None:
    sys.stdout.buffer.write(text.encode("utf-8", "surrogateescape"))
    sys.stdout.buffer.write(delimiter)


def _delimiter(args: argparse.Namespace) -> bytes:
    return b"\0" if args.null else b"\n"


def _cmd_junk(args: argparse.Namespace) -> int:
    """Echo back only the paths that are junk *by name*.

    The size probe stays in bash — it is an rclone call, and this process has
    no business reaching the network.  So the shell reads this as "these are
    junk whatever their size" and still probes the rest.
    """
    delimiter = _delimiter(args)
    for name in _read_names(delimiter):
        if is_junk_name(name):
            _write(name, delimiter)
    return EXIT_OK


def _cmd_sanitize(args: argparse.Namespace) -> int:
    delimiter = _delimiter(args)
    for name in _read_names(delimiter):
        _write(sanitize_filename(name), delimiter)
    return EXIT_OK


def _cmd_classify(args: argparse.Namespace) -> int:
    """Everything known about each name: junk verdict, parse, sanitized form.

    Two output shapes, and the second one is why this reads oddly.  ``--null``
    emits nine NUL-terminated fields per record so that a *shell* harness can
    compare bash's answers against these byte for byte with ``cmp``, without a
    JSON parser standing between the two implementations it is meant to be
    proving identical.  JSON lines are the default for every other caller.
    """
    delimiter = _delimiter(args)
    names = _read_names(delimiter)
    for record in classify(names):
        parsed = record["parsed"]
        if args.null:
            fields = [
                record["name"],
                "1" if record["junk"] else "0",
                "0" if parsed is None else "1",
                "" if parsed is None else parsed["type"],
                "" if parsed is None else parsed["title"],
                "" if parsed is None else parsed["year"],
                "" if parsed is None else parsed["season"],
                "" if parsed is None else parsed["episode"],
                record["sanitized"],
            ]
            for field in fields:
                sys.stdout.buffer.write(field.encode("utf-8", "surrogateescape"))
                sys.stdout.buffer.write(b"\0")
        else:
            sys.stdout.buffer.write(
                json.dumps(record, sort_keys=True).encode("ascii") + b"\n"
            )
    return EXIT_OK


# ---------------------------------------------------------------------------
# network subcommands
# ---------------------------------------------------------------------------
#
# `plex-scan` is the one call site this phase actually switches over, and the
# reason is the batching rule.  TMDb lookups are per *file* and happen inside
# identify_and_organize_media, which runs in a background subshell per download
# job — a Python call there would be one interpreter start per file, which is
# the mistake rotate_log already paid 36 seconds a run for.  The Plex scan is
# per *run*: at most two refreshes and a webhook, once, after everything has
# been organised.  One interpreter start for the whole thing is the batched
# shape, not an exception to it.
#
# So `tmdb-search` below exists to make the ported search runnable and
# checkable by hand, and takes a batch on stdin so it cannot become the wrong
# shape by accident.  Nothing in the download path calls it; phase 3 moves the
# whole organise path at once, at which point the exec is one-per-batch
# naturally.

#: What the bash sleeps between a Plex scan and poking Drovie, to give
#: the scan a moment to find the new file.  Overridable only so the harness
#: does not have to wait it out; production never passes the flag.
WEBHOOK_DELAY_SECONDS = 10


class _Emitter:
    """Log lines for bash to replay, one per line, ``LEVEL<TAB>message``.

    The processor owns the log file — its format
    (``[YYYY-MM-DD HH:MM:SS] [LEVEL]``) is an invariant that ``rotate_log``
    compares as strings — so this writes nothing to it.  It hands lines back
    and ``plex_scan_python`` puts each through ``log``.

    Flushed per line so the timestamps bash stamps on them stay in the order
    things happened: there is a ten-second sleep in the middle of this, and a
    buffered writer would bunch every line at the end of it.
    """

    def __init__(self, stream=None) -> None:
        self._stream = stream if stream is not None else sys.stdout

    def __call__(self, level: str, message: str) -> None:
        # A newline or tab inside a message would desynchronise the reader on
        # the other side, and bash's `read -r lvl msg` would log the tail of it
        # at whatever level the fragment started with.
        clean = " ".join(str(message).split())
        self._stream.write("{0}\t{1}\n".format(level, clean))
        self._stream.flush()


def _http_from(config: Config, emit: _Emitter) -> HttpClient:
    """A client with the processor's retry policy and its own logging.

    ``on_retry`` reproduces the ``[RETRY]`` line ``retry_command`` writes, so a
    run that had to retry reads the same way whichever side made the call.
    """
    return HttpClient(
        max_retries=config.max_retries,
        retry_delay=config.retry_delay,
        on_retry=lambda message: emit("WARN", "[RETRY] " + message),
    )


def _cmd_plex_scan(args: argparse.Namespace) -> int:
    """Trigger the end-of-run library scans and the Drovie webhook.

    A transcription of ``scan_plex_library``, called once for both libraries
    instead of once each.  Everything it used to log, it still logs, in the
    same words — including firing the webhook once per successful scan with its
    own ten-second wait, which is what the bash does when both a film and an
    episode landed in the same run.  Collapsing that to one webhook would be an
    improvement and is deliberately not made here; see the phase 2 notes.

    The exit code is a contract with ``plex_scan_python`` in the processor,
    and the important half of it is what *must not* happen: bash falls back to
    its own implementation when this command did not run, so this command must
    never fail after triggering a scan or the scan fires twice.  It therefore
    catches everything and reports ``DONE`` whenever any request was attempted.
    """
    config = Config.from_env()
    emit = _Emitter()

    wanted = []  # (processor name, configured id) in the order bash scans them
    if args.movie is not None:
        wanted.append(("movie", args.movie))
    if args.tv is not None:
        wanted.append(("tv", args.tv))

    if not wanted:
        # Nothing asked for is not an error, and it still counts as handled:
        # falling back to bash here would scan libraries nobody added to.
        emit("DONE", "nothing requested")
        return EXIT_OK

    attempted = False
    try:
        if not config.plex_token:
            # Bash's wording, and its behaviour: scan_plex_library returns 1
            # here and the caller ignores the return.  It says it once for the
            # whole call rather than once per library, which is the only place
            # this differs — and validate_config already refuses to start a run
            # with no PLEX_TOKEN, so the branch is unreachable in practice.
            emit("WARN", "[PLEX] PLEX_TOKEN not set, skipping scan")
            emit("DONE", "no token")
            return EXIT_OK

        http = _http_from(config, emit)
        plex = PlexClient(config.plex_url, config.plex_token, http)

        for library_type, library_id in wanted:
            section_type = SECTION_TYPES[library_type]

            if not library_id:
                # Deliberately does NOT set `attempted`.  Discovery is a GET;
                # it triggers nothing, so if it dies in a way the except below
                # cannot describe, bash re-doing the whole thing is both safe
                # and the only way the library gets scanned at all this run.
                emit("INFO", "[PLEX] Detecting library ID for {0}...".format(library_type))
                try:
                    library_id = plex.library_id(section_type) or ""
                except HttpError as exc:
                    # New: bash's `curl -s` could not tell a 401 from an empty
                    # library list, so both arrived as "could not find".
                    emit("ERROR", "[PLEX] Sections lookup failed: {0}".format(exc))
                    library_id = ""
                if not library_id:
                    emit(
                        "WARN",
                        "[PLEX] Could not find library ID for type '{0}'".format(section_type),
                    )
                    emit(
                        "ERROR",
                        "[PLEX] Could not determine library ID for {0}".format(library_type),
                    )
                    continue

            emit(
                "INFO",
                "[PLEX] Triggering scan for {0} library (ID: {1})...".format(
                    library_type, library_id
                ),
            )
            attempted = True
            try:
                plex.refresh(library_id)
            except HttpError as exc:
                # The whole point of the phase: `curl -s` exits 0 on the 404
                # Plex returns for a section id that does not exist, so this
                # branch was unreachable from bash and every scan was a success.
                emit("ERROR", "[PLEX] Failed to trigger {0} library scan".format(library_type))
                emit("ERROR", "[PLEX] {0}".format(exc))
                continue

            emit("INFO", "[PLEX] {0} library scan triggered successfully".format(library_type))
            _fire_webhook(config, plex, emit, args.webhook_delay)
    except Exception as exc:  # noqa: BLE001 - see the docstring
        emit("ERROR", "[PLEX] Unexpected failure: {0}: {1}".format(type(exc).__name__, exc))
        if not attempted:
            # Nothing happened, so bash re-doing it is safe and better than
            # skipping the scan entirely.
            return EXIT_FAIL

    emit("DONE", "handled")
    return EXIT_OK


def _fire_webhook(config: Config, plex: PlexClient, emit: _Emitter, delay: int) -> None:
    """Drovie's availability check, after the scan has had a moment."""
    if not (config.drovie_webhook_url and config.drovie_webhook_key):
        return

    emit(
        "INFO",
        "[WEBHOOK] Waiting {0} seconds before triggering Drovie availability check...".format(
            delay
        ),
    )
    if delay > 0:
        time.sleep(delay)

    try:
        plex.trigger_webhook(config.drovie_webhook_url, config.drovie_webhook_key)
    except HttpError as exc:
        emit("WARN", "[WEBHOOK] Failed to trigger Drovie availability check")
        emit("WARN", "[WEBHOOK] {0}".format(exc))
        return
    emit("INFO", "[WEBHOOK] Drovie availability check triggered successfully")


def _cmd_tmdb_search(args: argparse.Namespace) -> int:
    """Look up a whole batch of titles, one JSON object per line.

    Input is one query per line, optionally ``title<TAB>year`` — the two things
    ``parse_media_info`` produces.  There is no single-title form for the same
    reason ``junk`` has no single-name form: it would end up inside a shell
    loop within a week.

    Every line gets an answer, even a failing one, so a caller can line the
    output up with its input:

    * ``{"query": …, "match": {…}}``  — TMDb's first result
    * ``{"query": …, "match": null}`` — TMDb has no such title
    * ``{"query": …, "error": "…"}``  — the lookup itself failed
    """
    config = Config.from_env()
    emit = _Emitter(sys.stderr)

    if not config.tmdb_api_key:
        print("TMDB_API_KEY is not set", file=sys.stderr)
        return EXIT_FAIL

    client = TmdbClient(
        config.tmdb_api_key,
        _http_from(config, emit),
        _limiter(config, emit),
        on_note=emit,
    )
    search = client.search_tv if args.tv else client.search_movie

    failed = False
    for line in sys.stdin.read().splitlines():
        if not line.strip():
            continue
        title, _, year = line.partition("\t")
        record = {"query": title, "year": year}
        try:
            match = search(title, year)
        except HttpError as exc:
            record["error"] = str(exc)
            failed = True
        else:
            record["match"] = match.as_dict() if match is not None else None
        print(json.dumps(record, sort_keys=True))

    return EXIT_FAIL if failed else EXIT_OK



# ---------------------------------------------------------------------------
# phase 3: the organise path and the archive rules
# ---------------------------------------------------------------------------
#
# These three subcommands share one wire format, because they share one
# problem: they have to hand bash *structured* answers, and bash has to be able
# to read them back without a parser.
#
# Records are one line each, fields separated by :data:`FIELD_SEP` (US, 0x1f).
# Not a tab, and the reason is a shell detail worth knowing: a tab is IFS
# whitespace, so `IFS=$'\t' read -r a b c` collapses `a<TAB><TAB>b` into two
# fields and every field after an empty one shifts left.  US is not whitespace,
# so empty fields survive — and empty fields are normal here, since most
# records have nothing to say in at least one of them.
#
# The first field is the record type.  ``LOG`` lines are replayed by bash
# through ``log`` so the timestamps stay in run order; a final ``DONE`` means
# the subcommand ran to the end, which is the same fallback contract
# ``plex-scan`` uses.

#: Field separator for every phase 3 record.  See above for why not a tab.
FIELD_SEP = "\x1f"


def _clean(message: str) -> str:
    """Make a message safe to carry in one field of one line.

    Only the three characters that would desynchronise the reader are touched;
    everything else, including runs of spaces inside a TMDb title, is left as
    bash would have logged it.
    """
    out = str(message)
    for char in ("\r\n", "\n", "\r", FIELD_SEP):
        out = out.replace(char, " ")
    return out


#: ``download-batch`` writes records from the pool's worker threads, so the
#: write and the flush have to be one operation — two threads interleaving
#: inside a single line would hand bash a record with the wrong field count.
#: Every other subcommand is single-threaded and pays nothing for this.
_RECORD_LOCK = threading.Lock()


def _record(*fields: str) -> None:
    """One line out, flushed, encoded the way names arrived."""
    line = FIELD_SEP.join(fields) + "\n"
    with _RECORD_LOCK:
        sys.stdout.buffer.write(line.encode("utf-8", "surrogateescape"))
        sys.stdout.buffer.flush()


def _log(level: str, message: str) -> None:
    _record("LOG", level, _clean(message))


def _limiter(config: Config, emit: Callable[[str, str], None]) -> TokenWindow:
    """The window for a hand-run subcommand.

    In-process, like the run's — phase 5 deleted the file-backed limiter, and
    :mod:`putio.ratelimit` carries the argument for why keeping it for these
    two callers would have coordinated them with nothing.
    """
    return TokenWindow(
        max_requests=config.max_requests_per_window,
        window_seconds=config.rate_window_seconds,
        delay=config.rate_limit_delay,
        on_wait=lambda message: emit("WARN", message),
    )


def _tmdb_client(config: Config, emit: Callable[[str, str], None]) -> TmdbClient:
    """A search client wired to the processor's retry and rate-limit settings."""
    limiter = _limiter(config, emit)
    http = HttpClient(
        max_retries=config.max_retries,
        retry_delay=config.retry_delay,
        on_retry=lambda message: emit("WARN", "[RETRY] " + message),
    )
    return TmdbClient(config.tmdb_api_key, http, limiter, on_note=emit)


def _cmd_organize_plan(args: argparse.Namespace) -> int:
    """Decide where a whole batch of downloads belongs.

    Input is basenames on stdin.  Output is one ``PLAN`` record per name, plus
    whatever ``LOG`` lines the batch produced along the way, and ``DONE``.

    Partial output is useful and is not rolled back: bash looks each file up in
    the plan by name and falls back to its own implementation for any name that
    is not there, so a batch that dies halfway costs the remaining files a
    duplicate lookup rather than a wrong answer.  That is why this command,
    unlike ``plex-scan``, may fail after doing work — nothing it does has a
    side effect that must not happen twice.
    """
    config = Config.from_env()

    if not config.tmdb_api_key:
        # validate_config refuses to start a run without one, so this is a
        # hand-run.  No DONE, no records: bash does the whole batch itself.
        print("TMDB_API_KEY is not set", file=sys.stderr)
        return EXIT_FAIL

    names = _read_names(_delimiter(args))
    if not names:
        _record("DONE")
        return EXIT_OK

    client = _tmdb_client(config, _log)
    decisions = organize.plan(
        names,
        movie_library=config.movie_library,
        tv_library=config.tv_library,
        tmdb=client,
        on_log=_log,
    )

    dropped = 0
    for decision in decisions:
        if organize.unsafe_for_transport(decision, FIELD_SEP):
            dropped += 1
            continue
        # dest_name is appended rather than inserted: a reader that predates
        # it keeps every field at the index it already had.
        _record(
            "PLAN",
            decision.name,
            decision.action,
            decision.dest,
            decision.parse_level,
            _clean(decision.parse_note),
            decision.match_level,
            _clean(decision.match_note),
            decision.dest_name,
        )
    if dropped:
        _log(
            "WARN",
            "[ORGANIZE] {0} name(s) could not be planned safely — bash will "
            "organise them".format(dropped),
        )

    _record("DONE")
    return EXIT_OK


def _cmd_download_batch(args: argparse.Namespace) -> int:
    """The download loop for one listing, as a hand-runnable subcommand.

    Input is the put.io listing on stdin, one path per line, exactly as
    ``rclone lsf --files-only --recursive`` produces it.  Output is records
    separated by US (0x1f):

    ==============  =========================================================
    ``START``       committed; side effects may follow from here on
    ``LOG``         a log line, ``LEVEL`` then the message
    ``LOCAL``       how many leftovers the local sweep found
    ``DOWNLOADED``  one per completed transfer
    ``ADDED``       ``movie`` or ``tv`` — a library got something
    ``DONE``        the batch ran to the end
    ==============  =========================================================

    **Phase 4 built this to be called from bash and phase 5 stopped calling
    it.**  ``putio run`` now drives :class:`~putio.pipeline.Batch` directly, in
    the same process, so the records go to variables rather than down a pipe.
    What is left here is the documented hand-run door:

        rclone lsf --files-only --recursive "$REMOTE" | putio_python download-batch

    It is wired through :func:`putio.run.build_batch`, the same factory the run
    uses, so the two cannot drift — a hand-run batch that behaved differently
    from the real one would be worse than no hand-run door at all.

    ``START`` and ``DONE`` stay, and the reason they stay is worth writing
    down.  They were the fallback contract: bash had to be able to tell "this
    committed and died" from "nothing happened", because re-running a
    half-finished batch would try to download files put.io no longer has.
    There is no bash loop to fall back to any more, so nothing *reads* them —
    but they are still the honest record of when side effects began, and a
    person reading the output of a hand-run batch needs exactly that.
    """
    from .run import build_batch

    config = Config.from_env()

    # Everything that can refuse *before* START: a machine that cannot do the
    # work at all should say so without having touched anything.
    if not config.local_dir:
        print("LOCAL_DIR is not set", file=sys.stderr)
        return EXIT_FAIL
    if not config.rclone_cmd:
        print("RCLONE_CMD is not set", file=sys.stderr)
        return EXIT_FAIL
    if not config.tmdb_api_key:
        print("TMDB_API_KEY is not set", file=sys.stderr)
        return EXIT_FAIL

    listing = _read_names(_delimiter(args))

    wiring = build_batch(
        config,
        _log,
        _record,
        lambda name: _record("DOWNLOADED", _clean(name)),
    )

    _record("START")
    try:
        _record("LOCAL", str(wiring.batch.sweep_local()))
        wiring.batch.run(listing)
    except Exception as exc:  # noqa: BLE001 - a failure after START still ends in DONE
        _log("ERROR", "[PARALLEL] Batch failed: {0}".format(exc))
        wiring.progress.clear_all()
    _record("DONE")
    return EXIT_OK


def _cmd_markers_sweep(args: argparse.Namespace) -> int:
    """Remove extraction markers whose archive is gone — ``cleanup_old_markers``.

    Once per run over one directory, which is the batched shape: bash forks a
    ``basename`` per marker and re-globs the directory for each one.
    """
    config = Config.from_env()
    local_dir = args.local_dir or config.local_dir
    if not local_dir:
        print("LOCAL_DIR is not set", file=sys.stderr)
        return EXIT_FAIL

    # One implementation, in :mod:`putio.sweep`, because the run now calls it
    # directly as a startup sweep and this subcommand is the hand-run door onto
    # the same thing.  Two copies of "which markers are orphaned" is how the
    # answers drift.
    sweep.sweep_markers(local_dir, _log)
    _record("DONE")
    return EXIT_OK


def _cmd_archive_check(args: argparse.Namespace) -> int:
    """Is this archive already extracted, and are all its volumes here?

    ``check_extraction_marker`` and ``check_rar_completeness`` in one call, made
    at the moment bash is about to extract.  That is one interpreter start per
    *archive*, not per file, and the loop it sits in also runs ``unar`` over a
    multi-gigabyte file on every iteration — so the ~30ms is not measurable and
    there is nowhere earlier to batch it, because completeness depends on local
    files that are still arriving.
    """
    config = Config.from_env()
    local_dir = args.local_dir or config.local_dir
    if not local_dir:
        print("LOCAL_DIR is not set", file=sys.stderr)
        return EXIT_FAIL

    name = args.name
    try:
        entries = os.listdir(local_dir)
    except OSError as exc:
        print("cannot read {0}: {1}".format(local_dir, exc), file=sys.stderr)
        return EXIT_FAIL

    marker = archives.marker_name(name)
    extracted = marker in entries
    if extracted:
        _log("DEBUG", "[MARKER] Found extraction marker: {0}".format(
            archives.marker_base(name)))
        # Bash short-circuits here — an archive that is already extracted never
        # reaches check_rar_completeness — so neither does this, or a set whose
        # parts have since been cleaned up would start warning about itself.
        check = archives.PartCheck(True)
    else:
        check = archives.rar_completeness(name, entries)
        if not check.complete:
            _log("WARN", "[RAR] Missing essential part: {0}".format(check.missing))
        elif check.multipart:
            _log("DEBUG", "[RAR] Found {0} parts for {1}".format(
                check.parts, archives.marker_base(name)))

    _record(
        "CHECK",
        marker,
        "1" if extracted else "0",
        "1" if check.complete else "0",
        str(check.parts),
        check.missing or "",
    )
    _record("DONE")
    return EXIT_OK


def _cmd_archive_parts(args: argparse.Namespace) -> int:
    """Which files belong to this archive — locally, and on put.io.

    Local names come out as ``RM`` records in the order
    ``cleanup_archive_files`` would remove them; put.io names as ``PART``
    records in listing order, because bash logs one line per deletion.  The
    put.io listing arrives on stdin, since it is bash that holds the ``rclone``
    call.

    A name that is not a RAR part at all produces no ``PART`` records and a
    ``BASE`` record with an empty base, which is bash's signal to fall back to
    deleting the single file it was given.
    """
    config = Config.from_env()
    local_dir = args.local_dir or config.local_dir
    name = args.name

    try:
        entries = os.listdir(local_dir) if local_dir else []
    except OSError:
        entries = []

    for target in archives.local_cleanup_targets(name, entries):
        _record("RM", target)

    # stdin is drained unconditionally, even for a .zip that has no parts to
    # look for: the shell writes the listing into this process with a `printf |`
    # and exiting without reading leaves it holding a SIGPIPE for no reason.
    listing = _read_names(_delimiter(args))

    base = archives.putio_base(name)
    _record("BASE", base or "")
    if base is not None:
        for part in archives.putio_part_names(base, listing):
            _record("PART", part)

    _record("DONE")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="putio",
        description="Python components of the put.io -> Plex processor.",
    )
    subparsers = parser.add_subparsers(dest="command")

    # The entry point.  `putio_download_and_delete.sh` execs into this, so
    # every cron tick is `python3 -m putio run` with the pid the shim was
    # started with.  It takes no arguments on purpose: everything it needs
    # arrives on the environment, which keeps one contract (PUTIO_PY_VARS)
    # rather than two.
    sub = subparsers.add_parser("run", help="the whole run — what cron invokes")
    sub.set_defaults(func=_cmd_run)

    sub = subparsers.add_parser("version", help="print version and schema as JSON")
    sub.set_defaults(func=_cmd_version)

    sub = subparsers.add_parser(
        "config-dump", help="print the configuration bash handed over (secrets redacted)"
    )
    sub.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    sub.set_defaults(func=_cmd_config_dump)

    sub = subparsers.add_parser(
        "selftest", help="check the interpreter, the import path and the bash handoff"
    )
    sub.add_argument("-q", "--quiet", action="store_true", help="say nothing when healthy")
    sub.set_defaults(func=_cmd_selftest)

    sub = subparsers.add_parser(
        "plex-scan", help="trigger the end-of-run library scans and the webhook"
    )
    # Absent means "this library had nothing added"; present-but-empty means
    # "scan it, and discover the id first" — which is exactly the distinction
    # the bash draws between not calling scan_plex_library and calling it with
    # an empty PLEX_MOVIE_LIBRARY_ID.  nargs="?" is what gives both from one
    # flag, so the shell can pass `--movie "$PLEX_MOVIE_LIBRARY_ID"` unquoted
    # of any special case.
    sub.add_argument("--movie", nargs="?", const="", default=None, metavar="ID")
    sub.add_argument("--tv", nargs="?", const="", default=None, metavar="ID")
    sub.add_argument(
        "--webhook-delay",
        type=int,
        default=WEBHOOK_DELAY_SECONDS,
        help=argparse.SUPPRESS,  # test-only; production takes the default
    )
    sub.set_defaults(func=_cmd_plex_scan)

    sub = subparsers.add_parser(
        "tmdb-search", help="read 'title<TAB>year' lines on stdin, write JSON matches"
    )
    sub.add_argument("--tv", action="store_true", help="search TV shows instead of films")
    sub.set_defaults(func=_cmd_tmdb_search)

    sub = subparsers.add_parser(
        "organize-plan",
        help="read basenames on stdin, write where each one belongs",
    )
    sub.add_argument(
        "-0",
        "--null",
        action="store_true",
        help="delimit input with NUL instead of newline",
    )
    sub.set_defaults(func=_cmd_organize_plan)

    sub = subparsers.add_parser(
        "download-batch",
        help="read a put.io listing on stdin and run the whole download loop",
    )
    sub.add_argument(
        "-0",
        "--null",
        action="store_true",
        help="delimit the listing with NUL instead of newline",
    )
    sub.set_defaults(func=_cmd_download_batch)

    sub = subparsers.add_parser(
        "markers-sweep", help="remove extraction markers whose archive is gone"
    )
    sub.add_argument("--local-dir", default="", help=argparse.SUPPRESS)
    sub.set_defaults(func=_cmd_markers_sweep)

    sub = subparsers.add_parser(
        "archive-check", help="is this archive extracted, and are all its parts here"
    )
    sub.add_argument("name", metavar="NAME", help="archive file name, not a path")
    sub.add_argument("--local-dir", default="", help=argparse.SUPPRESS)
    sub.set_defaults(func=_cmd_archive_check)

    sub = subparsers.add_parser(
        "archive-parts", help="read a put.io listing on stdin, name this archive's parts"
    )
    sub.add_argument("name", metavar="NAME", help="archive file name, not a path")
    sub.add_argument("--local-dir", default="", help=argparse.SUPPRESS)
    sub.add_argument(
        "-0",
        "--null",
        action="store_true",
        help="delimit the listing with NUL instead of newline",
    )
    sub.set_defaults(func=_cmd_archive_parts)

    for name, help_text, func in (
        ("junk", "read paths on stdin, echo back the ones junk by name", _cmd_junk),
        ("sanitize", "read names on stdin, write the sanitized form of each", _cmd_sanitize),
        ("classify", "read names on stdin, write junk/parse/sanitize for each", _cmd_classify),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument(
            "-0",
            "--null",
            action="store_true",
            help="delimit input and output with NUL instead of newline",
        )
        sub.set_defaults(func=func)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "func", None) is None:
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
