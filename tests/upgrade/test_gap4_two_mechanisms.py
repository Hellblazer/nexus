# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-185 P3.2 (nexus-n7u38.24): Gap-4 pinned falsifiably.

"How far from current?" has EXACTLY TWO answer mechanisms, by class:

1. DATA-rung state — answered SOLELY by the ladder position DERIVED from
   per-rung completion records (``upgrade_ladder/completion.py``).
2. PRECONDITION freshness — answered SOLELY by a fresh on-disk
   installed-vs-required comparison (``upgrade_ladder/preconditions.py``),
   never recorded.

NO THIRD mechanism may independently answer the question: not the
``last_seen_version`` marker file (a transition-trigger optimization, not
an authority), not the daemon lease (an INPUT to mechanism 2), not the
name-encoded model segment or ad-hoc re-sampling as free-standing
authorities (they live INSIDE rung detect()). RQ4 counted SEVEN version
mechanisms pre-ladder; this suite is the tripwire that the consolidation
holds — the ``test_lifecycle_gate.py`` discipline (a docs rule alone
degrades to hope), with non-vacuity companions proving each detector
actually fires.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).parent.parent.parent
LADDER_ROOT = REPO_ROOT / "src" / "nexus" / "upgrade_ladder"
COMPLETION = LADDER_ROOT / "completion.py"
PRECONDITIONS = LADDER_ROOT / "preconditions.py"

_ALLOW_TOKEN = "# gap4-allow:"


def _ladder_files() -> list[pathlib.Path]:
    return [p for p in LADDER_ROOT.rglob("*.py") if "__pycache__" not in p.parts]


# ── Mechanism 1: derived position, defined once, never settable ──────────────


def _defs_named(tree: ast.AST, name: str) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]


def test_ladder_position_defined_only_in_completion_store() -> None:
    """The data-mechanism's single source of truth: the position derivation
    is defined exactly once, in completion.py. A second definition is a
    competing data authority. (RDR-186 .12: the SQLite store's
    ``ladder_position`` wrapper died with ladder.db; ``derive_ladder_position``
    is — and always was — the single pinned algorithm, so the scan tracks
    the surviving definition. Assertion shape unchanged: exactly one def,
    exactly one home.)"""
    definitions: list[pathlib.Path] = []
    for path in _ladder_files():
        if _defs_named(ast.parse(path.read_text(encoding="utf-8")), "derive_ladder_position"):
            definitions.append(path)
    assert definitions == [COMPLETION], (
        f"derive_ladder_position must be defined ONLY in completion.py; found "
        f"{[str(p.relative_to(REPO_ROOT)) for p in definitions]}"
    )


def test_retired_ladder_position_name_is_never_reintroduced() -> None:
    """The RETIRED name stays under surveillance (Hal directive 2026-07-18):
    ``ladder_position`` was the SQLite store's wrapper, deleted with
    ladder.db at RDR-186 .12. A NEW def by that name — anywhere in the
    ladder package — would be a second position authority alongside
    ``derive_ladder_position``, the exact competing-definition class this
    suite exists to ban. The scan above tracks the surviving single
    definition; this one bans the ghost coming back."""
    offenders: list[str] = []
    for path in _ladder_files():
        for node in _defs_named(
            ast.parse(path.read_text(encoding="utf-8")), "ladder_position"
        ):
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    assert offenders == [], (
        "the retired `ladder_position` name was reintroduced as a definition — "
        f"derive_ladder_position is the ONLY sanctioned algorithm: {offenders}"
    )


def test_no_position_setter_anywhere_in_the_ladder() -> None:
    """No function in the ladder package accepts-and-stores a position: the
    RQ6 'never independently settable' invariant, package-wide. Any def
    whose name matches set/write/advance + position is an offender."""
    offenders: list[str] = []
    banned = ("set_position", "set_ladder_position", "write_position", "advance_position")
    for path in _ladder_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.lstrip()
            for name in banned:
                if stripped.startswith(f"def {name}(") and _ALLOW_TOKEN not in line:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    assert not offenders, f"position setter reintroduced: {offenders}"


# ── Mechanism 2: preconditions are STATELESS (zero persistence) ──────────────


_WRITE_CALL_NAMES = frozenset(
    {"write_text", "write_bytes", "dump", "connect", "execute", "executemany"}
)


def _persistence_calls(tree: ast.AST) -> list[str]:
    """Call sites that could persist a verdict: file writes, json.dump,
    sqlite connects/executes, and open(..., mode with 'w'/'a'/'+') in BOTH
    the builtin two-arg form and the pathlib bound-method form (P3 review:
    ``path.open('w')`` carries the mode at args[0], not args[1])."""
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else (
            func.id if isinstance(func, ast.Name) else ""
        )
        if name in _WRITE_CALL_NAMES:
            hits.append(name)
        if name == "open":
            candidates = list(node.args) + [
                kw.value for kw in node.keywords if kw.arg == "mode"
            ]
            for arg in candidates:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and any(
                    m in arg.value for m in ("w", "a", "+")
                ):
                    hits.append("open(w)")
                    break
    return hits


def test_preconditions_module_persists_nothing() -> None:
    """Stateless means STATELESS: the preconditions module contains zero
    persistence call sites. Recording a verdict is the third-authority
    reintroduction Gap-4 bans (a stored 'engine current' fact is the
    RDR-142 stale-pointer class on the second axis)."""
    tree = ast.parse(PRECONDITIONS.read_text(encoding="utf-8"))
    assert _persistence_calls(tree) == [], (
        "upgrade_ladder/preconditions.py acquired a persistence call — "
        "precondition verdicts must be re-derived, never recorded"
    )


def test_persistence_detector_is_non_vacuous() -> None:
    """The detector actually fires on each persistence shape — including the
    two false-negative shapes the P3 review found (pathlib bound-method
    open, single-row execute)."""
    assert _persistence_calls(ast.parse("path.write_text('v1')")) == ["write_text"]
    assert _persistence_calls(ast.parse("json.dump(x, fh)")) == ["dump"]
    assert _persistence_calls(ast.parse("sqlite3.connect('x.db')")) == ["connect"]
    assert _persistence_calls(ast.parse("open(p, 'w')")) == ["open(w)"]
    assert _persistence_calls(ast.parse("path.open('w')")) == ["open(w)"]  # bound form
    assert _persistence_calls(ast.parse("path.open(mode='a')")) == ["open(w)"]
    assert _persistence_calls(ast.parse("cursor.execute('INSERT ...')")) == ["execute"]
    assert _persistence_calls(ast.parse("open(p)")) == []  # read-only open is fine
    assert _persistence_calls(ast.parse("path.open()")) == []


# ── No third authority ───────────────────────────────────────────────────────


_MARKER_AUTHORITY_TOKENS = ("STAMP_FILENAME", "last_seen_version", "marker_path")


def test_ladder_never_consults_the_marker_stamp() -> None:
    """The f0pmd ``last_seen_version`` stamp is a transition-trigger
    optimization OUTSIDE the ladder — the ladder package must never read it
    as an authority. (The lease is consumed ONLY as a comparison input via
    the injectable seam in preconditions.py.)"""
    offenders: list[str] = []
    for path in _ladder_files():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue  # prose may cite it; code may not
            for token in _MARKER_AUTHORITY_TOKENS:
                if token in line and _ALLOW_TOKEN not in line:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {token}")
    assert not offenders, (
        "the ladder package consults a marker/stamp as an authority — the "
        f"third-mechanism reintroduction Gap-4 bans: {offenders}"
    )


def test_completion_records_carry_no_precondition_state() -> None:
    """Class separation both ways: the completion FACT SHAPE has no
    engine/process/package-binary columns — provisioning state never enters
    the data mechanism's records. (RDR-186 .12 port: the SQLite schema died
    with ladder.db; the surviving authority on what a completion fact
    carries is the CompletionRecord dataclass every ledger serves.)"""
    from nexus.upgrade_ladder.completion import CompletionRecord

    fields = set(CompletionRecord.__dataclass_fields__)
    assert fields == {"rung_name", "verified_at", "package_version", "detail"}
    forbidden = {"engine", "process", "binary", "position", "installed", "required"}
    assert not any(
        any(token in f for token in forbidden) for f in fields
    ), f"precondition/position state leaked into the completion fact shape: {fields}"

def test_exactly_two_mechanism_modules_exist() -> None:
    """The census pin: the ladder package's answer surfaces are exactly
    {completion.py (data), preconditions.py (provisioning)} — a third
    sibling module answering 'how far from current' must be argued past
    this test, not slipped in."""
    answer_modules = {
        p.name
        for p in _ladder_files()
        if _defs_named(ast.parse(p.read_text(encoding="utf-8")), "derive_ladder_position")
        or _defs_named(ast.parse(p.read_text(encoding="utf-8")), "check_preconditions")
    }
    assert answer_modules == {"completion.py", "preconditions.py"}


SRC_ROOT = REPO_ROOT / "src" / "nexus"

#: Every module allowed to invoke the SINGLE engine-convergence mechanism
#: (detect_engine_convergence / converge_engine). P3 critique High: the
#: package-scoped scans above cannot see a third TRIGGER growing outside
#: upgrade_ladder/ — this census can. One mechanism, temporarily two
#: converge triggers (decision addendum
#: nexus_rdr/185-p3-engine-trigger-duality-decision):
_ENGINE_MECHANISM_CALLERS = frozenset({
    "upgrade_finish.py",              # defines it + the transition finisher
                                      # (engine leg P4-scoped for demotion, .28)
    "commands/daemon.py",             # restart-stale operator verb (P4 .28 scope)
    "upgrade_ladder/preconditions.py",  # the ladder's precondition stage
    "health.py",                      # doctor read-only convergence check
    "engine_version.py",              # docstring/derivation home (no live call)
})


def test_engine_mechanism_callers_are_allowlisted() -> None:
    """Codebase-wide census (not package-scoped): any NEW module invoking
    the engine-convergence mechanism must be argued past this allowlist —
    a third converge trigger reintroduced anywhere in src/ fails here."""
    offenders: list[str] = []
    for path in SRC_ROOT.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(SRC_ROOT).as_posix()
        if rel in _ENGINE_MECHANISM_CALLERS:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if (
                ("detect_engine_convergence" in line or "converge_engine" in line)
                and _ALLOW_TOKEN not in line
            ):
                offenders.append(f"src/nexus/{rel}:{lineno}")
    assert not offenders, (
        "a module outside the engine-convergence allowlist invokes the "
        "mechanism — a new trigger must be reconciled with the P3 decision "
        f"addendum first: {offenders}"
    )


def test_engine_caller_allowlist_is_non_vacuous() -> None:
    """The allowlisted callers actually exist and reference the mechanism —
    otherwise the census above is scanning for something extinct."""
    referencing = set()
    for rel in _ENGINE_MECHANISM_CALLERS:
        path = SRC_ROOT / rel
        assert path.exists(), f"allowlisted module vanished: {rel}"
        text = path.read_text(encoding="utf-8")
        if "detect_engine_convergence" in text or "converge_engine" in text:
            referencing.add(rel)
    assert "upgrade_ladder/preconditions.py" in referencing
    assert "upgrade_finish.py" in referencing


def test_lease_survives_only_as_comparison_input() -> None:
    """The lease field is read via the injectable ``_lease_fn`` seam and
    compared — never re-published, never stored by the ladder. Mechanical
    form: preconditions.py references the lease ONLY inside read/compare
    functions (no write-verbs near it), and no OTHER ladder module touches
    the lease at all."""
    for path in _ladder_files():
        if path == PRECONDITIONS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            ident = ""
            if isinstance(node, ast.Name):
                ident = node.id
            elif isinstance(node, ast.Attribute):
                ident = node.attr
            elif isinstance(node, ast.arg):
                ident = node.arg
            if "lease" in ident.lower():
                pytest.fail(
                    f"{path.relative_to(REPO_ROOT)}:{node.lineno} touches the "
                    f"lease ({ident!r}) — only preconditions.py may consume "
                    "it, as an input"
                )


# ── a RUNG's internal convergence check (P4.R2 Finding 4) ────────────────────


def test_rung_convergence_is_re_derived_live_never_cached() -> None:
    """A rung's "am I converged?" is answered from the LIVE world, every time.

    Gap-4 bans a THIRD mechanism that independently answers "how far is this
    install from current". A rung's internal detect()/verify() is not that —
    it is the level-triggered reconciliation (RQ5) the protocol requires, and
    the thing that lets a crashed run self-heal. What WOULD be the banned
    third mechanism is a rung persisting its own freestanding "converged"
    verdict: a cache file, a second table, a marker. So the property to pin is
    that the answer tracks the world rather than any remembered fact.

    EVIDENTIARY NOTE (the reason this test exists — P4.R2 Finding 4): the
    other pins in this module AST-scan for `ladder_position` /
    `check_preconditions` definitions and for specifically-named banned
    tokens. By construction none of them ever look at a rung's internal
    detect/verify logic, so they pass on THIS question coincidentally, not by
    evaluating it. Their green was cited as evidence once; it was not. This
    test evaluates it — and it is the one that fails if a future rung starts
    caching its verdict rather than re-deriving it.

    SANCTIONED CHEAP DETECT (RDR-186 .7, recorded so no future reader
    re-proposes a marker): an engine-side LIVE membership computation —
    ``nexus.remap_membership()`` over the raw-fact ``chash_remap`` map
    joined to the live chunk tables, called fresh by detect()/verify() each
    walk — is the compliant way to make the substrate rung's detect() cheap.
    It is the RESOLVED form of nexus-ixl85: one indexed SQL round trip
    replaces the paged probe, with NO stored verdict anywhere (the map rows
    are inert inputs a live computation interprets; the answer tracks the
    world in both directions exactly as this test demands).
    """
    from nexus.migration.detection import CollectionClassification  # noqa: PLC0415 — test-local fixture construction
    from nexus.upgrade_ladder.rungs.substrate_etl import SubstrateEtlRung  # noqa: PLC0415 — test-local fixture construction

    legacy = CollectionClassification(
        collection="knowledge__old", leg="local", model="voyage-context-3",
        dim=1024, support="supported-voyage-1024", source_count=10,
        has_data=True, legacy_ids=True,
    )
    world = {"target_rows": 0}
    rung = SubstrateEtlRung(
        footprint_fn=lambda: True,
        classify_fn=lambda: [legacy],
        voyage_key_fn=lambda: True,
        target_counts_fn=lambda: {"knowledge__old": world["target_rows"]},
        unreflected_fn=lambda: [],
        cascade_only_fn=lambda _r: "",
        # Pin the membership seam like every other rung test (critic-146xx-7
        # hygiene): None = could-not-tell, so count-equality alone drives
        # this test's world-tracking assertions — no engine construction.
        membership_fn=lambda _s, _t: None,
    )

    assert rung.detect().pending is True, "nothing migrated yet — must read pending"
    world["target_rows"] = 10  # the world converges underneath the rung
    assert rung.detect().pending is False, (
        "the rung answered from a remembered verdict, not the live world — a "
        "cached convergence fact is the Gap-4 third mechanism"
    )
    world["target_rows"] = 0  # ...and regresses (a rollback, a dropped target)
    assert rung.detect().pending is True, (
        "the rung kept saying converged after the world stopped being — "
        "level-triggered reconciliation means the answer follows the world "
        "in BOTH directions"
    )
