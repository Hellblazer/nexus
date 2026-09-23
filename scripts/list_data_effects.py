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

RELAY ATTESTATION (nexus-iu43o). ``--record-relay-attestation`` writes
``docs/data-effect-relay/<to-ref>.json`` after the table above has been
pasted into the conexus handoff; ``--verify-relay-attestation`` is what a
release battery runs to refuse when that never happened (exit 1), pass
when it did (exit 0), or pass as not-applicable when the range has no
data-effecting changesets at all (exit 0) — see
:func:`record_relay_attestation` / :func:`verify_relay_attestation`.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from data_effect_lint import (
    ChangesetInfo,
    DataEffectFinding,
    classify_changeset,
    extract_data_effect_text,
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
        Path(file_attr).name
        for el in root.iter(f"{ns}include")
        if (file_attr := el.get("file"))
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
        comment = finding.changeset.comment_text
        effect_text = extract_data_effect_text(comment)
        if effect_text is not None:
            effect_cell = effect_text.replace("|", "\\|")
        else:
            effect_cell = "**MISSING** — no `DATA EFFECT:` line in this changeset's `<comment>`"
        predicate_cell = _census_predicate(finding).replace("|", "\\|").replace("\n", "<br>")
        lines.append(
            f"| {row.file} | {row.changeset_id} | {effect_cell} | `{predicate_cell}` |"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Relay attestation (nexus-iu43o) -- mirrors docs/release-arming/'s shape
# (one JSON file per tag, a reasoned reader/writer split, checked in a
# release battery) for the OTHER direction of the same relay: release-
# arming is conexus-authored and nexus-read (is the DEPLOY armed); this is
# nexus-authored and nexus-read (did the DATA EFFECT table this script
# itself produces actually reach the relay, or did a human run the script,
# read the table, and then forget to paste it in). Both write/read sides
# live in this ONE repo -- unlike release-arming, there is no cross-repo
# ownership split to preserve, so one script owns both halves.
#
# THE GAP THIS CLOSES (nexus-iu43o, follow-up from nexus-f7dwp critic pass
# T2 nexus/f7dwp-critic-pass-2026-09-13). The engine-release skill already
# says "run this script, paste its table into the relay verbatim" -- prose,
# never checked. A release battery can now run --verify-relay-attestation
# and refuse if the table this script computed for the exact tag range was
# never recorded as included.
# ---------------------------------------------------------------------------

_RELAY_ATTESTATION_RELDIR = "docs/data-effect-relay"


def _relay_attestation_dir(repo_root: Path) -> Path:
    return repo_root / _RELAY_ATTESTATION_RELDIR


def _relay_attestation_path(to_ref: str, repo_root: Path) -> Path:
    return _relay_attestation_dir(repo_root) / f"{to_ref}.json"


def _row_key(row: AddedRow) -> str:
    return f"{row.file}:{row.changeset_id}"


def record_relay_attestation(
    from_ref: str, to_ref: str, repo_root: Path = _REPO_ROOT
) -> Path:
    """Write the attestation that this range's DATA EFFECT table was
    generated and is about to be pasted into the relay -- the WRITER half,
    run by the human/AI preparing the handoff right after generating the
    table (mirrors ``docs/release-arming``'s writer, conexus-side there,
    nexus-side here).

    ``changeset_ids`` is the exact ``file:changeset_id`` set
    :func:`find_added_data_effecting_changesets` computed for THIS range --
    the same computation :func:`verify_relay_attestation` re-runs at check
    time, so a stale attestation (a later commit added a NEW data-effecting
    changeset after this was recorded) is caught as a mismatch rather than
    silently trusted. Overwrites any existing attestation for *to_ref* --
    the latest recording for a given tag is the one that matters.
    """
    rows = find_added_data_effecting_changesets(from_ref, to_ref, repo_root)
    body = {
        "engine_tag": to_ref,
        "from_tag": from_ref,
        "changeset_ids": sorted(_row_key(r) for r in rows),
        "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    out_dir = _relay_attestation_dir(repo_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = _relay_attestation_path(to_ref, repo_root)
    path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def verify_relay_attestation(
    from_ref: str, to_ref: str, repo_root: Path = _REPO_ROOT
) -> int:
    """The READER half -- what a release battery runs. Three outcomes:

    - The range has NO data-effecting changesets at all: nothing could ever
      have been pasted into a relay, so there is nothing to attest.
      NOT-APPLICABLE, exit 0 (an informational pass, same shape as
      :func:`check_release_arming`'s "requirement: not_required" advisory).
    - An attestation exists for *to_ref*, names this EXACT ``from_ref`` (a
      stale attestation from a different range must not silently pass),
      and its recorded changeset set matches what this range actually
      computes today: PASS, exit 0.
    - Anything else (no file, unreadable/corrupt JSON, wrong from_tag,
      wrong engine_tag, or a changeset-set mismatch): REFUSE, exit 1, with
      the exact reason and remedy printed.
    """
    rows = find_added_data_effecting_changesets(from_ref, to_ref, repo_root)
    if not rows:
        print(
            f"NOT-APPLICABLE: no data-effecting changesets added in {from_ref}..{to_ref} "
            "-- nothing for a relay to attest."
        )
        return 0

    actual_ids = {_row_key(r) for r in rows}
    path = _relay_attestation_path(to_ref, repo_root)
    remedy = (
        f"run: uv run python scripts/list_data_effects.py {from_ref} {to_ref} "
        "--record-relay-attestation (AFTER pasting its table into the relay)"
    )

    if not path.exists():
        print(
            f"REFUSED: no relay attestation at {path} for {from_ref}..{to_ref} "
            f"({len(rows)} data-effecting changeset(s) in range: {sorted(actual_ids)}). "
            f"{remedy}",
            file=sys.stderr,
        )
        return 1

    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"REFUSED: {path} is unreadable or not valid JSON: {exc}. {remedy}", file=sys.stderr)
        return 1
    if not isinstance(body, dict):
        print(f"REFUSED: {path} top level is not a JSON object. {remedy}", file=sys.stderr)
        return 1

    if body.get("engine_tag") != to_ref:
        print(
            f"REFUSED: {path} declares engine_tag={body.get('engine_tag')!r}, expected {to_ref!r}. "
            f"{remedy}",
            file=sys.stderr,
        )
        return 1
    if body.get("from_tag") != from_ref:
        print(
            f"REFUSED: {path} declares from_tag={body.get('from_tag')!r}, expected {from_ref!r} "
            "-- this attestation is for a different range. "
            f"{remedy}",
            file=sys.stderr,
        )
        return 1

    declared_ids = set(body.get("changeset_ids") or [])
    if declared_ids != actual_ids:
        missing = actual_ids - declared_ids
        extra = declared_ids - actual_ids
        print(
            f"REFUSED: {path}'s attested changeset set does not match {from_ref}..{to_ref} today "
            f"(not attested: {sorted(missing)}; attested but no longer in range: {sorted(extra)}). "
            f"{remedy}",
            file=sys.stderr,
        )
        return 1

    print(f"RELAY ATTESTATION OK: {path} covers {len(actual_ids)} changeset(s) in {from_ref}..{to_ref}.")
    return 0


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
    parser.add_argument(
        "--record-relay-attestation",
        action="store_true",
        help=(
            "Record (nexus-iu43o) that this range's DATA EFFECT table was "
            "generated for the relay, at docs/data-effect-relay/<to_ref>.json. "
            "Run AFTER pasting the table this script prints into the relay, "
            "never before -- the attestation should confirm work already done."
        ),
    )
    parser.add_argument(
        "--verify-relay-attestation",
        action="store_true",
        help=(
            "Refuse (exit 1) unless this range's relay attestation exists and "
            "matches what this range computes today; exit 0 (informational) "
            "when the range has no data-effecting changesets at all. What a "
            "release battery runs."
        ),
    )
    args = parser.parse_args(argv)
    if args.record_relay_attestation and args.verify_relay_attestation:
        parser.error("--record-relay-attestation and --verify-relay-attestation are exclusive")
    if args.record_relay_attestation:
        path = record_relay_attestation(args.from_ref, args.to_ref, args.repo_root)
        print(f"recorded relay attestation: {path}")
        return 0
    if args.verify_relay_attestation:
        return verify_relay_attestation(args.from_ref, args.to_ref, args.repo_root)
    return check(args.from_ref, args.to_ref, args.repo_root)


if __name__ == "__main__":
    raise SystemExit(main())
