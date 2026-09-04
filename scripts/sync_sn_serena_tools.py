#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Regenerate the sn plugin's Serena tool snapshot from the pinned checkout.

The sn plugin auto-approves Serena MCP tools by full name and documents them
to subagents. Both lists rot silently when Serena adds or excludes tools
(nexus-jbt5x: five live tools were prompting, and three documented tools had
been excluded upstream). This script is the one place that knows the real
tool set: it reads the Serena revision pinned in ``sn/.mcp.json``, finds the
tool classes the way Serena's ``ToolRegistry`` does (every transitive
subclass of ``Tool`` whose own body defines ``apply``; the class name need
not end in ``Tool``, and two at the current pin do not), names them as
``Tool.get_name_from_cls`` does (strip a ``Tool`` suffix if present,
snake-case), drops the tools the ``claude-code`` context excludes,
``activate_project`` (disabled under ``single_project``) and the
``NEVER_APPROVE`` set below, and writes ``sn/hooks/scripts/serena-tools.txt``.
On every run it prints the tools added and removed against the previous
snapshot, so a pin bump has a decision point rather than a count.

``tests/test_sn_plugin.py`` pins the auto-approve allowlist and the injected
section to that snapshot, and the snapshot's header to the ``.mcp.json``
pin. Bumping Serena is therefore: change the pin, run this script, fix what
the tests name.

Usage::

    scripts/sync_sn_serena_tools.py            # clone the pinned revision
    scripts/sync_sn_serena_tools.py --checkout PATH   # reuse a local checkout
    scripts/sync_sn_serena_tools.py --check    # exit 1 if the snapshot is stale
"""
from __future__ import annotations

import argparse
import ast
import json
import pathlib
import re
import subprocess
import sys
import tempfile

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
MCP_JSON = REPO_ROOT / "sn" / ".mcp.json"
SNAPSHOT = REPO_ROOT / "sn" / "hooks" / "scripts" / "serena-tools.txt"

_PIN_RE = re.compile(r"^git\+(?P<url>https://[^@\s]+)@(?P<rev>[0-9a-f]{7,40})$")

#: Tools the plugin never auto-approves even though the context exposes them:
#: they act on Serena's own project registry or open UI, not on the code, and
#: a subagent has no business doing either unprompted. Everything else the
#: context exposes is approved, including the destructive symbol editors,
#: because the plugin's purpose is that code work never hits a prompt.
NEVER_APPROVE: frozenset[str] = frozenset({"remove_project", "open_dashboard"})


def serena_pin(mcp_json: pathlib.Path = MCP_JSON) -> tuple[str, str]:
    """Return ``(url, revision)`` from the ``--from git+URL@REV`` argument."""
    args = json.loads(mcp_json.read_text())["serena"]["args"]
    spec = args[args.index("--from") + 1]
    m = _PIN_RE.match(spec)
    if m is None:
        raise SystemExit(
            f"sn/.mcp.json serena --from is not pinned to a revision: {spec!r}. "
            "Use git+https://github.com/oraios/serena@<sha>."
        )
    return m.group("url"), m.group("rev")


def tool_name(class_name: str) -> str:
    """Serena's ``Tool.get_name_from_cls``, verbatim."""
    name = class_name[:-4] if class_name.endswith("Tool") else class_name
    return "".join("_" + c.lower() if c.isupper() else c for c in name).lstrip("_")


def tool_class_names(sources: dict[str, str]) -> set[str]:
    """Class names Serena's ``ToolRegistry`` would register, from module source.

    Mirrors ``iter_subclasses(Tool, inclusion_predicate=lambda c: "apply" in
    c.__dict__)``: a class is a tool when it descends from ``Tool`` (through
    any chain of bases defined in these modules) AND defines ``apply`` in its
    own body. Abstract intermediates such as ``EditingToolWithDiagnostics``
    have no ``apply`` and drop out; ``Tool`` itself is never a tool.
    """
    bases: dict[str, list[str]] = {}
    defines_apply: set[str] = set()
    for path, text in sources.items():
        for node in ast.parse(text, filename=path).body:
            if not isinstance(node, ast.ClassDef):
                continue
            names = []
            for b in node.bases:
                if isinstance(b, ast.Name):
                    names.append(b.id)
                elif isinstance(b, ast.Attribute):
                    names.append(b.attr)
            bases[node.name] = names
            if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "apply" for n in node.body):
                defines_apply.add(node.name)

    def is_tool(name: str, seen: frozenset[str] = frozenset()) -> bool:
        if name == "Tool":
            return True
        if name in seen or name not in bases:
            return False
        return any(is_tool(b, seen | {name}) for b in bases[name])

    return {c for c in bases if c != "Tool" and c in defines_apply and is_tool(c)}


def derive_tools(checkout: pathlib.Path) -> tuple[list[str], list[str]]:
    """Return ``(available, excluded)`` tool names for the claude-code context."""
    tools_dir = checkout / "src" / "serena" / "tools"
    context = checkout / "src" / "serena" / "resources" / "config" / "contexts" / "claude-code.yml"
    if not tools_dir.is_dir() or not context.is_file():
        raise SystemExit(f"{checkout} does not look like a Serena checkout")
    sources = {str(src): src.read_text() for src in sorted(tools_dir.glob("*.py"))}
    names = {tool_name(c) for c in tool_class_names(sources)}
    if not names:
        raise SystemExit(f"no Tool classes found under {tools_dir}")
    excluded = set(yaml.safe_load(context.read_text()).get("excluded_tools") or [])
    excluded.add("activate_project")  # disabled by single_project: true
    excluded |= NEVER_APPROVE
    return sorted(names - excluded), sorted(excluded & names)


def render(rev: str, available: list[str], excluded: list[str]) -> str:
    lines = [
        "# Serena tools available under the claude-code context.",
        "# GENERATED by scripts/sync_sn_serena_tools.py; do not edit by hand.",
        f"# serena-revision: {rev}",
        f"# excluded-by-context: {' '.join(excluded)}",
        *available,
    ]
    return "\n".join(lines) + "\n"


def parse_snapshot(path: pathlib.Path = SNAPSHOT) -> tuple[str, list[str], list[str]]:
    """Return ``(revision, available, excluded)`` from a snapshot file."""
    rev = ""
    excluded: list[str] = []
    available: list[str] = []
    for line in path.read_text().splitlines():
        if line.startswith("# serena-revision:"):
            rev = line.split(":", 1)[1].strip()
        elif line.startswith("# excluded-by-context:"):
            excluded = line.split(":", 1)[1].split()
        elif line and not line.startswith("#"):
            available.append(line.strip())
    return rev, available, excluded


def _clone(url: str, rev: str, dest: pathlib.Path) -> None:
    subprocess.run(["git", "init", "-q", str(dest)], check=True)
    subprocess.run(["git", "-C", str(dest), "fetch", "-q", "--depth", "1", url, rev], check=True)
    subprocess.run(["git", "-C", str(dest), "checkout", "-q", "FETCH_HEAD"], check=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--checkout", type=pathlib.Path, help="existing Serena checkout at the pinned revision")
    ap.add_argument("--check", action="store_true", help="exit 1 if the snapshot differs from the derived set")
    ns = ap.parse_args(argv)

    url, rev = serena_pin()
    if ns.checkout is not None:
        head = subprocess.run(
            ["git", "-C", str(ns.checkout), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        if not head.startswith(rev):
            raise SystemExit(f"--checkout is at {head[:12]}, but sn/.mcp.json pins {rev}")
        available, excluded = derive_tools(ns.checkout)
    else:
        with tempfile.TemporaryDirectory(prefix="serena-pin-") as tmp:
            dest = pathlib.Path(tmp) / "serena"
            _clone(url, rev, dest)
            available, excluded = derive_tools(dest)

    text = render(rev, available, excluded)
    if SNAPSHOT.exists():
        _, before, _ = parse_snapshot(SNAPSHOT)
        added, removed = sorted(set(available) - set(before)), sorted(set(before) - set(available))
        print(f"added: {' '.join(added) or '-'}")
        print(f"removed: {' '.join(removed) or '-'}")
    if ns.check:
        if SNAPSHOT.exists() and SNAPSHOT.read_text() == text:
            print(f"{SNAPSHOT.relative_to(REPO_ROOT)}: up to date ({len(available)} tools at {rev[:12]})")
            return 0
        print(f"{SNAPSHOT.relative_to(REPO_ROOT)}: STALE; rerun without --check", file=sys.stderr)
        return 1
    SNAPSHOT.write_text(text)
    print(f"wrote {SNAPSHOT.relative_to(REPO_ROOT)}: {len(available)} tools at {rev[:12]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
