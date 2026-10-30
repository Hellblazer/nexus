# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-236 Phase 0 (nexus-qyx35): a row read of a non-CHUNK metadata key is a finding.

A shared chunk row holds the last writer's value of every DOCUMENT, POSITION and LEGACY
key (RDR-236 key table, ``src/nexus/tables/chunk-metadata-keys.toml``). A reader that takes
one of them from ``metadata[...]`` or ``metadata.get(...)`` reads another document's value.
This lint finds those reads in ``src/nexus`` by AST and holds them to a RATCHET: the sites
that exist today are listed in ``tests/chunk_metadata_read_sites.toml`` (file, function, key,
count). The count may only fall: a site not listed is a finding naming the key's catalog
home, and a listed count above what the scan finds is a finding too (lower it, or delete the
entry) so the list shrinks as the readers move in Phase 2.

What a "read" is. A subscript load or ``.get(...)`` with a string-literal key, whose receiver is
a chunk-row metadata expression: a name in ``_ROW_NAMES`` (``metadata``, ``md``, ...), any
``<expr>.metadata`` attribute, a chained source (``r["metadata"][k]``,
``r.get("metadata", {}).get(k)``, ``(r.get("metadata") or {}).get(k)``), a local alias assigned
in the same function from one of those, or a bare name the sites file opts in for that file
(``[receivers]``: ``meta``, ``entry``, ``e`` where a Phase 2 reader names a parameter that way;
an entry may be scoped to one function as ``path::qualname``).

Blind spots, by construction (this is a floor on the reads, not a census):

* A row metadata dict bound to a name nobody opted in. ``meta``, ``m``, ``e`` and ``entry`` are
  also catalog document meta, T1 scratch and T2 memory entries, so they are not guessed at.
  Measured 2026-10-30 (RDR-236 Phase 0 fix round 1): 173 reads of a non-CHUNK key through a bare
  ``meta``/``m``/``e``/``entry`` receiver the ``_ROW_NAMES`` rule did not see (the reviewer's scratch
  count was 119, over a narrower key and receiver set). Opting in the Phase 2 reader sites
  brought in 56 of them; 117 remain unseen, an unclassified share of which read catalog meta,
  scratch or memory entries rather than a chunk row.
* ``"k" in metadata`` tests, ``.pop``, ``**metadata`` splats, and a key held in a variable.
* Nested closures do not inherit an outer alias; module-level aliases do not reach functions.
* Engine readers are outside this scan: ``ManifestLessCensus`` SQL, ``spanFromMeta``, the
  ``/reapable`` display (``service/``) read the same keys and are not Python.

The legacy-fallback module. The note TTL rules read the row until the keyless-note count is
zero (RDR-236 RF-8), so ``src/nexus/catalog/note_ttl.py`` is exempt from the site list, but
not from a bound: it may read only ``ckm.LEGACY_FALLBACK_KEYS`` (``catalog_doc_id`` and
``title``) and its reads are listed under ``[exempt]`` in the sites file and may only fall, so
the fallback cannot grow. There is no other fallback module; creating one would mean moving
the reads, which is Phase 2.

Non-vacuity. The floor is on files scanned (>= 300) and on the synthetic detection tests below,
never on the live site count: that count is the quantity the ratchet drives down.
"""
from __future__ import annotations

import ast
import collections
import functools
import pathlib
import tomllib
from collections.abc import Mapping

import pytest

from nexus.tables import chunk_metadata_keys as ckm

pytestmark = pytest.mark.lint

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src" / "nexus"
SITES_FILE = REPO_ROOT / "tests" / "chunk_metadata_read_sites.toml"

#: Bare names that hold one chunk row's metadata dict in this code base.
_ROW_NAMES = frozenset({"metadata", "md", "row_meta", "chunk_meta", "chunk_metadata", "meta_row"})

Site = tuple[str, str, str, int]  # relative path, function qualname, key, line
SiteKey = tuple[str, str, str]  # relative path, function qualname, key
#: Opted-in bare receiver names for one file: scope ("" = the whole file, else a function
#: qualname that also covers its nested functions) -> names.
Receivers = Mapping[str, frozenset[str]]


def _is_metadata_source(node: ast.AST) -> bool:
    """True for ``x.metadata``, ``x["metadata"]``, ``x.get("metadata"...)``, or an ``or`` of them."""
    if isinstance(node, ast.BoolOp):
        return any(_is_metadata_source(v) for v in node.values)
    if isinstance(node, ast.Attribute):
        return node.attr == "metadata"
    if isinstance(node, ast.Subscript):
        return isinstance(node.slice, ast.Constant) and node.slice.value == "metadata"
    if isinstance(node, ast.Call):
        return (
            isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and bool(node.args)
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "metadata"
        )
    return False


class _Scanner(ast.NodeVisitor):
    def __init__(self, relpath: str, keys: frozenset[str], receivers: Receivers) -> None:
        self.relpath = relpath
        self.keys = keys
        self.receivers = receivers
        self.stack: list[str] = []
        self.aliases: list[set[str]] = [set()]
        self.sites: list[Site] = []

    def _scope(self, node: ast.AST, name: str) -> None:
        self.stack.append(name)
        self.aliases.append(set())
        self.generic_visit(node)
        self.aliases.pop()
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scope(node, node.name)

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    def visit_Assign(self, node: ast.Assign) -> None:
        if len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and _is_metadata_source(node.value):
            self.aliases[-1].add(node.targets[0].id)
        self.generic_visit(node)

    def _opted_in(self) -> frozenset[str]:
        qual = ".".join(self.stack)
        out: set[str] = set()
        for scope, names in self.receivers.items():
            if not scope or qual == scope or qual.startswith(scope + "."):
                out |= names
        return frozenset(out)

    def _is_row_metadata(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in _ROW_NAMES or node.id in self.aliases[-1] or node.id in self._opted_in()
        # x.metadata, and the chained sources: r["metadata"][k], r.get("metadata", {}).get(k),
        # (r.get("metadata") or {}).get(k)
        return _is_metadata_source(node)

    def _hit(self, key: object, node: ast.AST) -> None:
        if isinstance(key, str) and key in self.keys:
            self.sites.append((self.relpath, ".".join(self.stack) or "<module>", key, node.lineno))

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.ctx, ast.Load) and isinstance(node.slice, ast.Constant) and self._is_row_metadata(node.value):
            self._hit(node.slice.value, node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        f = node.func
        if (
            isinstance(f, ast.Attribute)
            and f.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and self._is_row_metadata(f.value)
        ):
            self._hit(node.args[0].value, node)
        self.generic_visit(node)


@functools.cache
def _config() -> dict:
    return tomllib.loads(SITES_FILE.read_text(encoding="utf-8"))


def _receivers_for(relpath: str, doc: Mapping | None = None) -> dict[str, frozenset[str]]:
    """The sites file's ``[receivers]`` entries for *relpath* (``path`` or ``path::qualname``)."""
    out: dict[str, frozenset[str]] = {}
    for site, names in (doc if doc is not None else _config()).get("receivers", {}).items():
        rel, _, func = site.partition("::")
        if rel == relpath:
            out[func] = frozenset(names)
    return out


def scan_source(
    source: str, relpath: str, keys: frozenset[str] | None = None, receivers: Receivers | None = None
) -> list[Site]:
    """The row reads of a non-CHUNK key in *source* (opt-in receivers: the sites file's, unless given)."""
    scanner = _Scanner(
        relpath,
        keys if keys is not None else ckm.non_chunk_row_keys(),
        receivers if receivers is not None else _receivers_for(relpath),
    )
    scanner.visit(ast.parse(source))
    return scanner.sites


@functools.cache
def _scan_repo() -> tuple[tuple[Site, ...], tuple[Site, ...], int]:
    """(sites, sites in the exempt module, files scanned): one AST pass over ``src/nexus`` per session."""
    sites: list[Site] = []
    exempt: list[Site] = []
    files = 0
    for path in sorted(SRC_ROOT.rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        files += 1
        found = scan_source(path.read_text(encoding="utf-8"), rel)
        (exempt if rel in ckm.LEGACY_FALLBACK_MODULES else sites).extend(found)
    return tuple(sites), tuple(exempt), files


def _count(sites) -> dict[SiteKey, int]:
    counts: collections.Counter[SiteKey] = collections.Counter()
    for rel, func, key, _line in sites:
        counts[(rel, func, key)] += 1
    return dict(counts)


def _observed() -> dict[SiteKey, int]:
    return _count(_scan_repo()[0])


def _parse_counts(tables: Mapping) -> tuple[dict[SiteKey, int], list[str]]:
    """``file::function`` tables of ``key = count`` -> (counts, problems). A count that is not a
    positive integer is a problem to report, never a crash in the comparison."""
    out: dict[SiteKey, int] = {}
    problems: list[str] = []
    for site, keys in tables.items():
        rel, _, func = site.partition("::")
        for key, count in keys.items():
            if isinstance(count, int) and not isinstance(count, bool) and count >= 1:
                out[(rel, func, key)] = count
            else:
                problems.append(f"{site} {key}: count {count!r} is not a positive integer")
    return out, problems


def _listed() -> dict[SiteKey, int]:
    return _parse_counts(_config()["sites"])[0]


def _exempt_listed() -> dict[SiteKey, int]:
    return _parse_counts(_config()["exempt"])[0]


def _remedy(key: str) -> str:
    row = ckm.row_for(key)
    remedy = f"reader remedy {row.remedy_reader}, writer remedy {row.remedy}" if row.remedy_reader else f"remedy {row.remedy}"
    return f"{key} is a {row.cls} key ({remedy}); read it from: {row.home}. Moved by: {row.moved_by}."


def _exempt_findings(sites, listed: Mapping[SiteKey, int]) -> list[str]:
    """The exempt module may read only ``LEGACY_FALLBACK_KEYS`` and its listed count only falls."""
    findings: list[str] = []
    observed = _count(sites)
    for (rel, func, key), n in sorted(observed.items()):
        if key not in ckm.LEGACY_FALLBACK_KEYS:
            findings.append(
                f"  {rel}::{func} reads {key}: the legacy-fallback module may read only "
                f"{sorted(ckm.LEGACY_FALLBACK_KEYS)} ({_remedy(key)})"
            )
        elif n > listed.get((rel, func, key), 0):
            findings.append(f"  {rel}::{func} {key}: found {n}, listed {listed.get((rel, func, key), 0)}; the fallback cannot grow")
    for (rel, func, key), n in sorted(listed.items()):
        if n > observed.get((rel, func, key), 0):
            findings.append(f"  {rel}::{func} {key}: listed {n}, found {observed.get((rel, func, key), 0)}; lower it")
    return findings


# --------------------------------------------------------------------------
# The ratchet over the repository


def test_no_row_read_of_a_non_chunk_key_outside_the_list():
    observed, listed = _observed(), _listed()
    new = {k: n for k, n in observed.items() if n > listed.get(k, 0)}
    assert not new, (
        "A chunk-row metadata read of a non-CHUNK key that is not on the list (RDR-236). "
        "A shared chunk row holds the last writer's value; read the catalog owner instead.\n"
        + "\n".join(f"  {rel}::{func}: {_remedy(key)} (found {n}, listed {listed.get(k, 0)})"
                    for k, n in sorted(new.items()) for rel, func, key in [k])
    )


def test_the_list_only_shrinks():
    observed, listed = _observed(), _listed()
    stale = {k: n for k, n in listed.items() if n > observed.get(k, 0)}
    assert not stale, (
        "tests/chunk_metadata_read_sites.toml lists reads that are gone (or fewer). The count "
        "may only fall: lower it or delete the entry in the commit that moved the reader.\n"
        + "\n".join(f"  {rel}::{func} {key}: listed {n}, found {observed.get(k, 0)}"
                    for k, n in sorted(stale.items()) for rel, func, key in [k])
    )


def test_the_sites_file_counts_are_positive_integers():
    problems = _parse_counts(_config()["sites"])[1] + _parse_counts(_config()["exempt"])[1]
    assert not problems, "\n".join(problems)


def test_a_count_that_is_not_an_integer_is_a_finding_not_a_crash():
    counts, problems = _parse_counts({"a.py::f": {"title": "2", "tags": 0, "ttl_days": True, "indexed_at": 3}})
    assert counts == {("a.py", "f", "indexed_at"): 3}
    assert len(problems) == 3


def test_the_scan_examined_enough_to_mean_something():
    """The floor is on files scanned. It is never on the live site count: that is the quantity the
    ratchet drives down, so a floor on it reds when Phase 2 succeeds. Detection is proved by the
    synthetic tests below."""
    _sites, _exempt, files = _scan_repo()
    assert files >= 300, f"scanned only {files} files under src/nexus"


def test_the_exempt_module_is_real_and_would_otherwise_be_flagged():
    for rel in ckm.LEGACY_FALLBACK_MODULES:
        path = REPO_ROOT / rel
        assert path.exists(), f"{rel} is named the legacy-fallback module but does not exist"
        assert scan_source(path.read_text(encoding="utf-8"), rel), (
            f"{rel} has no row read of a non-CHUNK key; drop it from LEGACY_FALLBACK_MODULES"
        )


def test_the_exempt_module_reads_only_its_two_keys_and_never_more_of_them():
    findings = _exempt_findings(_scan_repo()[1], _exempt_listed())
    assert not findings, "\n".join(["The legacy-fallback module grew or shrank (RDR-236 RF-8):", *findings])
    assert _scan_repo()[1], "the exempt module has no reads to bound"


def test_a_planted_read_in_the_exempt_module_is_caught():
    rel = sorted(ckm.LEGACY_FALLBACK_MODULES)[0]
    source = (REPO_ROOT / rel).read_text(encoding="utf-8")
    listed = _exempt_listed()
    # a new key
    planted = source + "\n\ndef _planted(meta_row):\n    return meta_row.get('tags')\n"
    findings = _exempt_findings(scan_source(planted, rel), listed)
    assert any("tags" in f and "may read only" in f for f in findings), findings
    # one more read of an allowed key in a listed function
    func, key = next((f, k) for (_r, f, k) in sorted(listed))
    grown = source + f"\n\ndef _planted(md):\n    return md.get({key!r})\n"
    findings = _exempt_findings(scan_source(grown, rel), listed)
    assert any("_planted" in f and "cannot grow" in f for f in findings), findings
    # and a read that went away is a stale listing
    findings = _exempt_findings([], listed)
    assert any("lower it" in f for f in findings), findings


def test_every_listed_key_is_a_non_chunk_key():
    keys = ckm.non_chunk_row_keys()
    assert {k for (_r, _f, k) in _listed()} <= keys
    assert {k for (_r, _f, k) in _exempt_listed()} <= ckm.LEGACY_FALLBACK_KEYS


def test_every_opted_in_receiver_matches_something():
    """A ``[receivers]`` entry that yields no read is stale (the readers moved): delete it."""
    entries = _config().get("receivers", {})
    assert entries, "no receivers are opted in; the Phase 2 reader files are the point of the table"
    for site in sorted(entries):
        rel, _, func = site.partition("::")
        source = (REPO_ROOT / rel).read_text(encoding="utf-8")
        without = scan_source(source, rel, receivers={})
        with_it = scan_source(source, rel, receivers={func: frozenset(entries[site])})
        assert len(with_it) > len(without), f"[receivers] {site} = {entries[site]} matches no read of a non-CHUNK key"


def test_the_remedy_text_quotes_the_reader_remedy_when_there_is_one():
    assert "reader remedy R5" in _remedy("catalog_doc_id") and "writer remedy R2" in _remedy("catalog_doc_id")
    assert "reader remedy" not in _remedy("tags") and "remedy R3" in _remedy("tags")


# --------------------------------------------------------------------------
# Non-vacuity: the scanner finds what it claims, on synthetic source


def _keys(src: str, **kw) -> list[str]:
    return [k for (_r, _f, k, _l) in scan_source(src, "x.py", receivers=kw.pop("receivers", {}), **kw)]


def test_scanner_flags_subscript_and_get_on_a_metadata_name():
    assert _keys("def f(metadata):\n    a = metadata['title']\n    b = metadata.get('tags', '')\n") == ["title", "tags"]


def test_scanner_flags_a_dot_metadata_attribute_and_names_the_function():
    sites = scan_source("class C:\n    def m(self, r):\n        return r.metadata.get('bib_year')\n", "x.py", receivers={})
    assert sites == [("x.py", "C.m", "bib_year", 3)]


def test_scanner_follows_a_local_alias_of_the_metadata_attribute():
    src = "def f(hit):\n    meta = hit.metadata or {}\n    return meta.get('ttl_days')\n"
    assert _keys(src) == ["ttl_days"]
    src2 = "def f(r):\n    m = r.get('metadata') or {}\n    return m['indexed_at']\n"
    assert _keys(src2) == ["indexed_at"]


def test_scanner_flags_chained_receivers():
    assert _keys("def f(r):\n    return r.get('metadata', {}).get('tags')\n") == ["tags"]
    assert _keys("def f(r):\n    return r['metadata']['title']\n") == ["title"]
    assert _keys("def f(r):\n    return (r.get('metadata') or {}).get('ttl_days')\n") == ["ttl_days"]
    assert _keys("def f(r):\n    return (r.metadata or {})['bib_year']\n") == ["bib_year"]
    assert _keys("def f(r):\n    return r.get('other', {}).get('tags')\n") == []


def test_scanner_ignores_chunk_row_keys_and_stores_and_other_receivers():
    src = (
        "def f(metadata, entry, other):\n"
        "    metadata['title'] = 1\n"            # a write
        "    a = metadata.get('chunk_text_hash')\n"   # a CHUNK key
        "    b = metadata.get('quarantined_at')\n"    # a ROW key
        "    c = entry.meta.get('title')\n"           # not a row metadata expression
        "    d = other['title']\n"
        "    e = metadata.get(some_variable)\n"
    )
    assert _keys(src) == []


def test_an_alias_does_not_leak_out_of_its_function():
    src = "def f(r):\n    meta = r.metadata\n\ndef g(meta):\n    return meta.get('title')\n"
    assert _keys(src) == []


def test_an_opted_in_bare_receiver_is_a_row_read_only_where_it_is_opted_in():
    src = (
        "def f(meta):\n    return meta.get('tags')\n\n"
        "class C:\n    def g(self, meta):\n        return meta['title']\n\n"
        "def h(entry):\n    return entry.get('title')\n"
    )
    assert _keys(src) == []
    whole_file = {"": frozenset({"meta"})}
    assert _keys(src, receivers=whole_file) == ["tags", "title"]
    scoped = {"C": frozenset({"meta"}), "h": frozenset({"entry"})}
    assert _keys(src, receivers=scoped) == ["title", "title"]
