# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Literal

import structlog


def _config_dir() -> Path:
    """Return the nexus config directory, respecting NEXUS_CONFIG_DIR."""
    override = os.environ.get("NEXUS_CONFIG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".config" / "nexus"


#: Long-lived daemon entry points (nexus-ovbr7). For these, stderr is a crash
#: channel, not a logging surface: when stderr is NOT a tty (detached spawn,
#: launchd/systemd), configure_logging drops the stderr StreamHandler so the
#: rotating file is the single copy of every event. The spawner points the
#: daemon's fd 1/2 at ``<mode>.crash.log``; with the stderr handler removed,
#: that file receives ONLY pre-configure failures and interpreter-fatal
#: tracebacks — without the removal it would accumulate an unbounded duplicate
#: of the event stream. A tty stderr (--foreground in a terminal) keeps the
#: handler for interactive debugging.
_DAEMON_MODES: frozenset[str] = frozenset({"t2_daemon", "t3_daemon", "storage_service"})


def _resolve_level(mode: str, verbose: bool) -> int:
    """Resolve the effective log level.

    Precedence (high to low):
      1. ``NEXUS_LOG_LEVEL`` env var (DEBUG / INFO / WARNING / ERROR / CRITICAL)
      2. ``verbose=True`` flag, DEBUG
      3. mode default: ``cli`` is WARNING (zero behaviour change for the CLI),
         non-cli (``console`` / ``mcp`` / ``hook``) is INFO so subprocess
         lifecycle, tool dispatch, and structured warnings actually land in
         the file handler.
    """
    override = os.environ.get("NEXUS_LOG_LEVEL", "").strip().upper()
    if override:
        resolved = getattr(logging, override, None)
        if isinstance(resolved, int):
            return resolved
    if verbose:
        return logging.DEBUG
    if mode == "cli":
        return logging.WARNING
    return logging.INFO


def configure_logging(
    mode: Literal[
        "cli", "console", "mcp", "hook", "watchdog",
        "t2_daemon", "t3_daemon", "storage_service",
    ],
    verbose: bool = False,
    config_dir: Path | None = None,
) -> None:
    """Configure logging for the given nexus entry point.

    Routes structlog events through the stdlib ``logging`` module so the
    rotating file handler installed below catches them. Without this
    bridge, ``structlog.get_logger().info(...)`` writes via structlog's
    default ``PrintLoggerFactory`` which goes to stderr, bypassing the
    file handler entirely (the historical reason ``mcp.log`` was a 0-byte
    file even on a long-running server).

    Modes:
      * ``cli``: stderr only, WARNING default. Kept legacy-compatible so
        the human-facing CLI does not gain noise from this change.
      * ``console`` / ``mcp`` / ``hook`` / ``watchdog`` / ``t2_daemon`` /
        ``t3_daemon`` / ``storage_service``: stderr + RotatingFileHandler
        at ``<config_dir>/logs/<mode>.log``, INFO default. Lifecycle
        events, tool dispatches, and structured warnings now land in the
        log file.

    *config_dir* overrides the log directory root (default:
    ``NEXUS_CONFIG_DIR`` env or ``~/.config/nexus``). The T2 daemon
    passes its own ``config_dir`` so a ``--config-dir`` override (or a
    tmp dir under test) logs to the right place rather than the global
    default.

    The level is overridable via the ``NEXUS_LOG_LEVEL`` env var; useful
    for one-off DEBUG runs without code changes.
    """
    level = _resolve_level(mode, verbose)

    # Configure stdlib root logger first. ``force=True`` clears any prior
    # handlers so re-invocation (e.g. server hot-restart) does not stack
    # duplicates.
    logging.basicConfig(level=level, format="%(message)s", stream=sys.stderr, force=True)

    # Suppress noisy HTTP / telemetry wire-trace loggers even in verbose
    # mode. These produce so much output at DEBUG that the signal in
    # mcp.log would drown.
    for noisy in ("httpx", "httpcore", "chromadb.telemetry", "opentelemetry"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # Bridge structlog -> stdlib logging. Two things wired here:
    #   1. ``LoggerFactory`` makes ``structlog.get_logger()`` return a
    #      logger that writes via the stdlib logging module instead of
    #      stderr-print.
    #   2. The processor chain serialises the structured event to a
    #      single-line key=value rendering before stdlib formats it.
    #      ``add_log_level`` keeps the level visible in the rendered
    #      message; ``TimeStamper`` adds an ISO timestamp; the final
    #      ``KeyValueRenderer`` turns the event dict into a string the
    #      file formatter can prepend with its own ``asctime / name /
    #      levelname`` columns.
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.set_exc_info,
            structlog.processors.KeyValueRenderer(
                key_order=["event", "timestamp", "level"],
                drop_missing=True,
            ),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        # cache_logger_on_first_use is deliberately False: tests that
        # re-configure structlog (conftest.pytest_configure, individual
        # test fixtures) need the new config to take effect, otherwise
        # the first cached logger sticks for the entire pytest session
        # and breaks downstream tests that rely on a different
        # logger_factory (e.g. capsys-based capture of structlog's
        # default PrintLoggerFactory output).
        cache_logger_on_first_use=False,
    )

    if mode == "cli":
        return  # stderr only — zero behaviour change for the CLI entry point

    # Non-CLI modes get a rotating file handler at <config>/logs/<mode>.log.
    logs_dir = (config_dir or _config_dir()) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"{mode}.log"

    # Remove stale handler for the same file if re-called (e.g. server
    # restart in tests). Without this, repeated configure_logging calls
    # would stack handlers and write each event N times.
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.handlers.RotatingFileHandler) and h.baseFilename == str(log_path):
            root.removeHandler(h)
            h.close()

    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
    )
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    root.addHandler(handler)

    # Daemon modes with a non-tty stderr: the file above is the single copy
    # (see _DAEMON_MODES). Without this, a spawner that captures the daemon's
    # stderr to a file would record every event twice.
    if mode in _DAEMON_MODES and not sys.stderr.isatty():
        for h in list(root.handlers):
            if (
                isinstance(h, logging.StreamHandler)
                and not isinstance(h, logging.handlers.RotatingFileHandler)
                and getattr(h, "stream", None) is sys.stderr
            ):
                root.removeHandler(h)


#: Standard levels `make_filtering_bound_logger` recognizes — used to
#: recover a wrapper_class's own threshold (see `_filtering_bound_logger_level`).
_STRUCTLOG_LEVELS: tuple[int, ...] = (
    logging.NOTSET, logging.DEBUG, logging.INFO,
    logging.WARNING, logging.ERROR, logging.CRITICAL,
)


def _filtering_bound_logger_level(wrapper_class: type) -> int:
    """Recover the ``min_level`` a `structlog.make_filtering_bound_logger`
    wrapper class was built with, by identity against the level->class
    lookup it uses internally (the factory returns a stable singleton per
    level, so identity comparison is reliable — nexus-mjc9l).

    Returns ``logging.NOTSET`` (0, i.e. "treat as unfiltered") for any
    wrapper_class this can't identify — the safe default, since assuming
    "no filtering" only ever WIDENS what a caller sees, never narrows it.
    """
    for level in _STRUCTLOG_LEVELS:
        if wrapper_class is structlog.make_filtering_bound_logger(level):
            return level
    return logging.NOTSET


@contextmanager
def open_run_log(name: str, config_dir: Path | None = None) -> Iterator[Path]:
    """Attach a per-run log file at ``<config_dir>/logs/<name>.log`` for
    the duration of the ``with`` block (nexus-mjc9l / nexus-47ubt item b).

    For long-running CLI commands (``nx index repo``) that give an
    operator no way to ``tail -f`` progress independent of terminal
    buffering, and no record if the terminal session is lost. Scoped
    to ONE command's execution — does NOT touch :func:`configure_logging`
    mode="cli"``'s deliberate stderr-only, quiet-by-default contract
    (every ``nx`` command calls that once at entry; this is additive
    and reverted on exit).

    In cli mode, TWO filters drop INFO events before any handler sees
    them: the structlog wrapper (``make_filtering_bound_logger(WARNING)``)
    and the stdlib root logger level (WARNING). A file handler alone
    would capture nothing — this context manager also lowers both to
    INFO for its duration, while explicitly pinning the existing stderr
    ``StreamHandler``(s) at the prior (quiet) level so the terminal's
    output is unaffected; only the new file handler sees the
    newly-unlocked INFO events. Everything is restored on exit — success
    or exception.

    Two calls with the same *name* never stack duplicate handlers: any
    existing ``RotatingFileHandler`` for the same path is removed first
    (mirrors :func:`configure_logging`'s dedup-by-``baseFilename`` check).

    Not reentrant or thread-safe: this mutates process-global structlog/
    root-logger state with no lock. Fine for today's single-invocation-
    per-process CLI use (``nx index repo``); nesting or concurrent calls
    within one process would corrupt each other's save/restore snapshots.

    Yields the log file :class:`Path`.
    """
    logs_dir = (config_dir or _config_dir()) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"{name}.log"

    root = logging.getLogger()

    for h in list(root.handlers):
        if isinstance(h, logging.handlers.RotatingFileHandler) and h.baseFilename == str(log_path):
            root.removeHandler(h)
            h.close()

    # code-review-expert (nexus-mjc9l): build the handler — the failable
    # I/O (file open, permissions) — BEFORE mutating any global structlog/
    # logging state. Building it after mutation meant a construction
    # failure left structlog/root permanently reconfigured for the rest
    # of the process (empirically reproduced: root.level stuck at INFO,
    # structlog wrapper_class stuck at the INFO filter). Nothing below
    # this point can fail, so once it succeeds the mutations are safe.
    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    # substantive-critic (nexus-mjc9l): "per-repo, latest-run overwrite via
    # rotation" (design doc, user-approved) means each run starts with a
    # FRESH file, previous run's content preserved as the .1 backup — not
    # append-forever with only size-triggered rotation (RotatingFileHandler's
    # default). Roll over once per entry, unconditionally, so run N's content
    # never bleeds into run N+1's "live" file.
    handler.doRollover()

    # substantive-critic (nexus-mjc9l): the try/finally starts here, BEFORE
    # any global mutation — the prior version only moved the one failable
    # I/O call (handler construction) ahead of the mutations but left the
    # mutation sequence itself (structlog.configure, setLevel calls,
    # addHandler) outside try/finally. Stdlib logging calls essentially
    # cannot raise, but "essentially" isn't "structurally guaranteed" — this
    # closes the class rather than narrowing it.
    pre_root_level = root.level
    pre_structlog = structlog.get_config()
    stderr_handlers = [
        h for h in root.handlers
        if isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.handlers.RotatingFileHandler)
        and getattr(h, "stream", None) is sys.stderr
    ]
    pre_stderr_levels = [h.level for h in stderr_handlers]

    try:
        # substantive-critic (nexus-mjc9l): preserve DEBUG when the caller
        # already requested it (--verbose / NEXUS_LOG_LEVEL=DEBUG) — the
        # prior hardcoded INFO silently dropped every DEBUG event for the
        # whole run, defeating an explicit verbosity request. Never RAISE
        # the effective level, only ensure INFO is unlocked at minimum.
        #
        # nexus-mjc9l regression (found post-merge review): stdlib root
        # level and structlog's OWN filtering threshold are independent
        # knobs — configure_logging("cli") happens to set them to the
        # same value, but a caller that reaches this primitive WITHOUT
        # going through configure_logging first (e.g. invoking a Click
        # command directly in a test, bypassing the group callback) may
        # have an unfiltered/NOTSET structlog wrapper while the stdlib
        # root sits at its Python default (WARNING). Deriving the
        # structlog override from `pre_root_level` in that case RAISED
        # structlog's threshold from "everything passes" to "INFO only",
        # silently dropping DEBUG events a test's structlog.testing
        # .capture_logs() expected to see — a regression, not a
        # tightening. Each side must be derived from ITS OWN ambient
        # state, never cross-derived from the other.
        stdlib_effective_level = min(pre_root_level, logging.INFO)
        handler.setLevel(stdlib_effective_level)
        pre_structlog_level = _filtering_bound_logger_level(pre_structlog["wrapper_class"])
        structlog_effective_level = min(pre_structlog_level, logging.INFO)
        new_structlog_config = dict(pre_structlog)
        new_structlog_config["wrapper_class"] = structlog.make_filtering_bound_logger(structlog_effective_level)
        structlog.configure(**new_structlog_config)
        root.setLevel(stdlib_effective_level)
        # Pin stderr handlers at the prior (quiet) root level so lowering
        # root/structlog to INFO doesn't leak newly-unlocked INFO events onto
        # the terminal — only the file handler below should see them.
        for h in stderr_handlers:
            h.setLevel(pre_root_level)
        root.addHandler(handler)

        yield log_path
    finally:
        root.removeHandler(handler)
        handler.close()
        for h, level in zip(stderr_handlers, pre_stderr_levels):
            h.setLevel(level)
        root.setLevel(pre_root_level)
        structlog.configure(**pre_structlog)


def open_child_log(
    name: str,
    config_dir: Path | None = None,
    *,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 2,
) -> IO[bytes]:
    """Open ``<config_dir>/logs/<name>.log`` for a daemon child's output.

    The anti-DEVNULL primitive (nexus-ovbr7): daemon supervisors pass the
    returned handle as a child's ``stdout``/``stderr`` instead of
    ``subprocess.DEVNULL``, so JVM banners, chroma tracebacks, and any
    other output a crash leaves behind survive the process. Four
    storage-service supervisor deaths (2026-06) were undiagnosable
    because every byte of evidence went to DEVNULL.

    Opened in binary append (``O_APPEND``) so a respawned child never
    truncates the previous incarnation's final output — that tail IS the
    crash evidence.

    The file is size-rotated AT OPEN TIME (``.log`` -> ``.log.1`` -> ...
    up to *backup_count*) when it exceeds *max_bytes*. Open-time
    rotation, not continuous: the handle is handed to a child process
    whose writes bypass Python entirely, so in-flight rotation is
    impossible without a pipe pump — and a pipe pump couples the child's
    liveness to the supervisor's (a child blocks on a full pipe once the
    pump dies), which is exactly wrong when the known failure mode is
    the supervisor dying. A *backup_count* of 0 disables rotation
    (the file grows unbounded — callers should not pass it).

    Raises ``OSError`` on filesystem failure; spawn sites that must not
    let a logging failure block the daemon use
    :func:`open_child_log_or_devnull`.
    """
    logs_dir = (config_dir or _config_dir()) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / f"{name}.log"

    if log_path.exists() and log_path.stat().st_size > max_bytes:
        oldest = log_path.with_name(f"{name}.log.{backup_count}")
        oldest.unlink(missing_ok=True)
        for i in range(backup_count - 1, 0, -1):
            src = log_path.with_name(f"{name}.log.{i}")
            if src.exists():
                src.rename(log_path.with_name(f"{name}.log.{i + 1}"))
        if backup_count > 0:
            log_path.rename(log_path.with_name(f"{name}.log.1"))

    return open(log_path, "ab")


def open_child_log_or_devnull(
    name: str,
    config_dir: Path | None = None,
    **kwargs: int,
) -> IO[bytes] | int:
    """:func:`open_child_log`, degrading to ``subprocess.DEVNULL`` on
    ``OSError`` (permissions, disk full, read-only mount).

    Availability beats observability at a spawn site: the DEVNULL path
    this replaces could never fail, so a logging failure must not become
    the reason a daemon doesn't start (CRE finding HIGH-1, nexus-ovbr7).
    The degradation is logged loud — it is itself an observability loss.

    Callers must only ``close()`` the result when it is not the DEVNULL
    sentinel (an ``int``).
    """
    import subprocess  # noqa: PLC0415 — stdlib subprocess deferred to function scope

    try:
        return open_child_log(name, config_dir, **kwargs)
    except OSError as exc:
        structlog.get_logger(__name__).warning(
            "child_log_open_failed_using_devnull",
            name=name,
            error=str(exc),
            consequence="this child's output will not be captured",
        )
        return subprocess.DEVNULL


def flush_logging() -> None:
    """Flush every handler on the root logger so buffered records are
    durable on disk before the process exits.

    nexus-61539: under CI load the T2 daemon could exit after logging the
    ``t2_daemon_stop_requested`` breadcrumb but before its
    ``RotatingFileHandler`` flushed that line, so the diagnostic was lost
    both in the test and in production. Callers on a shutdown path invoke
    this immediately after writing a must-survive breadcrumb and before
    any teardown that might stall (e.g. a hung DB close). Best-effort: a
    handler that raises on flush is skipped rather than masking the
    shutdown.
    """
    for h in logging.getLogger().handlers:
        try:
            h.flush()
        except Exception:  # noqa: BLE001 - best-effort durability on shutdown
            pass
