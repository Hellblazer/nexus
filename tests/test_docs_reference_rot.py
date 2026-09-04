# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-tpuct: commit SHAs, bead ids and RDR ids cited in docs/ must resolve.

A hex-token sweep of docs/rdr on 2026-09-01 found 147 commit-shaped
citations with 29 dangling. The repo squash-merges, so a feature-branch
sha cited in an RDR's iteration log stops existing the day the PR lands,
and a decision record then narrates its own history in commits nobody can
open. Bead ids rot the same way when beads are compacted; RDR numbers rot
when an arc is scrapped.

Three shapes, each resolved against the thing it names, never against a
snapshot of it:

1. **Commit SHAs**: 7-12 or 40 hex characters containing at least one
   letter and one digit, resolved with ``git cat-file --batch-check``.
   Session ids, agent run ids, placeholders and content-hash prefixes
   share the shape; each one already in the tree is allowlisted BY VALUE
   with its reason below, so a new one has to be named or fixed.
2. **Bead ids**: ``nexus-`` plus exactly five ``[0-9a-z]`` (the live id
   width; four-character ids are the pre-compaction era and are gone in
   bulk), resolved against ``bd list --all``. A child id (``nexus-a2wmi.12``)
   or a branch-shaped suffix (``nexus-cbo4a-batch1a``) resolves through its
   five-character base; 285 of 1,958 citations carry such a suffix and the
   first cut skipped them all (review [24378] Major 1). The tracked
   ``.beads/issues.jsonl`` is NOT the store (255 rows from 2026-05; the
   store is Dolt), so this leg needs ``bd`` and SKIPS, saying so, where it
   is absent (CI). The other two legs still run there.
3. **RDR ids**: ``RDR-NNN`` must match a ``docs/rdr/rdr-NNN-*.md`` file.

Non-vacuity: each leg asserts it found citations to check, and a planted
bogus value of each shape reds its leg (``test_planted_*``).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).parent.parent
DOCS = REPO_ROOT / "docs"
RDR_DIR = DOCS / "rdr"

Cite = tuple[str, str, int]  # (value, relpath, lineno)

_SHA_RE = re.compile(r"(?<![0-9a-zA-Z_/.\-])([0-9a-f]{7,12}|[0-9a-f]{40})(?![0-9a-zA-Z_/\-])")
_BEAD_RE = re.compile(r"\bnexus-([0-9a-z]{5})(?![0-9a-z])")
_RDR_RE = re.compile(r"\bRDR-(\d{3})\b")

#: Dangling commit-shaped tokens that are not commits. Value -> reason.
#: Enumerated from the first run (2026-09-04, 23 of 194 commit-shaped
#: tokens in docs/ did not resolve). Add a row only with a reason a reader
#: can check; fix the citation instead when it was meant to be a commit.
SHA_ALLOWLIST: dict[str, str] = {
    "a1b2c3d4e5f6": "docs/catalog.md: chash placeholder in a usage example",
    "abc123def456": "rdr-053: chash placeholder in a usage example",
    "abc1234": "rdr-018: placeholder in an example message",
    "abc12345": "rdr-034: placeholder in an example message",
    "abcd1234": "rdr-083: chash placeholder in a usage example",
    "a337b930": "rdr-189: chunk_text_hash prefix, not a commit",
    "571b8edd": "rdr-049: example repo_hash field in a JSON sample",
    "2ad2825c": "rdr-049: example repo_hash field in a JSON sample",
    "abe8e36d": "docs/exploration: agent run id",
    "a78c5a1c": "docs/exploration: agent run id",
    "cac4bda5": "post-mortem 184: Claude session id",
    "bfbfa2fe": "post-mortem 184: Claude session id",
    "b819e8f3": "post-mortem 184: Claude session id",
    "cfaab35d": "rdr-120: Claude session id",
    "c76c1995": "rdr-149: Claude session id",
    "9bb22dc2": "rdr-182: Claude session id",
    "1aa8e0f": "rdr-161: sha in the sigstore/cosign-installer repo, not this one",
    "bc9f2a0": "rdr-201: sha in the cwensel/intrastate repo, not this one",
    "a228e079": "docs/wire-contract-pending.md: sha in the conexus repo, not this one",
    "433b036": "rdr-066: release commit squashed away at merge (2026-04-11); PR #148 is the durable pointer",
    "cbe7290": "rdr-066: feature-branch commit squashed away at merge (2026-04-11)",
    "6cf0703": "rdr-066: feature-branch commit squashed away at merge (2026-04-11)",
    "44e9fe9": "rdr-066: feature-branch commit squashed away at merge (2026-04-11)",
}

#: ``nexus-`` + five letters that are prose, not ids, and ids that are gone.
BEAD_ALLOWLIST: dict[str, str] = {
    "nexus-owned": "prose: 'nexus-owned' (adjective)",
    "nexus-aware": "prose: 'nexus-aware' (adjective)",
    "nexus-smart": "prose: 'nexus-smart-rules' (a DEVONthink smart-rule name)",
    "nexus-audit": "prose: 'nexus-audit-loop' (RDR-067's name for the loop)",
    "nexus-inode": "prose: 'nexus-inode-*' (a pathname fragment)",
    "nexus-shkww": "post-mortem 175: bead deleted; the record narrates its own misfiling",
}

#: RDR numbers with no file in docs/rdr/. Value -> reason.
RDR_ALLOWLIST: dict[str, str] = {
    "003": "palinex's RDR-003, cited as such in rdr-127",
    "114": "scrapped 2026-05-19 with the RDR-110-119 arc (docs/rdr/README.md)",
    "115": "scrapped 2026-05-19 with the RDR-110-119 arc (docs/rdr/README.md)",
    "117": "scrapped 2026-05-19 with the RDR-110-119 arc (docs/rdr/README.md)",
    "326": "arcaneum's RDR-326, cited as such in rdr-090",
    "651": "conexus's RDR-651, cited as such in rdr-170",
}


def _docs() -> list[Path]:
    return sorted(DOCS.rglob("*.md"))


def _scan(pattern: re.Pattern[str], files: list[Path] | None = None) -> list[Cite]:
    out: list[Cite] = []
    for path in files if files is not None else _docs():
        rel = str(path.relative_to(REPO_ROOT)) if path.is_relative_to(REPO_ROOT) else str(path)
        for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            for m in pattern.finditer(line):
                out.append((m.group(1), rel, lineno))
    return out


def _sha_cites(files: list[Path] | None = None) -> list[Cite]:
    """Commit-shaped tokens: hex with at least one letter AND one digit.
    All-digit runs are numbers; all-letter runs are words like ``deadbeef``
    only by accident and never a cited commit in this tree."""
    return [
        c
        for c in _scan(_SHA_RE, files)
        if any(ch.isalpha() for ch in c[0]) and any(ch.isdigit() for ch in c[0])
    ]


def _unresolved_shas(values: set[str]) -> set[str]:
    if not values:
        return set()
    proc = subprocess.run(
        ["git", "cat-file", "--batch-check"],
        cwd=REPO_ROOT,
        input="".join(f"{v}^{{commit}}\n" for v in sorted(values)),
        capture_output=True,
        text=True,
        check=False,
    )
    missing: set[str] = set()
    for line in proc.stdout.splitlines():
        if line.endswith(" missing") or line.endswith(" ambiguous"):
            missing.add(line.split()[0].split("^")[0])
    return missing


def _rdr_files() -> set[str]:
    return {
        m.group(1)
        for p in RDR_DIR.glob("rdr-*.md")
        if (m := re.match(r"rdr-(\d{3})-", p.name))
    }


def _bd_ids() -> set[str]:
    """Live bead ids from ``bd``. Raises :class:`BdUnavailable` naming WHY
    when bd is absent, exits non-zero, or returns something that is not
    the issue list; the caller skips with that reason, so "not installed"
    and "installed but broken" read differently in the summary."""
    if shutil.which("bd") is None:
        raise BdUnavailable("bd is not on PATH")
    proc = subprocess.run(
        ["bd", "list", "--all", "--json", "--skip-labels"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise BdUnavailable(f"bd list exited {proc.returncode}: {proc.stderr.strip()[:200]}")
    try:
        return {row["id"] for row in json.loads(proc.stdout)["issues"]}
    except (ValueError, KeyError, TypeError) as exc:
        raise BdUnavailable(f"bd list --json returned an unexpected shape: {exc}") from exc


class BdUnavailable(Exception):
    """bd cannot answer here; the bead leg skips and says why."""


def _report(kind: str, bad: list[Cite]) -> str:
    lines = [f"{len(bad)} {kind} citation(s) in docs/ do not resolve and are not allowlisted:"]
    lines += [f"  {rel}:{lineno}: {value}" for value, rel, lineno in bad]
    lines.append("Fix the citation, or add the value to the allowlist in this file with a reason.")
    return "\n".join(lines)


# ── the three legs ───────────────────────────────────────────────────────────


def test_cited_commit_shas_resolve() -> None:
    cites = _sha_cites()
    assert len(cites) >= 100, f"only {len(cites)} commit-shaped citations found; the scan is broken"
    missing = _unresolved_shas({c[0] for c in cites})
    bad = [c for c in cites if c[0] in missing and c[0] not in SHA_ALLOWLIST]
    assert not bad, _report("commit", bad)


def test_sha_allowlist_carries_no_dead_rows() -> None:
    """A row for a value no longer cited, or one that now resolves, is a
    rule nobody can see the reason for; delete it."""
    cited = {c[0] for c in _sha_cites()}
    dead = sorted(v for v in SHA_ALLOWLIST if v not in cited)
    assert not dead, f"SHA_ALLOWLIST rows no longer cited anywhere in docs/: {dead}"
    resolving = sorted(set(SHA_ALLOWLIST) - _unresolved_shas(set(SHA_ALLOWLIST)))
    assert not resolving, f"SHA_ALLOWLIST rows that resolve as commits now: {resolving}"


def test_cited_rdr_ids_resolve() -> None:
    cites = _scan(_RDR_RE)
    assert len(cites) >= 100, f"only {len(cites)} RDR citations found; the scan is broken"
    files = _rdr_files()
    bad = [c for c in cites if c[0] not in files and c[0] not in RDR_ALLOWLIST]
    assert not bad, _report("RDR", bad)
    dead = sorted(v for v in RDR_ALLOWLIST if v in files or v not in {c[0] for c in cites})
    assert not dead, f"RDR_ALLOWLIST rows that resolve or are no longer cited: {dead}"


def test_cited_bead_ids_resolve() -> None:
    try:
        ids = _bd_ids()
    except BdUnavailable as exc:
        pytest.skip(f"bead-id leg: {exc}; it runs where bd is (dev boxes), never in CI")
    assert len(ids) >= 1000, f"bd reported only {len(ids)} ids; the store is not the live one"
    cites = [(f"nexus-{v}", rel, ln) for v, rel, ln in _scan(_BEAD_RE)]
    assert len(cites) >= 100, f"only {len(cites)} bead citations found; the scan is broken"
    bad = [c for c in cites if c[0] not in ids and c[0] not in BEAD_ALLOWLIST]
    assert not bad, _report("bead", bad)
    dead = sorted(v for v in BEAD_ALLOWLIST if v in ids or v not in {c[0] for c in cites})
    assert not dead, f"BEAD_ALLOWLIST rows that resolve or are no longer cited: {dead}"


# ── non-vacuity: each detector reds on a planted bogus value ─────────────────


def test_planted_bogus_sha_is_detected(tmp_path: Path) -> None:
    doc = tmp_path / "planted.md"
    doc.write_text("Fixed in commit deadbee5 and released.\n")
    cites = _sha_cites([doc])
    assert [c[0] for c in cites] == ["deadbee5"]
    assert _unresolved_shas({"deadbee5"}) == {"deadbee5"}


def test_a_real_head_sha_resolves() -> None:
    head = subprocess.run(
        ["git", "rev-parse", "--short=9", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert _unresolved_shas({head}) == set()


def test_planted_bogus_rdr_is_detected(tmp_path: Path) -> None:
    doc = tmp_path / "planted.md"
    doc.write_text("See RDR-999 for the design.\n")
    assert [c[0] for c in _scan(_RDR_RE, [doc])] == ["999"]
    assert "999" not in _rdr_files()


def test_planted_bogus_bead_is_detected(tmp_path: Path) -> None:
    doc = tmp_path / "planted.md"
    doc.write_text("Tracked as nexus-zzzz9 (nexus-zzzz9.3 is its child, nexus-zzzz9-wip its branch).\n")
    assert [c[0] for c in _scan(_BEAD_RE, [doc])] == ["zzzz9", "zzzz9", "zzzz9"], "suffixed forms resolve through the base id"
    try:
        ids = _bd_ids()
    except BdUnavailable as exc:
        pytest.skip(f"bead-id leg: {exc}")
    assert "nexus-zzzz9" not in ids
