# SPDX-License-Identifier: AGPL-3.0-or-later
"""nx doctor — health check for all required services."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
import structlog


_log = structlog.get_logger(__name__)

_CHECK = "✓"
_WARN = "✗"


def _check_line(label: str, ok: bool, detail: str = "") -> str:
    status = _CHECK if ok else _WARN
    msg = f"  {status} {label}"
    if detail:
        msg += f": {detail}"
    return msg


def _fix(lines: list[str], *fix_lines: str) -> None:
    """Append indented Fix: lines after a failure entry."""
    first = True
    for fix_line in fix_lines:
        if first:
            lines.append(f"    Fix: {fix_line}")
            first = False
        else:
            lines.append(f"         {fix_line}")


# Keep old name so existing tests importing `_check` still work.
def _check(label: str, ok: bool, detail: str = "") -> str:
    return _check_line(label, ok, detail)


def _reinstall_command() -> str:
    """The reinstall command for THIS box's layout (nexus-utpuw.11).

    `uv tool install --reinstall conexus` rebuilds the uv tree, which a
    generation install does not use — and under .7's migration window a stray
    one re-symlinks over the nexus shims, which is the accepted risk doctor
    now reports separately.
    """
    from nexus import install_advice  # noqa: PLC0415 — deferred import

    return install_advice.upgrade_command("uv tool install --reinstall conexus")


def _run_check_schema(*, strict: bool = False) -> None:
    """Validate the T2 schema is actually applied (RDR-076; PORTED at
    nexus-vl8lk from an N/A stub).

    HISTORY: nexus-p0clh (2026-06-24) replaced this check's local-SQLite
    table/index/FTS5 census — which died with the =sqlite opt-out (RDR-158
    P3, nexus-7bomn) — with an unconditional "N/A in service mode" stub, to
    stop a fresh service-mode install from exiting non-zero on the
    misleading "T2 database not found". That traded one honesty problem for
    another: N/A ALWAYS printed and ALWAYS exited 0, even when the engine's
    schema genuinely failed to apply — a vacuous pass no different from the
    thing it replaced (nexus-vl8lk).

    PORT, not delete: the engine's ``GET /version`` (Java ``VersionHandler``,
    live since nexus-pebfx.4, well below the current
    ``REQUIRED_ENGINE_VERSION`` floor — no new engine route needed for this
    fix) already answers "is the schema applied" with the Liquibase
    changelog fingerprint (``schema_latest_id`` / ``schema_changeset_count``
    / ``schema_error``). :func:`nexus.health.probe_t2_schema_fingerprint`
    is the SHARED probe — the always-on ``nx doctor`` sweep
    (``nexus.health._check_t2_schema_applied``) asks the identical question
    through a terser ``HealthResult``; this flag exists for the deliberate,
    verbose, exit-code-bearing report an operator asks for explicitly.

    Exit codes: 2 when the engine is unreachable (state UNKNOWN — never
    conflated with "checked, clean"); 1 when the engine reports a
    schema_error or zero applied changesets; 0 (with an explicit N/A note)
    when the endpoint withholds the fingerprint by design (managed/cloud);
    0 with a changeset count otherwise.

    :param strict: nexus-b1v9z part B. The honest N/A above is a
        DELIBERATE, previously-litigated design (nexus-vl8lk) for
        interactive use — an operator asking "is my schema okay?" should
        not get a false failure just because their endpoint withholds the
        fingerprint by design. But a release-gate CALLER (release-
        sandbox.sh) cannot distinguish that N/A from a real pass by exit
        code alone, and the whole point of running this check there is to
        prove the substrate is present and correct — an N/A is exactly as
        uninformative as never having run the check. ``strict=True`` (the
        CLI's ``--fail-on-violation``, an existing doctor.py flag
        previously scoped to ``--check-storage-boundary``) makes ONLY the
        N/A outcome fatal; a genuine schema_error or zero-changeset FAIL
        was already non-zero regardless of this flag, and a healthy
        engine still exits 0.
    """
    from nexus.health import probe_t2_schema_fingerprint  # noqa: PLC0415 — deferred to keep CLI startup fast

    fp = probe_t2_schema_fingerprint()
    if not fp.reachable:
        click.echo(
            f"T2 schema check: service unavailable ({fp.unreachable_detail}). "
            "Schema state UNKNOWN — not reporting pass or fail.",
            err=True,
        )
        raise click.exceptions.Exit(2)

    if not fp.reported:
        click.echo(
            "T2 schema check: schema fingerprint not exposed by this "
            "endpoint (managed/cloud service withholds it by design, or "
            "the engine predates the /version schema fields) — N/A."
        )
        if strict:
            click.echo(
                "T2 schema check: FAIL (strict/gate mode) — an honest N/A "
                "is not proof the schema is applied; a release gate must "
                "see an actual OK.",
                err=True,
            )
            raise click.exceptions.Exit(1)
        return

    if fp.schema_error:
        click.echo(
            f"T2 schema check: FAIL — engine reported schema_error: {fp.schema_error}",
            err=True,
        )
        raise click.exceptions.Exit(1)

    if not fp.changeset_count:
        click.echo(
            f"T2 schema check: FAIL — schema_changeset_count={fp.changeset_count!r} "
            "(Liquibase applied nothing).",
            err=True,
        )
        raise click.exceptions.Exit(1)

    click.echo(
        f"T2 schema check: OK — {fp.changeset_count} changeset(s) applied, "
        f"latest={fp.latest_id} (Postgres, Liquibase-managed, via GET /version)."
    )




def _resolve_claude_cache_dir(cwd: Path | None = None) -> Path:
    """Return the Claude Code per-project MCP-log cache directory.

    Slug rule (observed on macOS 2026-04-25): cwd with both ``/`` and
    ``.`` replaced by ``-``. Example for cwd
    ``/Users/hal.hildebrand/git/nexus``:
    ``-Users-hal-hildebrand-git-nexus``.

    Empty slug means cwd was the filesystem root (very unusual);
    returns the cache parent so callers can detect the platform.
    """
    if cwd is None:
        cwd = Path.cwd()
    slug = str(cwd).replace("/", "-").replace(".", "-")
    if not slug or slug == "-":
        # Edge case: cwd is the root path. Return the cache parent so
        # caller's exists() check still does the right thing.
        return Path.home() / "Library" / "Caches" / "claude-cli-nodejs"
    return Path.home() / "Library" / "Caches" / "claude-cli-nodejs" / slug


#: Silent-death signatures from RDR-094 §Day 2 Operations §Diagnosing
#: nx-mcp silent death. Each signature is a substring matched against
#: the cache JSONL line's "debug" or "error" field.
_MCP_SILENT_DEATH_SIGNATURES: tuple[str, ...] = (
    "STDIO connection dropped after",
    "stdio transport error",
)

#: Tool-failure signatures (less severe; surfaced as info, not warning).
_MCP_TOOL_FAILURE_SIGNATURES: tuple[str, ...] = (
    "MCP error -32001: AbortError",
)


# ── nexus's OWN structured log (defect fix: the prior version of this
# ── check read ONLY Claude Code's client-side cache below and never
# ── looked here, where the MCP server's own tool-level failures land) ──────


def _resolve_nexus_log_dir() -> Path:
    """Return nexus's own structured-log directory, honouring
    ``NEXUS_CONFIG_DIR`` the same way the rest of the codebase does.

    ``configure_logging(mode="mcp", ...)`` (``nexus.logging_setup``)
    routes the running MCP server's own structlog events through a
    ``RotatingFileHandler`` at ``<config_dir>/logs/mcp.log`` (+ up to 5
    rotated backups). That is where tool-level failures such as
    ``mcp_memory_put_failed`` / ``mcp_query_failed`` /
    ``collection_search_failed`` actually land — evidence the prior
    version of ``--check-mcp-logs`` never read, because it only walked
    Claude Code's own client-side transport-death cache (see below).
    Delegates to :func:`nexus.config.nexus_config_dir`, the project's
    single source of truth for the ``NEXUS_CONFIG_DIR`` override,
    rather than hardcoding ``$HOME``.
    """
    from nexus.config import nexus_config_dir  # noqa: PLC0415 — deferred to keep CLI startup fast

    return nexus_config_dir() / "logs"


#: Matches one line written by the ``mcp`` logging mode's file formatter
#: (``nexus.logging_setup.configure_logging``:
#: ``"%(asctime)s %(name)s %(levelname)s %(message)s"``), where
#: ``message`` is structlog's ``KeyValueRenderer`` output
#: (``key_order=["event", "timestamp", "level"]``). Example:
#: ``2026-08-18 10:23:45,123 nexus.mcp.core ERROR event='mcp_query_failed' ...``
_NEXUS_LOG_LINE_RE = re.compile(
    r"^(?P<date>\S+) (?P<time>\S+) (?P<name>\S+) (?P<level>[A-Z]+) (?P<message>.*)$"
)
_NEXUS_LOG_EVENT_FIELD_RE = re.compile(r"event='([^']*)'")
_NEXUS_LOG_TIMESTAMP_FIELD_RE = re.compile(r"timestamp='([^']*)'")

#: Levels this check surfaces. WARNING is deliberately excluded — the task
#: is ERROR-level signal; a WARNING-inclusive sweep would drown the summary
#: in the same routine-warning noise ``_resolve_level`` already tunes the
#: file handler's default threshold for.
_NEXUS_LOG_ERROR_LEVELS: frozenset[str] = frozenset({"ERROR", "CRITICAL"})


def _iso_ts_to_epoch(ts_raw: str) -> float | None:
    """Best-effort ISO-8601 -> epoch-seconds. ``None`` on anything unparseable."""
    import datetime as _dt  # noqa: PLC0415 — deferred to keep CLI startup fast

    if not ts_raw:
        return None
    try:
        return _dt.datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def _parse_nexus_log_line(line: str) -> dict[str, str] | None:
    """Parse one line of nexus's own structured log. Returns a hit dict
    (``event``, ``level``, ``timestamp``, ``message``) for ERROR/CRITICAL
    lines this process's own formatter produced; ``None`` for everything
    else (other levels, blank lines, lines this formatter didn't write).
    """
    line = line.rstrip("\n")
    if not line:
        return None
    m = _NEXUS_LOG_LINE_RE.match(line)
    if not m or m.group("level") not in _NEXUS_LOG_ERROR_LEVELS:
        return None
    message = m.group("message")
    event_m = _NEXUS_LOG_EVENT_FIELD_RE.search(message)
    ts_m = _NEXUS_LOG_TIMESTAMP_FIELD_RE.search(message)
    return {
        "event": event_m.group(1) if event_m else "(unparsed)",
        "level": m.group("level"),
        "timestamp": ts_m.group(1) if ts_m else "",
        "message": message[:300],
    }


def _scan_nexus_log_errors(
    log_dir: Path,
    cutoff_epoch: float,
    *,
    stem: str = "mcp",
) -> list[dict[str, str]]:
    """Return ERROR/CRITICAL hits from nexus's own structured log within
    the lookback window.

    Walks ``<log_dir>/<stem>.log`` plus its ``RotatingFileHandler``
    backups (``<stem>.log.1`` .. ``<stem>.log.5``). A backup whose mtime
    predates *cutoff_epoch* is skipped outright — rotation stamps mtime
    at rotation time, so an old backup cannot contain in-window lines;
    the actively-written current file is always opened, with per-line
    timestamp filtering (the embedded structlog ``timestamp=`` field,
    always UTC) doing the rest. Best-effort throughout: a missing
    directory, an unreadable file, or a line this process's own
    formatter didn't produce is skipped rather than raised.
    """
    hits: list[dict[str, str]] = []
    if not log_dir.exists():
        return hits
    for path in sorted(log_dir.glob(f"{stem}.log*")):
        try:
            if path.stat().st_mtime < cutoff_epoch:
                continue
        except OSError:
            continue
        try:
            with path.open("r", errors="replace") as f:
                for line in f:
                    hit = _parse_nexus_log_line(line)
                    if hit is None:
                        continue
                    ts_epoch = _iso_ts_to_epoch(hit["timestamp"])
                    if ts_epoch is not None and ts_epoch < cutoff_epoch:
                        continue
                    hit["log_file"] = path.name
                    hits.append(hit)
        except OSError:
            continue
    return hits


def _summarize_nexus_log_errors(
    hits: list[dict[str, str]],
) -> dict[str, dict[str, Any]]:
    """Group ERROR/CRITICAL hits by event name: count + most-recent example.

    "Most recent" compares the embedded ISO-8601 ``timestamp`` strings
    lexicographically, which is time-ordered for same-precision UTC
    ISO-8601 -- true of every hit :func:`_parse_nexus_log_line` produces.
    """
    by_event: dict[str, dict[str, Any]] = {}
    for hit in hits:
        bucket = by_event.setdefault(hit["event"], {"count": 0, "most_recent": None})
        bucket["count"] += 1
        current = bucket["most_recent"]
        if current is None or hit["timestamp"] >= current.get("timestamp", ""):
            bucket["most_recent"] = hit
    return by_event


def _format_nexus_log_section(
    log_dir: Path,
    hours: int,
    hits: list[dict[str, str]],
    by_event: dict[str, dict[str, Any]],
) -> str:
    """Human-readable rendering of the nexus-own-log section."""
    lines = [f"nexus MCP server log ({log_dir}):"]
    if not hits:
        lines.append(f"  No ERROR/CRITICAL events in the last {hours}h.")
        return "\n".join(lines)
    lines.append(
        f"  [WARNING] {len(hits)} ERROR/CRITICAL event(s) across "
        f"{len(by_event)} distinct event name(s) in the last {hours}h:"
    )
    for name, info in sorted(by_event.items(), key=lambda kv: -kv[1]["count"]):
        recent = info["most_recent"] or {}
        ts = recent.get("timestamp") or "?"
        msg = recent.get("message", "")
        lines.append(
            f"    {name:<40} x{info['count']:<3} (most recent {ts}: {msg[:120]})"
        )
    return "\n".join(lines)


def _scan_mcp_log_jsonl(
    path: Path,
    cutoff_epoch: float,
) -> tuple[list[dict], list[dict]]:
    """Return (silent_deaths, tool_failures) found in *path*.

    Each match dict carries ``timestamp``, ``signature``, ``message``,
    ``session_id``, and ``log_file`` for cross-referencing against
    mcp.log + watchdog.log.
    """
    import json as _json  # noqa: PLC0415 — deferred to keep CLI startup fast
    import datetime as _dt  # noqa: PLC0415 — deferred to keep CLI startup fast

    silent_deaths: list[dict] = []
    tool_failures: list[dict] = []

    try:
        with path.open("r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                ts_raw = rec.get("timestamp", "")
                try:
                    ts_epoch = _dt.datetime.fromisoformat(
                        ts_raw.replace("Z", "+00:00"),
                    ).timestamp()
                except (ValueError, AttributeError):
                    continue
                if ts_epoch < cutoff_epoch:
                    continue
                msg = rec.get("debug", "") or rec.get("error", "")
                if not isinstance(msg, str):
                    continue
                hit = {
                    "timestamp": ts_raw,
                    "session_id": rec.get("sessionId", ""),
                    "message": msg[:200],
                    "log_file": str(path.name),
                }
                for sig in _MCP_SILENT_DEATH_SIGNATURES:
                    if sig in msg:
                        hit["signature"] = sig
                        silent_deaths.append(hit)
                        break
                else:
                    for sig in _MCP_TOOL_FAILURE_SIGNATURES:
                        if sig in msg:
                            hit["signature"] = sig
                            tool_failures.append(hit)
                            break
    except OSError:
        pass
    return silent_deaths, tool_failures


def _run_check_mcp_logs(*, json_out: bool, hours: int = 24) -> None:
    """Surface nx-mcp failure evidence from TWO independent sources.

    **Primary (defect fix, all platforms): nexus's own structured log**
    (``<config_dir>/logs/mcp.log`` -- see :func:`_resolve_nexus_log_dir`).
    This is where the MCP server's own tool-level failures land --
    ``mcp_memory_put_failed``, ``mcp_query_failed``,
    ``collection_search_failed``, etc. The check summarizes ERROR/
    CRITICAL events by name within the lookback window.

    **Secondary (preserved, RDR-094 §Day 2 Operations §Diagnosing nx-mcp
    silent death, nexus-3f95 + nexus-50u5): Claude Code's own per-server
    MCP cache** at
    ``~/Library/Caches/claude-cli-nodejs/<cwd-slug>/mcp-logs-*``. This is
    a DIFFERENT log, written by the Claude Code CLI client itself, and
    carries signal the server-side log structurally cannot: when
    nx-mcp's stdio transport dies before structlog can flush its last
    event, nothing lands in ``mcp.log`` -- only Claude Code's own
    client-side connection log sees the drop. Signatures scanned:

      * "STDIO connection dropped after Ns uptime"
      * "stdio transport error"

    Tool-failure events ("AbortError" client-side aborts) are surfaced
    as info entries; they may indicate user-cancelled tool calls
    rather than crashes.

    On non-macOS platforms (no ``~/Library/Caches/claude-cli-nodejs``)
    the Claude-cache half exits cleanly with "not present on this
    platform" -- the cache is a Claude Code CLI implementation detail,
    not part of the MCP protocol. The nexus-own-log half runs
    regardless of platform.
    """
    import json as _json  # noqa: PLC0415 — deferred to keep CLI startup fast
    import time  # noqa: PLC0415 — deferred to keep CLI startup fast

    cutoff_epoch = time.time() - hours * 3600.0

    # ── Primary: nexus's own structured log ────────────────────────────────
    nexus_log_dir = _resolve_nexus_log_dir()
    nexus_hits = _scan_nexus_log_errors(nexus_log_dir, cutoff_epoch)
    nexus_by_event = _summarize_nexus_log_errors(nexus_hits)

    # ── Secondary: Claude Code's client-side cache (preserved as-is) ───────
    cache_dir = _resolve_claude_cache_dir()

    payload: dict[str, Any] = {
        "cache_dir": str(cache_dir),
        "hours_window": hours,
        "platform_supported": False,
        "silent_deaths": [],
        "tool_failures": [],
        "log_dirs_scanned": 0,
        "log_files_scanned": 0,
        # Additive keys (nexus's own log; independent of platform_supported).
        "nexus_log_dir": str(nexus_log_dir),
        "nexus_log_error_count": len(nexus_hits),
        "nexus_log_by_event": nexus_by_event,
    }

    if not cache_dir.exists():
        if json_out:
            click.echo(_json.dumps(payload, indent=2))
        else:
            click.echo(_format_nexus_log_section(nexus_log_dir, hours, nexus_hits, nexus_by_event))
            click.echo("")
            click.echo(
                f"MCP log surface not present at {cache_dir} "
                f"(macOS-only path; nothing to check on this platform)."
            )
        return

    payload["platform_supported"] = True

    log_dirs = sorted(cache_dir.glob("mcp-logs-*"))
    payload["log_dirs_scanned"] = len(log_dirs)

    for log_dir in log_dirs:
        if not log_dir.is_dir():
            continue
        for jsonl in log_dir.glob("*.jsonl"):
            try:
                if jsonl.stat().st_mtime < cutoff_epoch:
                    continue
            except OSError:
                continue
            payload["log_files_scanned"] += 1
            sd, tf = _scan_mcp_log_jsonl(jsonl, cutoff_epoch)
            for hit in sd:
                hit["server"] = log_dir.name
                payload["silent_deaths"].append(hit)
            for hit in tf:
                hit["server"] = log_dir.name
                payload["tool_failures"].append(hit)

    if json_out:
        click.echo(_json.dumps(payload, indent=2))
        return

    click.echo(_format_nexus_log_section(nexus_log_dir, hours, nexus_hits, nexus_by_event))
    click.echo("")
    click.echo(
        f"Scanned {payload['log_files_scanned']} JSONL files across "
        f"{payload['log_dirs_scanned']} mcp-logs-* dirs under "
        f"{cache_dir} (last {hours}h)."
    )
    if not payload["silent_deaths"] and not payload["tool_failures"]:
        click.echo("No silent-death or tool-failure signatures found.")
        return

    if payload["silent_deaths"]:
        click.echo(
            f"\n[WARNING] Silent-death signatures: "
            f"{len(payload['silent_deaths'])}"
        )
        click.echo(
            "  Cross-reference these timestamps against "
            "~/.config/nexus/logs/mcp.log + ~/.config/nexus/logs/watchdog.log"
        )
        click.echo(
            "  to identify the gap. See RDR-094 §Day 2 Operations."
        )
        for hit in payload["silent_deaths"]:
            click.echo(
                f"  {hit['timestamp']}  {hit['signature']}  "
                f"server={hit['server']}  session={hit['session_id'][:8]}..."
            )

    if payload["tool_failures"]:
        click.echo(
            f"\n[INFO] Tool-failure signatures: "
            f"{len(payload['tool_failures'])} "
            f"(may be user-cancelled aborts, not crashes)"
        )
        for hit in payload["tool_failures"][:5]:
            click.echo(
                f"  {hit['timestamp']}  {hit['signature']}  "
                f"server={hit['server']}"
            )
        if len(payload["tool_failures"]) > 5:
            click.echo(
                f"  ... and {len(payload['tool_failures']) - 5} more "
                "(use --json for full list)"
            )


#: Minimum global-tier (``project=''``) builtin plan rows expected after
#: seeding (``nx plan reseed``, formerly ``nx catalog setup``). RDR-078
#: shipped 9; RDR-092 Phase 0a brought that to 12; the live count at
#: nexus-vl8lk was 15; nexus-h33x8.6 a1 added 2 single-query-step
#: fast-path templates, bringing it to 17 (``conexus/plans/builtin/
#: *.yml``). The check only fails below 9 so a partial install on an
#: older plugin is still tolerated — same conservative floor the
#: pre-N/A-stub check used.
#:
#: This floor is a BACKSTOP, not the check. It can only detect a
#: near-empty library: it passed green on this project's own install
#: while two templates had never been seeded and three descriptions had
#: drifted (nexus-f1mbo), because 15 > 9. The check that can actually
#: fail against that condition is the disk-vs-live parity assert below.
_MIN_GLOBAL_BUILTIN_COUNT: int = 9


@dataclass(frozen=True)
class _ParityReport:
    """Disk-vs-live comparison of the global builtin plan tier."""

    #: Templates on disk with no library row at their dimensions.
    missing: list[str]
    #: Templates whose library row no longer matches the file.
    drifted: list[str]
    #: builtin-tagged library rows matching no template on disk.
    orphaned: list[str]
    #: Set when parity could not be established at all (templates
    #: unreadable, or nothing to compare against).
    unavailable: str | None = None
    #: True when drift WAS checked but absence could not be: the live
    #: listing hit its page cap, so a template with no row here may simply
    #: be on a page we never read. Reported explicitly — a partially
    #: checked gate that reads as fully checked is the failure mode this
    #: whole check exists to remove.
    missing_unchecked: bool = False

    @property
    def failed(self) -> bool:
        return bool(self.missing or self.drifted)


def _summarise(names: list[str], limit: int = 5) -> str:
    """Join *names*, eliding past *limit* so one bad seed is still readable.

    A fresh or unseeded library puts every shipped template in this list,
    and a 17-item comma run wraps into an unreadable block exactly when
    the operator most needs to read it.
    """
    if len(names) <= limit:
        return ", ".join(names)
    return f"{', '.join(names[:limit])} and {len(names) - limit} more"


def _plan_library_parity(rows: list[dict[str, Any]], *, truncated: bool) -> _ParityReport:
    """Compare shipped builtin templates against the live library.

    The counterpart to the count floor: it answers "is the library the
    one these templates describe", which is the question the floor could
    never fail on.

    A truncated listing degrades to ``unavailable`` rather than reporting
    false MISSING rows — a template absent from a capped page proves
    nothing (the vacuous-gate inverse: never manufacture a red either).
    """
    from pathlib import Path  # noqa: PLC0415 — deferred to keep CLI startup fast

    import yaml  # noqa: PLC0415 — deferred to keep CLI startup fast

    from nexus.commands.catalog import _resolve_plugin_root  # noqa: PLC0415 — deferred to keep CLI startup fast
    from nexus.indexer_utils import find_repo_root  # noqa: PLC0415 — deferred to keep CLI startup fast
    from nexus.plans.schema import canonical_dimensions_json  # noqa: PLC0415 — deferred to keep CLI startup fast
    from nexus.plans.seed_loader import desired_row_for_template  # noqa: PLC0415 — deferred to keep CLI startup fast

    repo_root = find_repo_root(Path.cwd()) or Path.cwd()
    builtin_dir = _resolve_plugin_root(repo_root) / "plans" / "builtin"
    if not builtin_dir.is_dir():
        return _ParityReport([], [], [], unavailable=f"no template dir at {builtin_dir}")

    paths = sorted(
        p for p in builtin_dir.iterdir()
        if p.is_file() and p.suffix in (".yml", ".yaml")
    )
    if not paths:
        return _ParityReport([], [], [], unavailable=f"no templates under {builtin_dir}")

    live: dict[str, dict[str, Any]] = {
        d: r for r in rows
        if (r.get("project") or "") == "" and (d := r.get("dimensions"))
    }

    missing: list[str] = []
    drifted: list[str] = []
    seen: set[str] = set()
    for path in paths:
        try:
            template = dict(yaml.safe_load(path.read_text()) or {})
            # The global tier stores scope:global under project='', so apply
            # the same in-memory scope_override the loader applies for this
            # directory — before deriving BOTH the key and the row, or a file
            # declaring another scope would be compared against the wrong one.
            dims = dict(template.get("dimensions") or {})
            dims["scope"] = "global"
            template["dimensions"] = dims
            canonical = canonical_dimensions_json(dims)
            desired = desired_row_for_template(template)
        except Exception as exc:  # noqa: BLE001 — an unreadable template makes parity unknowable, not failed
            return _ParityReport(
                [], [], [], unavailable=f"{path.name}: {type(exc).__name__}: {exc}",
            )
        seen.add(canonical)
        row = live.get(canonical)
        if row is None:
            if not truncated:
                missing.append(path.name)
        elif desired.differs_from(row):
            drifted.append(path.name)

    if truncated:
        # Drift is still trustworthy — a row we DID see either matches its
        # template or does not. Absence is not, and neither is orphanhood,
        # since both are claims about rows we may never have read.
        return _ParityReport([], drifted, [], missing_unchecked=True)

    orphaned = sorted(
        str(r.get("name") or r.get("id"))
        for d, r in live.items()
        if d not in seen and "builtin-template" in (r.get("tags") or "")
    )
    return _ParityReport(missing, drifted, orphaned)


def _run_check_plan_library() -> None:
    """Plan-library dimensional health (RDR-092 Phase 0c.2), PORTED at
    nexus-vl8lk from an N/A stub.

    HISTORY: this check originally queried the local SQLite ``plans``
    table directly. RDR-158 P3 (nexus-7bomn) killed the =sqlite opt-out,
    and the check was stubbed to an unconditional "N/A in service mode"
    (mirroring nexus-p0clh's --check-schema treatment) so a fresh
    service-mode install stopped exiting non-zero on "T2 database not
    found". That stub ALWAYS printed N/A and ALWAYS exited 0 — a vacuous
    pass no different from the thing it replaced (nexus-vl8lk); the
    dedicated release-sandbox smoke arm even ran the (now-retired)
    ``nx catalog setup`` purely to satisfy this check, its failure
    swallowed by ``|| true``, so neither half was ever exercised.

    PORT decision (bead's own text): "the builtin-template floor is the
    valuable half and is answerable: the plan library lives in Postgres
    and the templates are countable." ``HttpPlanLibrary.list_plans``
    already exists (no new engine route) — calling it with ``project=""``
    omits the project filter server-side (``PlanRepository.listPlans``:
    ``project == null`` -> no ``WHERE project = ...`` clause). NOT quite
    the original SQLite whole-table census (``SELECT COUNT(*) WHERE 1=1``):
    the engine's ``listPlans`` unconditionally filters TTL-expired rows
    (``include_disabled`` lifts only the soft-disabled filter), so the
    counts here are non-expired rows, optionally including soft-disabled.
    Builtin templates ship ``ttl=None`` (permanent), so the gating floor
    check below is unaffected; only the informational totals differ.
    ``dimensions`` / ``tags`` / ``project`` cross the wire unchanged
    (``PlanRepository.recordToMap``), so the authored / backfilled /
    non-dimensional bucketing heuristic from the SQLite era still applies
    verbatim — only the storage engine changed, so it is PRESERVED rather
    than dropped (the bead's licensed alternative for a census that "no
    longer means anything" does not apply here: the tags/dimensions
    columns mean exactly what they meant before).

    Paging: bounded at ``MAX_QUERY_RESULTS`` (the project's paging
    ceiling) per call — the plan library is small (17 builtins + grown
    plans) but a future large deployment could exceed the cap; that case
    is reported as a NOTE, never silently undercounted.
    """
    import httpx  # noqa: PLC0415 — deferred to keep CLI startup fast

    from nexus.db.limits import MAX_QUERY_RESULTS  # noqa: PLC0415 — deferred to keep CLI startup fast
    from nexus.db.t2.http_plan_library import HttpPlanLibrary  # noqa: PLC0415 — deferred to keep CLI startup fast

    try:
        lib = HttpPlanLibrary()
        rows = lib.list_plans(limit=MAX_QUERY_RESULTS, include_disabled=True)
    except (httpx.HTTPError, RuntimeError) as exc:
        # RuntimeError is not redundant with httpx.HTTPError: constructing
        # the client resolves the endpoint, and an unresolvable one raises
        # ServiceEndpointUnresolvableError, a RuntimeError subclass that is
        # NOT an httpx.HTTPError (same trap documented on
        # _report_aspect_queue_service).
        click.echo(
            f"Plan library check: service backend unreachable ({exc}). "
            "Counts UNKNOWN — not reporting pass or fail.",
            err=True,
        )
        raise click.exceptions.Exit(2)

    total = len(rows)
    truncated = total == MAX_QUERY_RESULTS

    def _is_backfill(tags: str) -> bool:
        return "backfill" in tags

    non_dimensional = sum(1 for r in rows if not r.get("dimensions"))
    backfilled = sum(
        1 for r in rows if r.get("dimensions") and _is_backfill(r.get("tags") or "")
    )
    authored = sum(
        1 for r in rows if r.get("dimensions") and not _is_backfill(r.get("tags") or "")
    )
    global_builtin = sum(
        1
        for r in rows
        if (r.get("project") or "") == "" and "builtin-template" in (r.get("tags") or "")
    )

    click.echo("Plan library check (service backend):")
    trunc_note = " (hit the page cap — see NOTE below)" if truncated else ""
    click.echo(f"  total rows:         {total}{trunc_note}")
    click.echo(f"  authored:           {authored}")
    click.echo(f"  backfilled:         {backfilled}")
    click.echo(f"  non-dimensional:    {non_dimensional}")
    click.echo(f"  global-tier builtin count: {global_builtin}")
    click.echo("")

    failed = False
    if global_builtin < _MIN_GLOBAL_BUILTIN_COUNT:
        click.echo(
            f"  FAIL: global-tier builtin count {global_builtin} "
            f"< expected {_MIN_GLOBAL_BUILTIN_COUNT}",
            err=True,
        )
        click.echo("    Fix: run `nx plan reseed`.", err=True)
        failed = True

    # Disk-vs-live parity (nexus-f1mbo). The count floor above cannot fail
    # against a library that is the wrong SHAPE — only against one that is
    # nearly empty. This is the assert that can.
    parity = _plan_library_parity(rows, truncated=truncated)
    if parity.unavailable:
        click.echo(
            f"  NOTE: template parity not checked ({parity.unavailable}).",
            err=True,
        )
    warnings = 0
    if parity.unavailable is None:
        if parity.missing_unchecked:
            click.echo(
                "  NOTE: drift was checked, but MISSING templates were not — "
                "the live listing hit its page cap, so absence is unprovable.",
                err=True,
            )
        if parity.missing:
            click.echo(
                f"  FAIL: {len(parity.missing)} template(s) absent from the "
                f"library: {_summarise(parity.missing)}",
                err=True,
            )
            click.echo("    Fix: run `nx plan reseed`.", err=True)
        if parity.drifted:
            click.echo(
                f"  FAIL: {len(parity.drifted)} library row(s) no longer "
                f"match their template: {_summarise(parity.drifted)}",
                err=True,
            )
            click.echo("    Fix: run `nx plan reseed --force`.", err=True)
        if parity.orphaned:
            click.echo(
                f"  WARN: {len(parity.orphaned)} builtin row(s) have no "
                f"template shipped: {_summarise(parity.orphaned)}. Left in "
                "place — remove with `nx plan delete <id>` if intended.",
                err=True,
            )
            warnings += 1
        failed = failed or parity.failed
    if non_dimensional:
        click.echo(
            f"  WARN: {non_dimensional} non-dimensional row(s) "
            "(legacy / pre-RDR-078 seeds).",
            err=True,
        )
        warnings += 1
    if truncated:
        click.echo(
            f"  NOTE: plan count hit the {MAX_QUERY_RESULTS}-row page cap — "
            "counts above may undercount the true library size.",
            err=True,
        )

    if failed:
        raise click.exceptions.Exit(1)
    # A WARN alone does not change the exit code (nexus-eg5tw) — only the
    # FAIL:-class conditions above do that, via `failed`. But the verdict
    # line must say so: printing an unqualified "All checks passed." next
    # to a WARN emitted two lines above is a self-contradiction within the
    # same block, not two independent facts.
    if warnings:
        click.echo(f"All checks passed, with {warnings} warning(s).")
    else:
        click.echo("All checks passed.")


def _run_trim_telemetry(days: int, dry_run: bool = False) -> None:
    """Delete (or, with ``dry_run=True``, PREVIEW) aged audit-log rows older
    than *days* (RDR-087 P2.4; nexus-7365x).

    Trims both ``search_telemetry`` (RDR-087) and ``hook_failures`` (RDR-164 P0
    audit-table TTL parity) — the two age-reaped, no-cascade audit tables.

    ``dry_run=True`` reports what WOULD be removed without deleting anything
    (the search_telemetry trim-preview gap this closes: until now there was
    no way to learn the row count before ``--trim-telemetry`` deleted it —
    see T2 ``nexus/shakedown-2026-08-11-s11-telemetry``). Both tables are
    previewed together under one ``--dry-run`` — trimming ``search_telemetry``
    for real while only previewing ``hook_failures`` (or vice versa) would be
    a worse footgun than the missing feature, so
    :meth:`HttpTelemetryStore.trim_hook_failures` grew the identical
    ``dry_run`` contract alongside :meth:`trim_search_telemetry` rather than
    leaving it a partial, single-table preview.
    """
    # nexus-ingey: this used to construct Telemetry(db_path) unconditionally.
    # On a migrated box that is the FROZEN SQLite — the verb trimmed a file
    # nothing reads, printed "Trimmed N rows", and left the live PG audit tables
    # growing untrimmed. The engine has exposed the operation the whole time
    # (POST /v1/telemetry/{search,hook_failures}/trim -> TelemetryRepository
    # .trimSearchTelemetry / .trimHookFailures). Seam COLLAPSED (nexus-i711w
    # Stage 2 sub-stage A): HttpTelemetryStore is the only telemetry store —
    # the SQLite arm died with the store.
    import httpx  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

    from nexus.db.t2.http_telemetry_store import HttpTelemetryStore  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

    if dry_run:
        # nexus-5uoxu belt-and-braces: the dry-run branch is ENGINE-side.
        # An engine below the version that introduced the 3-arg overload
        # ignores the unknown ``dry_run`` JSON field and takes the DELETE
        # branch — a preview flag that deletes (the exact reason 2c1f929c
        # was reverted out of 7.6.1, which pinned engine v0.1.71). The
        # paired-release choreography prevents that pairing for THIS
        # product's installs; this probe protects every other pairing
        # (older cloud engine, lagging deploy, hand-configured endpoint):
        # refuse the preview outright when the SERVING engine cannot honor
        # it. Fail-closed: an unprobeable version is a refusal too — a
        # preview must never be a gamble.
        from nexus.engine_version import (  # noqa: PLC0415 — deferred local import
            TRIM_DRY_RUN_MIN_ENGINE_VERSION,
            parse_engine_version,
        )

        serving: tuple[int, int, int] | None = None
        try:
            # Evidence-gated resolver (review Important, nexus-7dsgp class):
            # the bare resolve_service_endpoint(wait_budget_s=0) can read
            # falsely unresolvable in the 5-10s supervisor-respawn gap and
            # spuriously refuse a preview the store call would have served.
            from nexus.db.service_endpoint import resolve_service_endpoint_with_evidence_gate  # noqa: PLC0415 — deferred local import

            base_url, _token = resolve_service_endpoint_with_evidence_gate()
            resp = httpx.get(f"{base_url.rstrip('/')}/version", timeout=10)
            resp.raise_for_status()
            body = resp.json()
            if isinstance(body, dict):
                serving = parse_engine_version(body.get("release_version"))
        except Exception:  # noqa: BLE001 — fail-closed below; the refusal message carries the remedy
            serving = None
        if serving is None or serving < TRIM_DRY_RUN_MIN_ENGINE_VERSION:
            floor = ".".join(str(x) for x in TRIM_DRY_RUN_MIN_ENGINE_VERSION)
            got = ".".join(str(x) for x in serving) if serving else "unprobeable"
            click.echo(
                f"Error: --dry-run requires engine-service v{floor}+ (serving: "
                f"{got}). On an older engine the preview flag is silently "
                "dropped and the trim DELETES what it claims to preview — "
                "refusing. Upgrade the engine (nx upgrade / redeploy), or run "
                "without --dry-run only if you intend a real trim.",
                err=True,
            )
            raise click.exceptions.Exit(2)

    try:
        store = HttpTelemetryStore()
        deleted_search = store.trim_search_telemetry(days=days, dry_run=dry_run)
        deleted_hooks = store.trim_hook_failures(days=days, dry_run=dry_run)
    except (httpx.HTTPError, RuntimeError) as exc:
        # Same class as _report_aspect_queue_service above (review
        # 2026-07-25): store CONSTRUCTION resolves the endpoint and raises
        # ServiceEndpointUnresolvableError (a RuntimeError, not an
        # httpx error) when it cannot. This branch originally had NO
        # handling at all, so an unresolvable endpoint or a transport blip
        # crashed `nx doctor --trim` outright.
        #
        # Reporting nothing trimmed would be the false-clean this whole
        # commit exists to remove — say UNKNOWN and exit non-zero so a
        # scripted caller cannot mistake a failed trim for a completed one.
        verb = "preview" if dry_run else "trim"
        click.echo(
            f"Error: telemetry {verb} unavailable ({exc}). Nothing was "
            "trimmed and the live retention state is UNKNOWN.",
            err=True,
        )
        raise click.exceptions.Exit(2)
    verb = "Would trim" if dry_run else "Trimmed"
    for table, deleted in (
        ("search_telemetry", deleted_search),
        ("hook_failures", deleted_hooks),
    ):
        noun = "row" if deleted == 1 else "rows"
        click.echo(f"{verb} {deleted} {table} {noun} older than {days} days.")


# ── --check-aspect-queue (nexus-1pfq) ────────────────────────────────────────


def _report_aspect_queue_service() -> None:
    """Aspect-queue depth from the LIVE PG queue over HTTP (nexus-k0luu).

    Mirrors the sqlite branch's output shape (total, per-status breakdown,
    failed rows with last_error) so the operator reads one report regardless of
    backend. Sourced from HttpAspectQueue, never from the frozen local file.

    Fails LOUD on a transport error rather than degrading to the sqlite path:
    silently falling back would reproduce the exact defect being fixed — a
    frozen-file reading presented as the live queue. A transport failure is
    reported as UNKNOWN (exit 0 -- not reporting pass or fail); a nonzero
    FAILED-row count from a reachable queue is a genuine content failure and
    raises ``click.exceptions.Exit(1)`` with a ✗ FAIL: marker (nexus-fylxo),
    matching the 4 sibling supplementary checks (resources / plan-library /
    taxonomy / t1) that already signal failure this way.
    """
    import httpx  # noqa: PLC0415 — deferred to keep CLI startup fast

    from nexus.db.t2.http_aspect_queue import HttpAspectQueue  # noqa: PLC0415 — deferred to keep CLI startup fast

    try:
        q = HttpAspectQueue()
        pending = q.pending_count()
        failed = q.list_failed()
    except (httpx.HTTPError, RuntimeError) as exc:
        # RuntimeError is NOT redundant with httpx.HTTPError: constructing the
        # store resolves the endpoint, and an unresolvable one raises
        # ServiceEndpointUnresolvableError, which subclasses RuntimeError and
        # NOT httpx.HTTPError (review 2026-07-25). Catching only the transport
        # error let a missing supervisor lease / absent NX_SERVICE_TOKEN escape
        # as a traceback out of `nx doctor` — turning a health check into a
        # crash, in the very commit whose purpose was to stop doctor from
        # misreporting health. The console twin
        # (console/routes/health.py::_collect_aspect_queue_data_service) had
        # this right; this call site did not.
        click.echo(
            f"aspect_extraction_queue: service backend unreachable ({exc}). "
            "Queue depth UNKNOWN — not reporting a count.",
            err=True,
        )
        return

    click.echo(f"aspect_extraction_queue: {pending} pending, {len(failed)} failed (service backend)")
    if failed:
        click.echo(f"\nFailed rows (showing top {min(len(failed), 20)}):")
        for row in failed[:20]:
            click.echo(
                f"  {row.collection or '?'} :: {row.source_path or '?'} "
                f"(retries {row.retry_count})"
            )
        # KNOWN GAP, stated rather than silently omitted: the sqlite branch
        # prints each row's last_error, this one cannot. AspectRepository
        # .listFailed does not select LAST_ERROR, so it never crosses the wire
        # — QueueRow has no field for it either. Omitting it quietly would make
        # a failing queue look like it had no recorded errors, which is the same
        # false-clean class this fix exists to remove. Closing it needs an
        # engine change (add LAST_ERROR to the projection) + a tag; tracked on
        # nexus-k0luu.
        click.echo(
            "\n  (last_error is not carried by the service list endpoint — "
            "see the worker logs for the failure text)"
        )
        click.echo("\nRe-enqueue them with: nx aspects requeue-failed")
        # nexus-fylxo: a nonzero failed-row backlog is a real content
        # problem, not merely descriptive detail -- its 4 siblings
        # (resources / plan-library / taxonomy / t1) all emit a ✗/FAIL:
        # marker and raise Exit on their own failure condition; this check
        # printed the numbers and silently returned 0 regardless of how
        # large the backlog grew. Match the sibling contract.
        click.echo(
            f"\n✗ FAIL: {len(failed)} failed aspect-extraction row(s) in "
            "the queue.",
            err=True,
        )
        raise click.exceptions.Exit(1)


def _run_check_aspect_queue() -> None:
    """Report aspect_extraction_queue depth + per-status breakdown.

    RDR-089 follow-up nexus-qeo8 introduced an async worker
    (``aspect_worker.py``) that drains this table on a daemon thread.
    Without observability, a backlog grows silently.

    nexus-k0luu: reads the SERVICE queue (PG). This check once had no
    service branch — on a migrated box it read the FROZEN SQLite queue and
    reported it as current while the live queue was in PG, which is worse
    than useless here: `nx aspects requeue-failed` directs operators to
    this very check for backlog visibility. The SQLite reader leg died
    with the =sqlite opt-out (RDR-158 P3, nexus-7bomn).
    """
    _report_aspect_queue_service()


# ── --check-tier-discipline (nexus-a52i) ─────────────────────────────────────


def _run_check_tier_discipline() -> None:
    """Audit tier-write activity for the current session.

    Reads ``tier_writes`` from T2 and prints the same summary as
    ``nx tier-status`` for the current session, plus a structured
    warning when a session has zero tier writes (a soft signal that
    the session may have produced findings without persisting them).

    Heuristic only — does NOT exit non-zero. Visibility, not
    enforcement.
    """
    import os as _os  # noqa: PLC0415 — deferred to keep CLI startup fast

    from nexus.session import read_claude_session_id as _read_claude_session_id  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

    session_id = (
        _os.environ.get("NX_SESSION_ID", "").strip()
        or _read_claude_session_id()
    )
    if not session_id:
        click.echo("Tier-discipline check:")
        click.echo("  No current session resolvable (skip).")
        return

    # nexus-59wjj: real counts via GET /v1/telemetry/tier_writes/query.
    # On any failure (engine predates the route, service unreachable) fall
    # back to the nexus-wyu1g honest message — never a false-clean "no
    # writes seen". (The local-SQLite tier_writes reader — and the 59wjj
    # Critical-1 ordering hazard of gating on the local file's existence —
    # died with the =sqlite opt-out, RDR-158 P3 nexus-7bomn.)
    try:
        from nexus.db.t2.http_telemetry_store import HttpTelemetryStore  # noqa: PLC0415 — deferred: heavy import, keep CLI startup fast

        svc_rows = HttpTelemetryStore().query_tier_writes(session_id=session_id)
    except Exception as exc:  # noqa: BLE001 — degrade to the honest failure-shaped message, never a silent 0
        _log.debug("doctor_tier_discipline_service_read_failed", exc_info=True)
        from nexus.db.t2.http_telemetry_store import tier_writes_read_failure_message  # noqa: PLC0415 — deferred: heavy import, keep CLI startup fast

        click.echo("Tier-discipline check:")
        click.echo(
            "  service-backed telemetry (Postgres) — local inspection N/A; "
            + tier_writes_read_failure_message(exc)
        )
        return
    by_tier: dict[str, int] = {}
    for _tool, tier, _agent, _project, n in svc_rows:
        by_tier[tier] = by_tier.get(tier, 0) + n
    total = sum(by_tier.values())
    click.echo(f"Tier-discipline check (session {session_id}, service-backed):")
    if total == 0:
        click.echo(
            "  WARNING: zero tier writes recorded for this session. "
            "Findings produced (if any) have not been persisted."
        )
        click.echo(
            "  Run with `nx tier-status --session " + session_id +
            "` for the structured view."
        )
        click.echo(
            "  Pass --json for downstream tooling. Use `nx memory put`, "
            "`nx scratch put`, or the MCP equivalents to write back."
        )
        return
    click.echo(f"  total writes: {total}")
    for tier in ("T1", "T2", "T3", "plan"):
        if by_tier.get(tier, 0):
            click.echo(f"    {tier:<6} {by_tier[tier]}")
    if not by_tier.get("T2", 0) and not by_tier.get("T3", 0):
        click.echo(
            "  NOTE: writes are T1/plan only. No persistent (T2/T3) "
            "write-back yet — durable findings are not surfaced."
        )


# ── --check-post-store-hooks (nexus-b0ka) ────────────────────────────────────


def _run_check_post_store_hooks() -> None:
    """Enumerate post-store hooks attached to a default ``HookRegistry``.

    The hook chains are no longer module-level globals (RDR-118
    successor): each entry point constructs its own ``HookRegistry`` and
    wires the load-bearing default consumers via
    :func:`nexus.hook_registry.install_default_hooks`. This diagnostic
    builds a fresh registry the same way and prints the consumers
    attached to each chain so operators can confirm the install factory
    is wiring the expected set.

    Use cases (from nexus-b0ka):

      * Confirm RDR-089 ``aspect_extraction_enqueue_hook`` registers
        on the document chain.
      * Detect drift if a hook silently fails to register due to
        import-order bugs in the factory.
      * Smoke after upgrade: does the install factory still wire the
        expected default consumers?
    """
    from nexus.hook_registry import HookRegistry, install_default_hooks  # noqa: PLC0415 — circular-dep avoidance (nexus.hook_registry)

    registry = HookRegistry()
    install_default_hooks(registry)

    chains: list[tuple[str, list]] = [
        ("Single-doc chain (RDR-070)", registry._single),
        ("Batch chain (RDR-095)", registry._batch),
        ("Document-grain chain (RDR-089)", registry._document),
    ]
    total = 0
    for label, hooks in chains:
        click.echo(f"\n{label}:")
        if not hooks:
            click.echo("  (none)")
            continue
        for hook in hooks:
            name = getattr(hook, "__name__", repr(hook))
            module = getattr(hook, "__module__", "?")
            click.echo(f"  - {name}  [{module}]")
            total += 1

    click.echo(f"\nTotal: {total} hook(s) registered across 3 chains.")


# ── --check-storage-boundary (RDR-120 P0.A / nexus-7xxxg) ────────────────────


def _run_check_storage_boundary(
    fail_on_violation: bool, phase: str | None
) -> None:
    """Run the storage-boundary lint and emit a structlog metric.

    Records the catalog-allowlist count to T2 at key
    ``120-phase-<phase>-catalog-allowlist-count`` when ``phase`` is
    set. The phase-boundary forcing function in RDR-120 §Approach
    reads this on each subsequent phase and asserts monotonic
    non-increase across phases.
    """
    import sys as _sys  # noqa: PLC0415 — deferred to keep CLI startup fast
    from pathlib import Path as _Path  # noqa: PLC0415 — deferred to keep CLI startup fast

    from nexus.storage_boundary_lint import scan_repo  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

    log = structlog.get_logger(__name__)

    # Discover repo root by walking up from CWD looking for .git/.
    # The installed `nx` lives in a uv-cache prefix; the lint targets
    # the working tree the user is operating against, which is the
    # current working directory's enclosing repo.
    cwd = _Path.cwd().resolve()
    repo_root: _Path | None = None
    for parent in (cwd, *cwd.parents):
        if (parent / ".git").exists():
            repo_root = parent
            break
    if repo_root is None:
        click.echo(
            f"storage-boundary lint: could not locate a git repo rooted at "
            f"or above {cwd}. Run from inside the nexus checkout.",
            err=True,
        )
        _sys.exit(2)

    result = scan_repo(repo_root=repo_root)

    from nexus.storage_boundary_lint import CATALOG_CONSTRUCTION_BASELINE  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

    catalog_over_baseline = (
        result.catalog_constructions > CATALOG_CONSTRUCTION_BASELINE
    )
    click.echo(
        f"Storage-boundary lint (RDR-120 P0.A / RDR-128 P0c / RDR-146 P0.1):\n"
        f"  violations:                {result.total_violations}\n"
        f"  catalog-allowlist count:   {result.catalog_allowlist_count}\n"
        f"  sqlite allowlisted connects: {result.sqlite_allowlisted_connects}\n"
        f"  T2Database constructions:  {result.t2database_constructions}\n"
        f"  catalog constructions:     {result.catalog_constructions}"
        f" (RDR-146 baseline {CATALOG_CONSTRUCTION_BASELINE},"
        f" cutover surface)"
    )

    if result.violations:
        click.echo("\nViolations:")
        for v in result.violations:
            click.echo(f"  {v.file}:{v.line}  {v.symbol}")

    log.info(
        "storage_boundary_lint",
        violations=result.total_violations,
        catalog_allowlist_count=result.catalog_allowlist_count,
        sqlite_allowlisted_connects=result.sqlite_allowlisted_connects,
        t2database_constructions=result.t2database_constructions,
        catalog_constructions=result.catalog_constructions,
        catalog_construction_baseline=CATALOG_CONSTRUCTION_BASELINE,
        phase=phase or "unset",
    )

    if catalog_over_baseline:
        click.echo(
            f"\nRDR-146: catalog constructions ({result.catalog_constructions}) "
            f"exceed the baseline ({CATALOG_CONSTRUCTION_BASELINE}). A new direct "
            f"Catalog(...) site was added in consumer code — route catalog writes "
            f"through the catalog factory (make_catalog_writer / "
            f"HttpCatalogClient) instead.",
            err=True,
        )

    if phase:
        try:
            # RDR-128 P3 (nexus-sbxbe.3): route the phase-metric write
            # through the daemon so `nx doctor` does not open memory.db
            # directly. memory.put is a routable store op.
            from nexus.mcp_infra import t2_index_write  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

            t2_index_write(
                lambda db: db.memory.put(
                    project="nexus_rdr",
                    title=f"120-phase-{phase}-catalog-allowlist-count",
                    content=str(result.catalog_allowlist_count),
                    tags="rdr-120,phase-metric,catalog-allowlist",
                    ttl=None,  # permanent
                )
            )
        except Exception as exc:  # noqa: BLE001 — telemetry metric write must not crash the lint check; logged
            log.warning(
                "storage_boundary_lint_metric_write_failed",
                error=str(exc),
                phase=phase,
            )

    if fail_on_violation and (result.violations or catalog_over_baseline):
        if result.violations:
            click.echo(
                f"\nFAIL: {result.total_violations} violation(s) found.",
                err=True,
            )
        if catalog_over_baseline:
            click.echo(
                f"FAIL: catalog constructions ({result.catalog_constructions}) "
                f"exceed the RDR-146 baseline ({CATALOG_CONSTRUCTION_BASELINE}).",
                err=True,
            )
        _sys.exit(1)


# ── --check-mineru (nexus-2fyb code-review R3-3) ────────────────────────────


def _run_check_mineru() -> None:
    """Verify MinerU is importable and the formula-aware extractor entry
    point is reachable.

    nexus-2fyb promoted ``mineru[all]`` from an optional extra to a default
    dependency. Before this check, a corrupt install (missing wheel,
    broken import chain) was silent until ``nx index pdf`` ran on a
    formula-bearing PDF. Surfacing it at doctor-time gives the user an
    actionable error before they try to use the feature.
    """
    try:
        from mineru.cli.common import do_parse  # noqa: PLC0415 — optional/heavy dependency deferred (mineru)
    except Exception as exc:  # noqa: BLE001 — boundary catch of optional MinerU import failure; surfaced via click.echo
        click.echo(_check("MinerU import", False, f"{type(exc).__name__}: {exc}"))
        click.echo(
            "  ↳ MinerU is required since nexus-2fyb. Reinstall with "
            f"`{_reinstall_command()}`."
        )
        return

    if do_parse is None:
        click.echo(_check("MinerU import", False, "do_parse is None"))
        click.echo(
            "  ↳ mineru.cli.common imported but do_parse is None — "
            "the import shim is broken. Reinstall conexus."
        )
        return

    click.echo(_check("MinerU import", True, "mineru.cli.common.do_parse OK"))

    # Optional: surface server-side state. The mineru-api server is opt-in;
    # not running is fine. Just report status.
    try:
        from nexus.config import get_mineru_server_url, mineru_server_provisioned  # noqa: PLC0415 — circular-dep avoidance (nexus.config)
        # nexus-9xfx5: never probe the built-in default URL on a box where
        # no server was ever provisioned — every fresh install rendered a
        # red ✗ ("unreachable ... OOM-risk") for a service init never set
        # up. Unprovisioned renders as the not-configured skip below; a ✗
        # now means a PROVISIONED server is actually unreachable.
        url = get_mineru_server_url() if mineru_server_provisioned() else None
    except Exception:  # noqa: BLE001 — best-effort config read; falls back to None
        url = None
    if url:
        try:
            import httpx  # noqa: PLC0415 — optional/heavy dependency deferred (httpx)
            with httpx.Client(timeout=2.0) as client:
                r = client.get(f"{url}/health")
            if r.status_code == 200:
                click.echo(_check("MinerU server", True, f"reachable at {url}"))
            else:
                click.echo(_check(
                    "MinerU server", False,
                    f"{url} returned HTTP {r.status_code}",
                ))
        except Exception as exc:  # noqa: BLE001 — boundary catch of health-probe failure; surfaced via click.echo
            click.echo(_check(
                "MinerU server", False,
                f"{url} unreachable: {type(exc).__name__}",
            ))
    else:
        click.echo("  (no mineru-api server configured; subprocess mode in use)")


# ── Supplementary checks: the cheap/read-only subset of the opt-in ─────────
# ── --check-* diagnostics, promoted into the default `nx doctor` sweep ─────
#
# ``nx doctor`` (no mode flag) runs ``nexus.health.run_health_checks()``
# only -- none of the fourteen ``--check-*`` diagnostics below participate,
# so a real backlog (this week: 303 aspect-queue claim failures) is
# invisible unless an operator happens to run the exact opt-in flag for it.
# health.py cannot be edited to close this gap (RDR-wide gate on this
# module), so the qualifying subset runs HERE, inline, right after the
# default sweep's own output.
#
# Inclusion criteria (ALL must hold):
#   * read-only -- no mutation, no destructive side effect
#   * no network beyond the already-configured engine (no third-party
#     service, no arbitrary-repo scan)
#   * sub-second-ish in the common case
#   * a FAILURE state means something concrete on an otherwise-healthy
#     install -- not "always exits 0", not "no pass/fail state at all"
#
#   flag                      | included? | why
#   --------------------------+-----------+----------------------------------
#   --check-schema             | NO        | ALREADY asked by the default
#                                          | sweep itself -- health.py's
#                                          | ``_check_t2_schema_applied``
#                                          | shares the identical
#                                          | ``probe_t2_schema_fingerprint``
#                                          | call (see ``_run_check_schema``'s
#                                          | own docstring). Promoting it
#                                          | would duplicate, not add, signal.
#   --check-search              | NO        | cost scales with the number of
#                                          | registered collections and name
#                                          | canaries (multiple resolve +
#                                          | search_cross_corpus calls each);
#                                          | not bounded sub-second.
#   --check-resources           | YES       | local POSIX semaphore probe +
#                                          | one ``ps`` call, zero network,
#                                          | sub-second; Errno 28 exhaustion
#                                          | is a real, otherwise-invisible
#                                          | failure with no default-sweep
#                                          | equivalent.
#   --check-quotas              | NO        | mostly a static limits dump;
#                                          | its one live signal (T3
#                                          | reachability) already duplicates
#                                          | the default sweep's own T3
#                                          | checks, and the verbose report
#                                          | is meant for on-demand reading.
#   --check-taxonomy            | YES       | one HTTP call to the
#                                          | already-configured engine
#                                          | (``HttpTaxonomyStore
#                                          | .get_link_drift``); a real
#                                          | invariant-drift signal with no
#                                          | default-sweep equivalent.
#   --check-plan-library        | YES       | one HTTP call, bounded page
#                                          | size; the global-tier builtin
#                                          | floor is a real, otherwise-
#                                          | invisible failure.
#   --check-mcp-logs            | NO        | cost scales with log volume /
#                                          | window (up to ~60MB across
#                                          | rotated files); a deliberately-
#                                          | scoped troubleshooting tool, not
#                                          | a routine signal.
#   --check-tier-discipline     | NO        | never fails by design (its own
#                                          | docstring: "does NOT exit
#                                          | non-zero... visibility, not
#                                          | enforcement") and is scoped to
#                                          | the invoking Claude session, not
#                                          | install health.
#   --check-storage-boundary    | NO        | O(repo) AST scan of the whole
#                                          | checkout (the project's own
#                                          | "out of hot loop" lint bucket);
#                                          | assumes a git-repo cwd and exits
#                                          | 2 outright otherwise -- not
#                                          | applicable to a generic
#                                          | installed ``nx``.
#   --check-post-store-hooks    | NO        | purely descriptive listing, no
#                                          | pass/fail state at all.
#   --check-mineru               | NO        | imports the heavy mineru[all]
#                                          | dependency tree -- far from
#                                          | sub-second.
#   --check-aspect-queue         | YES       | THE MOTIVATING CASE: one/two
#                                          | HTTP calls, real backlog signal
#                                          | nothing in the default sweep
#                                          | watches.
#   --check-t1                   | YES       | local lease-file read, zero
#                                          | network; complements (does not
#                                          | duplicate) the default sweep's
#                                          | orphan-lease SWEEP -- that one
#                                          | reaps expired leases across ALL
#                                          | sessions, this one reports
#                                          | freshness for THIS session.
#   --check-wal-retention         | NO        | explicitly "Always exit 0:
#                                          | this is informational" by its
#                                          | own docstring -- no failure
#                                          | state to surface.
#
# Non-gating by design: a supplementary check's failure is printed, never
# folded into the default sweep's exit code. Two of the five (schema-
# adjacent reasoning aside) checks distinguish "service unreachable /
# unknown" from "definite failure" with DIFFERENT exit codes that mean
# different things per-check (``--check-resources``'s Exit(2) is a real
# resource-exhaustion failure; ``--check-plan-library``'s Exit(2) is an
# explicit "not reporting pass or fail") -- there is no single correct
# mapping from "supplementary check raised" to "the sweep should now fail".
# Promoting these into the exit-code gate is a deliberate, separately-
# reviewed decision, not a side effect of adding visibility.
#: Names of the promoted checks, in run order. The actual callables are
#: resolved lazily inside :func:`_run_supplementary_checks` (module-
#: level binding is not possible here: ``_run_check_resources`` /
#: ``_run_check_taxonomy`` / ``_run_check_t1`` are defined further down
#: this file, after ``doctor_cmd``).
_SUPPLEMENTARY_CHECK_NAMES: tuple[str, ...] = (
    "resources", "plan-library", "taxonomy", "aspect-queue", "t1",
)

#: The remaining opt-in-only flags -- named in the summary line at the end
#: of the supplementary section so the operator knows what a default
#: `nx doctor` run does NOT cover. Kept as a literal tuple (not derived
#: from ``_SUPPLEMENTARY_CHECKS``) so the classification table above stays
#: the one place a reviewer needs to update when a flag's disposition
#: changes.
_OPT_IN_ONLY_CHECKS: tuple[str, ...] = (
    "--check-schema", "--check-search", "--check-quotas",
    "--check-mcp-logs", "--check-tier-discipline",
    "--check-storage-boundary", "--check-post-store-hooks",
    "--check-mineru", "--check-wal-retention",
)


def _run_supplementary_checks() -> None:
    """Run the cheap/read-only opt-in checks promoted into the default
    sweep (see the classification table above). Purely additive output --
    never affects the caller's exit code (see the non-gating note above).

    Each check is isolated: a check's ``click.exceptions.Exit`` or any
    other exception is caught and reported inline rather than aborting
    the remaining checks or the doctor process.
    """
    # Resolved at call time (see _SUPPLEMENTARY_CHECK_NAMES's docstring) --
    # every check function below is a module-level name defined by the
    # time this function is ever invoked (doctor_cmd only calls it at CLI
    # dispatch time, long after module import completes).
    runners: dict[str, Any] = {
        "resources": _run_check_resources,
        "plan-library": _run_check_plan_library,
        "taxonomy": _run_check_taxonomy,
        "aspect-queue": _run_check_aspect_queue,
        "t1": _run_check_t1,
    }
    click.echo(
        "\nSupplementary checks (cheap/read-only subset of the opt-in "
        "--check-* diagnostics):"
    )
    for name in _SUPPLEMENTARY_CHECK_NAMES:
        runner = runners[name]
        click.echo(f"\n--- {name} ---")
        try:
            runner()
        except click.exceptions.Exit:
            # The check already printed its own failure/status detail;
            # nothing further to say here (see non-gating note above).
            pass
        except Exception as exc:  # noqa: BLE001 — isolate one check's crash from the rest of the sweep
            click.echo(f"  [!] {name} check raised unexpectedly: {exc}", err=True)
            _log.warning(
                "doctor_supplementary_check_failed", check=name, error=str(exc)
            )
    click.echo(
        "\nRemaining opt-in-only checks (not run above; invoke explicitly): "
        + ", ".join(_OPT_IN_ONLY_CHECKS)
    )


@click.command("doctor")
@click.option(
    "--clean-checkpoints",
    is_flag=True,
    default=False,
    help="Delete orphaned PDF checkpoint files (where the source PDF no longer exists).",
)
@click.option(
    "--clean-pipelines",
    is_flag=True,
    default=False,
    help="Delete orphaned PDF pipeline buffer entries (stale or missing source PDF).",
)
@click.option(
    "--fix",
    is_flag=True,
    default=False,
    help="Apply HNSW ef tuning to all local collections (local mode only).",
)
@click.option(
    "--fix-paths",
    is_flag=True,
    default=False,
    help="Migrate absolute file_path entries to relative paths (catalog + T3).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Report affected entries without writing changes "
         "(use with --fix-paths or --trim-telemetry).",
)
@click.option(
    "--check-schema",
    is_flag=True,
    default=False,
    help="Validate the T2 schema is applied (Postgres, Liquibase-managed, "
         "via the engine's GET /version changelog fingerprint). Exits 2 "
         "when the engine is unreachable, 1 on schema_error or zero "
         "applied changesets; an honest N/A (endpoint withholds the "
         "fingerprint by design) exits 0 unless combined with "
         "--fail-on-violation. nexus-vl8lk, nexus-b1v9z.",
)
@click.option(
    "--check-search",
    "check_search",
    is_flag=True,
    default=False,
    help="Run probe 3a — the name-resolution canary from "
         "tests/fixtures/name_canaries.py. Exits 2 when any surface "
         "raises an unexpected exception. RDR-087 Phase 3.2.",
)
@click.option(
    "--check-resources",
    "check_resources",
    is_flag=True,
    default=False,
    help="Probe POSIX semaphore headroom and report orphan "
         "multiprocessing-tracker pressure. Exits 2 with 'Errno 28' "
         "when the namespace is exhausted (known sources: MinerU "
         "workers / orphan chroma children leaking via multiprocessing "
         "/ trackers re-parented to init after ungraceful MCP "
         "shutdowns). Beads nexus-dc57 + nexus-ze2a + nexus-9h1s.",
)
@click.option(
    "--check-quotas",
    "check_quotas",
    is_flag=True,
    default=False,
    help="Report vector-store limits (chunking/paging caps + the legacy "
         "Chroma-era reference table), embedder model caps, reranker "
         "substrate, and any transient-error retries observed this "
         "process. Exits 1 when T3 is unreachable in cloud mode "
         "(nexus-c590; relabeled for the pgvector substrate, nexus-d01js).",
)
@click.option(
    "--check-taxonomy",
    "check_taxonomy",
    is_flag=True,
    default=False,
    help="Verify the topic_links ≡ projection-assignment invariant "
         "(GH #252). Exits 1 on drift.",
)
@click.option(
    "--check-plan-library",
    "check_plan_library",
    is_flag=True,
    default=False,
    help="Report plan-library dimensional health: authored vs "
         "backfilled vs non-dimensional row counts, plus global-tier "
         "builtin count. Exits 1 when builtin count < 9. RDR-092 "
         "Phase 0c.2.",
)
@click.option(
    "--check-mcp-logs",
    "check_mcp_logs",
    is_flag=True,
    default=False,
    help="Scan Claude Code's per-server MCP cache for nx-mcp "
         "silent-death signatures ('STDIO connection dropped', "
         "'stdio transport error'). macOS only; skips cleanly on "
         "Linux/Windows. RDR-094 Phase H (nexus-50u5).",
)
@click.option(
    "--check-tier-discipline",
    "check_tier_discipline",
    is_flag=True,
    default=False,
    help="Audit tier-write activity for the current session: prints "
         "the tier-write summary from the tier_writes table and "
         "warns when a substantive session has no write-back. "
         "Phase 1B nexus-a52i.",
)
@click.option(
    "--check-storage-boundary",
    "check_storage_boundary",
    is_flag=True,
    default=False,
    help="RDR-120 P0.A. AST-scan for direct sqlite3.connect / "
         "voyageai.Client calls and T2Database/T3Database "
         "constructions outside the named allowlists in "
         "storage_boundary_lint.py. The per-line epsilon-allow "
         "escape token is RETIRED (RDR-186 P4): surviving sites are "
         "enumerated per file with exact counts; a new site is a "
         "hard violation, not a comment to write. Records "
         "catalog-allowlist count to T2 key 120-phase-<N>-catalog-"
         "allowlist-count for the phase-boundary forcing function. "
         "Exits 1 with --fail-on-violation when violations exist.",
)
@click.option(
    "--fail-on-violation",
    "fail_on_violation",
    is_flag=True,
    default=False,
    help="With --check-storage-boundary, exit 1 if any violation is "
         "found (informational without this flag). With --check-schema, "
         "treat an honest N/A (fingerprint withheld by design) as a "
         "failure too -- for release-gate callers that need an actual "
         "OK, not an unprovable N/A that reads identically to a pass "
         "(nexus-b1v9z).",
)
@click.option(
    "--phase",
    "phase",
    type=str,
    default=None,
    help="With --check-storage-boundary, the RDR-120 phase identifier "
         "for the T2 metric key (e.g. `0`, `1`, `2`, `3a`, `3b`, "
         "`4`, `5`). Used to record `120-phase-<phase>-catalog-"
         "allowlist-count`. Omit to skip the metric write.",
)
@click.option(
    "--mcp-log-hours",
    "mcp_log_hours",
    default=24,
    type=click.IntRange(min=1),
    show_default=True,
    help="Lookback window in hours for --check-mcp-logs.",
)
@click.option(
    "--json",
    "json_out",
    is_flag=True,
    default=False,
    help="Emit machine-parseable JSON. Works with the main sweep (no mode "
         "flag) plus --check-search, --check-quotas, --check-mcp-logs. "
         "Any other mode flag combined with --json is a usage error "
         "(nexus-0vycz).",
)
@click.option(
    "--trim-telemetry",
    "trim_telemetry",
    is_flag=True,
    default=False,
    help="Delete search_telemetry rows older than --days (default 30) to "
         "cap T2 disk use. RDR-087 Phase 2.4. Combine with --dry-run to "
         "preview the count without deleting.",
)
@click.option(
    "--check-post-store-hooks",
    "check_post_store_hooks",
    is_flag=True,
    default=False,
    help="Enumerate post-store hooks registered on each of the three "
         "chains (single-doc / batch / document-grain). nexus-b0ka.",
)
@click.option(
    "--check-mineru",
    "check_mineru",
    is_flag=True,
    default=False,
    help="Verify MinerU is importable. nexus-2fyb promoted mineru[all] "
         "from optional extra to default dep; this surfaces a corrupt "
         "install at doctor-time instead of waiting for the first "
         "math-PDF index to fail.",
)
@click.option(
    "--check-aspect-queue",
    "check_aspect_queue",
    is_flag=True,
    default=False,
    help="Report aspect_extraction_queue depth, per-status counts, "
         "oldest pending row, and any failed rows with their last "
         "error. nexus-1pfq.",
)
@click.option(
    "--check-t1",
    "check_t1",
    is_flag=True,
    default=False,
    help="Diagnose T1 session lease presence + freshness. Resolves the "
         "active session-id and checks its lease at "
         "~/.config/nexus/t1_session_lease.<session_id>. Exits 1 only "
         "when a session-id resolves AND a lease file exists AND it is "
         "expired/corrupt; a resolved session with no lease file at all "
         "is informational (a bare CLI legitimately has none).",
)
@click.option(
    "--check-wal-retention",
    "check_wal_retention",
    is_flag=True,
    default=False,
    help="Sample retained WAL bytes (local service only) via "
         "pg_ls_waldir(), escalating a nexus_svc session to pg_monitor "
         "with SET ROLE first — unconditionally, since nexus_svc is "
         "NOINHERIT in every deployment posture (cloud measured, local "
         "provisioning aligned nexus-v80f2), so pg_monitor's privileges "
         "are never ambient without it. Reports UNMEASURED, never a "
         "false clean, when no local nexus_svc credentials exist "
         "(managed/BYO deployment) or the escalation is refused "
         "(grants-004 not applied). Always exit 0 — informational. "
         "nexus-bb5c8.",
)
@click.option(
    "--days",
    "days",
    default=30,
    type=click.IntRange(min=1),
    show_default=True,
    help="Retention window for --trim-telemetry (days; minimum 1).",
)
@click.option(
    "--git-hooks-scope",
    "git_hooks_scope",
    default=None,
    type=click.Path(),
    help="Restrict the git-hooks stanza-drift check (part of the default "
         "sweep) to repos registered at or under this root; repos "
         "elsewhere are excluded from the walk instead of being reported. "
         "The registered-repo catalog is shared machine-wide, not scoped "
         "to $HOME, so a bare sweep run from an isolated automation "
         "sandbox otherwise also sees (and can be reddened by) every "
         "other repo ever indexed on the same machine. Default: unscoped, "
         "walks every registered repo. nexus-jds59.",
)
def doctor_cmd(clean_checkpoints: bool, clean_pipelines: bool, fix: bool,
               fix_paths: bool, dry_run: bool, check_schema: bool,
               check_search: bool, check_resources: bool,
               check_quotas: bool, check_taxonomy: bool,
               check_plan_library: bool,
               check_mcp_logs: bool, mcp_log_hours: int,
               check_mineru: bool,
               json_out: bool,
               trim_telemetry: bool, days: int,
               check_post_store_hooks: bool,
               check_aspect_queue: bool,
               check_t1: bool,
               check_wal_retention: bool,
               check_tier_discipline: bool,
               check_storage_boundary: bool,
               fail_on_violation: bool,
               phase: str | None,
               git_hooks_scope: str | None) -> None:
    """Verify that all required services and credentials are available."""
    if json_out:
        # nexus-0vycz: --json is honored by the main sweep (no mode flag)
        # plus --check-search / --check-quotas / --check-mcp-logs. Every
        # other mode silently ignored the flag before this fix -- fail
        # loud instead of pretending the combination was handled.
        _json_unsupported_modes = {
            "--check-schema": check_schema,
            "--check-resources": check_resources,
            "--check-taxonomy": check_taxonomy,
            "--check-plan-library": check_plan_library,
            "--check-tier-discipline": check_tier_discipline,
            "--check-storage-boundary": check_storage_boundary,
            "--trim-telemetry": trim_telemetry,
            "--check-post-store-hooks": check_post_store_hooks,
            "--check-mineru": check_mineru,
            "--check-aspect-queue": check_aspect_queue,
            "--check-t1": check_t1,
            "--check-wal-retention": check_wal_retention,
            "--fix": fix,
            "--fix-paths": fix_paths,
            "--clean-checkpoints": clean_checkpoints,
            "--clean-pipelines": clean_pipelines,
        }
        _requested_unsupported = [
            flag for flag, requested in _json_unsupported_modes.items() if requested
        ]
        if _requested_unsupported:
            raise click.UsageError(
                "--json is not supported with "
                f"{', '.join(_requested_unsupported)}. "
                "--json only works with the main sweep (no mode flag), "
                "--check-search, --check-quotas, or --check-mcp-logs."
            )

    if check_storage_boundary:
        _run_check_storage_boundary(
            fail_on_violation=fail_on_violation, phase=phase
        )
        return

    if check_schema:
        _run_check_schema(strict=fail_on_violation)
        return

    if check_search:
        from nexus.doctor_search import run_check_search  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
        run_check_search(json_out=json_out)
        return

    if check_resources:
        _run_check_resources()
        return

    if check_quotas:
        _run_check_quotas(json_out=json_out)
        return

    if check_mcp_logs:
        _run_check_mcp_logs(json_out=json_out, hours=mcp_log_hours)
        return

    if check_taxonomy:
        _run_check_taxonomy()
        return

    if check_plan_library:
        _run_check_plan_library()
        return

    if trim_telemetry:
        _run_trim_telemetry(days=days, dry_run=dry_run)
        return

    if check_post_store_hooks:
        _run_check_post_store_hooks()
        return

    if check_mineru:
        _run_check_mineru()
        return

    if check_aspect_queue:
        _run_check_aspect_queue()
        return

    if check_t1:
        _run_check_t1()
        return

    if check_wal_retention:
        _run_check_wal_retention()
        return

    if check_tier_discipline:
        _run_check_tier_discipline()
        return

    if fix:
        from nexus.config import is_local_mode  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
        from nexus.db import make_t3  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
        from nexus.db.t3 import apply_hnsw_ef  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
        if not is_local_mode():
            click.echo("SPANN defaults adequate — no HNSW tuning needed (cloud mode)")
            return
        # RDR-155 P4a.2 (nexus-1k8s1): make_t3() returns the service-backed
        # handle in production; apply_hnsw_ef no-ops on it (the chroma
        # hnsw:search_ef knob retired with the serving path — pgvector
        # tunes HNSW server-side). The tuning still applies for injected
        # chroma-backed handles (tests, the P5 ETL wrapper).
        try:
            db = make_t3()
        except Exception as exc:
            raise click.ClickException(
                f"T3 handle unavailable for HNSW tuning: {exc}"
            ) from exc
        count = apply_hnsw_ef(db)
        click.echo(f"Updated HNSW search_ef on {count} collection(s).")
        return

    if clean_checkpoints:
        from nexus.checkpoint import scan_orphaned_checkpoints  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
        deleted = scan_orphaned_checkpoints(delete=True)
        if deleted:
            click.echo(f"Deleted {len(deleted)} orphaned checkpoint(s).")
        else:
            click.echo("No orphaned checkpoints found.")
        return

    if clean_pipelines:
        from nexus.db.http_pipeline_client import HttpPipelineDB  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
        try:
            with HttpPipelineDB() as db:
                deleted = db.scan_orphaned_pipelines(delete=True)
        except Exception as exc:  # noqa: BLE001 — engine unreachable must not stack-trace a doctor verb
            raise click.ClickException(
                f"pipeline scan unavailable (engine unreachable?): {exc}"
            ) from exc
        if deleted:
            click.echo(f"Deleted {len(deleted)} orphaned pipeline entry/entries.")
        else:
            click.echo("No orphaned pipeline entries found.")
        return

    if fix_paths:
        from nexus.catalog.types import make_relative  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
        from nexus.catalog.factory import make_catalog_reader, make_catalog_writer  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
        from nexus.catalog.tumbler import Tumbler  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
        from nexus.db import make_t3  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

        # nexus-kmo9h: factory delegation — None only in SQLite opt-out mode
        # when uninitialised; service mode always proceeds (the old local
        # gate printed a false "run: nx catalog setup" on healthy boxes).
        reader = make_catalog_reader()
        if reader is None:
            click.echo("Catalog not initialized — run: nx catalog setup")
            return
        writer = make_catalog_writer()

        # Find all entries with absolute file_path (nexus-xnz0o: uses
        # docs_with_absolute_paths() which is uniform across SQLite and service mode).
        doc_rows = reader.docs_with_absolute_paths()
        rows = [(d["tumbler"], d["file_path"], d["physical_collection"]) for d in doc_rows]

        if not rows:
            click.echo("No absolute file_path entries found.")
            return

        click.echo(f"Found {len(rows)} entries with absolute paths.")

        # Load owners for repo_root lookup via uniform catalog API
        # (nexus-xnz0o: list_owners() works on both SQLite and service mode).
        owner_list = reader.list_owners()
        # Build a lookup dict keyed by tumbler_prefix, storing owner_type + repo_root.
        owners = {o["tumbler_prefix"]: o for o in owner_list}
        if not owners:
            click.echo(
                "Warning: no owners registered — run 'nx index repo "
                "<path>' to populate catalog owners before fix-paths."
            )
            return

        # RDR-137 Phase 3.7 (nexus-tts0d.12): registry fallback removed.
        # Post-nexus-nzyrh owner.repo_root is always populated for
        # freshly-registered owners; legacy owners with empty
        # repo_root surface via the WARN below for re-index targeting.
        # nexus-bm8dd: no T3 handle here any more. fix-paths repairs a
        # document's path, which lives entirely on the catalog row; the T3
        # source_path it also tried to rewrite has not existed since RDR-102 D2.
        fixed = 0
        for tumbler_str, file_path, physical_collection in rows:
            tumbler = Tumbler.parse(tumbler_str)
            owner_prefix = str(tumbler.owner_address())
            owner_rec = owners.get(owner_prefix)

            if not owner_rec:
                continue
            if owner_rec.get("owner_type") == "curator":
                continue

            # Determine repo_root (catalog-only; no registry fallback)
            repo_root = Path(owner_rec.get("repo_root") or "") if owner_rec.get("repo_root") else None

            if repo_root is None:
                _log.warning(
                    "fix_paths_no_root",
                    tumbler=tumbler_str, file_path=file_path,
                    hint="re-run 'nx index repo' on the source repo to backfill owners.repo_root",
                )
                continue

            new_rel = make_relative(file_path, repo_root)
            if new_rel == file_path:
                # Not under repo_root — skip
                _log.warning("fix_paths_not_under_root", tumbler=tumbler_str,
                             file_path=file_path, repo_root=str(repo_root))
                continue

            if dry_run:
                click.echo(f"  [dry-run] {tumbler_str}: {file_path} -> {new_rel}")
            else:
                # nexus-bm8dd: the T3 leg is GONE. This used to call
                # t3_db.update_source_path(...) first and report its count as
                # "(n chunks)". Chunk metadata has carried no source_path since
                # RDR-102 D2 removed it from the schema, so that call rewrote
                # nothing and n was always 0 — the message told the operator a
                # repair had a chunk-level component it never had. A document's
                # path lives on its CATALOG row, and the line below is the whole
                # fix.
                writer.update(tumbler, file_path=new_rel)
                click.echo(f"  fixed: {tumbler_str}: {file_path} -> {new_rel}")

            fixed += 1

        writer.close()
        if reader is not None:
            reader.close()

        if dry_run:
            click.echo(f"\n{fixed} entries would be fixed. Use --fix-paths without --dry-run to apply.")
        else:
            click.echo(f"\nFixed {fixed} entries.")
        return

    # ── Health check path — delegates to nexus.health ─────────────────────────
    from nexus.health import run_health_checks, format_health_for_cli, format_health_for_json  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

    results, is_local = run_health_checks(git_hooks_scope=git_hooks_scope)
    output, failed = format_health_for_cli(results, local_mode=is_local)
    if json_out:
        # nexus-0vycz: machine-parseable JSON on stdout only -- no human
        # prose mixed in, so the whole of stdout is one parseable document.
        click.echo(format_health_for_json(results, local_mode=is_local))
    else:
        click.echo(output)
        _run_supplementary_checks()

    # RDR-185 P4.2 (nexus-n7u38.29): the nexus-0rwwv bridge notice is RETIRED
    # here. A pending Chroma→pgvector cutover is reported by the ladder's own
    # read-only surface — health's `_check_pending_rungs` renders the
    # substrate rung's detect() ("Upgrade ladder: N pending rung(s) …") with
    # `nx upgrade` as the remedy, strictly superseding the bridge's coarse
    # count. Two lines for one state, with two different remedies (one of
    # them a verb P4.1 demoted out of --help), is the scattered-remediation
    # Gap-2 this RDR closes — and the bridge's ad-hoc re-sample is the third
    # DATA-rung mechanism the Gap-4 criterion bans.

    # nexus-be6x8: the exit code says what the glyphs say. 0 = healthy or
    # soft warnings only; 1 = at least one hard ✗; 2 = fatal (nothing will
    # start). `failed` (fatal-only) is still returned by format_health_for_cli
    # for its own footer; the code below is the whole exit contract.
    from nexus.health import health_exit_code  # noqa: PLC0415 — deferred local import, same reason as above

    code = health_exit_code(results)
    if code:
        raise click.exceptions.Exit(code)


def _probe_semaphore_namespace() -> tuple[bool, str]:
    """Probe POSIX named-semaphore availability.

    Attempts to allocate and immediately unlink one throwaway named
    semaphore. Returns ``(True, info_msg)`` when the kernel namespace
    has headroom; ``(False, error_repr)`` when allocation fails —
    typically ``[Errno 28] No such space left on device`` under
    exhaustion (beads nexus-dc57 + nexus-ze2a).

    Separated from the CLI handler so tests can monkeypatch it.
    """
    import os as _os  # noqa: PLC0415 — deferred to keep CLI startup fast
    try:
        from _multiprocessing import SemLock  # type: ignore[attr-defined]  # noqa: PLC0415 — deferred to keep CLI startup fast
    except ImportError:
        return True, "SemLock probe unavailable on this platform"
    probe_name = f"/nx-doctor-probe-{_os.getpid()}"
    try:
        lock = SemLock(0, 0, 1, name=probe_name, unlink=True)
        # SemLock ctor created and owns the semaphore; unlink happens
        # via the ``unlink=True`` flag on close.
        del lock
        return True, "POSIX named-semaphore namespace has headroom"
    except OSError as exc:
        return False, f"{exc!r}"


def _count_orphan_trackers() -> int | None:
    """Return the number of PPID=1 multiprocessing tracker orphans
    visible to this user, or ``None`` if the count cannot be
    obtained. Pure read; no side effects.

    Bead nexus-9h1s. Each orphan tracker holds POSIX semaphores
    until killed; the namespace is bounded
    (``kern.posix.sem.max=10000`` on macOS). A high count predicts
    imminent SemLock failure even when the live probe still passes.
    """
    try:
        import subprocess  # noqa: PLC0415 — deferred to keep CLI startup fast

        from nexus.session import _parse_orphan_tracker_candidates  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

        ps_output = subprocess.check_output(
            ["ps", "-eo", "pid,ppid,etime,command"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return len(_parse_orphan_tracker_candidates(ps_output))
    except Exception:  # noqa: BLE001 — best-effort helper; returns None on any failure
        return None


def _run_check_resources() -> None:
    """Emit a resource-pressure report to stdout; exit 2 on failure.

    Two signals:

    * ``_probe_semaphore_namespace`` -- direct SemLock allocation;
      fails with Errno 28 when the namespace is exhausted.
    * Orphan multiprocessing tracker count via
      :func:`_count_orphan_trackers`. Warns above 100 (advisory)
      and 1000 (urgent). Bead nexus-9h1s.
    """
    ok, msg = _probe_semaphore_namespace()
    orphan_count = _count_orphan_trackers()
    if ok:
        click.echo(f"[\u2713] resources: {msg}")
        if orphan_count is None:
            return
        if orphan_count >= 1000:
            click.echo(
                f"[!] orphan multiprocessing trackers: {orphan_count} "
                f"(URGENT - reap soon to avoid Errno 28; each leaks "
                f"POSIX semaphores until killed)",
                err=True,
            )
            click.echo(
                "    Reap inline: python -c 'from nexus.session import "
                "sweep_orphan_resource_trackers; "
                "print(sweep_orphan_resource_trackers())'\n"
                "    Or: ps -eo pid,ppid,command | "
                "awk '$2==1 && /multiprocessing/ {print $1}' | "
                "xargs kill -TERM",
                err=True,
            )
        elif orphan_count >= 100:
            click.echo(
                f"[!] orphan multiprocessing trackers: {orphan_count} "
                f"(advisory - accumulating)"
            )
        else:
            click.echo(
                f"[\u2713] orphan multiprocessing trackers: {orphan_count}"
            )
        return
    click.echo(f"[\u2717] resources: SemLock probe FAILED — {msg}", err=True)
    if orphan_count is not None:
        click.echo(
            f"    orphan multiprocessing trackers: {orphan_count}",
            err=True,
        )
    click.echo(
        "Known sources of POSIX semaphore exhaustion on this project:\n"
        "  - nexus-ze2a: MinerU workers leak semaphores.\n"
        "    Workaround: `nx mineru stop` (kills the whole process group).\n"
        "  - nexus-dc57: orphan chroma children from earlier nexus sessions.\n"
        "    Workaround: kill orphan chromas (`ps aux | grep 'chroma run'`).\n"
        "  - nexus-9h1s: multiprocessing.resource_tracker subprocesses\n"
        "    re-parented to init (PPID=1) after ungraceful MCP shutdowns.\n"
        "    Reap with: python -c 'from nexus.session import "
        "sweep_orphan_resource_trackers; "
        "print(sweep_orphan_resource_trackers())'\n"
        "If the count does not recover, reboot — macOS does not unlink\n"
        "leaked named semaphores until the next boot.",
        err=True,
    )
    raise click.exceptions.Exit(2)


# ── --check-t1 (RDR-105 P5 / nexus-ssdg) ─────────────────────────────────────


def _run_check_t1() -> None:
    """Diagnostic: T1 session lease presence + freshness.

    Ported (nexus-8zfwv, 2026-08-07) off the RDR-149 P4 ``t1_addr.*``
    ``ServiceRegistry`` lease -- ``T1LeasePublisher``, the only thing that
    ever published that format, is retired (deleted at ff744321); every
    reader of it was permanently stale. The live cross-process "session has
    a live T1 scope" signal is now the lease file
    ``nexus.db.t1.publish_t1_session_lease`` writes: a small JSON object
    ``{token, expires_at}`` at ``~/.config/nexus/t1_session_lease.<session_id>``,
    written by the MCP lifespan on mint + on every periodic refresh, and
    cleared on clean teardown. There is no host:port to TCP-probe any more
    -- T1 is one shared nexus-service, not a per-session chroma.

    Four outcomes:

    * **No session-id resolves.** Neither ``NX_SESSION_ID`` nor
      ``~/.config/nexus/current_session`` is set. Informational, exit 0
      (T1 is PG-only, nexus-4lkmz -- there is no in-process opt-out to
      fall back to).
    * **Session resolves, no lease file.** A bare ``nx`` shell legitimately
      has no lease of its own -- the MCP lifespan mints one at session
      start; a bare CLI invocation uses its own dedicated CLI scope
      instead (see ``nexus.db.t1``'s CLI-dedicated-session path). This is
      NOT a failure. Informational, exit 0.
    * **Session resolves, lease present and fresh.** Healthy. Exit 0.
    * **Session resolves, lease present but expired/corrupt.** Stale
      litter from an ungraceful owner death -- only clean teardown
      (``clear_t1_session_lease``) removes this file; nothing else sweeps
      it (see ``nx doctor``'s orphan-T1-lease health check, which DOES
      reap these). Exit 1.
    """
    import json as _json  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
    import time  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

    from nexus.db.t1 import (  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
        _t1_session_lease_path,
        read_t1_session_lease,
    )
    from nexus.session import (  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
        _nexus_config_dir_at_import,
        resolve_active_session_id,
    )

    session_id = resolve_active_session_id()

    if not session_id:
        click.echo("[ ] T1: no session-id resolves for this process")
        click.echo(
            "    This is informational. ``nx scratch`` from this shell "
            "will fail-loud (T1 is PG-only; no in-process opt-out) unless "
            "the SessionStart hook has written "
            "~/.config/nexus/current_session, or the storage service is "
            "reachable for a bare-CLI mint (`nx daemon service start`)."
        )
        return

    config_dir = _nexus_config_dir_at_import()
    lease_path = _t1_session_lease_path(session_id, config_dir)

    if not lease_path.exists():
        click.echo(
            f"[ ] T1: session {session_id!r} resolves but no lease at "
            f"{lease_path}"
        )
        click.echo(
            "    Informational: a bare `nx` shell legitimately has no "
            "lease of its own. The MCP lifespan mints one at session "
            "start; a bare CLI invocation uses its own dedicated scope "
            "instead. NOTE: this state cannot distinguish 'no MCP session "
            "was ever started' from 'the MCP server crashed before its "
            "first mint' -- if a live MCP session SHOULD exist for this "
            "session id, treat this as a symptom: check `nx daemon "
            "service status` and reconnect the MCP server (/mcp)."
        )
        return

    token = read_t1_session_lease(session_id, config_dir)
    if token is not None:
        detail = ""
        try:
            data = _json.loads(lease_path.read_text(encoding="utf-8"))
            expires_at = float(data["expires_at"])
            detail = f" (expires in {int(expires_at - time.time())}s)"
        except (OSError, _json.JSONDecodeError, KeyError, TypeError, ValueError):
            pass
        click.echo(
            f"[✓] T1: lease {lease_path.name} for session {session_id!r} "
            f"is fresh{detail}"
        )
        return

    click.echo(
        f"[✗] T1: lease {lease_path.name} for session {session_id!r} "
        "exists but is expired or unreadable."
    )
    click.echo(
        "    Stale litter from an ungraceful owner death -- only clean "
        "teardown clears this file, so it can outlive its session. "
        f"Reap it via `nx doctor` (the orphan-T1-lease check reaps "
        f"expired leases automatically), or remove it directly: "
        f"rm {lease_path}"
    )
    raise click.exceptions.Exit(1)


def _run_check_wal_retention() -> None:
    """Diagnostic: retained WAL bytes via nexus_svc's pg_monitor escalation.

    nexus-bb5c8: grants-004-monitor-wal-visibility grants nexus_svc
    MEMBERSHIP in pg_monitor, but membership alone does not make
    pg_ls_waldir() callable under NOINHERIT -- and the CLOUD deployment's
    nexus_svc IS NOINHERIT (measured live, conexus relay [22485]); local
    provisioning currently keeps PostgreSQL's INHERIT default instead
    (divergence tracked separately as nexus-v80f2). See
    nexus.db.svc_monitor's module docstring for the full posture split --
    its SET-ROLE escalation is issued unconditionally and is correct
    either way. This is the first product consumer of that escalation --
    no other call site samples pg_ls_waldir() in-repo.

    Always exit 0: this is informational (RDR-191 Phase 4 trough-window
    context, not a pass/fail gate), and every degrade path
    (nexus.db.svc_monitor.wal_retention_report) already renders an
    explicit UNMEASURED marker rather than a false clean -- there is
    nothing here for a hard failure to add.
    """
    from nexus.db.svc_monitor import wal_retention_report  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

    report = wal_retention_report()
    if report.startswith("WAL retention: UNMEASURED"):
        click.echo(f"[ ] {report}")
    else:
        click.echo(f"[✓] {report}")


# ── --check-quotas (nexus-c590) ──────────────────────────────────────────────


def _collect_quota_report() -> dict:
    """Build the structured quota-headroom report (nexus-c590).

    Returns a dict with three sections: ``vector_store`` (per-request
    limits + T3 reachability), ``voyage`` (per-model token + dimension
    caps), and ``retry`` (cumulative backoff observed in this process
    so far via :func:`nexus.retry.get_retry_stats`).

    The ``vector_store`` section was keyed ``chromadb`` until 7.0.0. It kept
    the dependency's name "for machine-consumer stability until P4b renames
    it" — P4b being the wave that removed the dependency. Renamed there
    rather than later because ``cli-reference.md`` advertises this payload
    for "dashboards / CI gates", so it is a real contract, and a MAJOR is the
    only defensible moment to break one. Pinned by
    ``tests/test_doctor_cmd.py``'s exact-key-set assertion.

    Pure data-shape; both the human-readable and ``--json`` renderers
    consume this same dict so they never drift.

    *Why static*: live "requests/min" probing would require a running
    counter at every outgoing HTTP call; not shipped here. The retry
    counters give operators the most actionable signal — "backed off N
    times, slept Xs total" — without new plumbing.
    """
    from nexus.db.limits import QUOTAS  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
    from nexus.retry import get_retry_stats  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

    vector_store_limits = {
        "max_embedding_dimensions": QUOTAS.MAX_EMBEDDING_DIMENSIONS,
        "max_document_bytes": QUOTAS.MAX_DOCUMENT_BYTES,
        "safe_chunk_bytes": QUOTAS.SAFE_CHUNK_BYTES,
        "max_query_results": QUOTAS.MAX_QUERY_RESULTS,
        "max_query_string_chars": QUOTAS.MAX_QUERY_STRING_CHARS,
        "max_where_predicates": QUOTAS.MAX_WHERE_PREDICATES,
        "max_concurrent_reads": QUOTAS.MAX_CONCURRENT_READS,
        "max_concurrent_writes": QUOTAS.MAX_CONCURRENT_WRITES,
        "max_records_per_write": QUOTAS.MAX_RECORDS_PER_WRITE,
        "max_records_per_collection": QUOTAS.MAX_RECORDS_PER_COLLECTION,
        "max_collections_per_account": QUOTAS.MAX_COLLECTIONS_PER_ACCOUNT,
    }

    # T3 reachability probe: is the configured cloud tenant reachable
    # right now? A quota report is only actionable if the client can
    # actually connect.
    t3_reachable = False
    t3_detail = ""
    try:
        from nexus.config import is_local_mode  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps
        from nexus.db import make_t3  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

        if is_local_mode():
            t3_reachable = True
            t3_detail = "local mode (pgvector service) — limits table is reference-only"
        else:
            make_t3()
            t3_reachable = True
            t3_detail = "T3 backend reachable (pgvector service / managed endpoint)"
    except Exception as exc:  # noqa: BLE001 — best-effort T3 probe; failure surfaced in detail string
        t3_detail = f"unreachable: {type(exc).__name__}: {str(exc)[:80]}"

    # Embedder limits. In cloud mode the three Voyage models we use
    # have a fixed 1024-dim space and 32k-token cap; in local mode the
    # ONNX MiniLM (384-dim) or fastembed bge (768-dim) is active. RDR-109
    # Phase 2: report what's actually embedding, not what the canonical
    # cloud schema would suggest.
    if is_local_mode():
        from nexus.db.local_ef import (  # noqa: PLC0415 — circular-dep avoidance (nexus.db.local_ef)
            LocalEmbeddingFunction,
            local_model_token,
        )
        _ef = LocalEmbeddingFunction()
        voyage_limits = {
            "mode": "local",
            "models": {
                local_model_token(): {
                    "max_tokens": 512,
                    "embedding_dims": _ef.dimensions,
                },
            },
            "target_rpm": 0,
            "api_key_set": False,
        }
    else:
        voyage_limits = {
            "mode": "cloud",
            "models": {
                # nexus-8g79.22: voyage-3 is the LEGACY base model name;
                # Voyage AI retired it in early 2025. Kept here as a
                # detection label so doctor reports operators with
                # leftover voyage-3 configs see the retired tag rather
                # than a "healthy" line. New code paths use
                # voyage-code-3 / voyage-context-3 exclusively (see
                # corpus.py:effective_embedding_model_for_writes).
                "voyage-3": {
                    "max_tokens": 32_000,
                    "embedding_dims": 1024,
                    "status": "retired",
                },
                "voyage-code-3": {"max_tokens": 32_000, "embedding_dims": 1024},
                "voyage-context-3": {"max_tokens": 32_000, "embedding_dims": 1024},
            },
            "target_rpm": 250,  # matches ``doc_indexer._RATE_LIMIT_RPM``
            "api_key_set": False,
        }
    try:
        from nexus.config import get_credential  # noqa: PLC0415 — deferred local import — avoids import-time cost / circular deps

        voyage_limits["api_key_set"] = bool(get_credential("voyage_api_key"))
    except Exception:  # noqa: BLE001 — best-effort retry-stat read; falls back to defaults
        pass

    # Observed retry load — cumulative this process. Zero on fresh
    # sessions; non-zero after any `nx index` run that hit a transient
    # error.
    retry = dict(get_retry_stats())

    # RDR-188 (nexus-9o6y2.9): reranking runs SERVER-side — the engine scores
    # with Voyage rerank-2.5 (server key) or its ms-marco cross-encoder. The
    # client substrate check survives only for the salience consumer
    # (RDR-109 P4; disposition finalized in bead nexus-9o6y2.19).
    from nexus.cross_encoder import cross_encoder_available  # noqa: PLC0415 — circular-dep avoidance (nexus.cross_encoder)
    cross_encoder_info = {
        "available": cross_encoder_available(),
        "backend": "server-side (engine: voyage-rerank-2.5 or ms-marco cross-encoder, RDR-188)",
        "client_role": "salience-only (nexus.salience; rerank caller retired, nexus-9o6y2.19)",
        "default_local_model": "cross-encoder/ms-marco-MiniLM-L-6-v2",
    }

    return {
        "vector_store": {
            "limits": vector_store_limits,
            "reachable": t3_reachable,
            "detail": t3_detail,
        },
        "voyage": voyage_limits,
        "cross_encoder": cross_encoder_info,
        "retry": retry,
    }


def _format_quota_report(report: dict) -> str:
    """Human-readable form of :func:`_collect_quota_report` output."""
    lines: list[str] = []
    lines.append("Quota headroom report (nexus-c590)")
    lines.append("")

    # ── Vector store ────────────────────────────────────────────────────
    # nexus-d01js: T3 serving is PG+pgvector since 6.0.0. The limits table
    # below originated as the Chroma-era QUOTAS module and was rehomed to
    # nexus.db.limits at rn3wo.2, kept because SAFE_CHUNK_BYTES /
    # MAX_QUERY_RESULTS remain the load-bearing chunking/paging caps. The JSON
    # key was renamed "chromadb" -> "vector_store" at 7.0.0 (RDR-155 P4b).
    vs = report["vector_store"]
    status = _CHECK if vs["reachable"] else _WARN
    lines.append(f"  {status} T3 vector store: {vs['detail']}")
    lines.append(
        "    limits (per-request caps from nexus.db.limits — chunking/paging "
        "caps are authoritative):"
    )
    for k, v in vs["limits"].items():
        lines.append(f"      {k:32} {v:,}")
    lines.append("")

    # ── Voyage ───────────────────────────────────────────────────────────
    v = report["voyage"]
    # RDR-188 (nexus-9o6y2.16): the client key is engine-bootstrap/migration
    # material — absence is INFO, not a warning (no client code path consumes
    # it; the engine's own key state is what matters and doctor's service
    # checks cover that).
    key_label = (
        "VOYAGE_API_KEY: set (engine-bootstrap/migration material)"
        if v["api_key_set"]
        else "VOYAGE_API_KEY: absent (client does not consume it; engine key plumbed at spawn)"
    )
    lines.append(f"  {_CHECK} Voyage AI: {key_label}")
    lines.append(f"    target rpm (indexer rate limiter):        {v['target_rpm']}")
    for model, caps in v["models"].items():
        lines.append(
            f"    {model:20} tokens={caps['max_tokens']:>6,}  "
            f"dims={caps['embedding_dims']}"
        )
    lines.append("")

    # ── Cross-encoder (RDR-109 Phase 3) ──────────────────────────────────
    ce = report.get("cross_encoder", {})
    if ce:
        ce_status = _CHECK if ce.get("available") else _WARN
        lines.append(
            f"  {ce_status} Cross-encoder backend: {ce.get('backend', 'unknown')}"
        )
        if ce.get("client_role"):
            lines.append(f"    client substrate: {ce.get('client_role')}")
        lines.append("")

    # ── Retry accumulator ────────────────────────────────────────────────
    r = report["retry"]
    if r.get("total_count", 0) > 0:
        lines.append(f"  {_WARN} Observed transient-error retries this process:")
        if r.get("voyage_count", 0) > 0:
            lines.append(
                f"    voyage:  {r['voyage_seconds']:>6.1f}s over "
                f"{r['voyage_count']} retries"
            )
        if r.get("vector_count", 0) > 0:
            lines.append(
                f"    chroma:  {r['vector_seconds']:>6.1f}s over "
                f"{r['vector_count']} retries"
            )
        lines.append(
            f"    total:   {r['total_seconds']:>6.1f}s over "
            f"{r['total_count']} retries"
        )
    else:
        lines.append(f"  {_CHECK} Retry accumulator: no transient backoffs observed")

    return "\n".join(lines)


def _run_check_taxonomy() -> None:
    """Verify the topic_links ≡ projection-assignment invariant (GH #252).

    ``topic_links`` is the materialized aggregate of ``topic_assignments``
    rows with ``assigned_by='projection'``. Today a single caller
    (``_persist_assignments``) maintains it via ``refresh_projection_links``.
    Any future caller that writes projection assignments through
    ``assign_topic`` directly — or a test fixture that seeds rows — will
    silently re-break the invariant. This check detects the drift.
    """
    # ENGINE ONLY. nexus-ypori made the engine the verdict; nexus-b1v9z part A
    # made an engine-side failure loud; 2026-08-29 (Hal: "there is no path
    # back to chromadb sqlite") deleted the frozen-source census that used to
    # run when no engine answered, together with the RDR-176 Gap-2 rationale
    # that kept it. No engine to ask is exit 2: unverifiable is never a pass.
    import httpx  # noqa: PLC0415 — deferred to keep CLI startup fast
    from contextlib import suppress as _suppress  # noqa: PLC0415 — branch-local

    report: dict[str, Any] | None = None
    try:
        from nexus.db.t2.http_taxonomy_store import HttpTaxonomyStore  # noqa: PLC0415 — deferred: CLI startup cost

        store = HttpTaxonomyStore()   # self-resolves the endpoint, as t2/__init__ does
        try:
            report = store.get_link_drift()
        finally:
            with _suppress(Exception):
                store.close()
    except httpx.HTTPStatusError as exc:
        if "404" in str(exc):
            # The route is newer than the deployed engine. Distinguish this
            # from "no service": the operator's action differs (deploy an
            # engine carrying /links/drift vs. start one).
            click.echo(
                "✗ taxonomy check cannot run: the deployed engine has no "
                "/links/drift route (added for nexus-ypori) — deploy an engine "
                "carrying it and re-run. Unverifiable is not a pass.",
                err=True,
            )
            raise click.exceptions.Exit(2)
        click.echo(f"✗ FAIL: taxonomy engine check failed: {exc}", err=True)
        raise click.exceptions.Exit(1)
    except (httpx.HTTPError, RuntimeError) as exc:
        # Transport failure (connect/timeout) or an unresolvable endpoint
        # (ServiceEndpointUnresolvableError, a RuntimeError subclass): no
        # engine to ask, and there is no other store to read.
        click.echo(
            f"✗ taxonomy check cannot run: no engine answered ({exc}). Start "
            "the service (`nx daemon service start`) and re-run. Unverifiable "
            "is not a pass.",
            err=True,
        )
        raise click.exceptions.Exit(2)
    except Exception as exc:  # noqa: BLE001 — any other failure is the verdict, never a traceback (critic on 7742b9c05)
        click.echo(f"✗ FAIL: taxonomy engine check failed: {exc}", err=True)
        raise click.exceptions.Exit(1)

    assert report is not None
    total = int(report.get("projection_total") or 0)
    count = int(report.get("drift_count") or 0)
    if not count:
        click.echo(
            f"✓ topic_links invariant holds ({total} topic(s) with "
            "projection assignments)."
        )
        return
    click.echo(
        f"✗ topic_links drift: {count}/{total} topic(s) have projection "
        "assignments but no topic_links row."
    )
    for row in (report.get("rows") or [])[:10]:
        tid = row.get("topic_id")
        pretty = row.get("label") or f"(unlabelled id={tid})"
        coll = row.get("collection")
        scope = f" [{coll}]" if coll else ""
        click.echo(f"  - topic {tid}: {pretty}{scope}")
    if count > 10:
        click.echo(f"  … {count - 10} more")
    click.echo(
        "Fix: re-run `nx taxonomy project --backfill --persist` to rebuild "
        "the materialized view."
    )
    raise click.exceptions.Exit(1)


def _run_check_quotas(*, json_out: bool = False) -> None:
    """Emit the quota-headroom report (nexus-c590).

    Exits 1 when the T3 vector store is unreachable — a quota report
    without a reachable store is not actionable. A reachable store exits 0.
    """
    import json as _json  # noqa: PLC0415 — deferred to keep CLI startup fast

    report = _collect_quota_report()
    if json_out:
        click.echo(_json.dumps(report, indent=2))
    else:
        click.echo(_format_quota_report(report))

    if not report["vector_store"]["reachable"]:
        raise click.exceptions.Exit(1)
