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
from datetime import datetime, timezone
from pathlib import Path

import click
import yaml

from nexus.tables.load import Table, TableLoadError, load_packaged_table
from nexus.tables.resolve import resolve


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
    "--tiers",
    default="cheap,strong",
    show_default=True,
    help="Two model tiers to dispatch, comma-separated (see nexus.operators.model_tiers).",
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
    tiers: str,
    timeout: float,
    max_budget_usd: float,
    as_json: bool,
) -> None:
    """Multi-model repeatability diff of RDR's design text (nexus-axwpn).

    Sends the Technical Design section to two model tiers, asks each for
    an implementation plan, and reports where the plans diverge: steps,
    files, decisions. A divergence is a place the text left open. Exits
    0 with a report; exits 2 when there is nothing to repeat.
    """
    import asyncio  # noqa: PLC0415 — only this verb runs an event loop
    import json as _json  # noqa: PLC0415

    from nexus.operators.model_tiers import UnknownTierError, resolve_model_for_tier  # noqa: PLC0415
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

    tier_names = [t.strip() for t in tiers.split(",") if t.strip()]
    if len(tier_names) != 2:
        click.echo("nx rdr repeat: --tiers needs exactly two tiers", err=True)
        sys.exit(2)
    try:
        models = [resolve_model_for_tier(t) for t in tier_names]
    except UnknownTierError as exc:
        click.echo(f"nx rdr repeat: {exc}", err=True)
        sys.exit(2)

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
                for m in models
            )
        )

    try:
        payloads = asyncio.run(_run())
        plans = [parse_plan(m, p) for m, p in zip(models, payloads, strict=True)]
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

@preamble.command("rdr-research")
@click.argument("args", nargs=-1)
def preamble_rdr_research(args: tuple[str, ...]) -> None:
    """Print RDR research context (file Research Findings + T2 entries)."""
    repo_root, repo_name = _preamble_resolve_repo()
    rdr_dir = _preamble_rdr_dir(repo_root)
    rdr_path = Path(repo_root) / rdr_dir
    args_str = " ".join(args).strip()

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
        m = re.match(r"^(\d+)\.\s+\*\*([^*]+)\*\*[:\s]*(.*)", line)
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
