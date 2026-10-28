# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-231 client half (nexus-bivz3.15/.16, code review S2): the CHANGELOG and the CLI reference say what
changed for a script or a person reading `--json` output.

The renamed label is `reason: "note_uri"` (was `chroma_uri`) in `nx catalog reconcile-stale --json`; the
code owns the value, the documents must follow it.
"""
from __future__ import annotations

from pathlib import Path

from nexus.commands.catalog_cmds.reconcile_stale import _STORE_PUT_ORIGIN_REASONS

REPO = Path(__file__).resolve().parent.parent


def _unreleased() -> str:
    """``## [Unreleased]`` plus the newest released section: the RDR-231 entry is pending on develop and
    moves into the release section when that release is cut (7.88.0), and it must be in one of them."""
    text = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
    start = text.index("## [Unreleased]")
    newest = text.index("\n## [", start + 1)
    return text[start:text.index("\n## [", newest + 1)]


def test_the_code_label_is_note_uri() -> None:
    assert "note_uri" in _STORE_PUT_ORIGIN_REASONS and "chroma_uri" not in _STORE_PUT_ORIGIN_REASONS


def test_the_changelog_names_the_renamed_labels_and_the_old_engine_read_gap() -> None:
    entry = _unreleased()
    for needle in (
        "`chroma_uri` is now `note_uri`",   # the label rename, old and new spelling
        "store_put_origin_by_reason",        # a --json key that carries it
        "zero_count_live",                   # and the second place it appears
        "--scheme nx-note",                  # enrich list
        "--check-references",                # doctor reads residue as unknown
        "analytic reads match no notes",     # stated plainly: an older engine
    ):
        assert needle in entry, f"neither the Unreleased nor the newest release section of CHANGELOG.md says: {needle!r}"


def test_the_cli_reference_uses_the_new_label() -> None:
    doc = (REPO / "docs" / "cli-reference.md").read_text(encoding="utf-8")
    assert 'reason: "chroma_uri"' not in doc and '"chroma_uri"` here' not in doc
    assert 'reason: "note_uri"' in doc and '`reason` is always `"note_uri"`' in doc
