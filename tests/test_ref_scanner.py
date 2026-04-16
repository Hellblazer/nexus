# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the RDR-081 stale-reference validator (src/nexus/doc/ref_scanner.py)."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nexus.doc.ref_scanner import (
    VERDICT_DRIFT,
    VERDICT_MISSING,
    VERDICT_OK,
    Drift,
    Reference,
    scan_markdown,
    validate,
)


DEFAULT_PREFIXES = ["docs", "code", "knowledge", "rdr"]


# ── scan_markdown ────────────────────────────────────────────────────────────


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "doc.md"
    p.write_text(body)
    return p


def test_finds_basic_reference(tmp_path):
    p = _write(tmp_path, "See the docs__architecture collection for details.\n")
    refs = scan_markdown(p, DEFAULT_PREFIXES)
    assert len(refs) == 1
    assert refs[0].collection == "docs__architecture"
    assert refs[0].prefix == "docs"
    assert refs[0].claimed_count is None
    assert refs[0].line == 1


def test_finds_multiple_prefixes(tmp_path):
    p = _write(
        tmp_path,
        "Paper chunks live in knowledge__art. Code chunks in code__myrepo.\n"
        "RDRs: rdr__nexus-abcd1234.\n",
    )
    refs = scan_markdown(p, DEFAULT_PREFIXES)
    names = {r.collection for r in refs}
    assert names == {"knowledge__art", "code__myrepo", "rdr__nexus-abcd1234"}


def test_chunk_count_plain_integer(tmp_path):
    p = _write(
        tmp_path,
        "We indexed 12,900 chunks into knowledge__art for the rebuild.\n",
    )
    refs = scan_markdown(p, DEFAULT_PREFIXES)
    assert len(refs) == 1
    assert refs[0].claimed_count == 12900


def test_chunk_count_k_shorthand(tmp_path):
    p = _write(
        tmp_path,
        "About ~13k chunks live in knowledge__art.\n",
    )
    refs = scan_markdown(p, DEFAULT_PREFIXES)
    assert refs[0].claimed_count == 13000


def test_chunk_count_decimal_k(tmp_path):
    p = _write(
        tmp_path,
        "~13.5k chunks in docs__art-architecture.\n",
    )
    refs = scan_markdown(p, DEFAULT_PREFIXES)
    assert refs[0].claimed_count == 13500


def test_no_chunk_count_means_none(tmp_path):
    p = _write(tmp_path, "docs__architecture has useful content.\n")
    refs = scan_markdown(p, DEFAULT_PREFIXES)
    assert refs[0].claimed_count is None


def test_fenced_code_block_ignored(tmp_path):
    p = _write(
        tmp_path,
        "Before fence.\n\n"
        "```\n"
        "docs__inside-fence should not match.\n"
        "```\n\n"
        "After fence: docs__outside-fence matches.\n",
    )
    refs = scan_markdown(p, DEFAULT_PREFIXES)
    names = {r.collection for r in refs}
    assert names == {"docs__outside-fence"}


def test_tilde_fence_ignored(tmp_path):
    p = _write(
        tmp_path,
        "Before.\n\n"
        "~~~\n"
        "knowledge__in-tilde-fence should be ignored.\n"
        "~~~\n\n"
        "After: knowledge__after matches.\n",
    )
    refs = scan_markdown(p, DEFAULT_PREFIXES)
    names = {r.collection for r in refs}
    assert names == {"knowledge__after"}


def test_internal_prefix_excluded(tmp_path):
    """taxonomy__centroids and plans__session are NOT user-facing prefixes.

    The default whitelist is [docs, code, knowledge, rdr] — references
    to internal-prefix collections (which never rename) should not be
    flagged even when present in prose.
    """
    p = _write(
        tmp_path,
        "T1 lives in plans__session; centroids in taxonomy__centroids. "
        "User collection: docs__test.\n",
    )
    refs = scan_markdown(p, DEFAULT_PREFIXES)
    names = {r.collection for r in refs}
    assert names == {"docs__test"}


def test_config_driven_prefix_override(tmp_path):
    """A caller-supplied prefix extends the scanner."""
    p = _write(tmp_path, "My project: custom__foo and docs__bar.\n")
    refs = scan_markdown(p, ["custom", "docs"])
    names = {r.collection for r in refs}
    assert names == {"custom__foo", "docs__bar"}


def test_malformed_prefix_rejected():
    """Invalid prefix-regex characters are rejected at scan time."""
    p = Path("/dev/null")
    with pytest.raises(ValueError, match="invalid prefix"):
        # parentheses in the prefix would smuggle regex metacharacters
        _ = scan_markdown(p, ["docs(foo"])


def test_embedded_reference_boundary(tmp_path):
    """A reference inside identifiers (e.g. `my_docs__foo`) should not match."""
    p = _write(
        tmp_path,
        "pre-docs__foo-suffix is not a collection ref.\n"
        "A bare docs__foo is.\n",
    )
    refs = scan_markdown(p, DEFAULT_PREFIXES)
    # Only the bare ref on line 2 — the one on line 1 has a leading
    # word char that the scanner's lookbehind filters out.
    assert len(refs) == 1
    assert refs[0].line == 2


# ── validate ─────────────────────────────────────────────────────────────────


def _fake_t3(collections: dict[str, int]):
    """Build a fake T3Database that reports the given collection → count mapping."""
    t3 = MagicMock()
    t3.list_collections.return_value = [{"name": n} for n in collections]

    def _goc(name):
        col = MagicMock()
        col.count.return_value = collections.get(name, 0)
        return col

    # MagicMock(side_effect=) preserves call_count for assertions.
    t3.get_or_create_collection = MagicMock(side_effect=_goc)
    return t3


def test_validate_ok_no_claim(tmp_path):
    refs = [Reference(path=tmp_path / "x.md", line=1,
                      collection="docs__exists", prefix="docs",
                      claimed_count=None)]
    drifts = validate(refs, _fake_t3({"docs__exists": 500}))
    assert drifts[0].verdict == VERDICT_OK
    assert drifts[0].actual_count is None  # no count call when no claim


def test_validate_drift_outside_tolerance(tmp_path):
    refs = [Reference(path=tmp_path / "x.md", line=1,
                      collection="docs__grown", prefix="docs",
                      claimed_count=100)]
    drifts = validate(refs, _fake_t3({"docs__grown": 500}),
                      tolerance=0.10)
    d = drifts[0]
    assert d.verdict == VERDICT_DRIFT
    assert d.actual_count == 500
    assert d.delta == 400


def test_validate_ok_within_tolerance(tmp_path):
    refs = [Reference(path=tmp_path / "x.md", line=1,
                      collection="docs__close", prefix="docs",
                      claimed_count=1000)]
    drifts = validate(refs, _fake_t3({"docs__close": 1050}),
                      tolerance=0.10)
    assert drifts[0].verdict == VERDICT_OK
    assert drifts[0].delta == 50


def test_validate_drift_just_outside(tmp_path):
    refs = [Reference(path=tmp_path / "x.md", line=1,
                      collection="docs__close", prefix="docs",
                      claimed_count=100)]
    # 100 → 111 is 11%, above 10% tolerance
    drifts = validate(refs, _fake_t3({"docs__close": 111}), tolerance=0.10)
    assert drifts[0].verdict == VERDICT_DRIFT


def test_validate_missing_collection(tmp_path):
    refs = [Reference(path=tmp_path / "x.md", line=1,
                      collection="docs__renamed", prefix="docs",
                      claimed_count=100)]
    drifts = validate(refs, _fake_t3({"docs__actual": 100}))
    assert drifts[0].verdict == VERDICT_MISSING
    assert drifts[0].actual_count is None


def test_validate_empty_refs():
    drifts = validate([], _fake_t3({"docs__whatever": 0}))
    assert drifts == []


def test_validate_zero_claimed_count_exact(tmp_path):
    """Claimed=0 with actual=0 is OK; claimed=0 with actual>0 is Drift."""
    refs_ok = [Reference(path=tmp_path / "x.md", line=1,
                         collection="docs__empty", prefix="docs",
                         claimed_count=0)]
    assert validate(refs_ok, _fake_t3({"docs__empty": 0}))[0].verdict == VERDICT_OK

    refs_drift = [Reference(path=tmp_path / "x.md", line=1,
                            collection="docs__nonempty", prefix="docs",
                            claimed_count=0)]
    assert validate(refs_drift, _fake_t3({"docs__nonempty": 50}))[0].verdict == VERDICT_DRIFT


def test_validate_count_cache_is_per_collection(tmp_path):
    """Two refs to the same collection call count() only once."""
    refs = [
        Reference(path=tmp_path / "x.md", line=1,
                  collection="docs__same", prefix="docs", claimed_count=100),
        Reference(path=tmp_path / "x.md", line=5,
                  collection="docs__same", prefix="docs", claimed_count=100),
    ]
    t3 = _fake_t3({"docs__same": 105})
    drifts = validate(refs, t3)
    # Both verdicts OK
    assert all(d.verdict == VERDICT_OK for d in drifts)
    # get_or_create_collection called exactly once due to cache
    assert t3.get_or_create_collection.call_count == 1


def test_validate_t3_unavailable_flags_all_missing(tmp_path):
    """When list_collections() raises, every ref becomes Missing."""
    t3 = MagicMock()
    t3.list_collections.side_effect = RuntimeError("no T3")

    refs = [Reference(path=tmp_path / "x.md", line=1,
                      collection="docs__anything", prefix="docs",
                      claimed_count=50)]
    drifts = validate(refs, t3)
    assert drifts[0].verdict == VERDICT_MISSING
