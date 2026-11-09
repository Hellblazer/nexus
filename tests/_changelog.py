# SPDX-License-Identifier: AGPL-3.0-or-later
"""The changelog as a doc-pin test should read it: ``CHANGELOG.md`` plus every unreleased fragment.

Since changelog fragments (nexus-3ceda.2) an entry lives in ``changelog/unreleased/<bead-id>.md``
until the release folds it into ``CHANGELOG.md``, so a test that pins an entry's wording reads both.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FRAGMENT_DIR = "changelog/unreleased"


def changelog_text(root: Path = REPO) -> str:
    """``CHANGELOG.md`` followed by each fragment's text, in name order (README.md and dotfiles skipped)."""
    parts = [(root / "CHANGELOG.md").read_text(encoding="utf-8")]
    frag_dir = root / FRAGMENT_DIR
    if frag_dir.is_dir():
        for p in sorted(frag_dir.iterdir()):
            if p.is_file() and p.suffix == ".md" and p.name != "README.md" and not p.name.startswith("."):
                parts.append(p.read_text(encoding="utf-8"))
    return "\n".join(parts)
