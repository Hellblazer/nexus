# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``where`` filter, three ways, from one seeded generator (nexus-3ceda.11).

The in-memory store (``InMemoryVectorClient``) is what unit tests read through in
place of the engine, so its ``where`` semantics are only useful while they agree with
the engine's. The engine has two translations of its own: the get path
(``PgVectorRepository.metadataCondition``, behind ``/v1/vectors/get``) and the search
path (``planWhere``, behind ``/v1/vectors/search``). Example-based tests pinned each
side on a few rows; nothing compared them on the same inputs, and the first run of
this comparison found that every multi-clause CLI filter with an operator in it
(``content_type=pdf,bib_year>=2020``) failed every collection with an HTTP 400, while
the in-memory store answered it.

Each test writes a seeded set of rows to both stores, generates seeded filters, and
asks all three sides. The first filter they disagree on fails the test with the seed,
the filter, every side's answer, and ``blame``: the side holding the minority
answer (every side, when no answer has a majority). Results are compared exactly (row sets), since a filter has no
float output.

The generators draw from what callers send: operands of the field's own type, and
CLI filters through :func:`nexus.filters.parse_where_str`. Where the two stores
differ on purpose or by an engine decision not yet taken, the difference is pinned in
:data:`_KNOWN_DIVERGENCES`, so a change on either side shows up as a failure there.
"""
from __future__ import annotations

import hashlib
import random
import uuid
from collections.abc import Callable
from typing import Any

import pytest

import nexus.db.http_vector_client as hvc
from nexus.catalog.store_hook import catalog_store_hook_tracked
from nexus.db.inmemory_vector_store import InMemoryVectorClient
from nexus.doc_indexer import _write_chunks_with_owner_rows
from nexus.filters import parse_where_str

_MODEL = "bge-base-en-v15-768"
_SEEDS: tuple[int, ...] = (0x5EED, 20261109, 7)
_ROWS = 12
_QUERIES = 60
#: Lowercase letters and digits only: PostgreSQL compares text under the database's
#: collation, and C and en_US agree on this alphabet, so a string range means the same
#: on every host the suite runs on.
_STRINGS = ("", "a", "ab", "b", "z", "1", "10", "9")
_INTS = (-3, -1, 0, 1, 2, 3)
#: Exact binary fractions, so each side renders the same digits.
_FLOATS = (-1.25, 0.5, 1.5, 2.25)
_POOLS: dict[str, tuple[Any, ...]] = {
    "dx_s": _STRINGS, "dx_i": _INTS, "dx_f": _FLOATS, "dx_b": (True, False),
}
_RANGE = ("$gt", "$gte", "$lt", "$lte")

REFUSED = "refused"
Answer = frozenset[str] | str


class _Stores:
    """The same rows in the in-memory store and in one engine collection, read three ways."""

    def __init__(self, tenant: str, rows: list[dict[str, Any]]) -> None:
        self.collection = f"knowledge__diffwhere-{uuid.uuid4().hex[:8]}__{_MODEL}__v1"
        texts = [f"where differential row {i} {uuid.uuid4().hex}" for i in range(len(rows))]
        chashes = [hashlib.sha256(t.encode()).hexdigest() for t in texts]
        self.label = {c: f"r{i}" for i, c in enumerate(chashes)}
        metas = [{**row, "chunk_text_hash": c} for row, c in zip(rows, chashes)]

        self.memory = InMemoryVectorClient().create_collection(
            self.collection, metadata={"hnsw:space": "cosine"})
        self.memory.add(ids=chashes, documents=texts, metadatas=metas,
                        embeddings=[[1.0, float(i), 0.0, 0.0] for i in range(len(rows))])

        self.client = hvc.HttpVectorClient(tenant=tenant)
        doc_hash = hashlib.sha256("".join(texts).encode()).hexdigest()
        tumbler, _ = catalog_store_hook_tracked(
            title=f"where-differential-{uuid.uuid4().hex}", doc_id=doc_hash,
            collection_name=self.collection)
        _write_chunks_with_owner_rows(self.collection, tumbler, doc_hash, chashes, texts, metas)
        stored = self.client.get_collection(self.collection).get(
            include=[], limit=_ROWS * 4, sensitivity="include")["ids"]
        assert sorted(stored) == sorted(chashes), "the engine did not store every generated row"

    def _labels(self, ids: list[str]) -> frozenset[str]:
        return frozenset(self.label[i] for i in ids)

    def sides(self) -> dict[str, Callable[[dict[str, Any]], Answer]]:
        def memory(where: dict[str, Any]) -> Answer:
            try:
                got = self.memory.get(where=where, include=[], sensitivity="include")
            except ValueError:  # the store's up-front refusal; any other error is a bug and raises
                return REFUSED
            return self._labels(got["ids"])

        def engine_get(where: dict[str, Any]) -> Answer:
            try:
                got = self.client.get_collection(self.collection).get(
                    where=where, include=[], limit=_ROWS * 4, sensitivity="include")
            except hvc.VectorServiceError as exc:
                if exc.code != 400:
                    raise
                return REFUSED
            return self._labels(got["ids"])

        def engine_search(where: dict[str, Any]) -> Answer:
            try:
                hits = self.client.search(
                    "where differential row", [self.collection], n_results=_ROWS * 4,
                    where=where, threshold=float("inf"), sensitivity="include")
            except hvc.VectorServiceError as exc:
                if exc.code != 400:
                    raise
                return REFUSED
            return self._labels([h["id"] for h in hits])

        return {"inmemory": memory, "engine.get": engine_get, "engine.search": engine_search}


def _answers(stores: _Stores, where: dict[str, Any]) -> dict[str, Answer]:
    return {name: ask(where) for name, ask in stores.sides().items()}


def _blame(answers: dict[str, Answer]) -> list[str]:
    """The sides holding a minority answer; all of them when no answer has a majority."""
    counts = {a: sum(1 for b in answers.values() if b == a) for a in answers.values()}
    top = max(counts.values())
    if top == 1:
        return sorted(answers)
    return sorted(name for name, a in answers.items() if counts[a] < top)


def _report(seed: int, n: int, where: object, answers: dict[str, Answer]) -> str:
    blame: object = _blame(answers)
    if set(answers.values()) == {REFUSED}:
        blame = "every side refused a filter it should accept"
    shown = {name: (a if a == REFUSED else sorted(a)) for name, a in answers.items()}
    return f"seed={seed:#x} query #{n}: where={where!r}\n  answers={shown}\n  blame={blame}"


def _vacuity(seed: int, seen: list[Answer]) -> list[str]:
    """Agreement only counts when the filters selected something and also left
    something out: a generator whose every filter matched nothing (or everything)
    would agree on every side and prove nothing."""
    sizes = [len(a) for a in seen if a != REFUSED]
    some = sum(1 for s in sizes if 0 < s < _ROWS)
    if some >= len(seen) // 4:
        return []
    return [f"seed={seed:#x}: only {some} of {len(seen)} filters selected some rows but not all "
            f"(sizes {sorted(sizes)}); the generator is not exercising the filter"]


def _gen_rows(rng: random.Random) -> list[dict[str, Any]]:
    """Each field present four times in five, so missing fields are exercised too."""
    return [
        {field: rng.choice(pool) for field, pool in _POOLS.items() if rng.random() < 0.8}
        for _ in range(_ROWS)
    ]


def _gen_clause(rng: random.Random, field: str) -> Any:
    pool = _POOLS[field]
    ops = ["implicit", "$eq", "$ne", "$in", "$nin"] + ([] if field == "dx_b" else list(_RANGE))
    op = rng.choice(ops)
    if op == "implicit":
        return rng.choice(pool)
    if op in ("$in", "$nin"):
        return {op: rng.sample(pool, rng.randint(0, min(3, len(pool))))}
    return {op: rng.choice(pool)}


def _gen_where(rng: random.Random) -> dict[str, Any]:
    fields = rng.sample(sorted(_POOLS), rng.randint(1, 3))
    return {field: _gen_clause(rng, field) for field in fields}


def _typed_filters_disagree(tenant: str, seed: int) -> list[str]:
    """Typed filters, operands of each field's own type: all three sides give the
    same rows and none refuses."""
    rng = random.Random(seed)
    stores = _Stores(tenant, _gen_rows(rng))
    seen: list[Answer] = []
    for n in range(_QUERIES):
        where = _gen_where(rng)
        answers = _answers(stores, where)
        if len(set(answers.values())) != 1 or REFUSED in answers.values():
            return ["typed: " + _report(seed, n, where, answers)]
        seen.append(answers["inmemory"])
    return _vacuity(seed, seen)


#: CLI fields: two numeric (``nexus.filters.NUMERIC_FIELDS``, so values parse as
#: ints) and two text, none of them in ``ManifestLabelStage.ANSWERED_KEYS``, so the
#: engine answers every one from the chunk row. The keys it answers from the
#: catalog owner (``bib_*``, ``title``, ``content_type``, ``extraction_method``,
#: the section labels and others) are left out: the in-memory store has no
#: catalog and answers them from the row (pinned below).
_CLI_INTS: dict[str, tuple[int, ...]] = {"page_count": (1, 2, 5, 9), "chunk_count": (0, 3, 10)}
_CLI_TEXT: dict[str, tuple[str, ...]] = {
    "dx_text": ("alpha", "beta"), "language": ("en", "de", "fr"),
}


def _gen_cli_rows(rng: random.Random) -> list[dict[str, Any]]:
    pools: dict[str, tuple[Any, ...]] = {**_CLI_INTS, **_CLI_TEXT}
    return [{k: rng.choice(p) for k, p in pools.items() if rng.random() < 0.8} for _ in range(_ROWS)]


def _gen_cli_where(rng: random.Random) -> str:
    """One to three clauses on distinct fields, the shape a user types."""
    clauses = []
    for field in rng.sample(sorted({**_CLI_INTS, **_CLI_TEXT}), rng.randint(1, 3)):
        if field in _CLI_INTS:
            op = rng.choice(("=", "!=", ">=", "<=", ">", "<"))
            clauses.append(f"{field}{op}{rng.choice(_CLI_INTS[field])}")
        else:
            clauses.append(f"{field}{rng.choice(('=', '!='))}{rng.choice(_CLI_TEXT[field])}")
    return ",".join(clauses)


def _cli_filters_disagree(tenant: str, seed: int) -> list[str]:
    """A filter the CLI grammar produces on distinct fields is one the engine accepts,
    with the in-memory answer."""
    rng = random.Random(seed)
    stores = _Stores(tenant, _gen_cli_rows(rng))
    seen: list[Answer] = []
    for n in range(_QUERIES):
        text = _gen_cli_where(rng)
        where = parse_where_str(text)
        assert where is not None
        answers = _answers(stores, where)
        if len(set(answers.values())) != 1 or REFUSED in answers.values():
            return ["cli: " + _report(seed, n, text, answers)]
        seen.append(answers["inmemory"])
    return _vacuity(seed, seen)


# ── Known divergences ─────────────────────────────────────────────────────────
# One fixed set of rows (the probe that first found them), and for each filter what
# each side answers today. The typed comparisons are engine decisions not yet taken,
# recorded on nexus-3ceda.11: the get path compares a typed value as text and the
# search path mostly does too, but not for a number. The owner-answered key is by
# design: the engine reads it from the catalog document, which the in-memory store
# does not model.

_PINNED_ROWS: list[dict[str, Any]] = [
    {"dx_s": "1", "dx_i": 1, "dx_f": 1.0, "dx_b": True, "bib_year": 2021},
    {"dx_s": "true", "dx_i": 2, "dx_f": 2.5, "dx_b": False},
    {"dx_s": "b", "dx_i": -3},
]
_NONE: frozenset[str] = frozenset()
_R0: frozenset[str] = frozenset({"r0"})

_KNOWN_DIVERGENCES: list[tuple[dict[str, Any], dict[str, Answer]]] = [
    # A string operand on an int field: both engine paths match it as text.
    ({"dx_i": "1"}, {"inmemory": _NONE, "engine.get": _R0, "engine.search": _R0}),
    # An int operand on a string field: the same, the other way round.
    ({"dx_s": 1}, {"inmemory": _NONE, "engine.get": _R0, "engine.search": _R0}),
    # A string "true" on a bool field.
    ({"dx_b": "true"}, {"inmemory": _NONE, "engine.get": _R0, "engine.search": _R0}),
    # An int operand on a stored 1.0: Python's 1 == 1.0 and the search path match;
    # the get path compares "1" with "1.0" and does not.
    ({"dx_f": 1}, {"inmemory": _R0, "engine.get": _NONE, "engine.search": _R0}),
    # True on an int field: Python's True == 1 matches; neither engine path does.
    ({"dx_i": True}, {"inmemory": _R0, "engine.get": _NONE, "engine.search": _NONE}),
    # A list operand for $eq: the search path refuses it, the in-memory store with it;
    # the get path matches the list's text, "[1]", which no row holds.
    ({"dx_i": {"$eq": [1]}}, {"inmemory": REFUSED, "engine.get": _NONE, "engine.search": REFUSED}),
    # A key the engine answers from the catalog owner, whose document has no bib_year.
    ({"bib_year": 2021}, {"inmemory": _R0, "engine.get": _NONE, "engine.search": _NONE}),
]


def _known_divergences_changed(stores: _Stores) -> list[str]:
    return [
        f"a known divergence changed (update the table and nexus-3ceda.11): where={where!r}\n"
        f"  expected={expected}\n  actual={actual}"
        for where, expected in _KNOWN_DIVERGENCES
        if (actual := _answers(stores, where)) != expected
    ]


#: Shapes the engine refuses with an HTTP 400 on both paths, whatever the rows:
#: the in-memory store must refuse them too, before it reads a row. If the engine
#: starts accepting one (``$and`` is an engine follow-on on nexus-3ceda.11), this
#: goes red and the store must follow.
_REFUSED_EVERYWHERE: list[dict[str, Any]] = [
    {"$and": [{"dx_i": {"$gte": 1}}, {"dx_i": {"$lte": 2}}]},
    {"$or": [{"dx_i": 1}, {"dx_s": "b"}]},
    {"dx_i": {"$gte": 1, "$lte": 2}},
    {"dx_s": "nomatch", "$and": [{"dx_i": 1}]},
    {"dx_s": "nomatch", "dx_i": {"$gt": 1, "$lt": 3}},
    {"dx_s": "nomatch", "dx_i": {"$bogus": 1}},
    {"dx_s": {"$in": "1b"}},
    {"dx_s": {"$nin": "1"}},
]

#: Filters across a type, where the two stores now give the same answer.
_AGREED: list[tuple[dict[str, Any], frozenset[str]]] = [
    # A numeric range on a string field: no stored string is a number, on either side.
    ({"dx_s": {"$gt": 0}}, _NONE),
]


def _refusals_or_agreements_differ(stores: _Stores) -> list[str]:
    wrong = []
    for where in _REFUSED_EVERYWHERE:
        answers = _answers(stores, where)
        if set(answers.values()) != {REFUSED}:
            wrong.append(f"not refused everywhere: where={where!r} answers={answers} blame={_blame(answers)}")
    for where, expected in _AGREED:
        answers = _answers(stores, where)
        if set(answers.values()) != {expected}:
            wrong.append(f"not agreed: where={where!r} answers={answers} expected={sorted(expected)}")
    return wrong


def _reported_filter_refused(stores: _Stores) -> list[str]:
    """``content_type=pdf,bib_year>=2020``: both keys are answered from the catalog
    owner, which the in-memory store does not model, so this asks only that the
    engine accept the parsed filter on both paths, where it used to answer 400."""
    where = parse_where_str("content_type=pdf,bib_year>=2020")
    sides = stores.sides()
    return [f"{name} refused the reported filter {where!r}"
            for name in ("engine.get", "engine.search") if sides[name](where) == REFUSED]


@pytest.mark.t2_tenant
def test_the_where_filter_agrees_across_the_in_memory_store_and_both_engine_paths(t2_service_env) -> None:
    """Every check in this module, in one test, so the module mints one tenant
    (``tests/storage_touch_ceiling.toml``: a test that mints a tenant counts against
    a ceiling with no tolerance). Each check reports its own first failure, and the
    test fails listing every check that failed."""
    failures: list[str] = []
    for seed in _SEEDS:
        failures += _typed_filters_disagree(t2_service_env, seed)
        failures += _cli_filters_disagree(t2_service_env, seed)
    pinned = _Stores(t2_service_env, _PINNED_ROWS)
    failures += _known_divergences_changed(pinned)
    failures += _refusals_or_agreements_differ(pinned)
    failures += _reported_filter_refused(pinned)
    assert not failures, "\n\n".join(failures)
