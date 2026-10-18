# SPDX-License-Identifier: AGPL-3.0-or-later
"""Census of the client sites that decide something from the engine's mode string
(RDR-210 Technical Design 10, Gap 8, bead nexus-yx833).

A ``dual`` engine reports a third value, so every site that branches on
``"onnx-local"``, ``== "voyage"`` or ``embedding_mode`` has to be reviewed for the
third value. This test finds those sites by AST (so comments and docstrings do not
count) and fails on any file whose count differs from the reviewed list: a new site
is a prompt to review it, not a lint to silence. Non-vacuity: it asserts it found at
least the reviewed total.
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "nexus"

#: file (relative to src/nexus) -> number of mode-string decision sites, as reviewed.
REVIEWED: dict[str, int] = {
    "commands/collection.py": 6,
    "commands/daemon.py": 2,
    "commands/init.py": 1,
    "commands/service_cmd.py": 1,
    "corpus.py": 4,
    "daemon/storage_service_daemon.py": 2,
    "db/http_engine_status.py": 2,
    "db/http_vector_client.py": 6,
    "db/managed_endpoint.py": 5,
    "errors.py": 1,
    "mcp_infra.py": 3,
    "search_engine.py": 3,
}


def _sites(tree: ast.AST) -> int:
    n = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == "onnx-local":
            n += 1
        elif isinstance(node, ast.Compare) and any(
            isinstance(c, ast.Constant) and c.value == "voyage" for c in node.comparators
        ) and any(isinstance(o, (ast.Eq, ast.NotEq)) for o in node.ops):
            n += 1
        elif isinstance(node, ast.Attribute) and node.attr == "embedding_mode":
            n += 1
        elif isinstance(node, ast.Name) and node.id == "embedding_mode":
            n += 1
        elif isinstance(node, ast.Constant) and node.value == "embedding_mode":
            n += 1
        elif isinstance(node, ast.keyword) and node.arg == "embedding_mode":
            n += 1
    return n


def _census() -> dict[str, int]:
    found: dict[str, int] = {}
    for path in sorted(SRC.rglob("*.py")):
        count = _sites(ast.parse(path.read_text(encoding="utf-8")))
        if count:
            found[str(path.relative_to(SRC))] = count
    return found


def test_every_mode_string_site_is_on_the_reviewed_list() -> None:
    found = _census()
    assert sum(found.values()) >= sum(REVIEWED.values()), "census found fewer sites than reviewed: vacuous"
    drift = {
        f: (REVIEWED.get(f, 0), found.get(f, 0))
        for f in sorted(set(REVIEWED) | set(found))
        if REVIEWED.get(f, 0) != found.get(f, 0)
    }
    assert not drift, (
        "mode-string decision sites changed (file: reviewed, found). Review each new site "
        f"for the `dual` value (RDR-210 TD10), then update REVIEWED: {drift}"
    )
