# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""nexus-w715w round 2 (code-review-expert on nexus-sevlu's landed fix):
``safe_link_target`` in ``nexus.doc.catalog_links`` is the ONE place in
this codebase allowed to compose a markdown link from a catalog entry's
``source_uri`` / ``file_path`` — those two fields are exactly what must
never leak a ``file://`` URI or a host-local absolute path into rendered
output, and every hardening fix (scheme case/slash sensitivity, the
host-OS-native ``is_absolute()`` gap) lives in that ONE function.

A second module reading ``entry.source_uri`` or ``entry.file_path`` to
assemble a link is exactly how the original leak happened (a naive
preference-order check hand-rolled at the call site) and how a *second*
one could reintroduce it even after this function is fixed — a fix in
one place, a bypass in the next module over, teaches nothing.

Scope is deliberately narrow (not "anywhere in src/nexus"): the modules
that render markdown links from a catalog entry — `nexus.doc` (except
`catalog_links.py` itself, which is exempt by definition) and the two
CLI commands that call into it (`nx doc render`/`validate`,
`nx catalog footnotes`). `nexus.commands.catalog`'s own `show` command,
for one, prints `entry.file_path`/`entry.source_uri` as plain diagnostic
text (never composes `[x](x)`) and is out of this lint's scope on
purpose.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FORBIDDEN_ATTRS = frozenset({"source_uri", "file_path"})

#: Module that OWNS the safety check — reading these fields here is the
#: point of the module, not a bypass.
_EXEMPT_FILE = _REPO_ROOT / "src" / "nexus" / "doc" / "catalog_links.py"

_SCANNED_FILES: tuple[Path, ...] = (
    *sorted((_REPO_ROOT / "src" / "nexus" / "doc").glob("*.py")),
    _REPO_ROOT / "src" / "nexus" / "commands" / "doc.py",
    _REPO_ROOT / "src" / "nexus" / "commands" / "catalog_cmds" / "footnotes.py",
)


def _find_forbidden_attr_reads(path: Path) -> list[str]:
    """Return ``"line:attr"`` for every ``<expr>.source_uri`` /
    ``<expr>.file_path`` attribute READ in *path* — an assignment
    target (``Attribute`` inside a ``Store`` context, e.g. setting a
    field on a dataclass) is not a read and is excluded."""
    tree = ast.parse(path.read_text(), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and node.attr in _FORBIDDEN_ATTRS
            and isinstance(node.ctx, ast.Load)
        ):
            hits.append(f"{node.lineno}:{node.attr}")
    return hits


@pytest.mark.parametrize(
    "path", _SCANNED_FILES, ids=[str(p.relative_to(_REPO_ROOT)) for p in _SCANNED_FILES],
)
def test_only_catalog_links_reads_source_uri_or_file_path(path: Path) -> None:
    assert path.exists(), f"scanned file missing: {path}"
    if path == _EXEMPT_FILE:
        pytest.skip("catalog_links.py owns safe_link_target — exempt by definition")
    hits = _find_forbidden_attr_reads(path)
    assert not hits, (
        f"{path.relative_to(_REPO_ROOT)} reads entry.source_uri/entry.file_path "
        f"directly at {hits} — route through "
        f"nexus.doc.catalog_links.safe_link_target instead, the ONE place "
        f"that knows the full leak-safety contract (scheme allowlist, "
        f"content-based path classification, working-relative-link "
        f"resolution)."
    )


def test_scan_set_is_non_vacuous() -> None:
    """nexus-moht0 doctrine: a lint whose scan set is empty (a bad glob,
    a moved directory) silently passes forever. Pin a floor."""
    assert len(_SCANNED_FILES) >= 3, _SCANNED_FILES


def test_exempt_file_actually_reads_the_fields() -> None:
    """The exemption is for a real reader, not a name that stopped
    matching anything (e.g. the module got renamed/split)."""
    hits = _find_forbidden_attr_reads(_EXEMPT_FILE)
    assert hits, (
        f"{_EXEMPT_FILE} is exempted as the fields' one legitimate reader, "
        f"but no read of source_uri/file_path was found there any more — "
        f"the exemption may be stale."
    )
