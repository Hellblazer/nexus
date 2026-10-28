# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-231 (nexus-bivz3.15/.16): every site under ``src/nexus`` that mints a note ``source_uri`` or
registers a note is categorized, so a new note writer cannot skip the serving-engine floor check unseen.

The floor check (``require_note_uri_engine``) is documented as "every note writer reaches ``put_note``".
That sentence was prose. This sweep makes it a census: it finds every reference to ``uri_for``,
``note_uri``, ``catalog_store_hook`` and ``catalog_store_hook_tracked`` in ``src/nexus`` (a call, a
non-call reference such as passing the function on, or an ``import ... as alias``) and compares the
set, with counts, to the table below. A site not in the table fails with the instruction to categorize
it; a table entry with no site left fails too.

Categories:

* ``guarded-writer``: registers or writes a note and runs the floor check first. The two guarded
  entry points are ``note_write.put_note`` (MCP ``store_put``, ``nx store put``, ``nx memory promote``,
  the recovery-bundle import) and ``exporter.import_collection``; ``test_the_guard_precedes_the_register``
  pins the order.
* ``read-or-transient-mint``: builds a URI to look a row up, to compare, to label output or to read a
  source; nothing is stored under it. An engine below the floor makes such a lookup miss (the reads
  match no note), which is stated in the CHANGELOG, and writes nothing.
* ``aspect-write``: stamps ``source_uri`` on an aspect row. No unique key on ``source_uri`` there, so
  the floor does not cover it by design (``nexus.db.serving_engine_version``).
* ``definition``: the mint itself (``uri_for`` delegating to ``note_uri``).

An alias import (``from ... import catalog_store_hook as _x``) is a reference the sweep counts under
its own kind and no table entry allows, so it fails until someone decides what it is. The unguarded
``_catalog_store_hook`` alias in ``commands/store.py`` was removed for this reason.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.lint

REPO = Path(__file__).resolve().parent.parent
_SRC = REPO / "src" / "nexus"
_NAMES = frozenset({"uri_for", "note_uri", "catalog_store_hook", "catalog_store_hook_tracked"})
_MIN_FILES = 300

GUARDED = "guarded-writer"
TRANSIENT = "read-or-transient-mint"
ASPECT = "aspect-write"
DEFINITION = "definition"

#: (relative path, name, kind) -> (category, count, reason)
_SITES: dict[tuple[str, str, str], tuple[str, int, str]] = {
    ("src/nexus/catalog/note_write.py", "catalog_store_hook_tracked", "call"): (
        GUARDED, 1, "put_note: the one registration for a note; require_note_uri_engine() runs above it"),
    ("src/nexus/catalog/store_hook.py", "catalog_store_hook_tracked", "call"): (
        DEFINITION, 1, "the back-compat catalog_store_hook wrapper; nothing in src calls the wrapper (an alias import fails this sweep)"),
    ("src/nexus/catalog/store_hook.py", "uri_for", "call"): (
        GUARDED, 1, "the registration's own title-only identity, reached through put_note and the exporter"),
    ("src/nexus/exporter.py", "uri_for", "call"): (
        GUARDED, 1, "title-only owner synthesis inside import_collection, which checks the floor for a note target"),
    ("src/nexus/aspect_readers.py", "note_uri", "call"): (
        DEFINITION, 2, "uri_for's non-file branch, and the https hint's lookup form (a transient mint)"),
    ("src/nexus/aspect_extractor.py", "note_uri", "call"): (
        TRANSIENT, 3, "read_indexed_text, the batch extractor and the staleness read build the lookup URI"),
    ("src/nexus/aspect_extractor.py", "uri_for", "call"): (
        ASPECT, 3, "the three aspect upserts stamp source_uri on the row"),
    ("src/nexus/aspect_uri_repair.py", "uri_for", "call"): (
        ASPECT, 1, "re-derives the URI of an aspect row that has none"),
    ("src/nexus/catalog/recovery_bundle.py", "uri_for", "call"): (
        TRANSIENT, 2, "the export's link join key and the import's endpoint re-derivation: lookups"),
    ("src/nexus/commands/catalog_cmds/backfill.py", "uri_for", "call"): (
        TRANSIENT, 1, "re-derives a file:// URI to compare with a stored one (file-routed residue repair)"),
    ("src/nexus/commands/enrich.py", "note_uri", "call"): (
        TRANSIENT, 1, "the enrich dry-run's read of a source"),
    ("src/nexus/db/http_vector_client.py", "uri_for", "call"): (
        TRANSIENT, 1, "the catalog lookup of a title's chashes"),
    ("src/nexus/operators/aspect_sql.py", "uri_for", "call"): (
        TRANSIENT, 3, "the aspect operators re-derive the URI of each ident to match rows"),
}


def _sites(root: Path = _SRC, repo: Path = REPO) -> tuple[int, dict[tuple[str, str, str], int]]:
    """``(files scanned, {(path, name, kind): count})``. Kinds: ``call``, ``ref`` (a load that is not
    the function of a call) and ``alias->NAME`` (an ``import ... as``)."""
    found: dict[tuple[str, str, str], int] = {}
    files = sorted(root.rglob("*.py"))
    for p in files:
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        rel = p.relative_to(repo).as_posix()
        func_ids = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}

        def _hit(name: str, kind: str, rel: str = rel) -> None:
            found[(rel, name, kind)] = found.get((rel, name, kind), 0) + 1

        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                f = n.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                if name in _NAMES:
                    _hit(name, "call")
            elif isinstance(n, (ast.Name, ast.Attribute)) and id(n) not in func_ids and isinstance(n.ctx, ast.Load):
                name = n.attr if isinstance(n, ast.Attribute) else n.id
                if name in _NAMES:
                    _hit(name, "ref")
            elif isinstance(n, ast.ImportFrom):
                for a in n.names:
                    if a.name in _NAMES and a.asname:
                        _hit(a.name, f"alias->{a.asname}")
    return len(files), found


def test_every_note_minting_and_registering_site_is_categorized() -> None:
    scanned, found = _sites()
    expected = {k: v[1] for k, v in _SITES.items()}
    new = {k: n for k, n in found.items() if expected.get(k) != n}
    assert not new, (
        f"files scanned={scanned}. A site that mints a note URI or registers a note is new or its count moved "
        "(path, name, kind -> count now). Decide which category it is (guarded-writer: reaches put_note or "
        "import_collection; read-or-transient-mint; aspect-write; definition), and if it WRITES a note, "
        f"route it through put_note so the serving-engine floor check runs first: {new}. "
        f"Table says: { {k: expected.get(k) for k in new} }"
    )


def test_the_table_has_no_stale_entries() -> None:
    _, found = _sites()
    stale = sorted(k for k in _SITES if k not in found)
    assert not stale, f"table entries with no site left (remove them): {stale}"
    for key, (category, count, reason) in _SITES.items():
        assert category in {GUARDED, TRANSIENT, ASPECT, DEFINITION}, key
        assert count >= 1 and len(reason) > 20, f"{key}: needs a real reason"


def test_the_sweep_is_not_vacuous() -> None:
    scanned, found = _sites()
    assert scanned >= _MIN_FILES, f"scanned only {scanned} files; the walk is broken"
    assert sum(found.values()) >= 20, "the sweep saw almost nothing"
    assert any(kind == "call" and name == "catalog_store_hook_tracked" for (_p, name, kind) in found)


_PLANTS = {
    "a_new_uri_for_call": "from nexus.aspect_readers import uri_for\ndef w(c, t):\n    return uri_for(c, t)\n",
    "a_new_note_uri_call": "def w(c, t):\n    return note_uri(c, t)\n",
    "an_attribute_call": "def w(sh):\n    return sh.catalog_store_hook_tracked(title='t', doc_id='d', collection_name='c')\n",
    "an_alias_import": "from nexus.catalog.store_hook import catalog_store_hook as _hook\n",
    "the_function_passed_on": "def w(register):\n    register(catalog_store_hook)\n",
}


@pytest.mark.parametrize("name", sorted(_PLANTS))
def test_a_planted_site_is_seen(tmp_path: Path, name: str) -> None:
    mod = tmp_path / "src" / "nexus" / "scratch_writer.py"
    mod.parent.mkdir(parents=True)
    mod.write_text(_PLANTS[name])
    scanned, found = _sites(tmp_path / "src" / "nexus", tmp_path)
    assert scanned == 1 and found, name
    assert all(path == "src/nexus/scratch_writer.py" for (path, _n, _k) in found)


def _line_of_first_call(tree: ast.AST, name: str) -> int | None:
    lines = [
        n.lineno for n in ast.walk(tree)
        if isinstance(n, ast.Call) and (n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", None)) == name
    ]
    return min(lines) if lines else None


def test_the_guard_precedes_the_register() -> None:
    """put_note runs the floor check above its registration; import_collection runs it at all. Without this
    the 'guarded-writer' label above would be a claim."""
    note_write = ast.parse((_SRC / "catalog" / "note_write.py").read_text(encoding="utf-8"))
    put_note = next(n for n in ast.walk(note_write) if isinstance(n, ast.FunctionDef) and n.name == "put_note")
    guard = _line_of_first_call(put_note, "require_note_uri_engine")
    register = _line_of_first_call(put_note, "catalog_store_hook_tracked")
    assert guard is not None and register is not None and guard < register, (guard, register)

    exporter = ast.parse((_SRC / "exporter.py").read_text(encoding="utf-8"))
    imp = next(n for n in ast.walk(exporter) if isinstance(n, ast.FunctionDef) and n.name == "import_collection")
    assert _line_of_first_call(imp, "require_note_uri_engine") is not None, "import_collection lost its floor check"
