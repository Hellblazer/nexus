# SPDX-License-Identifier: AGPL-3.0-or-later
"""A bead's changelog text, before or after the release fold.

A changelog entry lives in ``changelog/unreleased/<name>.md`` until a release runs
``scripts/changelog_fold.py fold``, which moves it under the release's section in
``CHANGELOG.md`` and deletes the fragment. A test that pins a fragment's text by
path therefore fails on every release tree (8.1.0's curate test, 8.1.2's backup
test). Read the entry through this helper instead.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def changelog_entry(fragment_name: str, bead_id: str) -> str:
    """Return the fragment's text if it exists, else the folded CHANGELOG bullet.

    The folded bullet is the ``- **...**`` line naming ``(<bead_id>`` plus its
    continuation lines, up to the next bullet or heading.
    """
    fragment = REPO / "changelog" / "unreleased" / fragment_name
    if fragment.exists():
        return fragment.read_text(encoding="utf-8")
    lines = (REPO / "CHANGELOG.md").read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.startswith("- **") and f"({bead_id}" in line:
            out = [line]
            for nxt in lines[i + 1:]:
                if nxt.startswith("- ") or nxt.startswith("#"):
                    break
                out.append(nxt)
            return "\n".join(out)
    raise AssertionError(f"no changelog entry for {bead_id}: neither {fragment} nor a folded CHANGELOG bullet")
