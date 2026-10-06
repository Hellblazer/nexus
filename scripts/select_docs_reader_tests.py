#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Select the test files that read the repo's own ``docs/`` tree (nexus-q99w4).

ci.yml's doc-only fast lane skips the pytest shard matrix when a push
touches nothing but docs, README, CHANGELOG or LICENSE. Some tests read
``docs/`` at assert time (RDR frontmatter, the supersedes graph, cited-sha
rot, tables), so a docs-only push could turn one red and nothing would run
it until the next code push. 15f4c7a94 did exactly that: an RDR status flip
broke ``tests/catalog/test_rdr_dependency_edges.py``'s exact supersedes pin,
and the red sat latent on develop until a hand-run full suite found it.

The ``pytest (docs readers)`` job runs what this script prints on every
docs-only push. A file is selected when it anchors ``docs`` to the repo root:
a repo-root name (``REPO_ROOT``, ``ROOT``, ``parents[N]``, ``parent.parent``,
``Path.cwd()`` and the like) followed within a short span by a ``"docs"`` or
``"docs/..."`` string. That is how a test reaches the real tree. A bare
``"docs"`` (a collection name, a tmp_path subdirectory) does not select: a
broad match measured 224 files on 2026-10-05, most of them reading nothing
under ``docs/``.

Stdlib only, so it runs under the runner's bare ``python3``.

Usage::

    scripts/select_docs_reader_tests.py --repo .
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

#: A repo-root anchor, then at most 40 non-comment characters, then a quoted
#: ``docs`` path segment.
_ANCHORED_DOCS = re.compile(
    r"(?:REPO_ROOT|REPO|_REPO|ROOT|repo_root|_ROOT|parents\[\d\]|parent\.parent|Path\.cwd\(\))"
    r"[^#\n]{0,40}[\"']docs[\"'/]"
)

#: Fewer files than this means the pattern or the tree moved, not that the
#: suite stopped reading docs. 32 selected on 2026-10-05.
MIN_SELECTED: int = 20


def select(repo: pathlib.Path) -> list[str]:
    """Repo-relative paths of the test files that read the docs tree, sorted."""
    tests = repo / "tests"
    found: list[str] = []
    for path in sorted(tests.rglob("test_*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        # Comment text never reads the tree, so drop everything after a '#'.
        if any(_ANCHORED_DOCS.search(line.split("#", 1)[0]) for line in text.splitlines()):
            found.append(path.relative_to(repo).as_posix())
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Select the tests that read docs/.")
    parser.add_argument("--repo", type=pathlib.Path, default=pathlib.Path("."))
    args = parser.parse_args(argv)
    files = select(args.repo.resolve())
    if len(files) < MIN_SELECTED:
        sys.stderr.write(
            f"select_docs_reader_tests: selected {len(files)} file(s), fewer than "
            f"the floor {MIN_SELECTED}; the selector or the tests moved\n"
        )
        return 1
    sys.stdout.write(" ".join(files) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
