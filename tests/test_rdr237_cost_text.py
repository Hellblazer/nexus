# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-237 (nexus-tr1rj.5): every PDF now runs MinerU, and the places a user reads say what that costs.

Sam accepted about 4 to 10 s a page and a 1.2 GB first download. Before this pin the CHANGELOG, the
CLI reference and the configuration reference said neither, and a user who ran ``nx index repo`` over a
tree with PDFs learned it from the clock.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

#: file -> a heading or line that locates the text in that file (the sentence must sit in the section it names).
SITES = {
    "CHANGELOG.md": "- **`mineru` is the default PDF extractor;",
    "docs/cli-reference.md": "### PDF Extraction Backends",
    "docs/configuration.md": "| `pdf.extractor` |",
}

REQUIRED = (
    "Every PDF now runs MinerU",
    "about 4 to 10 s per page",
    "about 1.2 GB of model weights",
    "`nx index repo`, `nx collection reindex` and `nx dt index` run MinerU on every PDF",
    "`--extractor pymupdf`",
    "text-only",
)


def _section(path: str, marker: str) -> str:
    text = (REPO / path).read_text(encoding="utf-8")
    start = text.index(marker)
    # The CHANGELOG entry and the configuration row are one line; the reference section runs to the next heading.
    end = text.find("\n### ", start + len(marker)) if marker.startswith("###") else text.find("\n", start)
    return text[start:end if end != -1 else len(text)]


@pytest.mark.parametrize("path", sorted(SITES))
def test_the_cost_of_the_mineru_default_is_stated_where_users_read_it(path: str) -> None:
    section = _section(path, SITES[path])
    missing = [phrase for phrase in REQUIRED if phrase not in section]
    assert missing == [], f"{path} must say, in the section about the MinerU default: {missing}"
