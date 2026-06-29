# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-ax12z: rdr-close back-of-funnel tripwire.

Front-of-funnel RDR lifecycle (create / gate / accept) is instrumented; the
*close* step is convention-only (the `rdr-close` skill), so RDRs routinely
drift: a post-mortem is the last thing authored and the easiest to skip. This
test enforces the one half of the close invariant that is *robustly
deterministic from committed files*:

    For every RDR whose frontmatter ``status`` is ``closed``, a post-mortem
    MUST exist at ``docs/rdr/post-mortem/<NNN>-*.md`` — unless the RDR id is on
    the explicit legacy grandfather waiver (or carries a
    ``postmortem_waiver`` frontmatter key).

Scope decision (documented, not silent — see nexus-ax12z notes and
mem:feedback_phase_closeout_scope_audit):

* The bead's fuller invariant ("an RDR whose *closing beads* are all closed
  must be status:closed AND have a post-mortem") additionally needs bead
  status. The only test-accessible bead snapshot, ``.beads/issues.jsonl``, is
  a STALE legacy export (no beads after 2026-05-10; the live tracker is
  dolt-backed and not committed as JSONL). Keying the gate on it would produce
  a flaky / false-negative tripwire. The "accepted-but-should-be-closed" and
  "closed-beads-imply-close" halves are therefore intentionally out of scope
  until a reliable committed bead snapshot exists; this file covers the
  closed→post-mortem half, which is the most common back-of-funnel miss and is
  100% deterministic from committed RDR + post-mortem files.

The grandfather waiver enumerates the legacy backlog of already-closed RDRs
that predate this gate (drained over time by nexus-t9oy4 and future
post-mortem work). NEW closures not on the list trip the gate.
"""
from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_RDR_DIR = _REPO_ROOT / "docs" / "rdr"
_PM_DIR = _RDR_DIR / "post-mortem"

#: Legacy already-closed RDRs that predate this tripwire (nexus-ax12z, 2026-06-23).
#: This is a GRANDFATHER list, not a blanket waiver: any RDR closed *after* this
#: gate landed must ship a post-mortem (or set ``postmortem_waiver`` in its
#: frontmatter) and must NOT be added here. The list is drained as post-mortems
#: are back-filled (nexus-t9oy4). A vacuous "waive everything" default is
#: deliberately avoided — the set is enumerated so the gate stays live.
_LEGACY_POSTMORTEM_GRANDFATHER: frozenset[int] = frozenset({
    4, 5, 6, 7, 8, 9, 13, 19, 20, 21, 22, 25, 26, 27, 30, 31, 32, 33, 34, 35,
    36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 54, 55, 56, 57, 59, 61, 62,
    64, 66, 68, 69, 70, 71, 72, 75, 76, 77, 78, 79, 80, 81, 82, 83, 84, 85, 86,
    87, 89, 91, 92, 93, 94, 95, 96, 97, 98, 100, 104, 105, 108, 109, 120, 121,
    125, 129, 130, 137, 139, 149, 153, 159, 160, 161, 164,
})


def _rdr_numeric_id(stem: str) -> int | None:
    nums = re.findall(r"\d+", stem)
    return int(nums[0]) if nums else None


def _frontmatter_status(text: str) -> str | None:
    """Return the lowercased ``status:`` value from YAML frontmatter, or None."""
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    m = re.search(r"^status:\s*(\S+)", parts[1], re.MULTILINE)
    return m.group(1).strip().lower() if m else None


def _has_frontmatter_waiver(text: str) -> bool:
    if not text.startswith("---"):
        return False
    parts = text.split("---", 2)
    if len(parts) < 3:
        return False
    return re.search(r"^postmortem_waiver:\s*\S", parts[1], re.MULTILINE) is not None


def _post_mortem_ids() -> set[int]:
    ids: set[int] = set()
    if not _PM_DIR.is_dir():
        return ids
    for f in _PM_DIR.glob("*.md"):
        nid = _rdr_numeric_id(f.stem)
        if nid is not None:
            ids.add(nid)
    return ids


def test_post_mortem_dir_exists() -> None:
    assert _PM_DIR.is_dir(), f"missing post-mortem dir: {_PM_DIR}"


def test_closed_rdrs_have_post_mortems() -> None:
    """Every status:closed RDR has a post-mortem, modulo the grandfather list."""
    pm_ids = _post_mortem_ids()
    offenders: list[str] = []
    for f in sorted(_RDR_DIR.glob("rdr-*.md")):
        nid = _rdr_numeric_id(f.stem)
        if nid is None:
            continue
        text = f.read_text(errors="replace")
        if _frontmatter_status(text) != "closed":
            continue
        if nid in pm_ids:
            continue
        if nid in _LEGACY_POSTMORTEM_GRANDFATHER:
            continue
        if _has_frontmatter_waiver(text):
            continue
        offenders.append(f"RDR-{nid:03d} ({f.name})")
    assert not offenders, (
        "These RDRs are status:closed but have no post-mortem at "
        "docs/rdr/post-mortem/<NNN>-*.md. Author one (nexus-t9oy4 pattern), or "
        "set `postmortem_waiver:` in the RDR frontmatter with a reason. Do NOT "
        "add new ids to _LEGACY_POSTMORTEM_GRANDFATHER — that list is frozen to "
        f"the pre-gate backlog.\nOffenders: {offenders}"
    )


#: Status words recognised in the README index status cell (kept in sync with
#: ``nexus.commands.rdr._KNOWN_STATUSES``).
_README_STATUS_WORDS: frozenset[str] = frozenset({
    "draft", "proposed", "accepted", "closed", "deferred", "superseded",
    "scrapped", "abandoned", "revised", "locked", "final",
})


def _readme_status_cell(readme_text: str, filename: str) -> str | None:
    """Return the lowercased status cell of the README index row for *filename*.

    Matches the row by the RDR filename link and returns the first cell whose
    content is a known status word (robust to the table's column ordering).
    Returns None when no row references the file.
    """
    for line in readme_text.splitlines():
        if f"]({filename})" not in line or "|" not in line:
            continue
        for cell in line.split("|"):
            if cell.strip().lower() in _README_STATUS_WORDS:
                return cell.strip().lower()
    return None


def test_readme_status_matches_frontmatter() -> None:
    """README index-row status must agree with the RDR file's frontmatter.

    This is the committed-file-only backstop for the accept/close *ledger-drift*
    class (RDR-165 / RDR-166): the lifecycle skills advance status in T2 and the
    RDR file via ``nx rdr set-status``; if a future edit flips one surface but
    not the other, this catches it in CI. No T2 / bead snapshot needed — both
    the README and the RDR markdown are committed, so the invariant is 100%
    deterministic (unlike the T2-vs-file parity the post-mortem half cannot test).

    Scope: parity is asserted only for RDRs that *have* a README index row.
    README completeness (every RDR appears in the index) is a separate concern
    and intentionally out of scope here.
    """
    readme_path = _RDR_DIR / "README.md"
    assert readme_path.is_file(), f"missing RDR index: {readme_path}"
    readme_text = readme_path.read_text(errors="replace")

    offenders: list[str] = []
    for f in sorted(_RDR_DIR.glob("rdr-*.md")):
        text = f.read_text(errors="replace")
        fm_status = _frontmatter_status(text)
        if fm_status is None:
            continue
        cell = _readme_status_cell(readme_text, f.name)
        if cell is None:
            continue  # no index row → out of scope
        if cell != fm_status:
            offenders.append(f"{f.name}: file=`{fm_status}` README=`{cell}`")
    assert not offenders, (
        "These RDRs' README index-row status disagrees with their file "
        "frontmatter status (ledger drift). Reconcile with "
        "`nx rdr set-status <id> <status>` or fix the README cell:\n"
        + "\n".join(offenders)
    )


def test_grandfather_list_does_not_mask_existing_post_mortems() -> None:
    """Keep the grandfather list honest: an id with a post-mortem already
    present must not also sit on the waiver (it would be dead weight and could
    later mask a deleted post-mortem)."""
    pm_ids = _post_mortem_ids()
    stale = sorted(pm_ids & _LEGACY_POSTMORTEM_GRANDFATHER)
    assert not stale, (
        "These ids are on _LEGACY_POSTMORTEM_GRANDFATHER but already have a "
        f"post-mortem — remove them from the grandfather set: {stale}"
    )
