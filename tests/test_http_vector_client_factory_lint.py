# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-4m6i0.4: ban production ``HttpVectorClient()`` construction outside
the gated factory.

Provenance: nexus-b6qlf / nexus-br90a (2026-07-09). The engine-version-floor
probe was wired into ``get_http_vector_client()`` (the process-local factory,
``src/nexus/db/http_vector_client.py``) — but two CLI commands
(``migrate_cmd.py``, ``storage_cmd.py``) constructed ``HttpVectorClient()``
directly, silently bypassing the fail-loud gate on exactly the
highest-stakes cloud operations (data migration/ETL). Routing those bypasses
through the factory was the nexus-br90a point-fix; THIS lint is the
exhaustive-surface-audit backstop (feedback_exhaustive_surface_audit) that
keeps new bypasses from re-appearing: any future production call site that
constructs the client directly fails CI here, loudly, with a pointer at the
factory.

Mechanism: AST-based (``ast.parse`` per file), NOT raw grep — the two
historical bypass sites live on in code comments (``migrate_cmd.py``,
``storage_cmd.py`` both narrate the old bug at the fixed call sites), and a
text grep would false-positive on them forever. A ``Call`` node whose callee
name is ``HttpVectorClient`` is a construction; a comment is not in the AST.

Allowlist entries are keyed ``(repo-relative path, enclosing function name)``
— resilient to line drift, unlike line numbers — and every entry must be
independently re-derived by a live scan (an UNUSED entry fails the
real-corpus test, same rot-detection discipline as the changelog lints'
allowlists). The two grandfathered sites:

1. ``db/http_vector_client.py`` / ``get_http_vector_client`` — the factory
   itself; the one place the singleton is legitimately constructed, directly
   behind the engine-version probe.
2. ``commands/storage_cmd.py`` / ``migrate_vectors_cmd`` — the DELIBERATE
   ``--dry-run`` carve-out (nexus-br90a review, accepted): dry-run only
   counts SOURCE chunks and never contacts the destination service, so
   probing the destination's engine version would add a network round-trip
   to a path that is defined by not touching the network. The non-dry-run
   branch of the same command routes through the factory.

Scope: ``src/nexus/`` only. ``tests/`` is a sibling directory and therefore
out of scope by construction — tests may construct directly (they exercise
client internals against faked transports and must not pay a probe).

Aliased imports (``from ... import HttpVectorClient as HVC`` then
``HVC()``) ARE detected: per-file ``ImportFrom`` aliases are resolved
before the call scan, the same mechanism ``storage_boundary_lint.py``'s
``_collect_constructor_aliases`` already applies to the T2Database/
T3Database/Catalog constructor bans (nexus-90nmj — aliased deferred
imports are idiomatic in this codebase's CLI commands, so alias evasion
is an ACCIDENTAL-bypass shape here, not just an adversarial one).

Documented limitation: assignment REBINDING (``X = module.HttpVectorClient``
then ``X()``) is not detected — that requires dataflow analysis, is absent
from the real corpus (the exact-count assertion below corroborates), and is
genuinely deliberate-evasion territory; code review owns that case, the
same posture as the sibling boundary lint.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
SRC_ROOT = REPO_ROOT / "src" / "nexus"

#: (repo-relative-to-src path, enclosing function name, reason). Keyed by
#: enclosing FUNCTION, not line number, so ordinary edits above a site don't
#: rot the entry. Module-level constructions (no enclosing function) key as
#: function name ``"<module>"``.
ALLOWLIST: tuple[tuple[str, str, str], ...] = (
    (
        "db/http_vector_client.py",
        "get_http_vector_client",
        "The gated factory itself — the singleton construction that sits "
        "directly behind the engine-version-floor probe (nexus-jn0nm).",
    ),
    # (RDR-155 P4b: the storage_cmd.py migrate_vectors_cmd --dry-run
    # carve-out entry died with the file.)
)


@dataclass(frozen=True)
class Construction:
    path: str  # relative to src/nexus/, posix separators
    function: str  # enclosing function name, or "<module>"
    lineno: int


def _collect_import_aliases(tree: ast.AST) -> set[str]:
    """Every local name ``HttpVectorClient`` is bound to via a ``from``
    import in *tree* — ``from ... import HttpVectorClient as HVC`` yields
    ``{"HVC"}`` (and a plain un-aliased import yields ``{"HttpVectorClient"}``,
    harmlessly redundant with the literal-name match). Port of
    ``storage_boundary_lint._collect_constructor_aliases``, scoped to this
    lint's single tracked class (nexus-90nmj)."""
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "HttpVectorClient":
                    aliases.add(alias.asname or alias.name)
    return aliases


class _ConstructionVisitor(ast.NodeVisitor):
    """Collect every ``HttpVectorClient(...)`` Call (literal name, module
    attribute, or ``from``-import alias) with its enclosing function name
    (innermost FunctionDef/AsyncFunctionDef, else module)."""

    def __init__(self, tracked_names: set[str]) -> None:
        self.tracked = tracked_names
        self.stack: list[str] = []
        self.found: list[tuple[str, int]] = []

    def _visit_func(self, node: ast.AST) -> None:
        self.stack.append(node.name)  # type: ignore[attr-defined]
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _visit_func
    visit_AsyncFunctionDef = _visit_func

    def visit_Call(self, node: ast.Call) -> None:
        callee = node.func
        name = None
        if isinstance(callee, ast.Name):
            name = callee.id
        elif isinstance(callee, ast.Attribute):
            # module-attribute form keys on the attr, which is always the
            # class's real name regardless of how the MODULE was aliased.
            name = callee.attr
            if name in self.tracked and name != "HttpVectorClient":
                # An aliased NAME used as an attribute (x.HVC) is not a
                # construction of our class; only match attrs by the
                # literal class name.
                name = None
        if name is not None and (
            name == "HttpVectorClient" or name in self.tracked
        ):
            self.found.append(
                (self.stack[-1] if self.stack else "<module>", node.lineno)
            )
        self.generic_visit(node)


def scan_constructions(src_root: Path = SRC_ROOT) -> list[Construction]:
    """AST-scan every ``*.py`` under *src_root* for ``HttpVectorClient(``
    Call nodes. Raises on syntax errors (a file this scan cannot parse is a
    scan hole, not a pass)."""
    out: list[Construction] = []
    files = sorted(src_root.rglob("*.py"))
    assert files, f"empty rglob under {src_root} — misconfigured SRC_ROOT?"
    for py in files:
        tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
        visitor = _ConstructionVisitor(_collect_import_aliases(tree))
        visitor.visit(tree)
        rel = py.relative_to(src_root).as_posix()
        out.extend(
            Construction(path=rel, function=fn, lineno=ln)
            for fn, ln in visitor.found
        )
    return out


def _allow_keys(
    allowlist: tuple[tuple[str, str, str], ...],
) -> set[tuple[str, str]]:
    return {(path, fn) for path, fn, _reason in allowlist}


def analyze(
    src_root: Path = SRC_ROOT,
    allowlist: tuple[tuple[str, str, str], ...] = ALLOWLIST,
) -> tuple[list[Construction], set[tuple[str, str]]]:
    """Return (violations, unused_allowlist_keys)."""
    constructions = scan_constructions(src_root)
    allow = _allow_keys(allowlist)
    violations = [
        c for c in constructions if (c.path, c.function) not in allow
    ]
    consumed = {
        (c.path, c.function)
        for c in constructions
        if (c.path, c.function) in allow
    }
    return violations, allow - consumed


# ---------------------------------------------------------------------------
# Mechanism tests (synthetic trees)
# ---------------------------------------------------------------------------


def _write_module(tmp_path: Path, rel: str, source: str) -> Path:
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(source, encoding="utf-8")
    return tmp_path


def test_bare_construction_in_function_is_flagged(tmp_path):
    root = _write_module(
        tmp_path,
        "commands/rogue.py",
        "def do_thing():\n    client = HttpVectorClient()\n    return client\n",
    )
    violations, unused = analyze(src_root=root, allowlist=())
    assert [(v.path, v.function) for v in violations] == [
        ("commands/rogue.py", "do_thing")
    ]
    assert unused == set()


def test_module_level_construction_is_flagged(tmp_path):
    root = _write_module(
        tmp_path, "singleton.py", "client = HttpVectorClient()\n"
    )
    violations, _ = analyze(src_root=root, allowlist=())
    assert [(v.path, v.function) for v in violations] == [
        ("singleton.py", "<module>")
    ]


def test_attribute_form_construction_is_flagged(tmp_path):
    """``module.HttpVectorClient()`` must be caught too — an Attribute
    callee, not a bare Name (the deferred-import style used throughout the
    CLI commands makes this form realistic)."""
    root = _write_module(
        tmp_path,
        "sneaky.py",
        "import nexus.db.http_vector_client as hvc\n"
        "def go():\n    return hvc.HttpVectorClient()\n",
    )
    violations, _ = analyze(src_root=root, allowlist=())
    assert [(v.path, v.function) for v in violations] == [("sneaky.py", "go")]


def test_aliased_import_construction_is_flagged(tmp_path):
    """nexus-90nmj: ``from ... import HttpVectorClient as HVC`` then
    ``HVC()`` must be caught — aliased deferred imports are idiomatic in
    this codebase's CLI commands, so this is an accidental-bypass shape,
    not just adversarial. (Assignment REBINDING remains a documented
    limitation; see module docstring.)"""
    root = _write_module(
        tmp_path,
        "commands/aliased.py",
        "from nexus.db.http_vector_client import HttpVectorClient as HVC\n"
        "def go():\n    return HVC()\n",
    )
    violations, _ = analyze(src_root=root, allowlist=())
    assert [(v.path, v.function) for v in violations] == [
        ("commands/aliased.py", "go")
    ]


def test_unrelated_name_matching_alias_is_not_flagged(tmp_path):
    """An unrelated class that HAPPENS to share an alias name with nothing
    tracked must not be flagged — the alias set is derived per-file from
    actual HttpVectorClient imports, never guessed from call names."""
    root = _write_module(
        tmp_path,
        "commands/unrelated.py",
        "from somewhere import OtherClient as HVC\n"
        "def go():\n    return HVC()\n",
    )
    violations, _ = analyze(src_root=root, allowlist=())
    assert violations == []


def test_comment_mention_is_not_flagged(tmp_path):
    """The exact false-positive class that forces AST over grep: the two
    fixed bypass sites narrate 'HttpVectorClient() directly' in comments."""
    root = _write_module(
        tmp_path,
        "commands/fixed.py",
        "# this used to construct HttpVectorClient() directly, bypassing\n"
        "# the gate; now it routes through the factory.\n"
        "def do_thing(get_http_vector_client):\n"
        "    return get_http_vector_client()\n",
    )
    violations, _ = analyze(src_root=root, allowlist=())
    assert violations == []


def test_allowlisted_site_is_accepted_and_consumed(tmp_path):
    root = _write_module(
        tmp_path,
        "db/factory.py",
        "def the_factory():\n    return HttpVectorClient()\n",
    )
    violations, unused = analyze(
        src_root=root,
        allowlist=(("db/factory.py", "the_factory", "the factory"),),
    )
    assert violations == []
    assert unused == set()


def test_unused_allowlist_entry_is_detected(tmp_path):
    """Rot detection: an allowlist entry no live scan reproduces must
    surface as unused, never silently persist."""
    root = _write_module(tmp_path, "clean.py", "x = 1\n")
    violations, unused = analyze(
        src_root=root,
        allowlist=(("gone.py", "removed_fn", "stale"),),
    )
    assert violations == []
    assert unused == {("gone.py", "removed_fn")}


def test_second_construction_in_allowlisted_function_still_counts_once():
    """The allowlist key is (path, function) — by design a SECOND
    construction inside the same allowlisted function would also be
    excused. Documented granularity decision: both current entries are
    single-construction functions, and the real-corpus exact-count
    assertion below (== 2 total constructions) is what catches a new
    construction being added ANYWHERE, including inside an allowlisted
    function."""
    constructions = scan_constructions()
    per_key: dict[tuple[str, str], int] = {}
    for c in constructions:
        per_key[(c.path, c.function)] = per_key.get((c.path, c.function), 0) + 1
    for path, fn, _reason in ALLOWLIST:
        assert per_key.get((path, fn), 0) == 1, (
            f"allowlisted function {path}::{fn} now contains "
            f"{per_key.get((path, fn), 0)} HttpVectorClient constructions — "
            "the (path, function) allowlist key excuses ALL of them; "
            "re-derive whether each is genuinely gate-exempt and split the "
            "function if not."
        )


# ---------------------------------------------------------------------------
# The real corpus — exact-set assertions
# ---------------------------------------------------------------------------


def test_real_src_tree_zero_violations_and_full_allowlist_consumption():
    violations, unused = analyze()
    assert violations == [], (
        "production HttpVectorClient() construction outside the gated "
        "factory — route it through get_http_vector_client() so the "
        "engine-version-floor probe applies (nexus-b6qlf/nexus-br90a: the "
        "bypass class this lint exists to prevent), or add a reasoned "
        f"allowlist entry: {[(v.path, v.function, v.lineno) for v in violations]}"
    )
    assert unused == set(), (
        "ALLOWLIST entries not reproduced by a live scan (rot — the site "
        f"moved or was renamed): {unused}"
    )


def test_real_src_tree_exact_construction_count():
    """Exact count (== per feedback_exact_assertions_for_fixture_regression),
    doubling as the non-vacuity floor: a silently-empty scan cannot pass."""
    constructions = scan_constructions()
    assert len(constructions) == len(ALLOWLIST) == 1, (
        f"expected exactly {len(ALLOWLIST)} HttpVectorClient constructions "
        f"in src/nexus/ (the allowlisted factory + dry-run carve-out), "
        f"found {len(constructions)}: "
        f"{[(c.path, c.function, c.lineno) for c in constructions]}"
    )


def test_allowlist_has_no_duplicate_keys():
    keys = [(path, fn) for path, fn, _ in ALLOWLIST]
    assert len(keys) == len(set(keys))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
