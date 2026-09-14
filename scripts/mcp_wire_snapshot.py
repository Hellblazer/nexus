#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Committed wire-schema snapshot for both nexus MCP servers (nexus-cnzei.5).

Origin: the nexus-cnzei.1 critic pass (T2 nexus/cnzei1-critic-pass-2026-09-13)
recommended a full-schema snapshot for both servers' complete tool set —
name -> {backing function qualname, sorted param names, types, defaults,
required} — diffed every run, EXCLUDING free-text description/title (tool
descriptions carry heavy prose/history that churns independently of schema;
snapshotting that would make the pin noisy for no safety gain). This closes
three defect classes the exact-name-SET tests
(``test_core_registered_tools`` / ``test_catalog_registered_tools`` in
``tests/test_mcp_package.py``) cannot see, because those check only the SET
of registered names, never function identity or parameter schema:

  (a) a decorator landing on a different PUBLIC function (the nexus-cnzei.1
      incident itself — no "starts with _" signal for
      ``test_no_registered_mcp_tool_is_backed_by_a_private_function`` to
      catch, since the misplaced decorator wrapped a private function whose
      registered NAME still read as public);
  (b) a parameter renamed, retyped, or redefaulted on any tool OTHER than
      the one that happened to break, while the registered NAME stays the
      same (the name-SET tests pass unchanged);
  (c) duplicate ``name=`` registration where a later decorator silently
      overwrites an earlier one in FastMCP's tool-manager dict (still one
      name in the set; whichever function wins is invisible).

Snapshot shape: ``{server_name: {tool_name: {"qualname": ..., "module":
..., "is_async": ..., "parameters": <JSON schema with every "description"
and "title" key stripped, recursively>}}}``. The JSON-schema fields
(properties, types, defaults via ``default``, ``required``) come straight
from FastMCP's own generated ``tool.parameters`` — the exact structure a
wire client receives minus the two prose fields that churn independently
of the actual contract.

Regenerate after an intentional schema change:

    uv run python scripts/mcp_wire_snapshot.py --write

Verify without writing (used by the test):

    uv run python scripts/mcp_wire_snapshot.py --check
"""
from __future__ import annotations

import argparse
import copy
import json
import pathlib
import sys
from typing import Any

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SNAPSHOT_PATH = _REPO_ROOT / "tests" / "fixtures" / "mcp_wire_snapshot.json"

#: (module path, server attribute) pairs snapshotted, in a fixed order so
#: the written JSON is stable across regenerations.
_SERVER_MODULES = (
    ("nexus.mcp.core", "nexus"),
    ("nexus.mcp.catalog", "nexus-catalog"),
)


def _strip_prose(node: Any) -> Any:
    """Recursively drop ``description`` and ``title`` keys from a JSON schema.

    Deep-copies first so the live FastMCP registry objects are never
    mutated by a caller that inspects the schema afterward.
    """
    if isinstance(node, dict):
        return {
            k: _strip_prose(v)
            for k, v in node.items()
            if k not in ("description", "title")
        }
    if isinstance(node, list):
        return [_strip_prose(v) for v in node]
    return node


def build_snapshot() -> dict[str, dict[str, dict[str, Any]]]:
    """Introspect both live FastMCP tool registries into the snapshot shape."""
    import importlib

    result: dict[str, dict[str, dict[str, Any]]] = {}
    for module_path, server_name in _SERVER_MODULES:
        mod = importlib.import_module(module_path)
        mcp = mod.mcp
        tools: dict[str, dict[str, Any]] = {}
        for name, tool in sorted(mcp._tool_manager._tools.items()):
            fn = tool.fn
            tools[name] = {
                "qualname": fn.__qualname__,
                "module": fn.__module__,
                "is_async": bool(tool.is_async),
                "parameters": _strip_prose(copy.deepcopy(tool.parameters)),
            }
        result[server_name] = tools
    return result


def _load_committed() -> dict[str, dict[str, dict[str, Any]]]:
    return json.loads(SNAPSHOT_PATH.read_text())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--write", action="store_true", help="Regenerate and write the committed snapshot.")
    group.add_argument("--check", action="store_true", help="Compare the live registries against the committed snapshot; exit nonzero on mismatch.")
    args = parser.parse_args(argv)

    live = build_snapshot()

    if args.write:
        SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        SNAPSHOT_PATH.write_text(json.dumps(live, indent=2, sort_keys=True) + "\n")
        print(f"Wrote {SNAPSHOT_PATH.relative_to(_REPO_ROOT)}")
        return 0

    # --check
    if not SNAPSHOT_PATH.is_file():
        print(f"MISSING: {SNAPSHOT_PATH.relative_to(_REPO_ROOT)} does not exist. Run with --write.")
        return 1
    committed = _load_committed()
    if live != committed:
        print(
            "MCP wire schema drift detected. If intentional, regenerate with:\n"
            "    uv run python scripts/mcp_wire_snapshot.py --write\n"
            "and review the diff before committing."
        )
        return 1
    print("MCP wire snapshot matches the live registries.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
