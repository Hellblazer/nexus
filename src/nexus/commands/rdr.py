# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx rdr`` — RDR authoring helpers.

Exposes:
  - ``lint``    : scan RDR markdown files for frontmatter parse hazards
  - ``preamble``: 9 lifecycle subcommands (RDR-130 P1.2)
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import click
import yaml

from nexus.tables.load import Table, TableLoadError, load_packaged_table
from nexus.tables.resolve import resolve
from nexus.tables.review_rounds import blocking_rounds, rule_for


# ---------------------------------------------------------------------------
# lint helpers (unchanged)
# ---------------------------------------------------------------------------

# Matches a flow-sequence opener followed (eventually) by an unquoted
# ``#`` before the closing ``]``. ``[^\]"']*?`` lets us span multiple
# lines (PyYAML's multi-line flow sequences parse silently into empty
# lists when ``#`` introduces comments mid-sequence — a true false
# negative for the single-line regex). The ``"'`` exclusion keeps quoted
# strings from being mis-flagged as the hazard.
_HASH_REF_IN_FLOW_SEQ = re.compile(r":\s*\[[^\]\"']*?#", re.DOTALL)


def _frontmatter_block(text: str) -> str | None:
    """Return the frontmatter block (without delimiters) or None."""
    if not text.startswith("---"):
        return None
    idx = text.find("\n---", 3)
    if idx == -1:
        return None
    return text[3:idx]


def _lint_one(path: Path) -> list[str]:
    """Return a list of human-readable findings for *path* (empty if clean)."""
    findings: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return [f"{path}: read failed ({type(exc).__name__}: {exc})"]

    fm = _frontmatter_block(text)
    if fm is None:
        return findings

    # _frontmatter_block returns text starting at index 3 (right after the
    # opening ``---``), which is typically the trailing ``\n`` of that line.
    # Strip the leading newline so the first content line maps cleanly to
    # file line 2 (file line 1 is the opening ``---``).
    fm_body = fm.lstrip("\n")

    for m in _HASH_REF_IN_FLOW_SEQ.finditer(fm_body):
        # If the opener line is itself a YAML comment (``# note: [#381]``),
        # the ``: [`` is inside a comment and the whole thing is benign.
        # Find the start of the line containing the match opener and
        # check the first non-whitespace char.
        line_start = fm_body.rfind("\n", 0, m.start()) + 1
        if fm_body[line_start:m.start()].lstrip().startswith("#"):
            continue
        # Line number within fm_body. +2 for the opening ``---`` line.
        line_no = fm_body.count("\n", 0, m.start()) + 2
        snippet = fm_body[m.start():m.end()].replace("\n", " ").strip()
        findings.append(
            f"{path}:{line_no}: unquoted #-ref in YAML flow sequence "
            f"({snippet!r}); quote the refs: "
            f'prs: ["#381", "#382"]'
        )

    try:
        yaml.safe_load(fm)
    except yaml.YAMLError as exc:
        findings.append(f"{path}: frontmatter YAML parse error: {exc}")

    return findings


# ---------------------------------------------------------------------------
# preamble shared helpers (RDR-130 P1.2)
# ---------------------------------------------------------------------------

_PREAMBLE_EXCLUDED: frozenset[str] = frozenset({
    "readme.md", "template.md", "index.md", "overview.md",
    "workflow.md", "templates.md",
})


def _preamble_resolve_repo() -> tuple[str, str]:
    """Return (repo_root, repo_name) by probing git; fall back to cwd."""
    try:
        repo_root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
        repo_name = Path(repo_root).name
    except Exception:  # noqa: BLE001 — best-effort cwd derivation; falls back to working dir on failure
        repo_root = str(Path.cwd())
        repo_name = Path(repo_root).name
    return repo_root, repo_name


def _preamble_rdr_dir(repo_root: str) -> str:
    """Resolve RDR directory from .nexus.yml or return 'docs/rdr'."""
    rdr_dir = "docs/rdr"
    nexus_yml = Path(repo_root) / ".nexus.yml"
    if nexus_yml.exists():
        content = nexus_yml.read_text()
        try:
            d = yaml.safe_load(content) or {}
            paths = (d.get("indexing") or {}).get("rdr_paths", ["docs/rdr"])
            rdr_dir = paths[0] if paths else "docs/rdr"
        except Exception:  # noqa: BLE001 — fallback parse path; tries alternate regex extraction on failure
            m_yml = (
                re.search(r"rdr_paths[^\[]*\[([^\]]+)\]", content)
                or re.search(r"rdr_paths:\s*\n\s+-\s*(.+)", content)
            )
            if m_yml:
                v = m_yml.group(1)
                parts = re.findall(r"[a-z][a-z0-9/_-]+", v)
                rdr_dir = parts[0] if parts else "docs/rdr"
    return rdr_dir


def _preamble_parse_frontmatter(filepath: Path) -> tuple[dict, str]:
    """Parse YAML frontmatter from *filepath*; return (meta, full_text)."""
    text = filepath.read_text(errors="replace")
    meta: dict = {}
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            block = parts[1]
            try:
                meta = yaml.safe_load(block) or {}
            except Exception:  # noqa: BLE001 — fallback parse path; degrades to line-by-line parsing
                for line in block.splitlines():
                    if ":" in line:
                        k, _, v = line.partition(":")
                        meta[k.strip().lower()] = v.strip()
    else:
        m = re.search(
            r"^## Metadata\s*\n(.*?)(?=^##|\Z)", text, re.MULTILINE | re.DOTALL
        )
        if m:
            for line in m.group(1).splitlines():
                kv = re.match(r"-?\s*\*\*(\w[\w\s]*?)\*\*:\s*(.+)", line.strip())
                if kv:
                    meta[kv.group(1).strip().lower()] = kv.group(2).strip()
    if "title" not in meta and "name" not in meta:
        h1 = re.search(r"^#\s+(.+)", text, re.MULTILINE)
        if h1:
            meta["title"] = h1.group(1).strip()
    return meta, text


def _preamble_find_rdr_file(rdr_path: Path, id_str: str) -> Path | None:
    """Find an RDR .md by numeric ID; return None if not found."""
    m = re.search(r"\d+", id_str)
    if not m:
        return None
    num_int = int(m.group(0))
    for f in sorted(rdr_path.glob("*.md")):
        nums = re.findall(r"\d+", f.stem)
        if nums and int(nums[0]) == num_int:
            return f
    return None


def _preamble_get_all_rdrs(rdr_path: Path) -> list[dict]:
    """Return a list of RDR dicts from .md files in *rdr_path*."""
    rdrs: list[dict] = []
    for f in sorted(rdr_path.glob("*.md")):
        if f.name.lower() in _PREAMBLE_EXCLUDED:
            continue
        fm, text = _preamble_parse_frontmatter(f)
        rtype = fm.get("type", "?")
        doc_status = fm.get("status", "?")
        if doc_status == "?" and rtype == "?":
            continue
        nums = re.findall(r"\d+", f.stem)
        rdrs.append({
            "id": nums[0] if nums else f.stem,
            "file": f.name,
            "path": f,
            "text": text,
            "title": fm.get("title", fm.get("name", f.stem)),
            "status": doc_status,
            "rtype": rtype,
            "priority": fm.get("priority", "?"),
        })
    return rdrs


def _preamble_parse_t2_field(content: str, field: str) -> str | None:
    """Extract a field value from T2 entry content."""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith(f"{field}:"):
            val = stripped.split(":", 1)[1].strip().strip('"').strip("'")
            return val
    return None


def _preamble_get_rdrs_from_t2(repo_name: str, rdr_dir: str) -> list[dict]:
    """Read RDR list from T2; return [] if T2 is unavailable or empty."""
    rdrs: list[dict] = []
    try:
        from nexus.commands._helpers import default_db_path  # noqa: PLC0415 — circular-dep avoidance: deferred intra-package import
        from nexus.db.t2 import T2Database  # noqa: PLC0415 — circular-dep avoidance: deferred intra-package import
        with T2Database(default_db_path()) as db:  # boundary-allow: short-lived read-only preamble CLI
            entries = db.get_all(project=f"{repo_name}_rdr")
            for entry in entries:
                title = entry.get("title", "")
                if not re.match(r"^\d+$", title):
                    continue
                content = entry.get("content", "")
                rdrs.append({
                    "id": title,
                    "title": _preamble_parse_t2_field(content, "title") or title,
                    "status": _preamble_parse_t2_field(content, "status") or "?",
                    "rtype": _preamble_parse_t2_field(content, "type") or "?",
                    "priority": _preamble_parse_t2_field(content, "priority") or "?",
                    "file_path": (
                        _preamble_parse_t2_field(content, "file_path")
                        or f"{rdr_dir}/{title}-*.md"
                    ),
                })
    except Exception:  # noqa: BLE001 — best-effort RDR scan; returns whatever was collected
        pass
    return rdrs


# ---------------------------------------------------------------------------
# rdr group + lint command
# ---------------------------------------------------------------------------

@click.group()
def rdr() -> None:
    """RDR authoring helpers."""


@rdr.command("lint")
@click.argument(
    "paths",
    nargs=-1,
    type=click.Path(exists=True, path_type=Path),
)
@click.option(
    "--root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Scan this directory recursively for *.md (default: docs/rdr/ if it exists).",
)
def lint(paths: tuple[Path, ...], root: Path | None) -> None:
    """Lint RDR frontmatter for parse hazards.

    Checks each *.md for frontmatter that would fail downstream YAML
    parsing — primarily the ``prs: [#NNN]`` flow-sequence hazard
    (nexus-u7ek). Exits non-zero when any finding is reported.
    """
    targets: list[Path] = []
    if paths:
        for p in paths:
            if p.is_dir():
                targets.extend(sorted(p.rglob("*.md")))
            else:
                targets.append(p)
    else:
        scan_root = root or Path("docs/rdr")
        if not scan_root.exists():
            click.echo(
                f"no paths given and {scan_root} not found; nothing to lint",
                err=True,
            )
            sys.exit(2)
        targets = sorted(scan_root.rglob("*.md"))

    all_findings: list[str] = []
    files_with_findings = 0
    for path in targets:
        per_file = _lint_one(path)
        if per_file:
            files_with_findings += 1
            all_findings.extend(per_file)

    if all_findings:
        for f in all_findings:
            click.echo(f, err=True)
        click.echo(
            f"\n{len(all_findings)} finding(s) in {files_with_findings} of "
            f"{len(targets)} file(s)",
            err=True,
        )
        sys.exit(1)

    click.echo(f"clean: {len(targets)} file(s) scanned")


# ---------------------------------------------------------------------------
# set-status — code-enforced frontmatter flip (RDR-165/166 ledger-drift fix)
# ---------------------------------------------------------------------------

#: status -> the date-stamp frontmatter key it should carry. Unchanged by
#: RDR-201 P1.4 — the table has no notion of date keys, so this stays a
#: plain literal.
_STATUS_DATE_KEY: dict[str, str] = {
    "accepted": "accepted_date",
    "closed": "closed_date",
}

#: ``open`` is retired from the rdr-lifecycle table's ``status`` domain but
#: remains a live, READ-TIME-ONLY pre-accept synonym for ``draft`` in the
#: rdr-accept preamble (GH #1409, nexus-qsryj) -- a project whose RDR
#: convention never uses ``draft`` (open -> accepted) can still accept.
#: Named once here so the two preamble sites that need it (RDR-201 P1.5)
#: don't each carry their own literal.
_OPEN_STATUS_ALIAS = "open"


def _from_statuses_for_event(table: Table, event: str) -> frozenset[str]:
    """Every status the table's non-escape *event* rows transition FROM.

    Queries the loaded rows directly instead of a hand-maintained status
    literal (RDR-201 P1.5, T2 nexus/plan-rdr-201-audit-round-3-residuals
    [23999] item 2 / nexus/plan-rdr-201-enrichment-deltas [24001]) -- a
    preamble guard built this way tracks the table by construction; one
    hardcoded independently is exactly the three-way disagreement RDR-201
    Finding 1 exists to end. Escape/``refuse`` rows are excluded: they are
    the table's own record of statuses *event* is illegal from, so they
    must never contribute to the "eligible" set.
    """
    return frozenset(
        str(row.match["status"])
        for row in table.rows
        if row.match.get("event") == event
        and not row.escape
        and row.outcome_kind == "to"
    )


def _to_status_for_event(table: Table, event: str) -> str:
    """The single status the table's non-escape *event* rows transition TO.

    Refuses loudly (:class:`TableLoadError`) if *event*'s non-escape rows
    don't converge on exactly one target. The rdr-accept preamble's
    idempotency check ("already accepted RDRs are allowed through") assumes
    a single "the status past this event" value to compare against; a table
    edit that broke that assumption should surface here, not produce a
    silently-wrong guard.
    """
    targets = frozenset(
        str(row.outcome["status"])
        for row in table.rows
        if row.match.get("event") == event
        and not row.escape
        and row.outcome_kind == "to"
    )
    if len(targets) != 1:
        raise TableLoadError(
            f"rdr-lifecycle table: event {event!r} has {len(targets)} distinct "
            f"non-escape 'to' targets ({sorted(targets)}), expected exactly 1"
        )
    return next(iter(targets))


def _target_status_to_event(table: Table) -> dict[str, str]:
    """Derive the requested-target-status -> table ``event`` mapping.

    RDR-201 P1.5 fix round (T2 nexus/critique-nexus-j9z30-5-2026-09-01
    [24042] finding 4): mechanically reproduces the old hand-maintained
    ``_TARGET_STATUS_TO_EVENT`` literal by calling :func:`_to_status_for_event`
    for every event in the table's ``event`` domain except ``resume`` --
    ``resume``'s target, ``draft``, is ambiguous on its own (resume from
    ``deferred`` vs. a no-op from ``draft`` itself) and stays resolved from
    (current, target) in :func:`set_status` instead (RDR-201 P1.4 audit
    residual, T2 nexus/plan-rdr-201-audit-round-3-residuals [23999] item 1)
    -- that is the one Python-side rule this derivation does not absorb.
    """
    return {
        _to_status_for_event(table, event): event
        for event in table.dimensions["event"].domain
        if event != "resume"
    }


def _rewrite_frontmatter_status(text: str, new_status: str, date: str) -> str:
    """Return *text* with the frontmatter ``status:`` set to *new_status*.

    Operates on the raw frontmatter block (only the first two ``---`` fences)
    so existing key order and formatting are preserved and a ``---`` horizontal
    rule inside the body is never mistaken for the fence. When *new_status* maps
    to a date key (accepted/closed): if the key is absent it is inserted
    immediately after the ``status:`` line; if the key is present but blank
    (``accepted_date:`` with no value, as the RDR template ships it) it is
    filled with *date*; an existing key that already carries a value is left
    untouched (never overwritten). (nexus-re3nm: the present-but-blank case
    previously left the date empty, forcing a hand-edit.)
    """
    if not text.startswith("---"):
        raise ValueError("RDR file has no YAML frontmatter fence")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError("RDR file frontmatter fence is malformed")
    fm = parts[1]

    if not re.search(r"^status:", fm, re.MULTILINE):
        raise ValueError("RDR frontmatter has no `status:` key")
    # ``.*?\r?`` keeps a CRLF file's carriage return out of the rewritten line
    # (``.*`` would greedily swallow it, leaving a lone ``\n`` line in an
    # otherwise ``\r\n`` file). ``re.sub`` replacement is via a callable so a
    # status value is never interpreted as a backreference.
    fm = re.sub(
        r"^status:.*?(\r?)$",
        lambda m: f"status: {new_status}{m.group(1)}",
        fm,
        count=1,
        flags=re.MULTILINE,
    )

    date_key = _STATUS_DATE_KEY.get(new_status)
    if date_key:
        # Present-but-blank (``accepted_date:`` with no value) -> fill it.
        blank_pat = rf"^{date_key}:[ \t]*(\r?)$"
        if re.search(blank_pat, fm, re.MULTILINE):
            fm = re.sub(
                blank_pat,
                lambda m: f"{date_key}: {date}{m.group(1)}",
                fm,
                count=1,
                flags=re.MULTILINE,
            )
        elif not re.search(rf"^{date_key}:", fm, re.MULTILINE):
            # Absent -> insert immediately after the ``status:`` line.
            fm = re.sub(
                r"^(status:.*?)(\r?)$",
                lambda m: f"{m.group(1)}{m.group(2)}\n{date_key}: {date}{m.group(2)}",
                fm,
                count=1,
                flags=re.MULTILINE,
            )

    return "---" + fm + "---" + parts[2]


#: Extracts a cell's leading word, tolerant of markdown decoration
#: (``**Scrapped 2026-05-19**`` -> ``Scrapped``) so a decorated README cell
#: is still detected without requiring the full shape-aware rewrite (that is
#: bead nexus-j9z30.7's job, per T2
#: nexus/plan-rdr-201-enrichment-deltas [24001] finding 4).
_README_CELL_LEADING_WORD = re.compile(r"[\*_]*([A-Za-z][A-Za-z-]*)")


def _update_readme_status_row(
    readme: Path, rdr_filename: str, label: str, status_domain: frozenset[str]
) -> bool:
    """Update the README index-row status cell for *rdr_filename*.

    *label* is the exact cell text to write — the caller decorates it
    (e.g. ``"Superseded by RDR-108"`` rather than the bare ``"Superseded"``
    for a supersede transition, since the successor id is otherwise not
    recorded anywhere on disk; code review, T2
    nexus/critique-nexus-j9z30-4-2026-09-01 [24034] finding 9).

    Returns True if a row was found and rewritten. Matches the row by the RDR
    filename link and replaces the first cell whose LEADING WORD (case-
    insensitive, decoration-stripped) is a member of *status_domain* — the
    rdr-lifecycle table's ``status`` dimension — so the rewrite is robust to
    both bare cells (``Draft``) and decorated ones (``Closed (implemented)``)
    without assuming a fixed column position.
    """
    if not readme.exists():
        return False
    lines = readme.read_text(encoding="utf-8").splitlines(keepends=True)
    target_cell = label
    changed = False
    for idx, line in enumerate(lines):
        if rdr_filename not in line or "|" not in line:
            continue
        cells = line.split("|")
        for i, cell in enumerate(cells):
            m = _README_CELL_LEADING_WORD.match(cell.strip())
            leading_word = m.group(1).lower() if m else ""
            if leading_word in status_domain:
                cells[i] = f" {target_cell} "
                changed = True
                break
        if changed:
            lines[idx] = "|".join(cells)
            break
    if changed:
        readme.write_text("".join(lines), encoding="utf-8")
    return changed


def _default_t2_client() -> object:
    """Construct the real T2 HTTP client used to read the gate result.

    ``nexus.db.t2.T2Database`` is the same facade ``nx rdr preamble``
    already uses (see ``_preamble_get_rdrs_from_t2``); this is a thin
    factory rather than a direct construction inside
    :func:`_gate_outcome_for` so tests can inject a fake by monkeypatching
    the module-level ``_t2_client_factory`` without touching any T2
    substrate (RDR-201 P1.4 follow-up — Sam, 2026-09-02: the accept
    event's gate guard needs a real T2 read, not a hardcoded constant).
    """
    from nexus.commands._helpers import default_db_path  # noqa: PLC0415 — circular-dep avoidance: deferred intra-package import
    from nexus.db.t2 import T2Database  # noqa: PLC0415 — circular-dep avoidance: deferred intra-package import

    return T2Database(default_db_path())  # boundary-allow: same short-lived preamble facade as :222, gate read for the accept event only


#: Injection seam for :func:`_gate_outcome_for` — production code never
#: calls ``_default_t2_client`` directly, only through this indirection, so
#: a test can monkeypatch it to a factory returning a fake client (any
#: context manager exposing ``get(project=..., title=...) -> dict | None``,
#: matching ``T2Database``'s facade ``get()``).
_t2_client_factory = _default_t2_client


# ---------------------------------------------------------------------------
# RDR-201 P3.3 (nexus-j9z30.22): needs-reexamination markers
# ---------------------------------------------------------------------------

#: The T2 field a dependent's entry gains when a record it is joined to by
#: a ``supersedes`` edge changes status. One line per flip, appended, never
#: consumed by anything but ``rdr-audit``'s listing -- report-only posture
#: (RDR-081 precedent): surfaced, never auto-resolved, never a block.
NEEDS_REEXAMINATION_FIELD = "needs-reexamination"

#: The ONLY catalog edge type the marker walk follows (ruling nexus-j9z30.22,
#: Sam 2026-09-02): of 265 edges the dependency generator proposes over the
#: real tree, 259 are ``relates`` from free-text ``related_rdrs`` -- a
#: reading aid an author typed, not a dependency -- against 6 curated
#: ``supersedes`` edges. Walking everything would flag dozens of loosely
#: associated records on every flip and the six meaningful markers would
#: vanish in the noise. Widen only on evidence that a specific missed
#: ``relates`` edge would have prevented a real error.
_MARKER_LINK_TYPE = "supersedes"


def _default_catalog_reader() -> object:
    from nexus.catalog.factory import make_catalog_reader  # noqa: PLC0415 — deferred import; catalog is only needed after a successful flip, and importing it at module load would pull the service client into every `nx rdr` invocation

    return make_catalog_reader()


#: Injectable seam, same shape as ``_t2_client_factory``: production builds
#: the service-backed reader; tests substitute a fake serving real
#: ``CatalogEntry`` / ``CatalogLink`` objects.
_catalog_reader_factory = _default_catalog_reader


def _rdr_repo_scope(cat: object, repo_root: str) -> tuple[object | None, str]:
    """``(current owner tumbler, docs/rdr source prefix)`` for *repo_root* --
    the two values every in-repo admission check needs
    (:func:`nexus.catalog.rdr_canonical.is_in_repo`). Derived from git
    identity in production; a seam so tests can scope a fake catalog
    without a git remote."""
    from nexus.catalog.rdr_canonical import current_rdr_owner, rdr_source_prefix  # noqa: PLC0415 — deferred import, see _default_catalog_reader

    return current_rdr_owner(cat, repo_root), rdr_source_prefix(repo_root)


def _supersedes_neighbours(
    cat: object, repo_root: str, rdr_num: int,
) -> tuple[list[tuple[int, str]], list[str], str | None]:
    """``(RDR number, edge text)`` pairs for every record *rdr_num*
    SUPERSEDES -- its predecessors, the targets of its outgoing
    ``supersedes`` edges -- resolved through the same canonical-tumbler
    chain the dependency generator used to create those edges
    (:func:`nexus.catalog.link_generator.rdr_resolution` -- one listing, one
    admission, one resolution; never a second copy here). One direction
    only (Sam's ruling 2026-09-02: successor -> predecessor): a successor's
    status changing leaves its predecessor's ``superseded`` verdict stale;
    a predecessor's own flip marks nobody. Returns
    ``(neighbours, unmapped_tumblers, note)``: *unmapped* are neighbour
    tumblers the numeric index could not map back to an RDR number (a
    number whose registrations collide with no unique ``id:``
    self-declaration -- RDR-040 and RDR-079 on this repo today), named so
    the caller reports them instead of dropping them; a non-empty *note*
    names why nothing could be walked at all (repo never indexed, the
    flipped RDR itself unresolvable)."""
    from nexus.catalog.link_generator import rdr_resolution  # noqa: PLC0415 — deferred import, see _default_catalog_reader

    owner, prefix = _rdr_repo_scope(cat, repo_root)
    if owner is None:
        return [], [], "no catalog owner registered for this repo (never indexed)"
    resolved, number_index = rdr_resolution(cat, owner, repo_source_prefix=prefix)
    number_to_tumbler = {
        n: resolved[key] for n, key in number_index.items() if resolved.get(key) is not None
    }
    me = number_to_tumbler.get(rdr_num)
    if me is None:
        return [], [], f"RDR {rdr_num} has no canonical catalog tumbler (unindexed or ambiguous)"
    tumbler_to_number = {str(t): n for n, t in number_to_tumbler.items()}
    # predecessor tumbler -> the edge as "RDR-<from> supersedes RDR-<to>", so
    # the marker records which way the edge runs (critique [24089] S3).
    edges: dict[str, str] = {}
    for link in cat.links_from(me, link_type=_MARKER_LINK_TYPE):  # type: ignore[attr-defined]
        other = str(link.to_tumbler)
        edges[other] = f"RDR-{rdr_num} supersedes RDR-{tumbler_to_number.get(other, '?')}"
    numbers = sorted(
        (n, edges[t]) for t in edges if (n := tumbler_to_number.get(t)) is not None and n != rdr_num
    )
    unmapped = sorted(t for t in edges if t not in tumbler_to_number)
    return numbers, unmapped, None


#: created_by stamped on the edge set-status writes; the dependency
#: generator's own edges say ``rdr_dependency_extractor``.
_SET_STATUS_LINK_AUTHOR = "nx rdr set-status"


def _ensure_supersedes_edge(
    cat: object, repo_root: str, rdr_num: int, successor: str,
) -> tuple[str | None, str | None]:
    """Write the ``successor -> predecessor`` supersedes edge for a flip to
    ``superseded`` (Sam's ruling 2026-09-04: the edge is part of the
    lifecycle, not only of the next index run). Returns ``(edge text,
    note)``: the text when the edge exists after this call (created or
    already there), the note when it could not be written. Never raises;
    the flip already stands."""
    from nexus.catalog.link_generator import (  # noqa: PLC0415 — deferred import, see _default_catalog_reader
        _extract_rdr_ref_numbers,
        rdr_resolution,
    )

    # The generator's own anchored RDR-NNN parser, never a bare digit
    # search: rdr-079 carries superseded_by "nexus.operators.dispatch
    # (PR #168)" (an RDR subsumed by code), and a digit search would
    # write "RDR-168 supersedes RDR-79" for real (review [24399]).
    numbers = _extract_rdr_ref_numbers(successor or "")
    if len(numbers) != 1:
        return None, f"superseded_by {successor!r} does not name exactly one RDR-NNN; no edge written"
    succ_num = numbers[0]
    try:
        owner, prefix = _rdr_repo_scope(cat, repo_root)
        if owner is None:
            return None, "no catalog owner registered for this repo (never indexed); no edge written"
        resolved, number_index = rdr_resolution(cat, owner, repo_source_prefix=prefix)
        tumblers = {n: resolved[k] for n, k in number_index.items() if resolved.get(k) is not None}
        me, succ = tumblers.get(rdr_num), tumblers.get(succ_num)
        if me is None or succ is None:
            missing = [f"RDR-{n}" for n, t in ((rdr_num, me), (succ_num, succ)) if t is None]
            return None, f"{', '.join(missing)} has no canonical catalog tumbler; no edge written"
        cat.link_if_absent(succ, me, _MARKER_LINK_TYPE, _SET_STATUS_LINK_AUTHOR)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 — report-only leg
        return None, f"{type(exc).__name__}: {exc}"
    return f"RDR-{succ_num} supersedes RDR-{rdr_num}", None


def _t2_rdr_titles(number: int) -> tuple[str, ...]:
    """The title shapes a record's T2 entry is found under, in lookup
    order: bare (``"42"``), zero-padded (``"042"`` -- the early records,
    e.g. RDR-014), and ``RDR-``-prefixed in both widths. The census
    matches ``^(?:RDR-)?\\d+$`` and so already counts every shape."""
    return (str(number), f"{number:03d}", f"RDR-{number}", f"RDR-{number:03d}")


def _append_marker_to_t2(client: object, project: str, number: int, marker: str) -> str | None:
    """Append *marker* as its own line to RDR *number*'s T2 entry, under
    whichever title shape the record uses (``"42"`` or ``"RDR-42"``, the two
    shapes :func:`_t2_rdr_status_census` counts). Returns the title written,
    or ``None`` when no entry exists -- an absent record is named by the
    caller, never invented here. An entry already carrying this exact marker
    line is left untouched (a repeated flip does not stack duplicates). The
    engine upserts on (project, title) and
    re-stamps every column from the payload: ``ttl=None`` and the entry's
    own ``tags``/``agent``/``session`` are passed back explicitly so the
    facade's defaults (30-day TTL, empty tags, THIS process's agent and
    session) cannot expire, strip or re-attribute a permanent record. The
    row's ``timestamp`` becomes now() regardless -- the engine owns it --
    and ``access_count`` is preserved server-side."""
    for title in _t2_rdr_titles(number):
        entry = client.get(project=project, title=title)  # type: ignore[attr-defined]
        if not entry:
            continue
        content = str(entry.get("content", "")).rstrip("\n")
        if marker in {line.strip() for line in content.splitlines()}:
            return title  # already carries this exact marker: idempotent, no re-put
        tags = entry.get("tags", "")
        if isinstance(tags, (list, tuple)):
            tags = ",".join(str(t) for t in tags)
        keep = {
            k: entry[k] for k in ("agent", "session")
            if isinstance(entry.get(k), str) and entry[k]
        }
        client.put(  # type: ignore[attr-defined]
            project=project, title=title, content=f"{content}\n{marker}\n",
            tags=str(tags or ""), ttl=None, **keep,
        )
        return title
    return None


_STATUS_DATE_KEY: dict[str, str] = {"accepted": "accepted_date", "closed": "closed_date"}


def _write_t2_status(repo_name: str, rdr_num: int, new_status: str, date: str) -> tuple[str | None, str | None]:
    """Mirror a successful file flip onto the record's own T2 entry
    (project ``<repo>_rdr``, title ``"<n>"`` or ``"RDR-<n>"``): rewrite the
    ``status:`` line (or prepend one -- several live records carried only
    a prose ``STATUS: x`` the census could not read, bead nexus-nxn5g), and
    set ``accepted_date`` / ``closed_date`` the way
    :func:`_rewrite_frontmatter_status` does on the file. Until 2026-09-02
    the lifecycle skills were the only T2 status writer, in prose; the
    nine drift rows in nexus-nxn5g are what that produced. Same
    preservation rules as :func:`_append_marker_to_t2` (tags, agent,
    session, ttl=None). Returns ``(title written, note)``; never raises."""
    project = f"{repo_name}_rdr"
    try:
        with _t2_client_factory() as client:
            for title in _t2_rdr_titles(rdr_num):
                entry = client.get(project=project, title=title)
                if not entry:
                    continue
                lines = str(entry.get("content", "")).splitlines()
                date_key = _STATUS_DATE_KEY.get(new_status)
                out: list[str] = []
                seen_status = seen_date = False
                for line in lines:
                    stripped = line.strip()
                    if stripped.startswith("status:") and not seen_status:
                        out.append(f"status: {new_status}")
                        seen_status = True
                    elif date_key and stripped.startswith(f"{date_key}:") and not seen_date:
                        out.append(f"{date_key}: {date}")
                        seen_date = True
                    else:
                        out.append(line)
                if not seen_status:
                    out.insert(0, f"status: {new_status}")
                if date_key and not seen_date:
                    out.insert(1 if not seen_status else out.index(f"status: {new_status}") + 1, f"{date_key}: {date}")
                tags = entry.get("tags", "")
                if isinstance(tags, (list, tuple)):
                    tags = ",".join(str(t) for t in tags)
                keep = {k: entry[k] for k in ("agent", "session") if isinstance(entry.get(k), str) and entry[k]}
                client.put(project=project, title=title, content="\n".join(out) + "\n", tags=str(tags or ""), ttl=None, **keep)
                return title, None
        return None, f"no T2 entry for RDR {rdr_num} in {project} -- status not mirrored"
    except Exception as exc:  # noqa: BLE001 — the file flip already happened; a T2 failure is named, never allowed to fail the command
        return None, f"T2 status not mirrored: {type(exc).__name__}: {exc}"


def _mark_dependents_needs_reexamination(
    rdr_num: int, old_status: str, new_status: str, repo_root: str, repo_name: str,
) -> tuple[list[str], list[int], list[str]]:
    """Walk the flipped record's ``supersedes`` neighbours and mark each
    one's T2 entry ``needs-reexamination: RDR-<from> <old>-><new>``.
    Returns ``(titles marked, numbers with no T2 entry, notes)``. The marker
    line is ``needs-reexamination: RDR-<from> <old>-><new> (RDR-a supersedes
    RDR-b)`` -- the edge named so the reader knows which way it runs. Never
    raises -- a catalog failure, an unmappable neighbour tumbler, or a T2
    failure on one entry each become a note, and a failure on entry N
    never discards what was already marked for 1..N-1; the flip that
    triggered this has already been written and stands regardless."""
    project = f"{repo_name}_rdr"
    notes: list[str] = []
    try:
        cat = _catalog_reader_factory()
        numbers, unmapped, note = _supersedes_neighbours(cat, repo_root, rdr_num)
    except Exception as exc:  # noqa: BLE001 — report-only leg: the flip already happened; a catalog failure is named on its own line, never allowed to fail the command
        return [], [], [f"{type(exc).__name__}: {exc}"]
    if note is not None:
        notes.append(note)
    for tumbler in unmapped:
        notes.append(
            f"supersedes neighbour {tumbler} could not be mapped to an RDR number "
            "(colliding registrations with no unique id: self-declaration) -- not marked"
        )
    if not numbers:
        return [], [], notes
    marked: list[str] = []
    missing: list[int] = []
    try:
        client_cm = _t2_client_factory()
    except Exception as exc:  # noqa: BLE001 — same report-only posture
        return [], [], notes + [f"{type(exc).__name__}: {exc}"]
    with client_cm as client:
        for number, edge in numbers:
            marker = f"{NEEDS_REEXAMINATION_FIELD}: RDR-{rdr_num} {old_status}->{new_status} ({edge})"
            try:
                title = _append_marker_to_t2(client, project, number, marker)
            except Exception as exc:  # noqa: BLE001 — one entry's failure is named; the loop continues so earlier marks are still reported
                notes.append(f"RDR {number}: {type(exc).__name__}: {exc}")
                continue
            if title is None:
                missing.append(number)
            else:
                marked.append(title)
    return marked, missing, notes


def _gate_outcome_for(rdr_num: str, repo_name: str) -> tuple[str, str | None]:
    """Read the T2 gate result for *rdr_num* and reduce it to the
    rdr-lifecycle table's ``gate`` dimension value.

    T2 project ``<repo>_rdr``, title ``<rdr_num>-gate-latest`` — the same
    coordinates ``nx rdr preamble rdr-accept`` already prints as an
    instruction. Parses the entry content's ``outcome:`` line: ``PASSED``
    -> ``"passed"``, ``BLOCKED`` -> ``"blocked"``; a missing record, a
    record with no ``outcome:`` line, or an unrecognised outcome value all
    reduce to ``"none"``.

    Returns ``(gate_value, note)``. *note* is ``None`` when the read
    behaved normally (PASSED, BLOCKED, or a legitimately absent gate
    record — no gate run yet is an ordinary ``"none"``, not a T2
    failure); it carries a short, named reason when the record is
    missing or T2 itself could not be reached, so the CLI's refusal
    message can say why instead of a bare ``gate-not-passed``.
    """
    project = f"{repo_name}_rdr"
    title = f"{rdr_num}-gate-latest"
    try:
        with _t2_client_factory() as client:
            entry = client.get(project=project, title=title)
    except Exception as exc:  # noqa: BLE001 — T2 unreachable is an expected, named failure mode here (connection errors, timeouts, ...); reduced to gate="none" with the exception surfaced in `note`, never silently swallowed and never re-raised past this CLI boundary.
        return "none", f"T2 unreachable: {type(exc).__name__}: {exc}"

    if entry is None:
        return "none", f"no gate record found (T2 project {project!r}, title {title!r})"

    content = entry.get("content", "") if isinstance(entry, dict) else ""
    outcome = _preamble_parse_t2_field(content, "outcome")
    if outcome is None:
        return "none", f"gate record {title!r} has no `outcome:` field"
    outcome_upper = outcome.strip().upper()
    if outcome_upper == "PASSED":
        return "passed", None
    if outcome_upper == "BLOCKED":
        return "blocked", None
    return "none", f"gate record {title!r} outcome is {outcome!r} (expected PASSED or BLOCKED)"


def _gate_repo_name(repo_root: str) -> str:
    """Worktree-stable repo basename for the T2 gate project (``<repo>_rdr``).

    Plain ``Path(repo_root).name`` returns the WORKTREE directory's own
    basename (e.g. ``agent-a9b6e48835b938551``) when *repo_root* is a
    Claude Code agent worktree, not the main checkout's name every other
    T2 write under this project already uses — resolving the gate lookup
    that way would silently address a per-agent T2 project no gate result
    was ever written to (code review, T2
    nexus/critique-nexus-j9z30-4-2026-09-01 [24034] finding 8).
    ``nexus.repo_identity._resolve_main_repo`` walks
    ``git rev-parse --git-common-dir`` to the main checkout even from a
    worktree path; a *repo_root* that is not a git repo at all (e.g. a
    bare ``tmp_path`` in a unit test) falls back to its own basename
    unchanged, matching the pre-existing non-worktree behavior.
    """
    from nexus.repo_identity import _resolve_main_repo  # noqa: PLC0415 — circular-dep avoidance: deferred intra-package import

    return _resolve_main_repo(Path(repo_root)).name


#: Injectable dispatch seam for ``nx rdr repeat``, same shape as
#: ``_t2_client_factory``: production resolves ``claude_dispatch`` lazily,
#: tests set this to an async fake and never spawn a child.
_repeat_dispatch = None


def _resolve_repeat_dispatch():
    if _repeat_dispatch is not None:
        return _repeat_dispatch
    from nexus.operators.dispatch import claude_dispatch  # noqa: PLC0415 — heavy operator dep deferred to call time

    return claude_dispatch


@rdr.command("repeat")
@click.argument("rdr", type=str)
@click.option(
    "--root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="RDR directory used to resolve a numeric id (default: docs/rdr/).",
)
@click.option(
    "--models",
    default="haiku,sonnet",
    show_default=True,
    help="Two claude -p model aliases to dispatch, comma-separated; they must differ.",
)
@click.option("--timeout", type=float, default=300.0, show_default=True, help="Seconds per dispatch.")
@click.option(
    "--max-budget-usd",
    type=float,
    default=0.50,
    show_default=True,
    help="Budget cap per dispatch.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit the two plans and the divergence as JSON.")
def repeat(
    rdr: str,
    root: Path | None,
    models: str,
    timeout: float,
    max_budget_usd: float,
    as_json: bool,
) -> None:
    """Multi-model repeatability diff of RDR's design text (nexus-axwpn).

    Sends the Technical Design section to two models, asks each for an
    implementation plan, and reports where the plans diverge: steps,
    files, decisions. A divergence is a place the text left open. Exits
    0 with a report; exits 2 when there is nothing to repeat.

    Models are named as claude -p aliases, never resolved through the
    operator tier table: that table's consumers are the two dispatch
    sites RDR-196 allowlists, and this verb is an explicit, hand-run
    comparison outside them.
    """
    import asyncio  # noqa: PLC0415 — only this verb runs an event loop
    import json as _json  # noqa: PLC0415

    from nexus.rdr_repeat import (  # noqa: PLC0415
        PLAN_SCHEMA,
        RepeatError,
        build_prompt,
        diff_plans,
        extract_design_section,
        parse_plan,
        render_report,
    )

    path = Path(rdr)
    if not path.is_file():
        scan_root = root or Path("docs/rdr")
        found = _preamble_find_rdr_file(scan_root, rdr) if scan_root.exists() else None
        if found is None:
            click.echo(f"nx rdr repeat: no RDR file for {rdr!r} under {scan_root}", err=True)
            sys.exit(2)
        path = found
    rdr_id = path.stem

    design = extract_design_section(path.read_text())
    if not design:
        click.echo(
            f"nx rdr repeat: {path} has no Technical Design / Proposed Design / Design section; "
            "nothing to repeat",
            err=True,
        )
        sys.exit(2)

    model_names = [m.strip() for m in models.split(",") if m.strip()]
    if len(model_names) != 2:
        click.echo("nx rdr repeat: --models needs exactly two model aliases", err=True)
        sys.exit(2)
    if model_names[0] == model_names[1]:
        click.echo(
            f"nx rdr repeat: both models are {model_names[0]!r}; a repeatability diff needs two readers",
            err=True,
        )
        sys.exit(2)
    models_resolved = model_names

    dispatch = _resolve_repeat_dispatch()
    prompt = build_prompt(rdr_id, design)

    async def _run():
        return await asyncio.gather(
            *(
                dispatch(
                    prompt,
                    PLAN_SCHEMA,
                    timeout=timeout,
                    model=m,
                    max_budget_usd=max_budget_usd,
                    operator="rdr_repeat",
                    isolated=True,
                )
                for m in models_resolved
            )
        )

    try:
        payloads = asyncio.run(_run())
        plans = [parse_plan(m, p) for m, p in zip(models_resolved, payloads, strict=True)]
    except (RepeatError, Exception) as exc:  # noqa: BLE001 - report, never traceback
        click.echo(f"nx rdr repeat: dispatch failed ({exc})", err=True)
        sys.exit(1)

    divergence = diff_plans(plans[0], plans[1])
    if as_json:
        click.echo(
            _json.dumps(
                {
                    "rdr": rdr_id,
                    "plans": [
                        {
                            "model": pl.model,
                            "steps": [
                                {"title": st.title, "files": list(st.files), "decisions": list(st.decisions)}
                                for st in pl.steps
                            ],
                        }
                        for pl in plans
                    ],
                    "divergence": {
                        k: v for k, v in divergence.__dict__.items()
                    }
                    | {"count": divergence.count},
                },
                indent=2,
            )
        )
        return
    click.echo(render_report(rdr_id, plans[0], plans[1], divergence))


@rdr.command("set-status")
@click.argument("rdr_id")
@click.argument("new_status")
@click.option(
    "--date",
    default=None,
    help="Date for accepted_date/closed_date (default: today, UTC).",
)
@click.option(
    "--root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Repo root (default: git toplevel / cwd).",
)
def set_status(
    rdr_id: str, new_status: str, date: str | None, root: Path | None
) -> None:
    """Flip an RDR's file frontmatter status (and README index row).

    The code-enforced half of the accept/close lifecycle: the skills call this
    instead of hand-editing frontmatter, closing the ledger-drift class where
    T2 was advanced to ``accepted``/``closed`` but the RDR file stayed ``draft``
    (RDR-165 / RDR-166).

    RDR-201 P1.4: the requested *new_status* is resolved to the packaged
    ``rdr-lifecycle`` state-machine table's ``event`` dimension (derived
    from the table itself, see :func:`_target_status_to_event`, RDR-201
    P1.5); the file's current frontmatter status binds ``status``. An
    illegal edge REFUSES with the table row's typed
    reason (``illegal-transition`` / ``gate-not-passed`` /
    ``successor-not-named``) instead of the old unconditional flip. The
    table is missing or unparsable -> exit 2, no fallback to a hardcoded
    list. Re-requesting the CURRENT status is a no-op (exit 0) for every
    status, not only ``draft`` — ``rdr-accept``'s self-heal and repeated
    ``rdr-close`` runs depend on this command being idempotent.

    ``open`` is a retired status word, but ``nx rdr preamble rdr-accept``
    still advertises it as a live pre-accept synonym for ``draft``
    (nexus-qsryj). A file whose current status is ``open`` is read as
    ``draft`` for the purpose of this resolution (never written back —
    only the requested *new_status* is ever written to the file); one
    line notes the alias so it is visible, not silent.

    ``gate`` is read from T2 (project ``<repo>_rdr``, title
    ``<id>-gate-latest``, using the WORKTREE-STABLE repo basename — see
    :func:`_gate_repo_name`) ONLY when the resolved event is ``accept``
    AND the (possibly alias-normalized) current status is ``draft`` —
    every other (status, event) pair that maps to ``accept`` is already
    illegal by the table's ``accept-otherwise`` escape row regardless of
    ``gate``, so no T2 round-trip happens for those, and no
    T2-unreachable/no-record note is ever appended to a refusal that
    never consulted T2. See :func:`_gate_outcome_for`. T2 unreachable, or
    no gate record yet, both reduce to ``gate="none"``, which the table
    refuses as ``gate-not-passed`` (never a silent pass).
    """
    new_status = new_status.strip().lower()

    try:
        table = load_packaged_table("rdr-lifecycle.toml")
    except (OSError, TableLoadError, tomllib.TOMLDecodeError) as exc:
        click.echo(
            f"cannot load the RDR lifecycle table: {type(exc).__name__}: {exc}",
            err=True,
        )
        sys.exit(2)

    status_domain = frozenset(table.dimensions["status"].domain)
    if new_status not in status_domain:
        click.echo(
            f"unknown status '{new_status}'. Valid statuses: "
            f"{', '.join(sorted(status_domain))}",
            err=True,
        )
        sys.exit(2)

    if root is not None:
        repo_root = str(root)
    else:
        repo_root, _ = _preamble_resolve_repo()
    rdr_dir = _preamble_rdr_dir(repo_root)
    rdr_path = Path(repo_root) / rdr_dir

    rdr_file = _preamble_find_rdr_file(rdr_path, rdr_id)
    if rdr_file is None:
        click.echo(f"RDR not found for ID: {rdr_id} (in {rdr_path})", err=True)
        sys.exit(1)

    meta, _ = _preamble_parse_frontmatter(rdr_file)
    current_status = str(meta.get("status") or "").strip().lower()

    # `open` is retired from the table's domain but still a live pre-accept
    # synonym for `draft` elsewhere in this file (rdr-accept preamble,
    # nexus-qsryj). Normalize for resolution only — new_status (what gets
    # WRITTEN) is never touched here. Named explicitly, not silently.
    if current_status == _OPEN_STATUS_ALIAS:
        click.echo(f"{rdr_file.name}: treating current status 'open' as 'draft' (pre-accept synonym)")
        current_status = "draft"

    # Re-requesting the status the record already carries is a no-op for
    # EVERY status (not only draft) — rdr-accept's self-heal and repeated
    # rdr-close runs depend on set-status being idempotent (Sam,
    # 2026-09-02). This also resolves draft's own ambiguity: draft ->
    # draft is caught here before `event` is ever computed.
    if new_status == current_status:
        click.echo(f"{rdr_file.name} is already {new_status} (no-op)")
        return
    target_status_to_event = _target_status_to_event(table)
    event = "resume" if new_status == "draft" else target_status_to_event[new_status]

    superseded_by = str(meta.get("superseded_by") or "").strip()

    # Only `accept` FROM `draft` ever consults the table's `gate` guard
    # (accept's other match rows are all escape/illegal-transition and
    # never reference `gate`) — consulting T2 for any other (status,
    # event) would cost a live round-trip for a refusal it can't affect,
    # and would attach a misleading "T2 unreachable" tail to an
    # illegal-transition refusal that never touched T2 (code review, T2
    # nexus/code-review-nexus-j9z30-4-2026-09-01 [24033] finding 3).
    gate_value = "none"
    gate_note: str | None = None
    if event == "accept" and current_status == "draft":
        rdr_num_match = re.search(r"\d+", rdr_file.stem)
        rdr_num = rdr_num_match.group(0) if rdr_num_match else rdr_file.stem
        gate_value, gate_note = _gate_outcome_for(rdr_num, _gate_repo_name(repo_root))

    assignment = {
        "status": current_status,
        "event": event,
        "gate": gate_value,
        "successor": "named" if superseded_by else "absent",
    }

    resolution = resolve(table, assignment)
    if resolution.refusal is not None:
        # Evaluator-level defect (unknown-value / ambiguous-match / no-match):
        # the checker's lint bucket should have made this unreachable for a
        # well-formed table; treat it as a defect, not a business refusal.
        click.echo(
            f"cannot resolve transition for {rdr_file.name}: "
            f"{resolution.refusal} {dict(resolution.detail)}",
            err=True,
        )
        sys.exit(2)

    row = resolution.row
    assert row is not None  # exactly one of row/refusal is set (Resolution invariant)
    if row.outcome_kind == "refuse":
        msg = (
            f"{rdr_file.name}: refused ({row.outcome}) for "
            f"(status={current_status!r}, event={event!r})"
        )
        if gate_note:
            msg += f" — {gate_note}"
        click.echo(msg, err=True)
        sys.exit(1)

    if date is None:
        date = datetime.now(timezone.utc).date().isoformat()

    text = rdr_file.read_text(encoding="utf-8")
    try:
        new_text = _rewrite_frontmatter_status(text, new_status, date)
    except ValueError as exc:
        click.echo(f"cannot set status on {rdr_file.name}: {exc}", err=True)
        sys.exit(1)

    if new_text != text:
        rdr_file.write_text(new_text, encoding="utf-8")

    # A supersede transition's ONLY on-disk record of the successor is
    # this README cell (the frontmatter's own `superseded_by` lives in the
    # FILE, not the index) — decorate it rather than writing a bare
    # "Superseded" (code review, T2
    # nexus/critique-nexus-j9z30-4-2026-09-01 [24034] finding 9).
    # `superseded_by` is guaranteed non-empty here: a supersede transition
    # with it empty would already have refused successor-not-named above.
    readme_label = (
        f"Superseded by {superseded_by}" if new_status == "superseded" else new_status.capitalize()
    )

    readme = rdr_path / "README.md"
    readme_updated = _update_readme_status_row(
        readme, rdr_file.name, readme_label, status_domain
    )

    click.echo(f"set {rdr_file.name} status -> {new_status}")
    if readme_updated:
        click.echo(f"updated README index row -> {readme_label}")
    else:
        click.echo("README index row not found (skipped)", err=True)

    # RDR-201 P3.3 (nexus-j9z30.22): decisions get memory across amendment.
    # Report-only; the flip above is already on disk whatever happens here.
    flipped_num_match = re.search(r"\d+", rdr_file.stem)
    if flipped_num_match:
        repo_name = _gate_repo_name(repo_root)
        t2_title, t2_note = _write_t2_status(repo_name, int(flipped_num_match.group(0)), new_status, date)
        if t2_title:
            click.echo(f"updated T2 {repo_name}_rdr/{t2_title} status -> {new_status}")
        if t2_note:
            click.echo(t2_note, err=True)
        if new_status == "superseded":
            edge, edge_note = _ensure_supersedes_edge(
                _catalog_reader_factory(), repo_root, int(flipped_num_match.group(0)), superseded_by,
            )
            if edge:
                click.echo(f"catalog edge ensured: {edge}")
            if edge_note:
                click.echo(f"catalog edge not written: {edge_note}", err=True)
        marked, missing, notes = _mark_dependents_needs_reexamination(
            int(flipped_num_match.group(0)), current_status, new_status, repo_root, repo_name,
        )
        for title in marked:
            click.echo(f"marked {repo_name}_rdr/{title} {NEEDS_REEXAMINATION_FIELD} (supersedes edge)")
        for number in missing:
            click.echo(
                f"dependent RDR {number} has no T2 entry in {repo_name}_rdr -- not marked",
                err=True,
            )
        for note in notes:
            click.echo(f"dependents not marked: {note}", err=True)


# ---------------------------------------------------------------------------
# preamble subgroup (RDR-130 P1.2)
# ---------------------------------------------------------------------------

@rdr.group("preamble")
def preamble() -> None:
    """RDR lifecycle preamble subcommands (nx rdr preamble <name>)."""


# ---------------------------------------------------------------------------
# preamble rdr-list
# ---------------------------------------------------------------------------

@preamble.command("rdr-list")
@click.argument("args", nargs=-1)
def preamble_rdr_list(args: tuple[str, ...]) -> None:
    """List all RDRs (T2 primary, file fallback)."""
    repo_root, repo_name = _preamble_resolve_repo()
    rdr_dir = _preamble_rdr_dir(repo_root)
    rdr_path = Path(repo_root) / rdr_dir

    print(f"**Repo:** `{repo_name}`  **RDR directory:** `{rdr_dir}`")
    print()

    if not rdr_path.exists():
        print(f"> No RDRs found — `{rdr_dir}` does not exist in this repo.")
        return

    # Primary: read from T2
    rdrs = _preamble_get_rdrs_from_t2(repo_name, rdr_dir)
    source = "T2"

    # Fallback: read from files if T2 is empty
    if not rdrs:
        rdrs = _preamble_get_all_rdrs(rdr_path)
        source = "files"

    print(f"### RDRs ({len(rdrs)} found, source: {source})")
    print()
    if rdrs:
        print("| ID | Title | Status | Type | Priority |")
        print("|----|-------|--------|------|----------|")
        for r in rdrs:
            print(f"| {r['id']} | {r['title']} | {r['status']} | {r['rtype']} | {r['priority']} |")
    else:
        print(f"No RDRs found in `{rdr_dir}`")


# ---------------------------------------------------------------------------
# preamble rdr-create
# ---------------------------------------------------------------------------

@preamble.command("rdr-create")
@click.argument("args", nargs=-1)
def preamble_rdr_create(args: tuple[str, ...]) -> None:
    """Print context for creating a new RDR."""
    repo_root, repo_name = _preamble_resolve_repo()
    rdr_dir = _preamble_rdr_dir(repo_root)
    rdr_path = Path(repo_root) / rdr_dir

    print(f"**Repo:** `{repo_name}`  **RDR directory:** `{rdr_dir}`")
    print()

    if not rdr_path.exists():
        print(f"> RDR directory `{rdr_dir}` does not exist — bootstrap required.")
        print()
        print("**Next ID:** `RDR-001`")
        print("**ID style detected:** `RDR-NNN-kebab-title.md` (default — no existing files)")
        print()
        print("### Existing RDRs (0 found)")
        print()
        print("None — this will be the first RDR.")
    else:
        rdrs = _preamble_get_all_rdrs(rdr_path)

        # Detect ID style from existing files (case-insensitive for RDR- prefix)
        rdr_prefix_style = False
        numeric_style = False
        for r in rdrs:
            if re.match(r"^[Rr][Dd][Rr]-\d+", r["file"]):
                rdr_prefix_style = True
                break
            elif re.match(r"^\d+", r["file"]):
                numeric_style = True

        if rdr_prefix_style:
            id_style = "RDR-NNN-kebab-title.md"
        elif numeric_style:
            id_style = "NNN-kebab-title.md"
        else:
            id_style = "RDR-NNN-kebab-title.md"

        # Compute next sequential ID
        max_num = 0
        for r in rdrs:
            nums = re.findall(r"\d+", r["file"])
            if nums:
                max_num = max(max_num, int(nums[0]))
        next_num = max_num + 1

        if rdr_prefix_style:
            next_id = f"RDR-{next_num:03d}"
        else:
            next_id = f"{next_num:03d}"

        print(f"**Next ID:** `{next_id}`")
        print(f"**ID style detected:** `{id_style}`")
        print()
        print(f"### Existing RDRs ({len(rdrs)} found)")
        print()
        if rdrs:
            print("| File | Title | Status |")
            print("|------|-------|--------|")
            for r in rdrs:
                print(f"| {r['file']} | {r['title']} | {r['status']} |")
        else:
            print("None — this will be the first RDR.")

    print()

    # Active beads (for Related Issues field)
    print("### Active Beads (for Related Issues field)")
    try:
        result = subprocess.run(
            ["bd", "list", "--status=in_progress", "--limit=5"],
            capture_output=True, text=True, timeout=10,
        )
        bd_out = (result.stdout or "").strip()
        print(bd_out if bd_out else "No in-progress beads")
    except Exception as exc:  # noqa: BLE001 — optional beads integration; absence reported, command continues
        print(f"Beads not available: {exc}")
    print()


# ---------------------------------------------------------------------------
# preamble rdr-show
# ---------------------------------------------------------------------------

def _preamble_get_excerpt(text: str) -> str:
    """Strip frontmatter and return a 250-char content excerpt."""
    if text.startswith("---"):
        parts = text.split("---", 2)
        text = parts[2] if len(parts) >= 3 else text
    else:
        m = re.search(r"^## Metadata\s*\n.*?(?=^##)", text, re.MULTILINE | re.DOTALL)
        if m:
            text = text[m.end():]
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    return " ".join(lines)[:250]


@preamble.command("rdr-show")
@click.argument("args", nargs=-1)
def preamble_rdr_show(args: tuple[str, ...]) -> None:
    """Show RDR list or details for a specific RDR."""
    repo_root, repo_name = _preamble_resolve_repo()
    rdr_dir = _preamble_rdr_dir(repo_root)
    rdr_path = Path(repo_root) / rdr_dir
    args_str = " ".join(args).strip()

    print(f"**Repo:** `{repo_name}`  **RDR directory:** `{rdr_dir}`")
    print()

    if not rdr_path.exists():
        print(f"> No RDRs found — `{rdr_dir}` does not exist in this repo.")
        return

    if args_str:
        # Show specific RDR
        rdr_file = _preamble_find_rdr_file(rdr_path, args_str)
        if rdr_file:
            fm, text = _preamble_parse_frontmatter(rdr_file)
            print(f"### RDR: {rdr_file.name}")
            print()

            # Metadata table
            print("#### Metadata")
            print()
            print("| Field | Value |")
            print("|-------|-------|")
            for key in ("status", "type", "priority", "title", "author", "date",
                        "supersedes", "superseded-by"):
                val = fm.get(key)
                if val:
                    print(f"| {key.title()} | {val} |")
            print()

            # Full content
            print("#### Content")
            print()
            print(text)
            print()

            # T2 metadata (printed as instruction; no direct T2 read needed here)
            rdr_num = re.search(r"\d+", rdr_file.stem)
            t2_key = rdr_num.group(0) if rdr_num else rdr_file.stem
            print("### T2 Metadata")
            try:
                t2_result = subprocess.run(
                    ["nx", "memory", "get", "--project", f"{repo_name}_rdr",
                     "--title", t2_key],
                    capture_output=True, text=True, timeout=10,
                )
                t2_out = (t2_result.stdout or "").strip()
                print(t2_out if t2_out else f"No T2 record for RDR {t2_key}")
            except Exception as exc:  # noqa: BLE001 — optional T2 lookup; absence reported, command continues
                print(f"T2 not available: {exc}")
            print()

            # T2 research findings
            print("### T2 Research Findings")
            try:
                list_result = subprocess.run(
                    ["nx", "memory", "list", "--project", f"{repo_name}_rdr"],
                    capture_output=True, text=True, timeout=10,
                )
                list_out = (list_result.stdout or "").strip()
                # `nx memory list` rows are "[id] <project>/<title>  (…)" —
                # match the title after the project slash, not line start
                # (the ^-anchored form matched nothing, so every preamble
                # reported "No research findings recorded" while T2 held
                # them; caught on RDR-188, 2026-07-22).
                research_lines = [
                    ln for ln in list_out.splitlines()
                    if re.search(rf"/{t2_key}-research", ln)
                ]
                print("\n".join(research_lines) if research_lines
                      else "No research findings recorded")
            except Exception as exc:  # noqa: BLE001 — optional T2 lookup; absence reported, command continues
                print(f"T2 not available: {exc}")
            print()

            # Linked beads
            print("### Linked Beads")
            try:
                bd_result = subprocess.run(
                    ["bd", "list", "--status=open", "--limit=20"],
                    capture_output=True, text=True, timeout=10,
                )
                bd_out = (bd_result.stdout or "").strip()
                matching = [
                    ln for ln in bd_out.splitlines()
                    if re.search(rf"rdr.*{t2_key}|{t2_key}.*rdr", ln, re.IGNORECASE)
                ]
                print("\n".join(matching) if matching
                      else "No beads linked (check epic_bead in T2)")
            except Exception as exc:  # noqa: BLE001 — optional beads integration; absence reported, command continues
                print(f"Beads not available: {exc}")
        else:
            print(f"> RDR not found for: `{args_str}`")
            print()
            print("Available RDRs:")
            rdrs = _preamble_get_all_rdrs(rdr_path)
            if rdrs:
                print()
                print("| File | Title | Status | Type | Priority |")
                print("|------|-------|--------|------|----------|")
                for r in rdrs:
                    print(f"| {r['file']} | {r['title']} | {r['status']} | {r['rtype']} | {r['priority']} |")
    else:
        # No ID — show list (most recently modified first)
        all_md = [f for f in rdr_path.glob("*.md")
                  if f.name.lower() not in _PREAMBLE_EXCLUDED]
        all_md_sorted = sorted(all_md, key=lambda f: f.stat().st_mtime, reverse=True)

        rdrs = []
        for f in all_md_sorted:
            fm, text = _preamble_parse_frontmatter(f)
            rtype = fm.get("type", "?")
            doc_status = fm.get("status", "?")
            if doc_status == "?" and rtype == "?":
                continue
            rdrs.append({
                "file": f.name,
                "path": f,
                "text": text,
                "title": fm.get("title", fm.get("name", f.stem)),
                "status": doc_status,
                "rtype": rtype,
                "priority": fm.get("priority", "?"),
            })

        print(f"### RDR Files ({len(rdrs)} found, most recently modified first)")
        print()
        if rdrs:
            print("| File | Title | Status | Type | Priority |")
            print("|------|-------|--------|------|----------|")
            for r in rdrs:
                print(f"| {r['file']} | {r['title']} | {r['status']} | {r['rtype']} | {r['priority']} |")
            print()
            print("### Content Index (for keyword and topic filtering)")
            print()
            for r in rdrs:
                excerpt = _preamble_get_excerpt(r["text"])
                print(f"**{r['file']}**: {excerpt}")
        else:
            print(f"No RDR files found in `{rdr_dir}`")


# ---------------------------------------------------------------------------
# preamble rdr-gate
# ---------------------------------------------------------------------------

@preamble.command("rdr-gate")
@click.argument("args", nargs=-1)
def preamble_rdr_gate(args: tuple[str, ...]) -> None:
    """Print RDR gate context (gap check + section structure)."""
    repo_root, repo_name = _preamble_resolve_repo()
    rdr_dir = _preamble_rdr_dir(repo_root)
    rdr_path = Path(repo_root) / rdr_dir
    args_str = " ".join(args).strip()

    print(f"**Repo:** `{repo_name}`  **RDR directory:** `{rdr_dir}`")
    print()

    if not rdr_path.exists():
        print(f"> No RDRs found — `{rdr_dir}` does not exist in this repo.")
        return

    id_match = re.search(r"\d+", args_str)

    if not id_match:
        print("> **Usage**: `nx rdr preamble rdr-gate <id>`")
        print()
        rdrs = _preamble_get_all_rdrs(rdr_path)
        print("### Available RDRs")
        print()
        if rdrs:
            print("| File | Title | Status | Type |")
            print("|------|-------|--------|------|")
            for r in rdrs:
                print(f"| {r['file']} | {r['title']} | {r['status']} | {r['rtype']} |")
        else:
            print(f"No RDRs found in `{rdr_dir}`")
        return

    rdr_file = _preamble_find_rdr_file(rdr_path, id_match.group(0))
    if not rdr_file:
        print(f"> RDR not found for ID: `{id_match.group(0)}`")
        return

    fm, text = _preamble_parse_frontmatter(rdr_file)
    title = fm.get("title", fm.get("name", rdr_file.stem))
    rdr_num = re.search(r"\d+", rdr_file.stem)
    t2_key = rdr_num.group(0) if rdr_num else rdr_file.stem

    print(f"### RDR File: {rdr_file.name}")
    print(f"**Title:** {title}  **Status:** {fm.get('status', '?')}  **Type:** {fm.get('type', '?')}")
    print()

    def _strip_code_blocks(src: str) -> str:
        return re.sub(r"```.*?```", "", src, flags=re.DOTALL)

    # Gap-structure pre-check for post-65 RDRs
    _skip_gaps = "--skip-gaps" in args_str
    _problem_idx = text.find("## Problem Statement")
    if _problem_idx == -1:
        _problem_idx = text.find("## Problem")
    _problem_section = ""
    if _problem_idx != -1:
        _rest = text[_problem_idx:]
        _nxt = re.search(r"\n## ", _rest[1:])
        _problem_section = _rest[:_nxt.start() + 1] if _nxt else _rest
    _gap_headings = re.findall(
        r"^#{3,5} Gap (\d+)([^\n:]*):\s*(.*)$", _problem_section, re.MULTILINE
    )
    try:
        _rdr_id_int = int(t2_key)
    except ValueError:
        _rdr_id_int = -1

    if _rdr_id_int >= 65 and len(_gap_headings) == 0 and not _skip_gaps:
        print(
            f"> **BLOCKED** (Layer 1 — gap structure): RDR-{t2_key} has no "
            f"`#### Gap N: <title>` headings in `## Problem Statement` or `## Problem`."
        )
        print(r"> Expected format: `#### Gap 1: <gap title>` (regex: `^#{3,5} Gap \d+:`).")
        print(">")
        print(
            "> The close skill enforces the same structure and will block closing. "
            "Add the headings now before accept, or re-run the gate "
            "with `--skip-gaps` to record an intentional override."
        )
        return
    elif _rdr_id_int >= 65 and len(_gap_headings) > 0:
        print(f"#### Gap structure: {len(_gap_headings)} gap heading(s) present")
        print()
        for _num, _qual, _title in _gap_headings:
            _qual_str = _qual.strip()
            _qual_disp = f" {_qual_str}" if _qual_str else ""
            print(f"- Gap{_num}{_qual_disp}: {_title.strip()}")
        print()
    elif _rdr_id_int < 65 and len(_gap_headings) == 0:
        print(
            f"> **Note**: RDR-{t2_key} predates the gap-structure convention (id < 65) — "
            "skipping the Layer 1 gap check."
        )
        print()

    # nexus-7vdf9: a re-gate after a BLOCKED round leads with the prior
    # findings and the diff since the gated commit.
    for _line in _preamble_regate_block(
        repo_root=repo_root, repo_name=repo_name, t2_key=t2_key, rdr_file=rdr_file,
        status=str(fm.get("status", "")),
    ):
        print(_line)

    clean = _strip_code_blocks(text)

    # Section headings
    headings = re.findall(r"^(#{1,3} .+)", clean, re.MULTILINE)
    print("#### Section Structure (for completeness check)")
    print()
    for h in headings:
        print(h)
    print()

    # Section summaries
    print("#### Section Summaries")
    print()
    sections = re.split(r"^(## .+)", clean, flags=re.MULTILINE)
    for i in range(1, len(sections) - 1, 2):
        heading = sections[i].strip()
        body = sections[i + 1]
        first_lines = [ln.strip() for ln in body.splitlines()
                       if ln.strip() and not ln.strip().startswith("#")]
        summary = first_lines[0][:120] if first_lines else "_empty_"
        print(f"**{heading}**: {summary}")
    print()

    # T2 metadata (instruction only)
    print("### T2 Metadata")
    print(
        f"Use **memory_get** tool: project=\"{repo_name}_rdr\", title=\"{t2_key}\" "
        "to retrieve T2 metadata."
    )
    print()

    # T2 research findings (instruction only)
    print("### T2 Research Findings")
    print(
        f"Use **memory_get** tool: project=\"{repo_name}_rdr\", title=\"\" "
        f"to list all entries, then filter for {t2_key}-research* titles."
    )
    print(
        f"If no research findings exist, run `nx rdr preamble rdr-research -- {t2_key}` "
        "to record findings before gating."
    )


# ---------------------------------------------------------------------------
# preamble rdr-accept
# ---------------------------------------------------------------------------

_CRITIQUE_SECTION_RE = re.compile(r"^\s*#{1,3}\s*(critical|significant)\b", re.IGNORECASE)
_CRITIQUE_ISSUE_RE = re.compile(r"^\s*#{1,6}\s*issue:\s*(.+)$", re.IGNORECASE)
_CRITIQUE_DETAIL_RE = re.compile(r"^\s*[-*]\s*\*{0,2}(location|problem|recommendation|issue|sites)\*{0,2}\s*:\s*(.+)$", re.IGNORECASE)
# A free-form finding opens with the severity and then a number, a colon, a
# bold close or the word "issue"; "Critical mass of the aspect queue" and
# "Significant prior art exists" are prose (deep critique [24873] S1).
_CRITIQUE_INLINE_RE = re.compile(
    r"^[-*#\s]*(?:\*\*)?(?:new\s+)?(?:critical|significant)(?:\s+issue)?(?:\s*\d+)?\s*(?::|\*\*|$)",
    re.IGNORECASE,
)


def _critique_findings(text: str) -> list[str]:
    """Extract the Critical and Significant findings from a critique body.

    Two formats are read. The substantive-critic's canonical output
    (``## Critical Issues`` / ``## Significant Issues`` sections holding
    ``### Issue: <title>`` blocks with ``- **Location**:`` and
    ``- **Recommendation**:`` details) yields one line per issue title plus
    its location and recommendation. Free-form critiques yield any line
    that opens with Critical, Significant, NEW CRITICAL, or ``Issue:``.
    Observations and other sections are never findings. Empty input, or a
    critique with none of either, yields ``[]``.
    """
    out: list[str] = []
    in_finding_section = False
    saw_sections = False
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if re.match(r"^\s*#{1,3}\s+\S", line) and not _CRITIQUE_ISSUE_RE.match(line):
            m = _CRITIQUE_SECTION_RE.match(line)
            in_finding_section = m is not None
            if m is not None:
                saw_sections = True
            continue
        if in_finding_section:
            if stripped.lower() in ("none.", "none"):
                continue
            m = _CRITIQUE_ISSUE_RE.match(line)
            if m:
                out.append(f"Issue: {m.group(1).strip()}")
                continue
            d = _CRITIQUE_DETAIL_RE.match(line)
            if d and d.group(1).lower() in ("location", "recommendation", "issue", "sites"):
                out.append(f"  {d.group(1).capitalize()}: {d.group(2).strip()}")
                continue
    if saw_sections:
        return out
    # Free-form fallback.
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if _CRITIQUE_INLINE_RE.match(stripped) or re.match(r"^(?:\*\*)?issue:", stripped, re.IGNORECASE):
            out.append(stripped.lstrip("-*# ").strip())
    return out


def _finding_title_key(text: str) -> str:
    """Normalise a finding or residual title for cross-surface matching.

    ``_critique_findings`` prefixes a canonical title with ``Issue: ``; a
    gate record's ``residuals:`` bullet carries the bare title with
    neither that prefix nor list decoration. One normalisation — strip the
    ``Issue:`` prefix, strip leading/trailing list and markdown
    decoration, collapse whitespace, casefold — so a residual's stored
    title and a finding's title match the same way everywhere a surface
    needs to tell them apart: the fix preamble's ship-blocker/residual
    split (nexus-yjf5l.2) and the Layer 0 survivor sweep's residual
    exemption (nexus-yjf5l.3) both call this one function, not their own
    copy.
    """
    t = text.strip()
    t = re.sub(r"^(?:issue\s*:\s*)", "", t, flags=re.IGNORECASE)
    t = t.lstrip("-*# ").rstrip("*").strip()
    t = re.sub(r"\s+", " ", t)
    return t.casefold()


def _preamble_regate_block(
    *, repo_root: str, repo_name: str, t2_key: str, rdr_file: Path, status: str = "",
) -> list[str]:
    """Lines for the re-gate block of ``nx rdr preamble rdr-gate`` (nexus-7vdf9).

    When the RDR's ``<id>-gate-latest`` record is BLOCKED, the previous
    round's findings are the first thing the author and the critic need:
    RDR-204 went through four gates, and rounds three and four were blocked
    on sentences that restated facts already fixed elsewhere in the same
    file, because the fix was applied at the quoted line and nothing
    surfaced the other occurrences. This block prints the prior outcome,
    the critique's Critical and Significant lines verbatim, the diff of
    the RDR file since the gated commit (when the record carries
    ``commit:``), and the Layer 0 survivor-sweep instruction.

    Returns ``[]`` only when there is no gate record (a first gate). The
    block fires after a PASSED gate too (nexus-g7zgw.4): of RDR-204's four
    rounds that introduced new Criticals (passes 3, 4, 7 and 9), the two
    after the design had stabilised (7 and 9) were fixes authored against a
    PASSED gate's Significants, and the sweep was structurally off for
    them. It also carries the gate round number, derived from the record's
    ``prior:`` chain (nexus-g7zgw.2), and the Fix check section naming the
    exact diff range whenever the file changed since the gated commit
    (nexus-g7zgw.1). A T2 read failure returns a single named note rather
    than nothing, so an unreachable T2 is visible and never mistaken for
    "no prior round".
    """
    project = f"{repo_name}_rdr"
    try:
        with _t2_client_factory() as client:
            latest = client.get(project=project, title=f"{t2_key}-gate-latest")
            if not latest:
                return []
            content = latest.get("content", "") if isinstance(latest, dict) else ""
            outcome = (_preamble_parse_t2_field(content, "outcome") or "").strip().upper() or "?"
            critique_title = (_preamble_parse_t2_field(content, "critique") or "").strip()
            # Tolerate the "project/title [id]" form the skill writes into the pointer.
            critique_title = re.sub(r"\s*\[\d+\]\s*$", "", critique_title)
            # The pointer may carry ANY project prefix ("nexus_rdr/<title>"); the
            # title is what T2 keys on within this project.
            critique_title = critique_title.rsplit("/", 1)[-1]
            gated_commit = (_preamble_parse_t2_field(content, "commit") or "").strip()
            fix_check_field = (_preamble_parse_t2_field(content, "fix_check") or "").strip()
            fix_check_exists: bool | None = None
            fc = re.search(r"fix-check-([0-9a-f]{6,40})", fix_check_field)
            if fc:
                fix_check_exists = client.get(project=project, title=f"{t2_key}-fix-check-{fc.group(1)}") is not None
            # Every gate round writes a critique record; their count is the
            # round count nobody retypes (deep critique [24873] Critical 1).
            critique_count = 0
            get_all = getattr(client, "get_all", None)
            if callable(get_all):
                prefix = f"{t2_key}-gate-critique-"
                critique_count = sum(
                    1 for row in (get_all(project=project) or [])
                    if isinstance(row, dict) and str(row.get("title", "")).startswith(prefix)
                )
            critique = None
            fetch_failed = False
            if critique_title:
                # Exact-then-prefix when the client offers it (a same-day
                # pointer written as "...-2026-09-07" resolves to "...-2026-09-07d"
                # only if unique); plain get otherwise.
                resolve = getattr(client, "resolve_title", None)
                if callable(resolve):
                    critique, _candidates = resolve(project=project, title=critique_title)
                else:
                    critique = client.get(project=project, title=critique_title)
                fetch_failed = critique is None
    except Exception as exc:  # noqa: BLE001 — T2 unreachable must be a visible note, never a silent "no prior round"
        return [
            f"> Re-gate check: T2 unreachable ({type(exc).__name__}: {exc}); the prior "
            "critique could not be loaded. Load it by hand before Layer 3.",
            "",
        ]

    lines = [f"### Re-gate: the previous gate was {outcome}", ""]
    date = _preamble_parse_t2_field(content, "date") or "?"
    summary = _preamble_parse_t2_field(content, "summary") or ""
    lines.append(f"Prior gate {date}: {outcome}. {summary}".rstrip())
    if critique_title:
        lines.append(f"Critique: `{project}/{critique_title}`")
    lines.append("")
    lines.extend(_gate_round_lines(content, critique_count))
    lines.extend(_fix_check_pointer_lines(
        fix_check_field, gated_commit,
        is_regate=bool(_t2_field_block(content, "prior")), record_exists=fix_check_exists,
    ))

    findings = _critique_findings(str(critique.get("content", ""))) if isinstance(critique, dict) else []
    # nexus-yjf5l.3: a finding recorded on the prior round's residuals: lines
    # was dispositioned at accept, not left open — it is not a survivor to
    # re-sweep. Match it out of the sweep list by the same normalisation
    # nexus-yjf5l.2's fix-preamble split uses, so the two never carry
    # separate copies of the comparison.
    residual_titles = _residual_titles(content)
    residual_keys = {_finding_title_key(t) for t in residual_titles}
    if findings:
        survivors: list[str] = []
        recorded: list[str] = []
        matched_keys: set[str] = set()
        is_residual = False
        for f in findings:
            if not f.startswith("  "):
                is_residual = _finding_title_key(f) in residual_keys
                if is_residual:
                    matched_keys.add(_finding_title_key(f))
            (recorded if is_residual else survivors).append(f)

        if recorded:
            lines.append("Recorded residuals (dispositioned at accept; not survivors — do not re-open):")
            lines.extend(f"- {f}" for f in recorded)
            lines.append("")
        unmatched = [t for t in residual_titles if _finding_title_key(t) not in matched_keys]
        for t in unmatched:
            lines.append(f"- Recorded residual, no matching finding in the critique: {t}")
        if unmatched:
            lines.append("")

        lines.append("Prior findings (each must be closed EVERYWHERE in the file, not at the quoted line):")
        # Cap on FINDINGS, not lines: a canonical issue is four lines (Issue,
        # Location, Recommendation, Sites) and a line cap dropped the Sites
        # lists Layer 0 sweeps (deep critique [24873] S2).
        shown = 0
        cut = len(survivors)
        for i, f in enumerate(survivors):
            if not f.startswith("  "):
                shown += 1
                if shown > _REGATE_MAX_FINDINGS:
                    cut = i
                    break
        if survivors:
            lines.extend(f"- {f}" for f in survivors[:cut])
        else:
            lines.append("(none — every finding this round matched the gate record's `residuals:` field)")
        hidden = sum(1 for f in survivors[cut:] if not f.startswith("  "))
        if hidden:
            lines.append(f"- ... and {hidden} more findings in the critique")
    elif fetch_failed:
        lines.append(f"The `critique:` pointer names `{critique_title}` but no such T2 record was found; "
                     "locate the prior critique by hand before Layer 3.")
    elif critique_title:
        lines.append("Prior critique loaded but no Critical/Significant findings were recognised; read it in full.")
    else:
        lines.append(
            "No `critique:` pointer in the gate record; find the prior critique by hand "
            f'(memory_get project="{project}" title="{t2_key}-gate-critique-*").'
        )
    lines.append("")

    if gated_commit:
        rel = os.path.relpath(str(rdr_file), repo_root)
        try:
            diff = subprocess.run(
                ["git", "-C", repo_root, "diff", "--stat", f"{gated_commit}..HEAD", "--", rel],
                capture_output=True, text=True, timeout=20, check=False,
            )
            if diff.returncode != 0:
                lines.append(
                    f"Changed since the gated commit `{gated_commit}`: unknown "
                    f"(git diff exited {diff.returncode}: {diff.stderr.strip()[:160]})"
                )
                lines.append("")
                lines.append(
                    f"Fix check: the gated commit `{gated_commit}` does not resolve, so the "
                    "diff to verify is unknown. Find the gated tree by hand (the critique "
                    "names its commit) before Layer 3."
                )
            else:
                stat = diff.stdout.strip().splitlines()
                lines.append(
                    f"Changed since the gated commit `{gated_commit}`: "
                    + (stat[-1].strip() if stat else "no changes to the RDR file")
                )
                lines.append("")
                if stat and status.strip().lower() not in ("", "draft", "open"):
                    # Post-accept edits (the status flip itself, residual
                    # dispositions) are not gate fixes (deep critique [24873]),
                    # so there is no re-gate to gate. The disposition still
                    # carries its own fix check (nexus-yjf5l.1) — saying only
                    # "not applicable" here contradicted rdr-accept.
                    lines.append(
                        f"Fix check: no re-gate fix check (RDR status is `{status.strip()}`; "
                        "that check gates a re-gate of a draft, and this RDR is past the "
                        "gate). A residual dispositioned by a change to the RDR file still "
                        f"carries a fix check on that change: `git diff {gated_commit}..HEAD "
                        f"-- {rel}`, verdict stored as `{t2_key}-fix-check-<sha>`, `<sha>` "
                        "the RDR file's tip after the disposition. A residual dispositioned "
                        "by a bead id changed nothing in the file and needs none."
                    )
                else:
                    lines.extend(_fix_check_lines(
                        repo_root=repo_root, t2_key=t2_key, rel=rel,
                        gated_commit=gated_commit, changed=bool(stat),
                    ))
        except (OSError, subprocess.SubprocessError) as exc:
            lines.append(f"Changed since the gated commit `{gated_commit}`: (git diff failed: {exc})")
        lines.append("")

    # The exemption clause names itself only when this round actually
    # recorded a residual — an unconditional clause would print on every
    # first-gate and every round-1/round-2 record too, and the no-residuals
    # regression pin (nexus-yjf5l.3) is byte-for-byte on that path.
    exempt_clause = (
        " that is not a recorded residual (dispositioned at accept, not swept as a survivor here)"
        if residual_keys else ""
    )
    lines.extend([
        "**Layer 0 (survivor sweep, before Layer 3):** for every prior finding"
        f"{exempt_clause}, sweep every "
        "site in its `Sites:` list; where a finding has none, grep the RDR for the refuted "
        "phrasing AND the corrected one; every occurrence must agree. A "
        "fact lives in Problem Statement, Research Findings, Technical Design and the "
        "Implementation Plan at once, and the last two are where survivors hide. Brief "
        "the critic to verify each prior finding closed everywhere, then run a full-document "
        "consistency pass; re-read related RDRs only if the Relationship section changed.",
        "",
    ])
    return lines


#: Prior findings printed in full by the re-gate block before "... and N more".
_REGATE_MAX_FINDINGS: int = 12

#: Gate rounds that may block on any Critical. From the next round on only
#: a ship-blocker blocks and everything else is a residual recorded for
#: accept (nexus-g7zgw.2). Derived from the review-rounds table
#: (nexus-dv7gw), the one statement of every review bound in this project.
GATE_MAX_ANY_CRITICAL_ROUNDS: int = blocking_rounds("rdr-gate", "any-critical")


def _t2_field_block(content: str, field: str) -> str:
    """The value of *field* including wrapped continuation lines: everything
    from ``field:`` up to the next ``word:`` line. ``_preamble_parse_t2_field``
    reads one line, which truncated a wrapped ``prior:`` chain to its first
    line and undercounted rounds (deep critique [24873] Critical 1)."""
    out: list[str] = []
    active = False
    for line in content.splitlines():
        stripped = line.strip()
        if active and re.match(r"^[A-Za-z_][A-Za-z0-9_]*:", stripped):
            break
        if stripped.startswith(f"{field}:"):
            out.append(stripped.split(":", 1)[1])
            active = True
        elif active:
            out.append(stripped)
    return " ".join(out).strip()


def _gate_round_lines(gate_record: str, critique_count: int = 0) -> list[str]:
    """The gate round number and its rule.

    Two sources, the larger wins: the number of ``{id}-gate-critique-*``
    records in T2 (*critique_count*, written by every gate round and never
    hand-retyped), and the record's ``prior:`` chain plus the record itself.
    The chain counts every ``[id]`` token whether or not it carries an
    outcome in parentheses; entries with no outcome word are tallied as
    unlabelled so the tally always sums. The count never resets for the
    RDR's life.
    """
    prior = _t2_field_block(gate_record, "prior")
    ids = re.findall(r"\[\d+\]", prior)
    labelled = re.findall(r"\[\d+\]\s*\(([^)]*)\)", prior)
    # A partition: an entry naming both words counts once, as BLOCKED.
    blocked = sum(1 for e in labelled if "BLOCKED" in e.upper())
    passed = sum(1 for e in labelled if "PASSED" in e.upper() and "BLOCKED" not in e.upper())
    this_outcome = (_preamble_parse_t2_field(gate_record, "outcome") or "").strip().upper()
    if this_outcome == "BLOCKED":
        blocked += 1
    elif this_outcome == "PASSED":
        passed += 1
    n_chain = 1 + len(ids)
    round_no = _gate_round_number(gate_record, critique_count)
    n_prior = round_no - 1
    unlabelled = n_prior - blocked - passed
    source = "critique records" if critique_count > n_chain else "prior chain"
    lines = [
        f"**Gate round {round_no}** (prior rounds: {n_prior} ({blocked} BLOCKED, {passed} PASSED, "
        f"{unlabelled} unlabelled; from the {source}); the count never resets for this RDR)."
    ]
    hand_typed = (_preamble_parse_t2_field(gate_record, "round") or "").strip()
    if hand_typed.isdigit() and int(hand_typed) != n_prior:
        lines.append(
            f"The record's hand-typed `round: {hand_typed}` disagrees with the derived count "
            f"({n_prior} rounds so far); the derived count is the one that applies. Drop the "
            "field or write the derived value."
        )
    if round_no > GATE_MAX_ANY_CRITICAL_ROUNDS:
        lines.append(
            f"From round {GATE_MAX_ANY_CRITICAL_ROUNDS + 1} only a ship-blocker blocks "
            "(`ship_blockers > 0` in the critic's Verdict; a Verdict with no `ship_blockers` "
            "line reads as `ship_blockers = critical_count`, never zero); every other Critical "
            "and Significant is a residual: record it in the gate record's `residuals:` lines "
            "and in Revision History, and disposition it at accept."
        )
    else:
        lines.append(
            f"Rounds 1 to {GATE_MAX_ANY_CRITICAL_ROUNDS} block on any Critical "
            "(`critical_count > 0`); Significants never block."
        )
    lines.append("")
    return lines


def _fix_check_pointer_lines(
    fix_check_field: str, gated_commit: str, *, is_regate: bool, record_exists: bool | None,
) -> list[str]:
    """Flag a gate record whose ``fix_check:`` is missing on a re-gate, names
    a sha other than its ``commit:``, or points at a T2 record that does not
    exist. Critique [24865] Critical 1 found the sha invariant prose-only and
    already violated live; deep critique [24873] Critical 3 found that
    omitting the field entirely was indistinguishable from a clean check.
    The field may carry the pointer form ``<project>/<id>-fix-check-<sha>
    (note)`` or the literal ``none (no change since <sha>)``.
    *record_exists* is None when the field named no sha."""
    if not fix_check_field:
        if is_regate:
            return [
                "**Fix check missing:** this gate record has a `prior:` chain, so it is a "
                "re-gate, and it carries no `fix_check:` field. Every re-gated record names "
                "either `{id}-fix-check-<sha>` (sha equal to `commit:`) or `none (no change "
                "since <sha>)`; a skipped fix check is not a clean one. Run the fix check on the "
                "current diff before Layer 3, and accept refuses this record until it is named.",
                "",
            ]
        return []
    if fix_check_field.lower().startswith("none"):
        return []
    if not gated_commit:
        return []
    m = re.search(r"fix-check-([0-9a-f]{6,40})", fix_check_field)
    sha = m.group(1) if m else fix_check_field.split()[0]
    shorter = min(len(sha), len(gated_commit))
    if not (shorter >= 7 and sha[:shorter] == gated_commit[:shorter]):
        return [
            f"**Fix check pointer mismatch:** the gate record's `fix_check:` names `{sha}` but "
            f"its `commit:` is `{gated_commit}`. The prior gate cited a fix check of an older "
            "tree; run the fix check on the current diff before Layer 3, and accept refuses "
            "this record until the two agree.",
            "",
        ]
    if record_exists is False:
        return [
            f"**Fix check record missing:** `fix_check:` names `{sha}` but no T2 record "
            f"`*-fix-check-{sha}` exists. The pointer is not evidence; the verdict is. Run "
            "the fix check and store its verdict before Layer 3.",
            "",
        ]
    return []


def _fix_check_lines(
    *, repo_root: str, t2_key: str, rel: str, gated_commit: str, changed: bool,
) -> list[str]:
    """The Fix check section (nexus-g7zgw.1): the exact diff range, the fix
    commits, the T2 title the verdict goes under, and the obligation.

    The T2 title carries the RDR file's tip sha, which is the sha the next
    gate record's ``commit:`` field will name, so a gate can never cite a
    fix check of an older tree.
    """
    if not changed:
        return [
            f"Fix check: not required (no change to the RDR file since `{gated_commit}`).",
        ]
    try:
        log = subprocess.run(
            ["git", "-C", repo_root, "log", "--format=%h %s", f"{gated_commit}..HEAD", "--", rel],
            capture_output=True, text=True, timeout=20, check=False,
        )
        tip = subprocess.run(
            ["git", "-C", repo_root, "log", "-1", "--format=%h", "--", rel],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [f"Fix check: git log failed ({exc}); list the fix commits by hand."]
    if log.returncode != 0 or tip.returncode != 0 or not tip.stdout.strip():
        err = (log.stderr or tip.stderr).strip()[:160]
        return [
            "### Fix check (required before Layer 1)",
            "",
            f"Range: `git diff {gated_commit}..HEAD -- {rel}`",
            f"The fix commits and the RDR file's tip sha could not be read (git log failed: {err}); "
            "read them by hand with `git log --format=%h %s` on that range before dispatching the "
            "fix check, and name the tip sha in the T2 title `{id}-fix-check-<sha>` yourself.",
        ]
    commits = [ln for ln in log.stdout.strip().splitlines() if ln.strip()]
    tip_sha = tip.stdout.strip()
    lines = [
        "### Fix check (required before Layer 1)",
        "",
        f"Range: `git diff {gated_commit}..HEAD -- {rel}`",
        "Fix commits:",
    ]
    lines.extend(f"- {c}" for c in commits)
    lines.extend([
        "",
        "Dispatch substantive-critic with ONLY that diff and the RDR file. For every ADDED "
        "or CHANGED clause (a parenthetical or a trailing 'and X' is its own item):",
        "1. Is it contradicted by any other line in this file? Cite both lines.",
        "2. Is it an attribution (X created / set / owns / defines Y), a count, or a universal "
        "(never / always / only / nothing / every / the one / all)? Then it needs an "
        "enumeration or the artifact's own text, quoted; two sites that agree are not a "
        "source. The same enumeration requirement applies to the research entry the fix cites.",
        "3. Does its cited source (changeset, file:line, RDR, T2 entry) contain the claim as stated?",
        "4. A `file:line` taken from a T3 search or query hit is a lead, not a citation: the "
        "store carries the line as of index time. The clause passes only when the line was "
        "re-read from the working tree.",
        "5. For every identifier whose meaning, bound, or owning phase this change alters (a "
        "column, a caller-supplied parameter, a typed error, a setting, a phase or step "
        "number), list every other occurrence in the file, and every check, bound or rule "
        "stated over the value it names under any other name, and say whether each still "
        "holds.",
        "",
        f"Verdict goes to T2 `{t2_key}-fix-check-{tip_sha}` (project `<repo>_rdr`); the gate "
        f"record's `fix_check:` must name `{tip_sha}`, equal to its `commit:`. Any FAIL: fix, "
        "re-run the fix check on the new diff. Do not enter Layer 1 or Layer 3 with a FAIL open. "
        "The fix check and the gate critique are never dispatched against the same commit in "
        "parallel: fix, then check, then Layer 1 and Layer 3.",
    ])
    return lines


@preamble.command("rdr-accept")
@click.argument("args", nargs=-1)
def preamble_rdr_accept(args: tuple[str, ...]) -> None:
    """Print RDR accept context and planning handoff."""
    repo_root, repo_name = _preamble_resolve_repo()
    rdr_dir = _preamble_rdr_dir(repo_root)
    rdr_path = Path(repo_root) / rdr_dir
    args_str = " ".join(args).strip()

    print(f"**Repo:** `{repo_name}`  **RDR directory:** `{rdr_dir}`")
    print()

    if not rdr_path.exists():
        print(f"> No RDRs found — `{rdr_dir}` does not exist in this repo.")
        return

    try:
        table = load_packaged_table("rdr-lifecycle.toml")
    except (OSError, TableLoadError, tomllib.TOMLDecodeError) as exc:
        print(f"> **ERROR**: cannot load the RDR lifecycle table: {type(exc).__name__}: {exc}")
        return
    # RDR-201 P1.5: derived from the table's `accept` event rows, not a
    # hand-maintained ("draft", "open") literal (T2
    # nexus/plan-rdr-201-audit-round-3-residuals [23999] item 2). `open` is
    # retired from the table's domain but stays a live pre-accept synonym for
    # `draft` here (GH #1409, nexus-qsryj).
    pre_accept_statuses = _from_statuses_for_event(table, "accept") | {_OPEN_STATUS_ALIAS}
    accept_target_status = _to_status_for_event(table, "accept")

    id_match = re.search(r"\d+", args_str)

    if not id_match:
        print("> **Usage**: `nx rdr preamble rdr-accept <id>`")
        print()
        rdrs = _preamble_get_all_rdrs(rdr_path)
        # GH #1409 (nexus-qsryj): `open` is an accepted pre-accept synonym for
        # `draft` — some projects' RDR conventions (open -> accepted) never use
        # draft at all; the rdr-gate PASSED check is the real acceptance guard.
        draft_rdrs = [r for r in rdrs if r["status"].lower() in pre_accept_statuses]
        print("### Draft RDRs (eligible for acceptance)")
        print()
        if draft_rdrs:
            print("| File | Title | Status | Type |")
            print("|------|-------|--------|------|")
            for r in draft_rdrs:
                print(f"| {r['file']} | {r['title']} | {r['status']} | {r['rtype']} |")
        else:
            print("No draft/open RDRs found. Only pre-accept (draft or open) RDRs can be accepted.")
        return

    rdr_file = _preamble_find_rdr_file(rdr_path, id_match.group(0))
    if not rdr_file:
        print(f"> RDR not found for ID: `{id_match.group(0)}`")
        return

    fm, text = _preamble_parse_frontmatter(rdr_file)
    title = fm.get("title", fm.get("name", rdr_file.stem))
    current_status = fm.get("status", "?")
    rdr_type = fm.get("type", "?")
    rdr_num = re.search(r"\d+", rdr_file.stem)
    t2_key = rdr_num.group(0) if rdr_num else rdr_file.stem

    print(f"### RDR: {rdr_file.name}")
    print(
        f"**RDR ID:** {t2_key}  **Title:** {title}  "
        f"**Type:** {rdr_type}  **File Status:** {current_status}"
    )
    print()

    # Accepted status is allowed — agent handles idempotency. `open` is a
    # pre-accept synonym for `draft` (GH #1409, nexus-qsryj): the rdr-gate
    # PASSED lookup below is the real acceptance guard, not the status word.
    # RDR-201 P1.5: derived from the table (pre-accept statuses plus the
    # `accept` event's own target status), not a hand-maintained literal.
    if current_status.lower() not in pre_accept_statuses | {accept_target_status}:
        print(
            f"> **BLOCKED**: RDR status is `{current_status}`. "
            "Only pre-accept (draft or open) RDRs can be accepted."
        )
        return

    # T2 lookups — printed as instructions
    print("### T2 Lookups (call these before executing Action steps)")
    print()
    print(
        f"1. **T2 metadata**: Use **memory_get** tool: "
        f"project=\"{repo_name}_rdr\", title=\"{t2_key}\""
    )
    print(
        f"2. **T2 gate result**: Use **memory_get** tool: "
        f"project=\"{repo_name}_rdr\", title=\"{t2_key}-gate-latest\""
    )
    print(
        f"   If no gate record exists, run `nx rdr preamble rdr-gate -- {t2_key}` first."
    )
    print(
        "   Every `residuals:` line in that record needs a disposition (the commit sha "
        "that fixed it, or the bead id that carries it) recorded in Revision History "
        "before the T2 write; a residual with no disposition blocks accept (nexus-g7zgw.2)."
    )
    print(
        "   A residual dispositioned by a change to the RDR file carries a fix check on "
        f"that change: read `git diff <the record's commit:>..HEAD -- "
        f"{os.path.relpath(str(rdr_file), repo_root)}` and store the verdict as "
        f"`{t2_key}-fix-check-<sha>` (project `{repo_name}_rdr`), `<sha>` the RDR file's "
        "tip after the disposition. A residual dispositioned by a bead id changed nothing "
        "in the file and needs none (nexus-yjf5l.1)."
    )
    print()
    print(f"**RDR file path:** `{rdr_file}`")
    print()
    print("### Flip the file frontmatter (code-enforced — do NOT hand-edit)")
    print()
    print(
        "After the T2 write, run this to flip the RDR file frontmatter + README "
        "index row atomically (closes the RDR-165/166 ledger-drift class where "
        "T2 advanced but the file stayed `draft`):"
    )
    print()
    print(f"    nx rdr set-status {t2_key} accepted")
    print()

    # Step count auto-detection
    plan_headers = [
        r"^## Implementation Plan",
        r"^## Approach",
        r"^## Plan",
        r"^## Design",
        r"^## Steps",
        r"^## Execution",
    ]
    plan_section = None
    for hdr in plan_headers:
        m = re.search(hdr + r"\s*\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
        if m:
            plan_section = m.group(1)
            break

    step_count = 0
    has_plan = plan_section is not None
    if has_plan:
        step_count = len(re.findall(
            r"^### (?:Phase|Step|Stage|Part)\s", plan_section, re.MULTILINE
        ))
        if step_count == 0:
            step_count = len(re.findall(r"^### \d", plan_section, re.MULTILINE))
        if step_count == 0:
            # nexus RDR convention: numbered implementation steps live as a
            # top-level numbered list directly under ## Approach (no ###
            # subheadings). Count only top-level items (no leading indent) so
            # nested/prose-numbered sub-lists are not double-counted.
            step_count = len(re.findall(r"^\d+\.\s", plan_section, re.MULTILINE))

    print("### Planning Handoff")
    print(f"**Step count detected:** {step_count}")
    print(f"**Has plan section:** {'yes' if has_plan else 'no'}")
    if step_count >= 2:
        print("**Recommendation:** Invoke strategic planner (multi-step RDR)")
        print("**Default:** yes")
    else:
        print("**Recommendation:** Invoke strategic planner")
        print("**Default:** yes")
    print()


# ---------------------------------------------------------------------------
# preamble rdr-close
# ---------------------------------------------------------------------------

@preamble.command("rdr-close")
@click.argument("args", nargs=-1)
def preamble_rdr_close(args: tuple[str, ...]) -> None:
    """Print RDR close context (gap check + T2 metadata instructions)."""
    repo_root, repo_name = _preamble_resolve_repo()
    rdr_dir = _preamble_rdr_dir(repo_root)
    rdr_path = Path(repo_root) / rdr_dir
    args_str = " ".join(args).strip()

    print(f"**Repo:** `{repo_name}`  **RDR directory:** `{rdr_dir}`")
    print()

    if not rdr_path.exists():
        print(f"> No RDRs found — `{rdr_dir}` does not exist in this repo.")
        return

    # Strip flags from args before extracting ID
    reason_match = re.search(r"--reason\s+(\S+)", args_str)
    close_reason = reason_match.group(1) if reason_match else None
    force = bool(re.search(r"--force(?!-)", args_str))
    pointers_match = (
        re.search(r"--pointers\s+'([^']+)'", args_str)
        or re.search(r'--pointers\s+"([^"]+)"', args_str)
        or re.search(r"--pointers\s+(\S+)", args_str)
    )
    pointers_arg = pointers_match.group(1) if pointers_match else None
    force_implemented_match = (
        re.search(r"--force-implemented\s+'([^']*)'", args_str)
        or re.search(r'--force-implemented\s+"([^"]*)"', args_str)
        or re.search(r"--force-implemented\s+(\S+)", args_str)
    )
    force_implemented_reason = (
        force_implemented_match.group(1) if force_implemented_match else None
    )
    # S1: guard — --force-implemented requires a non-empty reason string (original rdr_close.py:133-137).
    # Detect the flag either via regex match OR by presence in the raw args tuple (CLI path where
    # an empty-string arg won't produce a \S+ regex match but the flag token is still present).
    _force_impl_flag_present = (
        force_implemented_match is not None
        or "--force-implemented" in args
    )
    if _force_impl_flag_present and not (force_implemented_reason or "").strip():
        print("> **ERROR**: `--force-implemented` requires a non-empty reason string.")
        print(
            "> Example: `nx rdr preamble rdr-close 069 --reason implemented"
            " --force-implemented 'critic false positive — gap addressed at src/foo.py:42'`"
        )
        return

    args_clean = re.sub(r"--reason\s+\S+", "", args_str)
    args_clean = re.sub(r"--force-implemented\s+'[^']*'", "", args_clean)
    args_clean = re.sub(r'--force-implemented\s+"[^"]*"', "", args_clean)
    args_clean = re.sub(r"--force-implemented\s+\S+", "", args_clean)
    args_clean = re.sub(r"--force(?!-)", "", args_clean)
    args_clean = re.sub(r"--pointers\s+'[^']+'", "", args_clean)
    args_clean = re.sub(r'--pointers\s+"[^"]+"', "", args_clean)
    args_clean = re.sub(r"--pointers\s+\S+", "", args_clean).strip()

    id_match = re.search(r"\d+", args_clean)

    if not id_match:
        print("> **Usage**: `nx rdr preamble rdr-close <id> [--reason implemented|...]`")
        print()
        rdrs = _preamble_get_all_rdrs(rdr_path)
        print("### Open/Draft RDRs")
        print()
        if rdrs:
            print("| File | Title | Status | Type |")
            print("|------|-------|--------|------|")
            for r in rdrs:
                print(f"| {r['file']} | {r['title']} | {r['status']} | {r['rtype']} |")
        else:
            print(f"No RDRs found in `{rdr_dir}`")
        return

    rdr_file = _preamble_find_rdr_file(rdr_path, id_match.group(0))
    if not rdr_file:
        print(f"> RDR not found for ID: `{id_match.group(0)}`")
        return

    fm, text = _preamble_parse_frontmatter(rdr_file)
    title = fm.get("title", fm.get("name", rdr_file.stem))
    current_status = fm.get("status", "?")
    rdr_num = re.search(r"\d+", rdr_file.stem)
    t2_key = rdr_num.group(0) if rdr_num else rdr_file.stem

    print(f"### RDR: {rdr_file.name}")
    print(f"**Title:** {title}  **Current Status:** {current_status}")
    if close_reason:
        print(f"**Close Reason:** {close_reason}")
    if force_implemented_reason:
        print(f"**Force Implemented (audit):** {force_implemented_reason}")
    print()

    # Hard-block: refuse to close unless status is a table close-source
    # (RDR-201 P1.5: derived from the table's `close` event rows, not a
    # hand-maintained ("accepted", "final") literal — `final` is retired
    # from the table's domain, RDR-201 Revision History).
    try:
        close_table = load_packaged_table("rdr-lifecycle.toml")
    except (OSError, TableLoadError, tomllib.TOMLDecodeError) as exc:
        print(f"> **ERROR**: cannot load the RDR lifecycle table: {type(exc).__name__}: {exc}")
        return
    close_source_statuses = _from_statuses_for_event(close_table, "close")
    close_source_label = " or ".join(f"`{s}`" for s in sorted(close_source_statuses))
    if current_status.lower() not in close_source_statuses:
        if force:
            print(
                f"> **Override**: RDR status is `{current_status}` (not {close_source_label}). "
                "Proceeding with `--force`."
            )
            print()
        else:
            print(
                f"> **BLOCKED**: RDR status is `{current_status}`. "
                f"Close requires status {close_source_label}."
            )
            print("> Run `nx rdr preamble rdr-gate` to validate, or use `--force` to override.")
            print()
            return

    # Gap-check for --reason implemented
    if (close_reason or "").lower() == "implemented":
        def _extract_section(doc: str, *headings: str) -> str:
            for heading in headings:
                idx = doc.find(heading)
                if idx != -1:
                    rest = doc[idx + len(heading):]
                    nxt = re.search(r"\n## ", rest)
                    return rest[:nxt.start()] if nxt else rest
            return ""

        def _parse_pointers(s: str) -> dict[str, str]:
            out: dict[str, str] = {}
            for tok in s.split(","):
                tok = tok.strip()
                if "=" in tok:
                    k, _, v = tok.partition("=")
                    out[k.strip()] = v.strip()
            return out

        try:
            rdr_id_int = int(t2_key)
        except ValueError:
            rdr_id_int = -1

        problem_stmt = _extract_section(text, "## Problem Statement", "## Problem")
        gap_matches = re.findall(
            r"^#{3,5} Gap (\d+)([^\n:]*):\s*(.*)$", problem_stmt, re.MULTILINE
        )
        gap_count = len(gap_matches)

        if rdr_id_int < 65 and gap_count == 0:
            print("> **WARN**: This RDR predates structured gaps; no action required.")
            print()
        elif rdr_id_int >= 65 and gap_count == 0:
            print(
                f"> **ERROR**: RDR-{t2_key} has no `#### Gap N: <title>` headings "
                "in `## Problem Statement` or `## Problem`."
            )
            print(r"> Expected format: `#### Gap 1: <gap title>` (regex: `^#{3,5} Gap \d+:`)")
            print()
            return
        elif gap_count > 0 and not pointers_arg:
            print("### Problem Statement Gaps")
            print()
            for num, qual, gap_title in gap_matches:
                qual_str = qual.strip()
                qual_disp = f" {qual_str}" if qual_str else ""
                print(f"- Gap{num}{qual_disp}: {gap_title.strip()}")
            print()
            print("**Re-invoke with per-gap closure pointers:**")
            print()
            example = ",".join(
                f"Gap{num}=path/to/file.py:LINE" for num, _q, _t in gap_matches
            )
            print(
                f"nx rdr preamble rdr-close -- {t2_key} --reason implemented "
                f"--pointers '{example}'"
            )
            print()
            return
        else:
            # PASS 2: validate pointers
            pointers = _parse_pointers(pointers_arg or "")
            failures = []
            for num, _qual, _title in gap_matches:
                gap_key = f"Gap{num}"
                if gap_key not in pointers:
                    failures.append(f"{gap_key}: no pointer supplied")
                    continue
                ptr = pointers[gap_key]
                file_part, sep, line_part = ptr.partition(":")
                if not sep:
                    failures.append(
                        f"{gap_key}: pointer '{ptr}' missing ':LINE' — expected file:line shape"
                    )
                    continue
                if not re.match(r"^\d+", line_part):
                    failures.append(
                        f"{gap_key}: pointer '{ptr}' has no line number after ':'"
                    )
                    continue
                if not (Path(repo_root) / file_part).exists():
                    failures.append(f"{gap_key}: file '{file_part}' does not exist in repo")
            if failures:
                print("> **ERROR**: Problem Statement pointer validation failed:")
                for f in failures:
                    print(f">   - {f}")
                print()
                return
            # Passed
            print("### PROBLEM STATEMENT REPLAY: validation passed")
            print()
            for gap_key, ptr in sorted(pointers.items()):
                print(f"- {gap_key} → {ptr}")
            print()
            # S2: best-effort T1 scratch marker — downstream hook/skill consumes rdr-close-active tag
            # (original rdr_close.py:299-303)
            try:
                subprocess.run(
                    ["nx", "scratch", "put", t2_key,
                     "--tags", f"rdr-close-active,rdr-{t2_key}"],
                    capture_output=True, timeout=5,
                )
            except Exception:  # noqa: BLE001 — best-effort optional lookup; ignored if unavailable
                pass

    # T2 metadata
    print("### T2 Metadata (current status)")
    print(
        f"Use **memory_get** tool: project=\"{repo_name}_rdr\", title=\"{t2_key}\" "
        "to retrieve T2 metadata."
    )
    print()

    # Code-enforced frontmatter flip (RDR-165/166 ledger-drift fix)
    print("### Flip the file frontmatter (code-enforced — do NOT hand-edit)")
    print()
    print(
        "When closing, flip the RDR file frontmatter + README index row "
        "atomically with the CLI instead of editing by hand:"
    )
    print()
    print(f"    nx rdr set-status {t2_key} closed")
    print()

    # Bead status advisory
    print("### Bead Status Advisory")
    print(
        f"Use **memory_get** tool: project=\"{repo_name}_rdr\", title=\"{t2_key}\" "
        "to check for `epic_bead` field."
    )
    print()

    # Active beads — S3: track has_open_beads for conditional WARNING
    has_open_beads = False
    print("### Active Beads")
    try:
        bd_result = subprocess.run(
            ["bd", "list", "--status=open,in_progress", "--limit=20"],
            capture_output=True, text=True, timeout=10,
        )
        bd_out = (bd_result.stdout or "").strip()
        if bd_out and bd_out != "No issues found.":
            has_open_beads = True
            print(bd_out)
        else:
            print("No open or in-progress beads.")
    except Exception as exc:  # noqa: BLE001 — optional beads integration; absence reported, command continues
        print(f"Beads not available: {exc}")

    # S3: WARNING block — required by feedback_rdr_close_protocol (original rdr_close.py:335-341)
    if has_open_beads:
        print()
        print("> **⚠ WARNING: Open beads exist.** You MUST ask the user for explicit")
        print("> confirmation before closing this RDR. Do NOT proceed without their approval.")
        print("> Show them the open beads above and ask: \"Close RDR with these beads still open?\"")


# ---------------------------------------------------------------------------
# preamble rdr-research
# ---------------------------------------------------------------------------

#: Matches a T2 research-finding title, e.g. "201-research-3".
# A title may carry a ": <summary>" suffix ("204-research-16: the Key
# Discoveries bullet"); the seq scan must see it, or the next add lands on
# seq 1 over sixteen existing entries (measured live, nexus-zbdm0).
_RDR_RESEARCH_TITLE_RE = re.compile(r"^(\d+)-research-(\d+)(?::.*)?$")

#: Bound on the "advance past a collision" retry loop in
#: :func:`_rdr_research_add` — a defensive ceiling against looping forever
#: under sustained contention, never expected to be hit in practice.
_RDR_RESEARCH_MAX_SEQ_ATTEMPTS = 50


def _rdr_research_next_seq(entries: list[dict], t2_key: str) -> int:
    """Return the next free research sequence number for *t2_key*.

    ``max(existing seq for "<t2_key>-research-*" titles) + 1``, or ``1``
    when none exist. *entries* is whatever ``T2Database.get_all`` /
    a T2 client double returns — a list of ``{"title": ..., ...}`` dicts.
    """
    seqs = [
        int(m.group(2))
        for entry in entries
        if (m := _RDR_RESEARCH_TITLE_RE.match(entry.get("title", "") if isinstance(entry, dict) else ""))
        and m.group(1) == t2_key
    ]
    return max(seqs) + 1 if seqs else 1


def _rdr_research_add(t2_key: str, finding_text: str, repo_name: str) -> str:
    """Record a research finding for RDR *t2_key* in T2, returning the title.

    Bug this closes (nexus-zu1q0): the previous scheme (list existing
    ``<id>-research-*`` titles, take max+1, then ``memory_put`` the result)
    was computed by the calling agent as prose, not derived deterministically
    here — two consecutive adds could both compute seq 1, and the second
    ``memory_put`` (an upsert by ``(project, title)``) silently overwrote the
    first finding rather than creating seq 2.

    This still cannot fully rule out a genuine two-writer race (this process
    lists, then writes, with no cross-process lock in between), so the write
    is additionally guarded: immediately before writing, the target title is
    checked for existence, and a collision advances to the next sequence
    number rather than ever upserting over it. Giving up after
    :data:`_RDR_RESEARCH_MAX_SEQ_ATTEMPTS` collisions fails loud instead of
    looping forever under sustained contention.
    """
    project = f"{repo_name}_rdr"
    with _t2_client_factory() as client:
        entries = client.get_all(project=project)
        seq = _rdr_research_next_seq(entries, t2_key)
        for _ in range(_RDR_RESEARCH_MAX_SEQ_ATTEMPTS):
            title = f"{t2_key}-research-{seq}"
            if client.get(project=project, title=title) is None:
                content = f"rdr_id: {t2_key}\nseq: {seq}\nfinding: {finding_text}\n"
                client.put(project=project, title=title, content=content, tags="rdr,research", ttl=None)
                return title
            seq += 1
    raise click.ClickException(
        f"rdr-research add: could not claim a free sequence number for RDR "
        f"{t2_key} after {_RDR_RESEARCH_MAX_SEQ_ATTEMPTS} attempts — titles "
        "are being created faster than this command can claim one; "
        "investigate concurrent writers before retrying."
    )


@preamble.command("rdr-research")
@click.argument("args", nargs=-1)
def preamble_rdr_research(args: tuple[str, ...]) -> None:
    """Print RDR research context (file Research Findings + T2 entries), or
    record a new finding when invoked as ``add <id> <finding text...>``."""
    repo_root, repo_name = _preamble_resolve_repo()
    rdr_dir = _preamble_rdr_dir(repo_root)
    rdr_path = Path(repo_root) / rdr_dir
    args_str = " ".join(args).strip()

    # ``add <id> <finding text>`` (at least one text token) performs the T2
    # write here, deterministically — see _rdr_research_add. ``add <id>``
    # alone (no text) is unchanged: it falls through to the context-printing
    # path below, exactly as before (nexus-zu1q0 scope).
    if len(args) >= 3 and args[0].lower() == "add" and re.match(r"^\d+$", args[1]):
        # Zero-padded to three digits: that is the shape every live
        # ``<id>-research-N`` title carries (``097-research-9``), so an
        # unpadded ``97`` must find them, not fork a second namespace.
        t2_key = f"{int(args[1]):03d}"
        finding_text = " ".join(args[2:]).strip()
        title = _rdr_research_add(t2_key, finding_text, repo_name)
        print(f"Recorded T2 research finding: `{repo_name}_rdr/{title}`")
        return

    print(f"**Repo:** `{repo_name}`  **RDR directory:** `{rdr_dir}`")
    print()

    if not rdr_path.exists():
        print(f"> No RDRs found — `{rdr_dir}` does not exist in this repo.")
        return

    # Extract numeric ID from args (skip subcommand words like "add", "status")
    id_match = re.search(r"\d+", args_str)

    if id_match:
        rdr_file = _preamble_find_rdr_file(rdr_path, id_match.group(0))
        if rdr_file:
            fm, text = _preamble_parse_frontmatter(rdr_file)
            title = fm.get("title", fm.get("name", rdr_file.stem))
            rdr_num = re.search(r"\d+", rdr_file.stem)
            # Strip leading zeros so "001" -> "1" for display
            t2_key = str(int(rdr_num.group(0))) if rdr_num else rdr_file.stem

            print(f"### RDR {t2_key}: {title}")
            print(f"**File:** `{rdr_file.name}`")
            print()

            rf_match = re.search(
                r"^## Research Findings\s*\n(.*?)(?=^## |\Z)",
                text, re.MULTILINE | re.DOTALL,
            )
            print("#### Research Findings (from file)")
            print()
            if rf_match:
                section = rf_match.group(1).strip()
                print(
                    section if section
                    else "_No content in Research Findings section yet._"
                )
            else:
                print("_No `## Research Findings` section found in this RDR._")
            print()

            # T2 research findings
            print("### Existing Research Findings (T2)")
            try:
                list_result = subprocess.run(
                    ["nx", "memory", "list", "--project", f"{repo_name}_rdr"],
                    capture_output=True, text=True, timeout=10,
                )
                list_out = (list_result.stdout or "").strip()
                # `nx memory list` rows are "[id] <project>/<title>  (…)" —
                # match the title after the project slash, not line start
                # (the ^-anchored form matched nothing, so every preamble
                # reported "No research findings recorded" while T2 held
                # them; caught on RDR-188, 2026-07-22).
                research_lines = [
                    ln for ln in list_out.splitlines()
                    if re.search(rf"/{t2_key}-research", ln)
                ]
                print(
                    "\n".join(research_lines) if research_lines
                    else "No research findings recorded yet"
                )
            except Exception as exc:  # noqa: BLE001 — optional T2 lookup; absence reported, command continues
                print(f"T2 not available: {exc}")
        else:
            print(f"> RDR not found for ID: `{id_match.group(0)}`")
            print()
            rdrs = _preamble_get_all_rdrs(rdr_path)
            print("### Available RDRs")
            print()
            if rdrs:
                print("| File | Title | Status | Type |")
                print("|------|-------|--------|------|")
                for r in rdrs:
                    print(f"| {r['file']} | {r['title']} | {r['status']} | {r['rtype']} |")
            else:
                print(f"No RDRs found in `{rdr_dir}`")
    else:
        # No ID — show available RDRs
        rdrs = _preamble_get_all_rdrs(rdr_path)
        print("### Available RDRs")
        print()
        if rdrs:
            print("| File | Title | Status | Type |")
            print("|------|-------|--------|------|")
            for r in rdrs:
                print(f"| {r['file']} | {r['title']} | {r['status']} | {r['rtype']} |")
        else:
            print(f"No RDRs found in `{rdr_dir}`")
        print()
        print(
            "> **Usage**: `nx rdr preamble rdr-research -- <id>` or "
            "`nx rdr preamble rdr-research -- add <id>`"
        )


# ---------------------------------------------------------------------------
# preamble rdr-fix — the fix step's own surface (nexus-zbdm0)
# ---------------------------------------------------------------------------

_FIX_RULES: tuple[str, ...] = (
    "A fix changes the fact the critic named and nothing else; a gloss, rationale, "
    "parenthetical or count is a separate commit with its own fix check.",
    "Every clause the fix adds carries a tool-produced quote from its source, or the "
    "marker \"inferred, not read\".",
    "A count or a universal (never / always / only / nothing / every / the one / all) "
    "needs a census of the whole surface, captured in the research entry as an enumeration.",
    "Sweep every site in the finding's Sites: list; a fact lives in Problem Statement, "
    "Research Findings, Technical Design and the Implementation Plan at once.",
    "From round 3, the fix closes only findings marked `Ship-blocker: yes`; every other "
    "Critical and Significant is a residual, recorded and dispositioned at accept — never "
    "re-gated for this change.",
    "A Criterion 6 readability WARN is never closed inside a fix commit.",
    "The fix check and the gate critique are never dispatched against the same commit in "
    "parallel: fix, then check, then Layer 1 and Layer 3.",
)


def _git_out(repo_root: str, *args: str) -> str | None:
    """stdout of a git command, or None when it fails."""
    try:
        proc = subprocess.run(
            ["git", "-C", repo_root, *args], capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


@preamble.command("rdr-fix")
@click.argument("args", nargs=-1)
def preamble_rdr_fix(args: tuple[str, ...]) -> None:
    """Print the fix-step context for an RDR: the latest gate's findings with
    their Sites, the diff and fix commits since the gated commit, the
    pre-edit research title, and the fix rules."""
    repo_root, repo_name = _preamble_resolve_repo()
    rdr_dir = _preamble_rdr_dir(repo_root)
    rdr_path = Path(repo_root) / rdr_dir
    args_str = " ".join(args).strip()
    id_match = re.search(r"\d+", args_str)
    if not id_match:
        print("> **Usage**: `nx rdr preamble rdr-fix <id>`")
        return
    rdr_file = _preamble_find_rdr_file(rdr_path, id_match.group(0))
    if not rdr_file:
        print(f"> RDR not found for ID: `{id_match.group(0)}`")
        return
    fm, _text = _preamble_parse_frontmatter(rdr_file)
    rdr_num = re.search(r"\d+", rdr_file.stem)
    t2_key = rdr_num.group(0) if rdr_num else rdr_file.stem
    status = str(fm.get("status", "")).strip()
    rel = os.path.relpath(str(rdr_file), repo_root)
    project = f"{repo_name}_rdr"

    print(f"### Fix RDR-{t2_key} ({rdr_file.name}, status `{status or '?'}`)")
    print()
    if status.lower() not in ("", "draft", "open"):
        print(
            f"> RDR-{t2_key} is past the gate (status `{status}`). Post-accept edits are "
            "not gate fixes and there is no gate fix to make here; residual dispositions "
            "go through rdr-accept, and a design change reopens the RDR. A residual "
            "dispositioned by a change to the RDR file carries a fix check on that change, "
            f"stored as `{t2_key}-fix-check-<sha>` with `<sha>` the RDR file's tip after "
            "the disposition; a residual dispositioned by a bead id changed nothing in the "
            "file and needs none."
        )
        return

    try:
        with _t2_client_factory() as client:
            latest = client.get(project=project, title=f"{t2_key}-gate-latest")
            if not latest:
                print(
                    f"> No gate record for RDR-{t2_key}; there is nothing to fix. A finding "
                    f"from a review goes through `nx rdr preamble rdr-research -- add {t2_key} ...`."
                )
                return
            content = latest.get("content", "") if isinstance(latest, dict) else ""
            outcome = (_preamble_parse_t2_field(content, "outcome") or "?").strip().upper()
            date = _preamble_parse_t2_field(content, "date") or "?"
            gated_commit = (_preamble_parse_t2_field(content, "commit") or "").strip()
            critique_title = (_preamble_parse_t2_field(content, "critique") or "").strip()
            critique_title = re.sub(r"\s*\[\d+\]\s*$", "", critique_title).rsplit("/", 1)[-1]
            rows = client.get_all(project=project) or [] if callable(getattr(client, "get_all", None)) else []
            prefix = f"{t2_key}-gate-critique-"
            critique_count = sum(
                1 for r in rows if isinstance(r, dict) and str(r.get("title", "")).startswith(prefix)
            )
            next_seq = _rdr_research_next_seq(rows, t2_key)
            critique = client.get(project=project, title=critique_title) if critique_title else None
            tip = (_git_out(repo_root, "log", "-1", "--format=%h", "--", rel) or "").strip()
            fix_check_exists = (
                client.get(project=project, title=f"{t2_key}-fix-check-{tip}") is not None if tip else False
            )
    except Exception as exc:  # noqa: BLE001 — T2 unreachable must be a visible note, never silence
        print(f"> T2 unreachable ({type(exc).__name__}: {exc}); load `{project}/{t2_key}-gate-latest` by hand.")
        return

    print(f"Latest gate {date}: {outcome}." + (f" Critique: `{project}/{critique_title}`" if critique_title else ""))
    print()
    for line in _gate_round_lines(content, critique_count):
        print(line)

    findings = _critique_findings(str(critique.get("content", ""))) if isinstance(critique, dict) else []
    own_round = _gate_round_number(content, critique_count) - 1
    print("#### Findings to fix (each at every site named)")
    print()
    if findings and own_round > GATE_MAX_ANY_CRITICAL_ROUNDS:
        residual_keys = {_finding_title_key(t) for t in _residual_titles(content)}
        ship_blockers: list[str] = []
        residuals_out: list[str] = []
        is_residual = False
        for f in findings:
            if not f.startswith("  "):
                is_residual = _finding_title_key(f) in residual_keys
            (residuals_out if is_residual else ship_blockers).append(f)
        print(
            f"From round {GATE_MAX_ANY_CRITICAL_ROUNDS + 1}, fix only the ship-blockers below; "
            "every other Critical and Significant is a residual — record it, do not fix it in "
            "this change; it is dispositioned at accept, never re-gated."
        )
        print()
        print("**Ship-blockers (fix these):**")
        print()
        if ship_blockers:
            for f in ship_blockers:
                print(f"- {f}")
        else:
            print("(none — every finding this round matched the gate record's `residuals:` field)")
        print()
        print("**Residuals (record; do not fix in this change):**")
        print()
        if residuals_out:
            for f in residuals_out:
                print(f"- {f}")
        else:
            print("(none)")
    elif findings:
        for f in findings:
            print(f"- {f}")
    elif critique_title:
        print(f"The critique `{critique_title}` could not be loaded or holds no Critical/Significant findings; read it in full.")
    else:
        print("The gate record carries no `critique:` pointer; find the prior critique by hand.")
    print()

    print("#### Tree state")
    print()
    if gated_commit:
        stat = (_git_out(repo_root, "diff", "--stat", f"{gated_commit}..HEAD", "--", rel) or "").strip().splitlines()
        log = (_git_out(repo_root, "log", "--format=%h %s", f"{gated_commit}..HEAD", "--", rel) or "").strip().splitlines()
        print(f"Gated commit `{gated_commit}`; RDR file tip `{tip or '?'}`.")
        print(f"Range the fix check will read: `git diff {gated_commit}..HEAD -- {rel}`")
        print("Changed since the gated commit: " + (stat[-1].strip() if stat else "no changes to the RDR file"))
        if log:
            print("Fix commits so far:")
            for ln in log:
                print(f"- {ln}")
        if tip:
            if fix_check_exists:
                print(f"Fix-check record `{t2_key}-fix-check-{tip}` exists for the current tip.")
            else:
                print(f"No fix-check record yet for the current tip (`{t2_key}-fix-check-{tip}`).")
    else:
        print("The gate record carries no `commit:`; the fix check has no range. Name the gated tree by hand.")
    print()

    print("#### Before the edit")
    print()
    print(
        f"1. Record the research entry first: `nx rdr preamble rdr-research -- add {t2_key} "
        f"<finding tokens>` (it becomes `{project}/{t2_key}-research-{next_seq}`); the quote or "
        "enumeration for every clause the fix will add lives there."
    )
    print("2. Edit the RDR at every site the finding names.")
    print(
        f"3. Commit, then `nx rdr preamble rdr-gate -- {t2_key}` prints the Fix check section; "
        "dispatch it and store the verdict under the title it names before any re-gate."
    )
    print()
    print("#### Rules")
    print()
    for rule in _FIX_RULES:
        print(f"- {rule}")
    print()


def _residual_count(content: str) -> int:
    """Residuals in a gate record: bullets under ``residuals:`` (the live
    shape), or one per ``residuals:`` key line carrying inline text. Never a
    split on punctuation inside a residual's own prose (code review [24883]
    finding 1)."""
    count = 0
    active = False
    inline = 0
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("residuals:"):
            active = True
            if stripped.split(":", 1)[1].strip():
                inline += 1
            continue
        if active and re.match(r"^[A-Za-z_][A-Za-z0-9_]*:", stripped):
            active = False
            continue
        if active and stripped.startswith("-"):
            count += 1
    return count + inline


def _residual_titles(content: str) -> list[str]:
    """The titles recorded in a gate record's ``residuals:`` block: one
    per ``- <title>`` bullet, or the inline text on the ``residuals:``
    line itself. Mirrors ``_residual_count``'s parse but returns the text
    for matching against a finding's title via ``_finding_title_key``
    (the fix preamble's round-3+ ship-blocker/residual split, nexus-yjf5l.2;
    reused by the Layer 0 survivor sweep's residual exemption, nexus-yjf5l.3)."""
    out: list[str] = []
    active = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("residuals:"):
            active = True
            inline = stripped.split(":", 1)[1].strip()
            if inline:
                out.append(inline)
            continue
        if active and re.match(r"^[A-Za-z_][A-Za-z0-9_]*:", stripped):
            active = False
            continue
        if active and stripped.startswith("-"):
            out.append(stripped.lstrip("-").strip())
    return out


#: The day the round cap shipped (nexus-g7zgw.2). Gate records dated before
#: it are the uncapped baseline the doctrine's rise/fall test compares against.
GATE_CAP_SHIPPED: str = "2026-09-07"


def _gate_loop_health_lines(rows: list[dict]) -> list[str]:
    """Per gated RDR: rounds, the Criticals-per-round series read from the
    prior chain plus the record itself, the residual count, and a flag when
    the loop ran past the round cap. Then the doctrine's own test
    (conexus/skills/orchestration/SKILL.md § bounds): across RDRs gated
    before and since :data:`GATE_CAP_SHIPPED`, findings per round must rise
    while rounds per RDR fall; when both fall the bound is suppressing recall
    (nexus-zbdm0; critique [24884] Critical 1)."""
    out: list[str] = []
    before: list[tuple[int, float]] = []
    since: list[tuple[int, float]] = []
    for row in sorted(rows, key=lambda r: str(r.get("title", ""))):
        title = str(row.get("title", ""))
        m = re.match(r"^(\d+)-gate-latest$", title)
        if not m:
            continue
        rdr_id = m.group(1)
        content = str(row.get("content", ""))
        prior = _t2_field_block(content, "prior")
        entries = re.findall(r"\[\d+\]\s*(?:\(([^)]*)\))?", prior)
        series: list[str] = []
        for e in reversed(entries):
            c = re.search(r"(\d+)C\b", e or "")
            series.append(c.group(1) if c else "?")
        latest_c = (_preamble_parse_t2_field(content, "critical_count") or "").strip()
        series.append(latest_c if latest_c.isdigit() else "?")
        # The critique records are the count nobody retypes; the chain can
        # undercount (deep critique [24873]), so the larger wins, exactly as
        # in _gate_round_lines.
        prefix = f"{rdr_id}-gate-critique-"
        critique_count = sum(
            1 for r in rows if isinstance(r, dict) and str(r.get("title", "")).startswith(prefix)
        )
        rounds = max(len(entries) + 1, critique_count)
        n_res = _residual_count(content)
        line = (
            f"- RDR-{rdr_id}: {rounds} round{'s' if rounds != 1 else ''}; "
            f"Criticals per round: {', '.join(series)}; residuals: {n_res}"
        )
        if rounds > GATE_MAX_ANY_CRITICAL_ROUNDS + 1:
            line += (
                f"; the cap did not end the loop (more than {GATE_MAX_ANY_CRITICAL_ROUNDS + 1} rounds): "
                "check whether findings per round fell while rounds kept coming"
            )
        out.append(line)
        known = [int(x) for x in series if x.isdigit()]
        if known:
            date = (_preamble_parse_t2_field(content, "date") or "").strip()
            bucket = since if date >= GATE_CAP_SHIPPED else before
            bucket.append((rounds, sum(known) / len(known)))
    if not out:
        return out
    out.append("")

    def _mean(pairs: list[tuple[int, float]], i: int) -> float:
        return sum(p[i] for p in pairs) / len(pairs)

    if before and since:
        r_b, r_s = _mean(before, 0), _mean(since, 0)
        f_b, f_s = _mean(before, 1), _mean(since, 1)
        out.append(
            f"Bound test (RDRs gated before {GATE_CAP_SHIPPED}: {len(before)}; since: {len(since)}): "
            f"rounds per RDR {r_b:.1f} -> {r_s:.1f}; Criticals per round {f_b:.2f} -> {f_s:.2f}."
        )
        if r_s < r_b and f_s < f_b:
            out.append(
                "BOTH FELL: the round cap may be suppressing recall. Revert to full-review "
                "rounds for the next gate and surface it (orchestration doctrine)."
            )
        elif r_s <= r_b and f_s >= f_b:
            out.append("Rounds fell and findings per round did not: the bound is doing its job.")
        else:
            out.append("Mixed: rounds did not fall; the cap is not the limiting factor yet.")
    else:
        out.append(
            f"Bound test: not yet measurable ({len(before)} RDRs gated before {GATE_CAP_SHIPPED}, "
            f"{len(since)} since); it needs both sides."
        )
    return out


# ---------------------------------------------------------------------------
# preamble rdr-verdict — the gate outcome computed from the critique (nexus-yxo2l)
# ---------------------------------------------------------------------------

_VERDICT_FIELD_RE = re.compile(r"^\s*-\s*\*\*(outcome|critical_count|significant_count|ship_blockers)\*\*\s*:\s*(\S+)", re.IGNORECASE | re.MULTILINE)
_VERDICT_INLINE_RE = re.compile(r"\b(critical_count|significant_count|ship_blockers)\s*=\s*(\d+)", re.IGNORECASE)
_SHIP_BLOCKER_RE = re.compile(r"^\s*(?:-\s*)?\*{0,2}Ship-blocker\*{0,2}\s*:\s*\*{0,2}(yes|no)\b", re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True)
class CritiqueTally:
    """What a critique says and what it contains, side by side."""

    criticals: list[str]
    significants: list[str]
    ship_blocker_titles: list[str]
    reported_critical: int | None
    reported_significant: int | None
    reported_ship_blockers: int | None


def _critique_tally(text: str) -> CritiqueTally:
    """Count the issues in a critique and read its Verdict.

    Canonical shape: ``## Critical Issues`` / ``## Significant Issues``
    sections holding ``### Issue: <title>`` blocks, each with a
    ``- **Ship-blocker**: yes|no`` line. Free-form shape (the RDR-204
    seventh gate): paragraphs opening ``CRITICAL — <title>`` /
    ``SIGNIFICANT — <title>`` with a bare ``Ship-blocker: yes`` line, and
    a ``VERDICT: ... critical_count=N ... ship_blockers=N`` line.
    """
    criticals: list[str] = []
    significants: list[str] = []
    blockers: list[str] = []
    section: str | None = None
    current: str | None = None
    current_kind: str | None = None

    def _mark(kind: str | None, title: str | None, yes: bool) -> None:
        # One mark per issue: a second Ship-blocker line under the same
        # block must not count twice (code review [24900] finding 2).
        if yes and title is not None and kind in ("critical", "significant") and title not in blockers:
            blockers.append(title)

    # Canonical critiques carry section headings; free-form ones do not.
    # A ``CRITICAL —`` paragraph counts only in a free-form critique and
    # never inside its OBSERVATIONS block (code review [24900] finding 3).
    canonical = bool(re.search(r"^\s*#{1,3}\s*(critical|significant)", text, re.IGNORECASE | re.MULTILINE))
    freeform_off = False

    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        sec = re.match(r"^\s*#{1,3}\s*(critical|significant|observation|verification|verdict)", line, re.IGNORECASE)
        if sec and not _CRITIQUE_ISSUE_RE.match(line):
            word = sec.group(1).lower()
            section = word if word in ("critical", "significant") else None
            current = None
            continue
        issue = _CRITIQUE_ISSUE_RE.match(line)
        if issue and section:
            current = issue.group(1).strip()
            current_kind = section
            (criticals if section == "critical" else significants).append(current)
            continue
        if not canonical and re.match(r"^[A-Z][A-Z0-9 ,()'/-]+$", stripped):
            # An all-caps free-form heading: OBSERVATIONS (and anything after
            # it until the next heading) is not a findings block.
            freeform_off = stripped.startswith("OBSERVATION") or stripped.startswith("VERIFICATION")
            current = None
            continue
        free = re.match(r"^\s*(CRITICAL|SIGNIFICANT)\s*[—-]+\s*(.+)$", stripped)
        if free and not canonical and not freeform_off:
            current = free.group(2).strip()
            current_kind = free.group(1).lower()
            (criticals if current_kind == "critical" else significants).append(current)
            continue
        sb = _SHIP_BLOCKER_RE.match(line)
        if sb:
            _mark(current_kind, current, sb.group(1).lower() == "yes")
    # The Verdict is read from the LAST ``## Verdict`` section (or the last
    # ``VERDICT:`` line), with fenced code stripped first, so a quoted or
    # example verdict earlier in the text cannot poison the counts (code
    # review [24900] finding 1).
    unfenced = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    verdict_slice = ""
    heads = list(re.finditer(r"^\s*#{1,3}\s*Verdict\b.*$", unfenced, re.IGNORECASE | re.MULTILINE))
    if heads:
        verdict_slice = unfenced[heads[-1].end():]
    else:
        inline = list(re.finditer(r"^\s*VERDICT\s*:.*$", unfenced, re.IGNORECASE | re.MULTILINE))
        if inline:
            verdict_slice = inline[-1].group(0)
    reported: dict[str, str] = {k.lower(): v for k, v in _VERDICT_FIELD_RE.findall(verdict_slice)}
    for k, v in _VERDICT_INLINE_RE.findall(verdict_slice):
        reported.setdefault(k.lower(), v)

    def _int(key: str) -> int | None:
        v = reported.get(key, "").strip().rstrip(".,")
        return int(v) if v.isdigit() else None

    return CritiqueTally(
        criticals=criticals,
        significants=significants,
        ship_blocker_titles=blockers,
        reported_critical=_int("critical_count"),
        reported_significant=_int("significant_count"),
        reported_ship_blockers=_int("ship_blockers"),
    )


def _gate_round_number(gate_record: str, critique_count: int) -> int:
    """The round the NEXT gate is: gate records so far plus one."""
    if not gate_record:
        return max(1, critique_count + 1) if critique_count else 1
    prior = _t2_field_block(gate_record, "prior")
    n_prior = max(1 + len(re.findall(r"\[\d+\]", prior)), critique_count)
    return n_prior + 1


@preamble.command("rdr-verdict")
@click.argument("args", nargs=-1)
def preamble_rdr_verdict(args: tuple[str, ...]) -> None:
    """Compute a gate's outcome from its critique and its round, and print
    the gate record to write. The critic reports; this decides."""
    repo_root, repo_name = _preamble_resolve_repo()
    rdr_dir = _preamble_rdr_dir(repo_root)
    rdr_path = Path(repo_root) / rdr_dir
    tokens = [a for a in args if a.strip()]
    if len(tokens) < 2 or not re.search(r"\d+", tokens[0]):
        print("> **Usage**: `nx rdr preamble rdr-verdict <id> <critique-title>`")
        return
    id_match = re.search(r"\d+", tokens[0])
    critique_title = tokens[1].strip()
    critique_title = re.sub(r"\s*\[\d+\]\s*$", "", critique_title).rsplit("/", 1)[-1]
    rdr_file = _preamble_find_rdr_file(rdr_path, id_match.group(0))
    if not rdr_file:
        print(f"> RDR not found for ID: `{id_match.group(0)}`")
        return
    rdr_num = re.search(r"\d+", rdr_file.stem)
    t2_key = rdr_num.group(0) if rdr_num else rdr_file.stem
    rel = os.path.relpath(str(rdr_file), repo_root)
    project = f"{repo_name}_rdr"

    try:
        with _t2_client_factory() as client:
            critique = client.get(project=project, title=critique_title)
            if not critique:
                print(f"> The critique `{project}/{critique_title}` names no such T2 record; store the critique first.")
                return
            latest = client.get(project=project, title=f"{t2_key}-gate-latest")
            latest_content = latest.get("content", "") if isinstance(latest, dict) else ""
            latest_id = latest.get("id") if isinstance(latest, dict) else None
            rows = client.get_all(project=project) or [] if callable(getattr(client, "get_all", None)) else []
            prefix = f"{t2_key}-gate-critique-"
            critique_count = sum(
                1 for r in rows if isinstance(r, dict) and str(r.get("title", "")).startswith(prefix)
                and str(r.get("title", "")) != critique_title
            )
    except Exception as exc:  # noqa: BLE001 — T2 unreachable must be a visible note, never silence
        print(f"> T2 unreachable ({type(exc).__name__}: {exc}); the verdict cannot be computed.")
        return

    critique_text = str(critique.get("content", ""))
    tally = _critique_tally(critique_text)
    recognised = bool(
        re.search(r"^\s*#{1,3}\s*(critical|significant)", critique_text, re.IGNORECASE | re.MULTILINE)
        or re.search(r"^\s*(CRITICAL|SIGNIFICANT)\s*[—-]", critique_text, re.MULTILINE)
        or tally.reported_critical is not None
    )
    if not recognised:
        print(
            f"> The critique `{critique_title}` is in neither recognised shape (no `## Critical Issues` / "
            "`## Significant Issues` sections, no `CRITICAL —` paragraphs, no Verdict counts), so its "
            "findings cannot be counted. No outcome is computed. Re-store it in the canonical format "
            "(conexus/agents/substantive-critic.md § Output Format) and run this again."
        )
        return
    round_no = _gate_round_number(latest_content, critique_count)
    rule = rule_for("rdr-gate", round_no)
    already_gated = (
        critique_title in (_preamble_parse_t2_field(latest_content, "critique") or "")
    )

    notes: list[str] = []
    critical_count = len(tally.criticals)
    if tally.reported_critical is not None and tally.reported_critical != critical_count:
        notes.append(
            f"critical_count: self-reported {tally.reported_critical}, counted {critical_count} "
            f"Critical issue block(s); the larger is used."
        )
        critical_count = max(critical_count, tally.reported_critical)
    significant_count = len(tally.significants)
    if tally.reported_significant is not None and tally.reported_significant != significant_count:
        notes.append(
            f"significant_count: self-reported {tally.reported_significant}, counted {significant_count}; the larger is used."
        )
        significant_count = max(significant_count, tally.reported_significant)
    counted_sb = len(tally.ship_blocker_titles)
    if tally.reported_ship_blockers is None:
        ship_blockers = max(counted_sb, critical_count)
        notes.append(
            f"ship_blockers: the Verdict has no ship_blockers line; read as critical_count ({critical_count}), never zero."
        )
    else:
        ship_blockers = max(counted_sb, tally.reported_ship_blockers)
        if tally.reported_ship_blockers != counted_sb:
            notes.append(
                f"ship_blockers: self-reported {tally.reported_ship_blockers}, counted {counted_sb} "
                f"issue(s) marked Ship-blocker: yes; the larger is used."
            )

    if rule.blocks_on == "any-critical":
        blocked = critical_count > 0
    elif rule.blocks_on == "ship-blocker":
        blocked = ship_blockers > 0
    else:
        blocked = False
    outcome = "BLOCKED" if blocked else "PASSED"
    residuals: list[str] = []
    if rule.blocks_on == "ship-blocker":
        residuals = [t for t in tally.criticals + tally.significants if t not in tally.ship_blocker_titles]

    commit = (_git_out(repo_root, "log", "-1", "--format=%h", "--", rel) or "").strip()
    prev_outcome = (_preamble_parse_t2_field(latest_content, "outcome") or "").strip().upper()
    prev_c = (_preamble_parse_t2_field(latest_content, "critical_count") or "?").strip()
    prev_s = (_preamble_parse_t2_field(latest_content, "significant_count") or "?").strip()
    prev_chain = _t2_field_block(latest_content, "prior")
    prior_parts: list[str] = []
    if latest_content:
        prior_parts.append(f"[{latest_id if latest_id is not None else '?'}] ({prev_outcome or '?'} {prev_c}C {prev_s}S)")
    if prev_chain:
        prior_parts.append(prev_chain)

    print(f"### Gate verdict for RDR-{t2_key} from `{project}/{critique_title}`")
    print()
    print(f"**Gate round {round_no}**; rule: {rule.blocks_on} (review-rounds.toml, rdr-gate); next round by: {rule.next_round_by}.")
    if already_gated:
        print(
            f"This critique is already the one `{t2_key}-gate-latest` records, so the round above is the "
            "NEXT gate's, not this critique's historical round; the outcome below is a recomputation, "
            "not a new gate."
        )
    else:
        print("The round assumes this critique is the new, not yet recorded, gate.")
    print(f"Counted: {len(tally.criticals)} Critical, {len(tally.significants)} Significant, {counted_sb} marked Ship-blocker: yes.")
    for n in notes:
        print(f"- {n}")
    print()
    print(f"**Outcome: {outcome}**")
    if residuals:
        print(f"Residuals ({len(residuals)}), recorded for accept to disposition:")
        for r in residuals:
            print(f"- {r}")
    print()
    print("Gate record to write (memory_put project=\"" + project + f"\", title=\"{t2_key}-gate-latest\", ttl=\"permanent\", tags=\"rdr,gate\"):")
    print()
    print("```")
    print(f'outcome: "{outcome}"')
    print(f'date: "{datetime.now(timezone.utc).date().isoformat()}"')
    print(f"critical_count: {critical_count}")
    print(f"significant_count: {significant_count}")
    print(f"ship_blockers: {ship_blockers}")
    print(f"round: {round_no}")
    print("summary: <one sentence>")
    print(f"critique: {project}/{critique_title}")
    print(f"commit: {commit or '<git log -1 --format=%h -- ' + rel + '>'}")
    print(f"fix_check: {'<' + project + '/' + t2_key + '-fix-check-' + (commit or '<sha>') + ', or none (no change since <sha>)>' if latest_content else 'none (first gate)'}")
    if residuals:
        print("residuals:")
        for r in residuals:
            print(f"  - {r}")
    if prior_parts:
        print("prior: " + ", ".join(prior_parts))
    print("```")
    print()
    print("Write these fields as printed; the outcome is not recomputed by hand.")


# ---------------------------------------------------------------------------
# preamble rdr-audit — closed-vocabulary scan (RDR-201 P1.8, nexus-j9z30.8)
# ---------------------------------------------------------------------------

_RDR_AUDIT_EXCLUDED_FILENAMES: frozenset[str] = frozenset({"agents.md", "readme.md"})


def _rdr_audit_status_findings(
    rdr_dir: Path, status_domain: frozenset[str]
) -> tuple[list[tuple[str, str]], int, int]:
    """Scan *rdr_dir* non-recursively for frontmatter statuses outside
    *status_domain*.

    Scan scope (RDR-201 P1.8 task corrections, T2
    nexus/plan-rdr-201-closed-vocabularies.md [23998] residual 7,
    nexus/plan-rdr-201-enrichment-deltas [24001]): ``docs/rdr/*.md``,
    non-recursive — ``docs/rdr/post-mortem/`` is a separate document set
    carrying its own status values and is excluded by construction (a
    single-level glob), not a special case — excluding ``AGENTS.md`` /
    ``README.md`` by filename, case-insensitive.

    A file carrying ``kind: companion`` in its frontmatter has no
    lifecycle status at all and is SKIPPED entirely — counted, never
    reported as a finding, regardless of any leftover ``status:`` value
    (the ``revised-after-implementation`` shape keeps ``status: closed``
    *and* ``kind: companion`` — RDR-201 P1.7 leg 1). A file with no
    frontmatter, or frontmatter with no ``status:`` field and no
    ``kind: companion``, contributes to neither list — there is nothing
    to check.

    Returns ``(findings, companion_count, scanned_count)`` where
    *findings* is a sorted-by-filename list of ``(filename, status)`` for
    every out-of-vocabulary status found, and *scanned_count* counts every
    non-excluded ``.md`` file (companions included).
    """
    findings: list[tuple[str, str]] = []
    companion_count = 0
    scanned_count = 0
    for path in sorted(rdr_dir.glob("*.md")):
        if path.name.lower() in _RDR_AUDIT_EXCLUDED_FILENAMES:
            continue
        scanned_count += 1
        fm, _text = _preamble_parse_frontmatter(path)
        if fm.get("kind") == "companion":
            companion_count += 1
            continue
        status = fm.get("status")
        if status is None:
            continue
        if status not in status_domain:
            findings.append((path.name, str(status)))
    findings.sort(key=lambda pair: pair[0])
    return findings, companion_count, scanned_count


def _t2_rdr_status_census(
    repo_name: str,
) -> tuple[Counter[str], list[str], str | None, dict[str, str]]:
    """Read T2 project ``<repo_name>_rdr`` and count entries by their
    ``status:`` field, through the same injectable ``_t2_client_factory``
    seam :func:`_gate_outcome_for` already uses (RDR-201 P1.8 task
    corrections: "through the existing preamble T2 facade, injected for
    tests").

    This is a SEPARATE, clearly labelled census line — never merged into
    :func:`_rdr_audit_status_findings`'s file findings. Nothing keeps the
    two surfaces in sync automatically (the reconcile hook that claimed to
    never ran and was deleted, nexus-e19sa; ``set-status`` writes the file,
    the lifecycle skills write T2), so the fourth return value — ``{number:
    status}`` for every unambiguous record — feeds
    :func:`_file_vs_t2_status_drift`, the DETECTOR the audit prints so a
    disagreement is a finding a human sees rather than a writer's guess.

    Title shapes: T2 RDR records are titled either the bare number
    (``"42"``) or ``"RDR-42"`` — both are counted, matched via
    ``^(?:RDR-)?(\\d+)$`` (RDR-201 P1.8 fix round: the bare-digit-only
    match originally here silently dropped every ``RDR-NNN``-titled
    record, ~42 of them on the live project). Each distinct RDR number is
    counted ONCE. When the same number appears under both title shapes
    with DIFFERING statuses, that is reported as ambiguous rather than
    silently picking one shape's value — this reads live production T2,
    and guessing which shape is authoritative is not this census's call
    to make.

    A T2 read failure (unreachable service, timeout, ...) is reported ON
    THIS LINE and never allowed to fail the audit — returns an empty
    ``Counter`` plus a named reason string instead of raising.

    Returns ``(counts, ambiguous, error, statuses_by_number)`` — *ambiguous*
    is a sorted list of human-readable ``"<number> (<title>=<status>,
    <title>=<status>)"`` notes, empty when there is nothing to report.
    """
    project = f"{repo_name}_rdr"
    counts: Counter[str] = Counter()
    ambiguous: list[str] = []
    statuses_by_number: dict[str, str] = {}
    try:
        with _t2_client_factory() as client:
            entries = client.get_all(project=project)
    except Exception as exc:  # noqa: BLE001 — T2 unreachable is an expected, named failure mode; reported on the census line, never allowed to fail the audit
        return counts, ambiguous, f"T2 unreachable: {type(exc).__name__}: {exc}", statuses_by_number

    by_number: dict[str, dict[str, str]] = {}
    for entry in entries:
        title = entry.get("title", "") if isinstance(entry, dict) else ""
        m = re.match(r"^(?:RDR-)?(\d+)$", title)
        if not m:
            continue
        number = m.group(1)
        content = entry.get("content", "") if isinstance(entry, dict) else ""
        status = _preamble_parse_t2_field(content, "status") or "<no status>"
        by_number.setdefault(number, {})[title] = status

    for number, shapes in by_number.items():
        statuses = set(shapes.values())
        if len(statuses) > 1:
            detail = ", ".join(f"{t}={s}" for t, s in sorted(shapes.items()))
            ambiguous.append(f"{number} ({detail})")
            continue
        status = next(iter(statuses))
        counts[status] += 1
        statuses_by_number[number] = status

    return counts, sorted(ambiguous), None, statuses_by_number


def _rdr_file_statuses(rdr_dir: Path) -> dict[str, str]:
    """``{number: status}`` from ``docs/rdr/*.md`` frontmatter, non-recursive,
    same scope and companion rule as :func:`_rdr_audit_status_findings`
    (a ``kind: companion`` file carries no lifecycle status and is skipped).
    Numbers are the leading digits of the filename, unpadded, matching the
    T2 title shapes the census counts."""
    statuses: dict[str, str] = {}
    for path in sorted(rdr_dir.glob("*.md")):
        if path.name.lower() in _RDR_AUDIT_EXCLUDED_FILENAMES:
            continue
        m = re.match(r"(?:rdr-?)?(\d+)", path.stem, re.IGNORECASE)
        if not m:
            continue
        fm, _text = _preamble_parse_frontmatter(path)
        if fm.get("kind") == "companion" or fm.get("status") is None:
            continue
        statuses[str(int(m.group(1)))] = str(fm["status"]).strip().lower()
    return statuses


def _file_vs_t2_status_drift(
    file_statuses: dict[str, str], t2_statuses: dict[str, str],
) -> list[tuple[str, str, str]]:
    """Every RDR number present on BOTH surfaces whose statuses disagree, as
    sorted ``(number, file_status, t2_status)`` — the detector for the
    drift class nexus-e19sa's deleted reconciler used to arbitrate silently
    (critique [24089] Critical). Numbers on one side only are not drift
    (an unregistered file, a T2-only research note); ``open`` is read as
    ``draft`` on the file side, the pre-accept synonym set-status itself
    honours."""
    drift: list[tuple[str, str, str]] = []
    for number in sorted(set(file_statuses) & set(t2_statuses), key=int):
        file_status = file_statuses[number]
        if file_status == _OPEN_STATUS_ALIAS:
            file_status = "draft"
        if file_status != t2_statuses[number].lower():
            drift.append((number, file_statuses[number], t2_statuses[number]))
    return drift


def _t2_needs_reexamination_markers(
    repo_name: str, *, client_factory: Callable[[], object] | None = None,
) -> tuple[list[tuple[str, str]], str | None]:
    """Every ``needs-reexamination:`` line on every RDR-titled entry in T2
    project ``<repo_name>_rdr`` (RDR-201 P3.3, nexus-j9z30.22), as sorted
    ``(title, marker line)`` pairs -- one pair per marker, so a record
    flagged twice appears twice. Same title-shape rule and same
    never-fail-the-audit posture as :func:`_t2_rdr_status_census`: a T2
    failure comes back as the *error* string, never raised."""
    project = f"{repo_name}_rdr"
    factory = client_factory or _t2_client_factory
    try:
        with factory() as client:  # type: ignore[attr-defined]
            entries = client.get_all(project=project)
    except Exception as exc:  # noqa: BLE001 — T2 unreachable is reported on this line, never allowed to fail the audit
        return [], f"T2 unreachable: {type(exc).__name__}: {exc}"
    rows: list[tuple[str, str]] = []
    for entry in entries:
        title = entry.get("title", "") if isinstance(entry, dict) else ""
        if not re.match(r"^(?:RDR-)?\d+$", title):
            continue
        content = entry.get("content", "") if isinstance(entry, dict) else ""
        for line in str(content).splitlines():
            stripped = line.strip()
            if stripped.startswith(f"{NEEDS_REEXAMINATION_FIELD}:"):
                rows.append((title, stripped))
    rows.sort(key=lambda pair: (int(re.sub(r"\D", "", pair[0])), pair[1]))
    return rows, None


@preamble.command("rdr-audit")
@click.argument("args", nargs=-1)
def preamble_rdr_audit(args: tuple[str, ...]) -> None:
    """Print RDR audit dispatch context."""
    args_str = " ".join(args).strip()

    # Derive current project name: git remote -> git root -> cwd
    def _derive_project_name() -> str:
        try:
            url = subprocess.check_output(
                ["git", "remote", "get-url", "origin"],
                stderr=subprocess.DEVNULL, text=True,
            ).strip()
            if url:
                name = url.rsplit("/", 1)[-1]
                if name.endswith(".git"):
                    name = name[:-4]
                if name:
                    return name
        except Exception:  # noqa: BLE001 — best-effort git lookup; falls back to next strategy
            pass
        try:
            root = subprocess.check_output(
                ["git", "rev-parse", "--show-toplevel"],
                stderr=subprocess.DEVNULL, text=True,
            ).strip()
            if root:
                return Path(root).name
        except Exception:  # noqa: BLE001 — best-effort git lookup; falls back to cwd name
            pass
        return Path.cwd().name

    current_project = _derive_project_name()

    _READONLY_SUBCOMMANDS = {"list", "status", "history"}
    _PRINTONLY_SUBCOMMANDS = {"schedule", "unschedule"}
    _SUBCOMMANDS = _READONLY_SUBCOMMANDS | _PRINTONLY_SUBCOMMANDS

    first_token = args_str.split()[0] if args_str else ""
    if first_token in _SUBCOMMANDS:
        subcommand = first_token
        target = args_str[len(first_token):].strip() or (
            current_project if subcommand != "list" else ""
        )
        safety_class = (
            "read-only" if subcommand in _READONLY_SUBCOMMANDS else "print-only"
        )
        print(f"**Mode:** management subcommand `{subcommand}` ({safety_class})")
        if target:
            print(f"**Target project:** `{target}`")
        else:
            print("**Scope:** all scheduled audits on this machine")
        print()
        if safety_class == "read-only":
            print(
                f"> `{subcommand}` is read-only — no OS state mutation, "
                "no T2 state mutation."
            )
        else:
            print(
                f"> `{subcommand}` is print-only — prints install/uninstall instructions "
                "for user review."
            )
        print()
    else:
        target = first_token or current_project
        print("**Mode:** audit dispatch (default)")
        print(
            f"**Target project:** `{target}`"
            + (" (derived from current repo)" if not first_token else "")
        )
        print()
        print("### Gate loop health")
        print()
        try:
            with _t2_client_factory() as client:
                rows = client.get_all(project=f"{target}_rdr") or []
            health = _gate_loop_health_lines([r for r in rows if isinstance(r, dict)])
            if health:
                for line in health:
                    print(line)
            else:
                print(f"No gate records in `{target}_rdr`.")
        except Exception as exc:  # noqa: BLE001 — an unreachable T2 is a named note, never a silent skip
            print(f"Gate loop health: T2 unreachable ({type(exc).__name__}: {exc}).")
        print()

        home = Path.home()
        roots_env = os.environ.get("NEXUS_PROJECT_ROOTS", "").strip()
        if roots_env:
            roots = [Path(os.path.expanduser(r)) for r in roots_env.split(":") if r]
            roots_source = "NEXUS_PROJECT_ROOTS"
        else:
            roots = [
                home / "git",
                home / "src",
                home / "projects",
                home / "code",
                home / "work",
                home / "dev",
                home / "Documents" / "git",
            ]
            roots_source = "default candidates (set NEXUS_PROJECT_ROOTS to override)"

        candidate_paths = [r / target for r in roots if r.is_dir()]
        found_path = next(
            (p for p in candidate_paths if p.exists() and p.is_dir()), None
        )
        if found_path:
            print(f"**Worktree found:** `{found_path}`")
            postmortem_dir = found_path / "docs" / "rdr" / "post-mortem"
            if postmortem_dir.exists():
                count = len(list(postmortem_dir.glob("*.md")))
                print(f"**Post-mortems available:** {count} files in `{postmortem_dir}`")
            else:
                print(
                    f"> No `docs/rdr/post-mortem/` directory found at `{found_path}`."
                )
        else:
            probed = (
                ", ".join(str(r) for r in roots if r.is_dir())
                or "(no existing roots)"
            )
            print(
                f"> No local worktree found for `{target}`. "
                f"Probed roots ({roots_source}): {probed}."
            )
            print("> Set `NEXUS_PROJECT_ROOTS` to the directory(ies) for project worktrees.")

        print()
        print("**Status vocabulary scan (`docs/rdr/*.md`, non-recursive):**")
        if found_path:
            scan_dir = found_path / "docs" / "rdr"
            if scan_dir.is_dir():
                try:
                    status_domain = frozenset(
                        load_packaged_table("rdr-lifecycle.toml").dimensions["status"].domain
                    )
                except (OSError, TableLoadError, tomllib.TOMLDecodeError) as exc:
                    print(
                        f"> Cannot load the RDR lifecycle table — scan skipped "
                        f"({type(exc).__name__}: {exc})."
                    )
                else:
                    findings, companion_count, scanned_count = (
                        _rdr_audit_status_findings(scan_dir, status_domain)
                    )
                    if findings:
                        for filename, status in findings:
                            print(
                                f"- FINDING: `{filename}` status `{status}` is "
                                "outside the lifecycle domain "
                                f"({', '.join(sorted(status_domain))})"
                            )
                    print(
                        f"> {len(findings)} finding(s), {scanned_count} file(s) "
                        f"scanned, {companion_count} `kind: companion` file(s) skipped."
                    )
            else:
                print(f"> No `docs/rdr/` directory found at `{found_path}` — nothing to scan.")
        else:
            print("> No local worktree found for the target project — nothing to scan.")

        t2_counts, t2_ambiguous, t2_error, t2_by_number = _t2_rdr_status_census(target)
        if t2_error:
            print(f"**T2 `{target}_rdr` status census:** {t2_error}")
        else:
            census_str = (
                ", ".join(f"{status}={count}" for status, count in sorted(t2_counts.items()))
                or "(no entries)"
            )
            if t2_ambiguous:
                census_str += "; ambiguous: " + "; ".join(t2_ambiguous)
            print(f"**T2 `{target}_rdr` status census:** {census_str}")
            scan_dir = (found_path / "docs" / "rdr") if found_path else None
            if scan_dir is not None and scan_dir.is_dir():
                drift = _file_vs_t2_status_drift(_rdr_file_statuses(scan_dir), t2_by_number)
                for number, file_status, t2_status in drift:
                    print(f"- DRIFT: RDR-{number} file=`{file_status}` T2=`{t2_status}`")
                print(
                    f"> {len(drift)} file-vs-T2 status disagreement(s) (nexus-e19sa: nothing "
                    "reconciles these automatically; fix by hand, see nexus-nxn5g)."
                )
        marker_rows, marker_error = _t2_needs_reexamination_markers(target)
        print(f"**Needs re-examination (T2 `{target}_rdr` markers):**", end="")
        if marker_error:
            print(f" {marker_error}")
        elif not marker_rows:
            print(" (none)")
        else:
            print()
            for title, marker in marker_rows:
                print(f"- `{title}`: {marker}")
        print()

        claude_projects = home / ".claude" / "projects"
        if claude_projects.exists():
            match_candidates = list(claude_projects.glob(f"*{target}*"))
            if match_candidates:
                print(
                    f"**Session transcripts available:** {len(match_candidates)} "
                    f"matching directory entries in `~/.claude/projects/`"
                )
            else:
                print(
                    f"> No session transcripts found for `{target}` "
                    "under `~/.claude/projects/`."
                )

    print()


# ---------------------------------------------------------------------------
# preamble phase-review-gate
# ---------------------------------------------------------------------------

def _prg_extract_approach_section(text: str) -> str:
    """Extract the phase-structure section to cross-walk.

    Recognises §Approach plus the common synonym headings RDRs use to
    structure phased work — ``Implementation Plan`` (e.g. conexus RDR-001),
    ``Phases``, and plain ``Plan`` — at ``##``/``###``/``####`` level,
    case-insensitively. Returns the earliest such section in the document
    (nexus-2pw1x).

    ``Approach`` / ``Implementation Plan`` / ``Phases`` tolerate trailing
    heading text (``### Approach (two tracks)``) but are word-anchored so
    they do not match longer words. Bare ``Plan`` is matched ONLY as the
    whole heading name (optionally trailing whitespace) so it does NOT
    false-positive on ``## Planned Work`` / ``## Planning`` (prefix) or
    ``## Plan Optimization`` (a differently-scoped section) — review
    findings on the first cut of this fix.

    An optional ``Proposed`` prefix is honoured (``## Proposed Approach``,
    ``### Proposed Plan``) — the most common RDR phrasing (RDR-176) — but
    ONLY in front of the Approach/Plan synonyms, so ``## Proposed Solution``
    (a differently-scoped section) still does not match (nexus phase-gate fix).
    """
    heading = re.search(
        r"\n(#{2,4})[ \t]+(?:Proposed[ \t]+)?(?:(?:Approach|Implementation Plan|Phases)\b[^\n]*|Plan[ \t]*)\r?\n",
        text,
        re.IGNORECASE,
    )
    if heading:
        start = heading.end()
        heading_depth = len(heading.group(1))
        end_pat = r"\n#{1," + str(heading_depth) + r"} "
        nxt = re.search(end_pat, text[start:])
        return text[start: start + nxt.start()] if nxt else text[start:]
    return ""


_PRG_ITEM_RE = re.compile(r"^(\d+)\.\s+\*\*([^*]+)\*\*[:\s]*(.*)")


def _prg_parse_approach_items(
    approach_text: str,
) -> list[tuple[int, str, str]]:
    """Parse numbered bold items from §Approach text.

    Returns list of (item_num, label, summary).
    """
    items: list[tuple[int, str, str]] = []
    lines = approach_text.splitlines()
    current_num: int | None = None
    current_label = ""
    current_lines: list[str] = []

    for line in lines:
        m = _PRG_ITEM_RE.match(line)
        if m:
            if current_num is not None:
                items.append(
                    (current_num, current_label, " ".join(current_lines).strip())
                )
            current_num = int(m.group(1))
            current_label = m.group(2).strip()
            current_lines = [m.group(3).strip()] if m.group(3).strip() else []
        elif current_num is not None:
            stripped = line.strip()
            if stripped and not stripped.startswith("-"):
                current_lines.append(stripped)

    if current_num is not None:
        items.append(
            (current_num, current_label, " ".join(current_lines).strip())
        )
    return items


# Column 0, like _PRG_ITEM_RE: an INDENTED numbered line is a nested list or
# a recipe inside a code fence, never a missed top-level item (review of
# ad158133b: rdr-037 and rdr-063 both carry them and were refused).
_PRG_ITEM_START_RE = re.compile(r"^(\d+(?:\.\d+)*[a-z]?)[.)](\s|$)")
_PRG_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def _prg_find_unparsed_item_starts(approach_text: str) -> list[str]:
    """Lines of §Approach that look like the start of an item but that
    :func:`_prg_parse_approach_items` would silently absorb as continuation
    text of the previous item (GH #1443).

    The shapes measured to go invisible (8 of 10 items enumerated on a
    downstream RDR, gate reported PASSED) all start at column 0 with a
    number the item regex then rejects: a sub-number (``5a.``, ``5.1.``), a
    paren number (``2)``), a bare number whose label sits on the next line,
    a bold label that wraps before it closes, or a plain ``N. text`` item
    with no bold label at all. Column 0 is the whole test: an indented
    numbered line is a nested list or a recipe, and a line inside a fenced
    code block is code; the first version of this also flagged unclosed
    bold, which is how this repo writes wrapped emphasis, and refused 11 of
    the repo's own RDRs.

    SCOPE. When the section parses numbered items, only the item list's
    own block is scanned: heading to heading around the parsed items, and
    within it up to the first numbered line that restarts at ``1.`` after
    an item (a second list, such as rdr-195's two-point consequences aside
    under the same heading). The extracted section can span several
    ``###`` subsections, and a numbered aside under a later one is not a
    lost item. A section with no parsed items (phase-block structure) is
    scanned whole. A list where one incidental bold phrase makes one step
    parse and the rest are plain steps (rdr-063, rdr-102) IS refused: the
    gate cannot tell which of those steps are items, and enumerating the
    one would be the silent subset this exists to stop.

    The one shape left invisible is a bold label whose number was dropped
    entirely (``**Label**: ...`` at column 0): in this corpus that line is
    a bold aside inside an item far more often than a lost item, so it is
    not flagged. Returned lines are stripped; the caller refuses to
    enumerate rather than pass on a subset, for both §Approach structures.
    """
    lines = approach_text.splitlines()
    item_idx = [k for k, line in enumerate(lines) if _PRG_ITEM_RE.match(line)]
    start, end = 0, len(lines)
    if item_idx:
        start = next(
            (k + 1 for k in range(item_idx[0] - 1, -1, -1) if lines[k].startswith("#")), 0,
        )
        end = next(
            (k for k in range(item_idx[-1] + 1, len(lines)) if lines[k].startswith("#")),
            len(lines),
        )
    unparsed: list[str] = []
    in_fence = False
    seen_item = False
    for line in lines[start:end]:
        if _PRG_FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _PRG_ITEM_RE.match(line):
            seen_item = True
            continue
        m = _PRG_ITEM_START_RE.match(line)
        if not m:
            continue
        if seen_item and m.group(1) == "1":
            break  # a second list restarting at 1. after the items: not a lost item
        unparsed.append(line.strip())
    return unparsed


def _prg_parse_phase_block_items(
    approach_text: str, phase: str | None = None,
) -> list[tuple[int, str, str]]:
    """Parse phase-block sub-bullet §Approach structure (nexus-4u6mt).

    Some RDRs (e.g. RDR-120) structure §Approach as phase blocks rather
    than top-level numbered items::

        **Phase 0: Lint + cutover flag scaffolding**

        - Implement nx doctor --check-storage-boundary
        - Add NX_STORAGE_MODE env-var

        **Phase 1: T3 daemon**

        - ...

    When *phase* is given (e.g. ``"1"`` or ``"Phase 1"``), enumerate the
    bullets of the MATCHING phase block as Items 1..K. When *phase* is
    ``None``, enumerate bullets across ALL phase blocks sequentially.

    Returns ``[]`` when no ``**Phase N: ...**`` header is present (so the
    caller can distinguish "not phase-block structured" from "phase
    block has no bullets"). Label is the bullet's leading bold span if
    present, else a truncated prefix of the bullet text.
    """
    # Normalize the requested phase to its integer, if numeric.
    want_phase: str | None = None
    if phase:
        pm = re.search(r"(\d+)", phase)
        want_phase = pm.group(1) if pm else phase.strip()

    lines = approach_text.splitlines()
    # (phase_num_str, phase_title, [bullet_text, ...])
    blocks: list[tuple[str, str, list[str]]] = []
    cur: tuple[str, str, list[str]] | None = None
    header_re = re.compile(r"^\s*\*\*Phase\s+([0-9.]+)\s*:?\s*([^*]*)\*\*\s*$")
    bullet_re = re.compile(r"^\s*[-*]\s+(.*)")

    for line in lines:
        hm = header_re.match(line)
        if hm:
            if cur is not None:
                blocks.append(cur)
            cur = (hm.group(1).strip(), hm.group(2).strip(), [])
            continue
        if cur is not None:
            bm = bullet_re.match(line)
            if bm and bm.group(1).strip():
                cur[2].append(bm.group(1).strip())
            elif cur[2] and line.strip():
                # A non-bulleted line inside a block after a bullet is that
                # bullet's continuation (a wrapped label or summary), not
                # something to drop on the floor (GH #1443 critique residual).
                cur[2][-1] = cur[2][-1] + " " + line.strip()
    if cur is not None:
        blocks.append(cur)

    if not blocks:
        return []

    # Select blocks: matching phase, or all when unspecified.
    selected: list[tuple[str, str, list[str]]]
    if want_phase is not None:
        selected = [b for b in blocks if b[0] == want_phase]
    else:
        selected = blocks

    items: list[tuple[int, str, str]] = []
    n = 0
    for phase_num, phase_title, bullets in selected:
        for bullet in bullets:
            n += 1
            # Label: leading **bold** span if present, else a short prefix.
            lbl_m = re.match(r"\*\*([^*]+)\*\*[:\s]*(.*)", bullet)
            if lbl_m:
                label = lbl_m.group(1).strip()
                summary = lbl_m.group(2).strip()
            else:
                # First clause / first 60 chars as the label.
                label = bullet.split(".")[0][:60].strip()
                summary = bullet
            # Prefix the label with the phase so the cross-walk table
            # is unambiguous when enumerating across multiple blocks.
            phase_prefix = f"Phase {phase_num}"
            items.append((n, f"{phase_prefix}: {label}", summary))
    return items


def _prg_parse_evidence(evidence_str: str) -> dict[int, str]:
    """Parse 'Item1=val1,Item2=val2,...' -> {1: 'val1', 2: 'val2'}."""
    out: dict[int, str] = {}
    for tok in evidence_str.split(","):
        tok = tok.strip()
        if not tok or "=" not in tok:
            continue
        k, _, v = tok.partition("=")
        num_m = re.search(r"(\d+)", k.strip())
        if num_m:
            out[int(num_m.group(1))] = v.strip()
    return out


@preamble.command("phase-review-gate")
@click.argument("args", nargs=-1)
def preamble_phase_review_gate(args: tuple[str, ...]) -> None:
    """Cross-walk §Approach items against closing beads at a phase boundary."""
    repo_root, repo_name = _preamble_resolve_repo()
    rdr_dir = _preamble_rdr_dir(repo_root)
    rdr_path = Path(repo_root) / rdr_dir
    args_str = " ".join(args).strip()

    # Parse flags
    phase_match = re.search(r"--phase\s+(\S+)", args_str)
    phase_arg = phase_match.group(1) if phase_match else None

    evidence_match = (
        re.search(r"--evidence\s+'([^']+)'", args_str)
        or re.search(r'--evidence\s+"([^"]+)"', args_str)
        or re.search(r"--evidence\s+(\S+)", args_str)
    )
    evidence_arg = evidence_match.group(1) if evidence_match else None

    # Strip flags to find RDR ID
    args_clean = re.sub(r"--phase\s+\S+", "", args_str)
    args_clean = re.sub(r"--evidence\s+'[^']+'", "", args_clean)
    args_clean = re.sub(r'--evidence\s+"[^"]+"', "", args_clean)
    args_clean = re.sub(r"--evidence\s+\S+", "", args_clean).strip()

    id_match = re.search(r"\d+", args_clean)

    if not id_match:
        print(
            "> **Usage**: `nx rdr preamble phase-review-gate -- <id> "
            "--phase <N> [--evidence 'Item1=bead-id,...']`"
        )
        print()
        print("### What this gate does")
        print()
        print(
            "At each phase-review boundary, cross-walk the RDR §Approach sub-items "
            "against the closing beads."
        )
        print(
            "Pass 1 enumerates items; Pass 2 validates evidence."
        )
        print()
        print("**Pass 1** (no --evidence): list approach items for the phase.")
        print("**Pass 2** (with --evidence): validate every item has an evidence pointer.")
        print()
        print("Evidence format: `Item1=nexus-abc1,Item2=nexus-xyz2,Item3=none`")
        print(
            "Use `none` for items explicitly deferred or acknowledged as out-of-phase scope."
        )
        return

    rdr_file = _preamble_find_rdr_file(rdr_path, id_match.group(0))
    if not rdr_file:
        print(f"> **ERROR**: RDR not found for ID: `{id_match.group(0)}`")
        print(f"> Looked in: `{rdr_path}`")
        return

    fm, text = _preamble_parse_frontmatter(rdr_file)
    title = fm.get("title", fm.get("name", rdr_file.stem))
    rdr_num_m = re.search(r"\d+", rdr_file.stem)
    rdr_id_label = rdr_num_m.group(0) if rdr_num_m else rdr_file.stem

    print(f"**Repo:** `{repo_name}`  **RDR:** `{rdr_file.name}`")
    print(f"**Title:** {title}")
    print(f"**Phase:** {phase_arg or '(not specified)'}")
    print()

    # Extract §Approach items
    approach_text = _prg_extract_approach_section(text)
    if not approach_text.strip():
        print(
            "> **ERROR**: No `### Approach` (or `## Implementation Plan` / "
            "`## Phases` / `## Plan`) section found in this RDR."
        )
        print("> Phase-review gate requires a phase-structure section to cross-walk against closing beads.")
        return

    items = _prg_parse_approach_items(approach_text)
    # Guard BOTH structures (critique of ad158133b): the phase-block fallback
    # below absorbs a stray column-0 numbered line just as silently.
    unparsed = _prg_find_unparsed_item_starts(approach_text)
    if unparsed:
        # GH #1443: a line that looks like an item start but fails the item
        # regex used to be absorbed as continuation text, so the gate
        # enumerated a SUBSET of §Approach and could report PASSED on it.
        # Refuse to enumerate: a partial cross-walk is the silent scope
        # reduction this gate exists to catch.
        print(
            f"> **ERROR**: §Approach has {len(unparsed)} line(s) that look like "
            "an item start but do not parse as `N. **Label**: description` "
            "(GH #1443). The gate does not cross-walk a subset."
        )
        for raw in unparsed:
            print(f">   - `{raw[:120]}`")
        print(
            "> Fix the RDR: integer item numbers only (no `5a.`, `5.1.` or "
            "`2)`; renumber or nest as an indented bullet), the bold label "
            "opened and closed on the item's own line, and every label "
            "carrying its number."
        )
        return
    if not items:
        # nexus-4u6mt: fall back to phase-block sub-bullet enumeration
        # (RDR-120-style §Approach). Filters to the requested --phase
        # block; enumerates that block's bullets as Items 1..K.
        items = _prg_parse_phase_block_items(approach_text, phase=phase_arg)
    if not items:
        print("> **ERROR**: §Approach section found but no items parsed.")
        print("> Expected either `N. **Label**: description` numbered items")
        print("> or `**Phase N: title**` blocks followed by `- bullet` lists.")
        if phase_arg:
            print(
                f"> (Searched for phase-block matching `--phase {phase_arg}`; "
                "check the phase number exists in §Approach.)"
            )
        return

    # === PASS 1: enumerate approach items ===
    if not evidence_arg:
        print(f"### §Approach Cross-Walk — Phase {phase_arg or '?'}")
        print()
        print(
            "Enumerate each numbered §Approach item below, then provide an evidence pointer "
            "for each item."
        )
        print()
        print("| # | Label | Evidence needed |")
        print("|---|-------|-----------------|")
        for num, label, _summary in items:
            print(f"| Item{num} | **{label}** | (provide bead-id or `none`) |")
        print()
        example_parts = ",".join(f"Item{num}=nexus-xxxx" for num, _, _ in items)
        print("**Re-invoke with evidence once all items are accounted for:**")
        print()
        print(
            f"nx rdr preamble phase-review-gate -- {rdr_id_label} "
            f"--phase {phase_arg or '1'} --evidence '{example_parts}'"
        )
        print()
        return

    # === PASS 2: validate evidence coverage ===
    evidence = _prg_parse_evidence(evidence_arg)
    failures: list[tuple[int, str, str]] = []
    covered: list[tuple[int, str, str]] = []

    for num, label, _summary in items:
        val = evidence.get(num, "").strip()
        if not val:
            failures.append((num, label, "no evidence pointer supplied"))
        else:
            covered.append((num, label, val))

    if failures:
        print(f"> **BLOCKED** — Phase {phase_arg or '?'} cross-walk incomplete.")
        print(
            f"> {len(failures)} of {len(items)} approach item(s) have no evidence pointer."
        )
        print()
        print("### Missing Evidence")
        print()
        for num, label, reason in failures:
            print(f"- **Item{num}** ({label}): {reason}")
        print()
        print("These items must be accounted for before closing this phase.")
        print()
        return

    # All items covered
    print(f"### APPROACH CROSS-WALK PASSED — Phase {phase_arg or '?'}")
    print()
    print(f"All {len(items)} §Approach items accounted for:")
    print()
    for num, label, val in covered:
        print(f"- Item{num} ({label}) → `{val}`")
    print()
    print("> The gate verifies every §Approach item has a named evidence pointer.")
    print("> Review each pointer manually before allowing the phase close to proceed.")
    print()

    # Write T1 scratch marker (best-effort)
    try:
        subprocess.run(
            [
                "nx", "scratch", "put",
                f"phase-review-gate PASSED: RDR-{rdr_id_label} Phase {phase_arg}",
                "--tags",
                f"phase-review-passed,rdr-{rdr_id_label},phase-{phase_arg}",
            ],
            capture_output=True, timeout=5,
        )
    except Exception:  # noqa: BLE001 — best-effort sentinel write (RDR-121 P2); ignored on failure
        pass

    # Write phase_review_sentinel (best-effort, RDR-121 P2 co-requirement)
    try:
        from nexus.phase_review_sentinel import write_sentinel  # noqa: PLC0415 — circular-dep avoidance: deferred intra-package import
        write_sentinel(rdr_id_label, str(phase_arg or "1"))
    except Exception:  # noqa: BLE001 — best-effort sentinel write (RDR-121 P2); ignored on failure
        pass
