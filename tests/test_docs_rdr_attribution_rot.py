# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-g7zgw.3: changeset citations in docs/rdr resolve, and an attribution
of an identifier to a changeset or an RDR is backed by that artifact's text.

RDR-204's gate loop (T2 ``nexus/deep-analysis-rdr-gate-fix-loop-2026-09-07``)
blocked twice on provenance prose that a mechanical check refutes in
milliseconds: "``fk-002`` created the registry table" (the changelog file
``fk-002-collection-registry.xml`` never mentions ``catalog_collections``
in a CREATE; ``catalog-001-baseline.xml`` does) and "the flag RDR-103 set"
about ``legacy_grandfathered`` (the identifier does not occur in RDR-103's
file). The reference-rot lint (``tests/test_docs_reference_rot.py``)
covers commit, bead, RDR and JDR ids; this file adds the two legs it lacks.

1. **Changeset citations.** A token of the shape ``<family>-NNN[-suffix]``
   whose ``<family>-NNN`` prefix names a Liquibase changelog family under
   ``service/src/main/resources/db/changelog`` must be a changelog file
   stem, a ``changeSet id``, or the bare family. Tokens whose family does
   not exist in the changelog are ignored (a ``catalog-rename-500`` is not
   a changeset). Every dangling value already in the tree is allowlisted
   BY VALUE with its reason, so a new one has to be named or fixed.
2. **Attributions.** A sentence of the shape ``<changeset> ... <verb> ...
   `ident` `` or ``RDR-NNN <verb> ... `ident` `` (and their passive
   forms, `` `ident` <verb> by <artifact> ``), where the verb is one of
   :data:`ATTRIBUTION_VERBS`, claims the artifact is where the identifier
   comes from. The lint asserts the identifier occurs in the artifact's
   own text: the changelog file that carries the changeset id, or the
   RDR's markdown file. It does not judge the verb (a changeset that
   mentions a table it "dropped" passes); it catches the case where the
   named artifact has never heard of the identifier, which is the shape
   both RDR-204 provenance Criticals took.

Non-vacuity: each leg asserts it found citations to check, and a planted
bogus value of each shape reds its leg (``test_planted_*``).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = Path(__file__).parent.parent
RDR_DIR = REPO_ROOT / "docs" / "rdr"
CHANGELOG_DIR = REPO_ROOT / "service" / "src" / "main" / "resources" / "db" / "changelog"

Cite = tuple[str, str, int]  # (value, relpath, lineno)
Attribution = tuple[str, str, str, int]  # (artifact, identifier, relpath, lineno)

_FAMILY_RE = re.compile(r"^([a-z][a-z0-9]*-\d{3})")
_CHANGESET_RE = re.compile(r"(?<![\w/.-])([a-z][a-z0-9]*-\d{3}(?:-[a-z0-9]+)*)(?![\w/-])")
_CHANGESET_ID_RE = re.compile(r'changeSet id="([^"]+)"')

#: Verbs that attribute an identifier's origin to the named artifact.
ATTRIBUTION_VERBS: tuple[str, ...] = (
    "created", "creates", "create",
    "added", "adds", "add",
    "introduced", "introduces",
    "defined", "defines",
    "dropped", "drops",
    "renamed", "renames",
    "minted", "mints",
    "owns",
)
# "set" is a verb only in the trailing form below ("the flag RDR-103 set");
# as a generic verb it reads "the set of `chunk_id`" as an attribution.
_VERB = r"\b(?:" + "|".join(ATTRIBUTION_VERBS) + r")\b"
# At least one lowercase letter: an all-caps span (`UPDATE`, `NOT VALID`) is
# SQL, not an identifier whose origin is being attributed.
_IDENT = r"`((?=[A-Za-z_.]*[a-z])[A-Za-z_][A-Za-z0-9_.]*)`"
_CS = r"(?<![\w/.-])([a-z][a-z0-9]*-\d{3}(?:-[a-z0-9]+)*)(?![\w/-])"
_RDR = r"\bRDR-(\d{3})\b"
# Windows are short on purpose: "RDR-091 added `_scope_fit`" and
# "RDR-108 Phase 1c introduced (`migrate_x`" are attributions; a verb and an
# identifier forty words apart in the same sentence are not.
_ACTIVE_CS = re.compile(_CS + r"[^.;\n]{0,30}?" + _VERB + r"[^.;\n]{0,40}?" + _IDENT)
_PASSIVE_CS = re.compile(_IDENT + r"[^.;\n]{0,30}?" + _VERB + r"\s+(?:by|in)\s+(?:changeset\s+)?`?" + _CS)
_ACTIVE_RDR = re.compile(_RDR + r"[^.;\n]{0,20}?" + _VERB + r"[^.;\n]{0,40}?" + _IDENT)
_PASSIVE_RDR = re.compile(_IDENT + r"[^.;\n]{0,30}?" + _VERB + r"\s+(?:by|in)\s+" + _RDR)
# "`legacy_grandfathered` (the flag RDR-103 set)": identifier first, the
# artifact and its verb trailing (the RDR-204 pass-9 Critical 2 shape).
_TRAILING_RDR = re.compile(
    _IDENT + r"[^.;\n]{0,40}?" + _RDR + r"\s+(?:" + _VERB[2:-2] + r"|set|sets)\b(?!\s+of\b)"
)

#: Changeset-shaped citations with no changelog file or changeSet id.
#: Value -> reason. Enumerated from the first run (2026-09-07, 4 of 253).
CHANGESET_ALLOWLIST: dict[str, str] = {
    "catalog-032-reconcile": "rdr-193: design-time name; shipped as catalog-032-links-tumbler-fk",
    "fk-003-0": "rdr-164: prefix of changeSet id fk-003-0-backfill-stubs",
    "taxonomy-008-serverside-projection-links": "rdr-193: design-time name; shipped as taxonomy-008-link-types-jsonb",
    "taxonomy-013-topics-tenant-unique": "rdr-194: design-time name; shipped as taxonomy-014-topics-tenant-unique",
    "fk-004-1": "post-mortem rdr-191: prefix of changeSet id fk-004-1-reconcile",
}

#: (artifact, identifier) attributions whose identifier is absent from the
#: artifact's text. Enumerated at lint introduction (2026-09-07); each is an
#: RDR citing an earlier RDR for an identifier that earlier RDR never names,
#: left as found (an RDR file is never swept to satisfy a lint). Add a row
#: only with a reason a reader can check; fix the sentence when it was wrong.
ATTRIBUTION_ALLOWLIST: dict[tuple[str, str], str] = {
    ("RDR-025", "nexus.languages.LANGUAGE_REGISTRY"): "rdr-032:111, pre-lint; RDR-025 names the module, not the constant",
    ("RDR-091", "_scope_fit"): "rdr-092:62, pre-lint; RDR-091 describes scope-fit re-ranking without the function name",
    ("RDR-070", "register_post_store_hook"): "rdr-095:20, pre-lint; RDR-070 predates the registration function's name",
    ("RDR-108", "migrate_document_aspects_pk_to_doc_id"): "rdr-142:42, pre-lint; RDR-108 names the migration by phase, not by function",
    ("RDR-176", "relation_counts"): "rdr-177:26, pre-lint; RDR-176 names the field under a different key",
}


def _rdr_docs() -> list[Path]:
    return sorted(RDR_DIR.rglob("*.md"))


def _relpath(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT)) if path.is_relative_to(REPO_ROOT) else str(path)


def _changelog_texts() -> dict[str, str]:
    return {p.stem: p.read_text(errors="replace") for p in sorted(CHANGELOG_DIR.glob("*.xml"))}


def _changelog_index(texts: dict[str, str]) -> tuple[set[str], set[str], dict[str, str]]:
    """(families, resolvable tokens, changeSet id -> file stem)."""
    families = {m.group(1) for s in texts if (m := _FAMILY_RE.match(s))}
    id_to_stem: dict[str, str] = {}
    for stem, text in texts.items():
        for cid in _CHANGESET_ID_RE.findall(text):
            id_to_stem[cid] = stem
    resolvable = set(texts) | set(id_to_stem) | families
    return families, resolvable, id_to_stem


def _changeset_cites(files: list[Path], families: set[str]) -> list[Cite]:
    out: list[Cite] = []
    for path in files:
        rel = _relpath(path)
        for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            for m in _CHANGESET_RE.finditer(line):
                token = m.group(1)
                fam = _FAMILY_RE.match(token)
                if fam and fam.group(1) in families:
                    out.append((token, rel, lineno))
    return out


def _attributions(files: list[Path]) -> list[Attribution]:
    """Every (artifact, identifier) attribution in *files*; artifact is a
    changeset token or ``RDR-NNN``."""
    out: list[Attribution] = []
    for path in files:
        rel = _relpath(path)
        for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            for m in _ACTIVE_CS.finditer(line):
                out.append((m.group(1), m.group(2), rel, lineno))
            for m in _PASSIVE_CS.finditer(line):
                out.append((m.group(2), m.group(1), rel, lineno))
            for m in _ACTIVE_RDR.finditer(line):
                out.append((f"RDR-{m.group(1)}", m.group(2), rel, lineno))
            for m in _PASSIVE_RDR.finditer(line):
                out.append((f"RDR-{m.group(2)}", m.group(1), rel, lineno))
            for m in _TRAILING_RDR.finditer(line):
                out.append((f"RDR-{m.group(2)}", m.group(1), rel, lineno))
    return sorted(set(out))


def _rdr_texts() -> dict[str, str]:
    return {
        f"RDR-{m.group(1)}": p.read_text(errors="replace")
        for p in RDR_DIR.glob("rdr-*.md")
        if (m := re.match(r"rdr-(\d{3})-", p.name))
    }


def _artifact_text(
    artifact: str, texts: dict[str, str], id_to_stem: dict[str, str], rdrs: dict[str, str],
) -> str | None:
    """The artifact's own text, or None when the artifact does not resolve
    (the citation leg owns that failure)."""
    if artifact.startswith("RDR-"):
        return rdrs.get(artifact)
    if artifact in texts:
        return texts[artifact]
    if artifact in id_to_stem:
        return texts[id_to_stem[artifact]]
    family = [t for stem, t in texts.items() if stem == artifact or stem.startswith(artifact + "-")]
    if family and _FAMILY_RE.fullmatch(artifact):
        # A bare family ("fk-002") is every file of that family.
        return "\n".join(family)
    return None


def _unbacked(
    attributions: list[Attribution],
    texts: dict[str, str],
    id_to_stem: dict[str, str],
    rdrs: dict[str, str],
) -> tuple[list[Attribution], int]:
    """(attributions whose identifier is absent from the artifact, number checked)."""
    checked = 0
    bad: list[Attribution] = []
    for artifact, ident, rel, lineno in attributions:
        text = _artifact_text(artifact, texts, id_to_stem, rdrs)
        if text is None:
            continue
        checked += 1
        if not re.search(r"(?<![A-Za-z0-9_])" + re.escape(ident) + r"(?![A-Za-z0-9_])", text):
            bad.append((artifact, ident, rel, lineno))
    return bad, checked


# --- leg 1: changeset citations resolve ------------------------------------


def test_cited_changesets_resolve() -> None:
    texts = _changelog_texts()
    families, resolvable, _ = _changelog_index(texts)
    cites = _changeset_cites(_rdr_docs(), families)
    assert len(cites) >= 100, f"only {len(cites)} changeset citations found; the scan is broken"
    dangling = sorted(
        {(v, rel, ln) for v, rel, ln in cites if v not in resolvable and v not in CHANGESET_ALLOWLIST}
    )
    assert not dangling, (
        "changeset citations in docs/rdr that name no changelog file, changeSet id or family "
        "(fix the citation, or allowlist it BY VALUE with a reason):\n"
        + "\n".join(f"  {rel}:{ln}: {v}" for v, rel, ln in dangling)
    )


def test_changeset_allowlist_carries_no_dead_rows() -> None:
    texts = _changelog_texts()
    families, resolvable, _ = _changelog_index(texts)
    cited = {v for v, _, _ in _changeset_cites(_rdr_docs(), families)}
    dead = sorted(v for v in CHANGESET_ALLOWLIST if v not in cited or v in resolvable)
    assert not dead, f"allowlist rows no longer needed (cited nowhere, or now resolve): {dead}"


# --- leg 2: attributions are backed by the artifact -------------------------


def test_attributions_backed_by_artifact_text() -> None:
    texts = _changelog_texts()
    _, _, id_to_stem = _changelog_index(texts)
    rdrs = _rdr_texts()
    bad, checked = _unbacked(_attributions(_rdr_docs()), texts, id_to_stem, rdrs)
    assert checked >= 15, f"only {checked} attributions checked; the scan is broken"
    unlisted = sorted(b for b in bad if (b[0], b[1]) not in ATTRIBUTION_ALLOWLIST)
    assert not unlisted, (
        "docs/rdr attributes an identifier to an artifact whose own text never names it "
        "(quote the artifact and fix the sentence, or allowlist the pair with a reason):\n"
        + "\n".join(f"  {rel}:{ln}: {artifact} -> `{ident}`" for artifact, ident, rel, ln in unlisted)
    )


def test_attribution_allowlist_carries_no_dead_rows() -> None:
    texts = _changelog_texts()
    _, _, id_to_stem = _changelog_index(texts)
    rdrs = _rdr_texts()
    bad, _ = _unbacked(_attributions(_rdr_docs()), texts, id_to_stem, rdrs)
    live = {(a, i) for a, i, _, _ in bad}
    dead = sorted(k for k in ATTRIBUTION_ALLOWLIST if k not in live)
    assert not dead, f"allowlist rows no longer needed (sentence fixed or gone): {dead}"


# --- non-vacuity ------------------------------------------------------------


def test_planted_dangling_changeset_is_detected(tmp_path: Path) -> None:
    texts = _changelog_texts()
    families, resolvable, _ = _changelog_index(texts)
    planted = tmp_path / "rdr-999-planted.md"
    planted.write_text("changeset `catalog-001-999-nonexistent` created the table.\n")
    cites = _changeset_cites([planted], families)
    assert [v for v, _, _ in cites] == ["catalog-001-999-nonexistent"]
    assert cites[0][0] not in resolvable


def test_planted_unbacked_changeset_attribution_is_detected(tmp_path: Path) -> None:
    """The RDR-204 shape: the registry-table CREATE lives in catalog-001-baseline,
    not in fk-002-collection-registry."""
    texts = _changelog_texts()
    _, _, id_to_stem = _changelog_index(texts)
    planted = tmp_path / "rdr-999-planted.md"
    planted.write_text(
        "`fk-002` created `nonexistent_table_xyz`. "
        "`catalog_collections` was created by `catalog-001-5`.\n"
    )
    bad, checked = _unbacked(_attributions([planted]), texts, id_to_stem, {})
    assert checked == 2
    assert [(a, i) for a, i, _, _ in bad] == [("fk-002", "nonexistent_table_xyz")]


def test_planted_unbacked_rdr_attribution_is_detected(tmp_path: Path) -> None:
    """The RDR-204 shape: `legacy_grandfathered` attributed to RDR-103."""
    rdrs = _rdr_texts()
    assert "RDR-103" in rdrs and "RDR-101" in rdrs
    planted = tmp_path / "rdr-999-planted.md"
    planted.write_text(
        "`legacy_grandfathered_nonexistent` (the flag RDR-103 set) is read; "
        "RDR-101 defined `legacy_grandfathered`.\n"
    )
    bad, checked = _unbacked(_attributions([planted]), {}, {}, rdrs)
    assert checked == 2
    assert [(a, i) for a, i, _, _ in bad] == [("RDR-103", "legacy_grandfathered_nonexistent")]


def test_identifier_match_is_word_bounded() -> None:
    """`chunk_id` attributed to an artifact that only says `chunk_ids` is unbacked."""
    bad, checked = _unbacked([("RDR-999", "chunk_id", "x.md", 1)], {}, {}, {"RDR-999": "the chunk_ids column"})
    assert checked == 1 and bad, "substring match would have passed this"
    bad, _ = _unbacked([("RDR-999", "chunk_id", "x.md", 1)], {}, {}, {"RDR-999": "the `chunk_id` column"})
    assert not bad


def test_attribution_pattern_ignores_distant_verbs() -> None:
    """A verb and an identifier far apart in one sentence are not an attribution."""
    line = ("The critic verified consistency with RDR-089 and RDR-090 and the reviewer "
            "later added a long aside before mentioning `some_ident` at the end of it all")
    assert not _ACTIVE_RDR.search(line)
    assert not _TRAILING_RDR.search(line)
    assert not _ACTIVE_RDR.search("RDR-086 by returning the set of `chunk_id` values")
    assert _TRAILING_RDR.search("`legacy_grandfathered` (the flag RDR-103 set) is read")
