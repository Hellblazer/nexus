# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""DEVONthink selector helpers behind ``nx dt`` (RDR-099 P1).

DEVONthink is the macOS knowledge manager that owns the canonical paths
for many of Hal's PDF/markdown collections. Substrate already exists for
reading individual records by ``x-devonthink-item://<UUID>`` URI
(``aspect_readers._devonthink_resolver_default``); this module adds the
*bulk* selector verbs the operator needs at the CLI:

* ``_dt_selection`` — what's currently selected in DT
* ``_dt_uuid_record`` — a single UUID (reuses the existing resolver)
* ``_dt_tag_records`` — every record with tag ``X``
* ``_dt_group_records`` — every record under group path ``/A/B`` (recursive)
* ``_dt_smart_group_records`` — execute a user-authored smart group's
  query, honouring its ``search group`` scope and ``exclude subgroups``
  flag

Each helper returns ``list[tuple[str, str]]`` of ``(uuid, absolute_path)``.

Multi-database iteration: when ``database is None`` (the default) every
helper iterates DT's ``databases`` collection so a tag/group/smart-group
named identically across libraries returns one merged, UUID-deduped
list. Pass ``database="MyLib"`` to scope to a single library.

Platform gate: every spawning helper raises
:class:`DTNotAvailableError` on non-darwin, with the same friendly
message the reader emits. ``_dt_uuid_record(..., dt_resolver=...)``
bypasses the gate for tests (the injected resolver IS the contract).

AppleScript dialect: tokens are sdef-canonical for DT 4.2.2 per
``nexus_rdr/099-research-5`` and ``-6`` — ``selected records``,
``lookup records with tags``, ``parents whose record type is smart
group``, and ``search predicates`` (PLURAL — the singular form is
silently accepted but only reads the first predicate).

Output wire format: each record is one ``uuid<TAB>path<LF>`` line. The
parser tolerates trailing newlines and skips blank lines; UUID dedupe
across multiple databases happens in Python.
"""
from __future__ import annotations

import subprocess
import sys

import structlog

import nexus.aspect_readers as _aspect_readers

__all__ = [
    "DTNotAvailableError",
    "_dt_group_records",
    "_dt_selection",
    "_dt_smart_group_records",
    "_dt_tag_records",
    "_dt_uuid_record",
    "_is_darwin",
    "_run_osascript",
]

_log = structlog.get_logger(__name__)


# ── Errors ───────────────────────────────────────────────────────────────────


class DTNotAvailableError(RuntimeError):
    """DEVONthink isn't running, isn't installed, or isn't reachable.

    Subclass of :class:`RuntimeError` so callers that already catch
    runtime errors get sensible behaviour without a try/except per
    helper.
    """


# ── Platform gate ────────────────────────────────────────────────────────────


def _is_darwin() -> bool:
    """``True`` iff this process is running on macOS.

    Wrapped so tests can ``monkeypatch.setattr("sys.platform", ...)``
    and exercise both branches without per-call subprocess machinery.
    """
    return sys.platform == "darwin"


def _require_darwin() -> None:
    if not _is_darwin():
        raise DTNotAvailableError(
            "DEVONthink integration is macOS-only",
        )


# ── osascript wrapper ────────────────────────────────────────────────────────


_NOT_RUNNING_TOKENS: tuple[str, ...] = (
    "Application isn't running",
    "application isn't running",
)


def _run_osascript(script: str, timeout: int) -> str:
    """Spawn ``osascript -e <script>`` and return its stdout.

    Raises:
        subprocess.TimeoutExpired: propagated unchanged so callers can
            distinguish "DT is hung" from "DT isn't there at all".
        DTNotAvailableError: when stderr contains DT's
            ``Application isn't running`` token. The message is
            operator-friendly so the CLI can surface it without
            additional translation.
    """
    proc = subprocess.run(  # noqa: S603 - osascript is a system binary
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        stderr = proc.stderr or ""
        if any(token in stderr for token in _NOT_RUNNING_TOKENS):
            raise DTNotAvailableError(
                "DEVONthink is not running. Open it and retry, or pass "
                "--uuid for a UUID you already have.",
            )
        # Non-DT-availability failures still need to be surfaced — keep
        # CalledProcessError so the operator sees the AppleScript error
        # detail rather than a silent empty list.
        raise subprocess.CalledProcessError(
            returncode=proc.returncode,
            cmd=["osascript", "-e", script],
            output=proc.stdout,
            stderr=stderr,
        )
    return proc.stdout


# ── Output parser ────────────────────────────────────────────────────────────


def _parse_records(stdout: str) -> list[tuple[str, str]]:
    """Parse ``uuid<TAB>path<LF>`` lines into ``(uuid, path)`` tuples.

    Blank lines are skipped; lines without a tab are skipped (defensive
    against AppleScript dropping a record with no path). UUID dedupe
    is the caller's responsibility because multi-DB iteration may
    legitimately want to know that a UUID surfaces in two libraries
    before collapsing.
    """
    records: list[tuple[str, str]] = []
    for line in stdout.splitlines():
        if not line:
            continue
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        uuid, path = parts[0].strip(), parts[1].strip()
        if uuid and path:
            records.append((uuid, path))
    return records


def _dedupe_by_uuid(records: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """First-wins dedupe by UUID, preserving order."""
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for uuid, path in records:
        if uuid in seen:
            continue
        seen.add(uuid)
        out.append((uuid, path))
    return out


# ── AppleScript fragment helpers ─────────────────────────────────────────────


def _databases_clause(database: str | None) -> str:
    """Return the AppleScript expression for the database scope.

    ``None`` → ``databases`` (every open library).
    A name → ``{database "<escaped>"}`` (single-element list so the
    same ``repeat with theDb in <expr>`` template covers both shapes).
    """
    if database is None:
        return "databases"
    return f'{{database "{_applescript_escape(database)}"}}'


def _applescript_escape(s: str) -> str:
    """Escape backslashes and double-quotes for AppleScript string literals."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


# ── Selector: current selection ──────────────────────────────────────────────


def _dt_selection() -> list[tuple[str, str]]:
    """Records currently selected in DEVONthink's UI.

    Returns ``[]`` when nothing is selected. 10s timeout — the
    selection is in-memory state, no I/O.
    """
    _require_darwin()
    script = (
        'tell application id "DNtp"\n'
        '  set theOutput to ""\n'
        '  set theRecords to selected records\n'
        '  repeat with theRecord in theRecords\n'
        '    set theOutput to theOutput & ((uuid of theRecord) as string) '
        '& tab & ((path of theRecord) as string) & linefeed\n'
        '  end repeat\n'
        '  return theOutput\n'
        'end tell'
    )
    stdout = _run_osascript(script, timeout=10)
    records = _parse_records(stdout)
    _log.debug("dt_selection", count=len(records))
    return records


# ── Selector: single UUID ────────────────────────────────────────────────────


def _dt_uuid_record(
    uuid: str,
    *,
    dt_resolver=None,
) -> list[tuple[str, str]]:
    """Resolve a single DEVONthink record UUID to ``[(uuid, path)]``.

    Reuses :func:`nexus.aspect_readers._devonthink_resolver_default` for
    the osascript path so we don't duplicate the
    ``record with uuid "..."`` wrangling. ``dt_resolver`` lets tests
    inject a fake without spawning subprocesses.

    Returns ``[]`` when the resolver reports a missing record so the
    return type stays consistent with the multi-record selectors.
    """
    if dt_resolver is None:
        _require_darwin()
        # Module attribute access (not local-name binding) so tests
        # that monkeypatch ``nexus.aspect_readers._devonthink_resolver_default``
        # see their fake here, and we don't pay a re-import cost on
        # every call (audit fix F2 restated; per code-review feedback).
        dt_resolver = _aspect_readers._devonthink_resolver_default

    path, error_detail = dt_resolver(uuid)
    if path is None:
        _log.debug("dt_uuid_record_miss", uuid=uuid, detail=error_detail)
        return []
    return [(uuid, path)]


# ── Selector: tag ────────────────────────────────────────────────────────────


def _dt_tag_records(
    tag: str,
    database: str | None = None,
) -> list[tuple[str, str]]:
    """Every record carrying ``tag`` across the requested database scope.

    Empty ``tag`` short-circuits to ``[]`` without spawning osascript —
    DT's ``lookup records with tags {""}`` would happily return every
    record, which is never what the operator meant.
    """
    if not tag:
        return []
    _require_darwin()
    db_clause = _databases_clause(database)
    tag_lit = _applescript_escape(tag)
    script = (
        'tell application id "DNtp"\n'
        '  set theOutput to ""\n'
        f'  set theDbs to {db_clause}\n'
        '  repeat with theDb in theDbs\n'
        f'    set theRecords to lookup records with tags {{"{tag_lit}"}} in theDb\n'
        '    repeat with theRecord in theRecords\n'
        '      set theOutput to theOutput & ((uuid of theRecord) as string) '
        '& tab & ((path of theRecord) as string) & linefeed\n'
        '    end repeat\n'
        '  end repeat\n'
        '  return theOutput\n'
        'end tell'
    )
    stdout = _run_osascript(script, timeout=30)
    records = _dedupe_by_uuid(_parse_records(stdout))
    _log.debug("dt_tag_records", tag=tag, database=database, count=len(records))
    return records


# ── Selector: group (recursive) ──────────────────────────────────────────────


def _dt_group_records(
    group_path: str,
    database: str | None = None,
) -> list[tuple[str, str]]:
    """Every leaf record under ``group_path`` (recursively).

    ``group_path`` is the DT path inside the database, e.g.
    ``/Research/Papers``. ``/Trash`` and ``/Tags`` are valid root names
    in DT 4 — the helper passes them through unchanged.

    When ``database is None`` the helper iterates every open library
    and dedupes by UUID; when named, it scopes to that library.
    """
    _require_darwin()
    db_clause = _databases_clause(database)
    group_lit = _applescript_escape(group_path)
    # Recursive walk via AppleScript handler — ``children`` includes
    # both items and groups; recurse into groups, accumulate items.
    script = (
        'on collectRecords(theGroup, accumRef)\n'
        '  tell application id "DNtp"\n'
        '    repeat with theChild in children of theGroup\n'
        '      if type of theChild is group then\n'
        '        my collectRecords(theChild, accumRef)\n'
        '      else\n'
        '        set contents of accumRef to (contents of accumRef) & '
        '((uuid of theChild) as string) & tab & '
        '((path of theChild) as string) & linefeed\n'
        '      end if\n'
        '    end repeat\n'
        '  end tell\n'
        'end collectRecords\n'
        'tell application id "DNtp"\n'
        '  set theOutput to ""\n'
        f'  set theDbs to {db_clause}\n'
        '  repeat with theDb in theDbs\n'
        '    try\n'
        f'      set theGroup to get record at "{group_lit}" in theDb\n'
        '      if theGroup is not missing value then\n'
        '        my collectRecords(theGroup, a reference to theOutput)\n'
        '      end if\n'
        '    on error\n'
        '      -- group_path not present in this database — skip\n'
        '    end try\n'
        '  end repeat\n'
        '  return theOutput\n'
        'end tell'
    )
    stdout = _run_osascript(script, timeout=30)
    records = _dedupe_by_uuid(_parse_records(stdout))
    _log.debug(
        "dt_group_records",
        group_path=group_path,
        database=database,
        count=len(records),
    )
    return records


# ── Selector: smart group ────────────────────────────────────────────────────


def _dt_smart_group_records(
    name: str,
    database: str | None = None,
) -> list[tuple[str, str]]:
    """Execute a user-authored smart group's query.

    Three-property read (sdef-canonical, locked by RDR research):

    * ``search predicates`` — PLURAL; singular form silently reads only
      the first predicate.
    * ``search group`` — the user-authored scope. ``missing value``
      means "whole library", which we honour by falling through to
      ``root of theDb``.
    * ``exclude subgroups`` — recursion behaviour for the search.

    The smart group itself is found via
    ``parents whose record type is smart group`` filtered by name.
    Re-execution uses ``search <pred> in <scope> exclude subgroups
    <bool>`` so the operator's intent (scope, recursion) is preserved.
    """
    _require_darwin()
    db_clause = _databases_clause(database)
    name_lit = _applescript_escape(name)
    script = (
        'tell application id "DNtp"\n'
        '  set theOutput to ""\n'
        f'  set theDbs to {db_clause}\n'
        '  repeat with theDb in theDbs\n'
        '    tell theDb\n'
        '      set theSmartGroups to (parents whose record type is smart group)\n'
        '    end tell\n'
        '    repeat with sg in theSmartGroups\n'
        f'      if name of sg is "{name_lit}" then\n'
        '        set thePred to search predicates of sg\n'
        '        set theScope to search group of sg\n'
        '        set theExclude to exclude subgroups of sg\n'
        '        if theScope is missing value then\n'
        '          set theScope to root of theDb\n'
        '        end if\n'
        '        set theResults to search thePred in theScope '
        'exclude subgroups theExclude\n'
        '        repeat with r in theResults\n'
        '          set theOutput to theOutput & ((uuid of r) as string) & '
        'tab & ((path of r) as string) & linefeed\n'
        '        end repeat\n'
        '      end if\n'
        '    end repeat\n'
        '  end repeat\n'
        '  return theOutput\n'
        'end tell'
    )
    stdout = _run_osascript(script, timeout=30)
    records = _dedupe_by_uuid(_parse_records(stdout))
    _log.debug(
        "dt_smart_group_records",
        name=name,
        database=database,
        count=len(records),
    )
    return records
