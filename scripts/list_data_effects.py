#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""List DATA EFFECT changesets added between two refs (nexus-f7dwp).

THE GAP THIS CLOSES. A changeset that changes or deletes existing rows
(tuples-003-2's DELETE, tuples-004-1's irreversible NULL) was disclosed,
before this tool, only in its own ``<comment>`` prose, a commit message,
and ``docs/tuple-space.md`` — no record a deployer reads carried it. For
the v0.1.118 engine-release handoff, both effects were typed into the
conexus handoff by hand. This script is the mechanical replacement: run
it between the previous and current engine tags, paste its table into the
handoff, done.

WHAT IT DOES. Walks ``db.changelog-master.xml``'s include order at
*to-ref*, finds every changeset id that exists at *to-ref* but did not
exist (in the same file) at *from-ref* — a brand-new file counts every
one of its changesets as added — classifies each with
``scripts/data_effect_lint.py``'s shared classifier, and prints a
markdown table of the ones that are DATA-EFFECTING (additive changesets,
the overwhelming majority of any release, are omitted entirely — this is
a disclosure tool, not a changelog diff).

Each row carries the changeset's own ``DATA EFFECT:`` line (or
``MISSING`` — a non-zero exit — if a data-effecting changeset added in
this range still lacks one; the lint test
``tests/test_changelog_data_effect_lint.py`` is what stops that from
reaching a release in the first place, this is the release-time
belt-and-suspenders read) plus a CENSUS PREDICATE column: the exact
matched SQL statement (or, for a structured Liquibase change-type
element, a placeholder naming the tag) that a deployer can turn into a
``SELECT count(*) FROM ... WHERE ...`` probe against their OWN fork
before trusting a predicted row count — see the engine-release skill's
handoff step for how conexus is expected to use this column.

USAGE::

    uv run python scripts/list_data_effects.py v0.1.117 v0.1.118
    uv run python scripts/list_data_effects.py <from-ref> <to-ref> [--changelog-dir PATH]

Reads changelog file content via ``git show <ref>:<path>`` — never
checks either ref out, so it is safe to run against the current working
tree regardless of what is staged or dirty. Exit codes: ``0`` clean
(nothing added, or everything added carries its DATA EFFECT line);
``1`` at least one added data-effecting changeset is MISSING its line;
``2`` a ref or path could not be resolved (git error, not a data
question).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from data_effect_lint import (
    ChangesetInfo,
    DataEffectFinding,
    classify_changeset,
    has_data_effect_line,
    iter_changesets_from_text,
    parse_master_include_order,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_CHANGELOG_RELDIR = "service/src/main/resources/db/changelog"


def _git_show(ref: str, path: str, repo_root: Path) -> str | None:
    """Return the file's content at *ref*, or ``None`` if it doesn't exist
    there (a genuinely new file, not a git error)."""
    proc = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.lower()
        if "exists on disk, but not in" in stderr or "does not exist" in stderr:
            return None
        raise RuntimeError(f"git show {ref}:{path} failed: {proc.stderr.strip()}")
    return proc.stdout


def _master_include_order_at_ref(ref: str, repo_root: Path) -> list[str]:
    content = _git_show(ref, f"{_CHANGELOG_RELDIR}/db.changelog-master.xml", repo_root)
    if content is None:
        raise RuntimeError(f"db.changelog-master.xml does not exist at {ref}")
    import xml.etree.ElementTree as ET

    root = ET.fromstring(content)
    ns = "{http://www.liquibase.org/xml/ns/dbchangelog}"
    return [
        Path(el.get("file")).name for el in root.iter(f"{ns}include") if el.get("file")
    ]


def _changesets_at_ref(ref: str, basename: str, repo_root: Path) -> dict[str, ChangesetInfo]:
    """Return {changeset_id: ChangesetInfo} for *basename* at *ref*, or an
    empty dict if the file does not exist there (a brand-new file)."""
    content = _git_show(ref, f"{_CHANGELOG_RELDIR}/{basename}", repo_root)
    if content is None:
        return {}
    return {cs.changeset_id: cs for cs in iter_changesets_from_text(content, basename)}


@dataclass
class AddedRow:
    file: str
    changeset_id: str
    finding: DataEffectFinding | None  # None when not data-effecting


def find_added_data_effecting_changesets(
    from_ref: str, to_ref: str, repo_root: Path = _REPO_ROOT
) -> list[AddedRow]:
    """Every changeset added between *from_ref* and *to_ref* that is
    data-effecting, in ``db.changelog-master.xml``'s *to_ref* include order.

    "Added" means: exists at *to_ref* under this filename, did not exist at
    *from_ref* under this filename (a whole new file counts every one of
    its changesets; an existing file counts only its new ids — an EDITED
    changeset with the SAME id is invisible here by design, since Liquibase
    forbids editing an already-shipped changeset's body in place, so a
    genuinely new data effect always arrives under a new id).
    """
    to_order = _master_include_order_at_ref(to_ref, repo_root)
    rows: list[AddedRow] = []

    for basename in to_order:
        to_changesets = _changesets_at_ref(to_ref, basename, repo_root)
        from_changesets = _changesets_at_ref(from_ref, basename, repo_root)
        added_ids = set(to_changesets) - set(from_changesets)
        if not added_ids:
            continue
        # Preserve to_ref's document order among the added ids.
        for cs_id, cs in to_changesets.items():
            if cs_id not in added_ids:
                continue
            reasons = classify_changeset(cs.sql_text, cs.structured_tags)
            if not reasons:
                continue
            finding = DataEffectFinding(changeset=cs, reasons=reasons)
            rows.append(AddedRow(file=basename, changeset_id=cs_id, finding=finding))

    return rows


def _census_predicate(finding: DataEffectFinding) -> str:
    """One-line census predicate a deployer can adapt into a ``SELECT
    count(*) ... WHERE ...`` probe: the exact matched statement(s), or a
    placeholder for a structured change-type reason with no <sql> text."""
    parts = []
    for r in finding.reasons:
        if r.statement:
            parts.append(r.statement.strip())
        else:
            parts.append(f"<structured {r.kind.split(':', 1)[-1]} element — inspect changeset body>")
    return " ;\n".join(parts)


def render_markdown_table(rows: list[AddedRow]) -> str:
    if not rows:
        return "No data-effecting changesets added in this range."

    header = "| File | Changeset | Data effect | Census predicate |\n"
    header += "|---|---|---|---|\n"
    lines = [header.rstrip("\n")]
    for row in rows:
        finding = row.finding
        assert finding is not None
        comment = finding.changeset.comment_text or ""
        if has_data_effect_line(comment):
            effect_lines = [
                line.strip()
                for line in comment.splitlines()
                if "DATA EFFECT:" in line
            ]
            effect_cell = " ".join(effect_lines).replace("|", "\\|")
        else:
            effect_cell = "**MISSING** — no `DATA EFFECT:` line in this changeset's `<comment>`"
        predicate_cell = _census_predicate(finding).replace("|", "\\|").replace("\n", "<br>")
        lines.append(
            f"| {row.file} | {row.changeset_id} | {effect_cell} | `{predicate_cell}` |"
        )
    return "\n".join(lines)


def check(from_ref: str, to_ref: str, repo_root: Path = _REPO_ROOT) -> int:
    try:
        rows = find_added_data_effecting_changesets(from_ref, to_ref, repo_root)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(render_markdown_table(rows))

    missing = [
        row
        for row in rows
        if not has_data_effect_line(row.finding.changeset.comment_text if row.finding else None)
    ]
    if missing:
        print(
            f"\n{len(missing)} added data-effecting changeset(s) MISSING a DATA EFFECT: line "
            "— fix before this range ships (see tests/test_changelog_data_effect_lint.py).",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("from_ref", help="Older git ref (e.g. the previous engine tag).")
    parser.add_argument("to_ref", help="Newer git ref (e.g. the candidate engine tag / HEAD).")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=_REPO_ROOT,
        help=f"Repository root to run git show in (default: {_REPO_ROOT}).",
    )
    args = parser.parse_args(argv)
    return check(args.from_ref, args.to_ref, args.repo_root)


if __name__ == "__main__":
    raise SystemExit(main())
