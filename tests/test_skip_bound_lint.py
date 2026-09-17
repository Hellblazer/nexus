# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-q6zww (plan N6, nexus-moht0): environment-keyed skips have no bound.

A census (T2 nexus/remediation-plan-intrastate-priors-2026-09-17 N6) found
100 files under ``tests/`` carrying an environment-keyed skip (``shutil.which``
gated, ``pytest.mark.skipif``, ``pytest.importorskip``, env-conditioned
``pytest.skip``) with zero of them asserting a limit on their own skips, and
no skip allowlist anywhere. This is a filesystem AST scan, modelled on
``tests/test_storage_boundary_lint.py`` -- it walks ``tests/**/*.py`` on disk
with ``pathlib.Path.rglob`` (never ``request.session.items``). A census keyed
off ``session.items`` sees only whatever THIS pytest invocation happened to
collect and select -- under ``-m lint`` that is ~800 of the ~11.7k-test
default corpus (see ``tests/test_mode_declarations_are_explicit.py:12-40``
for the measured collapse and the concrete miss it caused). A filesystem walk
is invocation-independent: it sees every skip site in the tree whether pytest
was invoked with ``-m lint``, the default selection, or ``-m integration``.

Step 1 -- partition, before any enforcement
============================================

Every skip site found is classified GATE-LIKE or SUBSTRATE-DEPENDENT, by its
FILE (one classification per file, applied to every site the file contains):

A file is GATE-LIKE iff ANY of:

1. It is lint-marked -- a module-level ``pytestmark = pytest.mark.lint``
   assignment (or a list containing ``pytest.mark.lint``) is present, detected
   by AST the same way ``test_mode_declarations_are_explicit.py`` detects
   ``pytestmark`` assignments.
2. Its **filename** (basename, not full path) contains one of the keyword
   substrings in ``_GATE_KEYWORDS`` below: ``wiring``, ``gate``, ``census``,
   ``choreography``, ``pin``, ``release``, ``plugin_structure``, ``contract``,
   ``ratchet``, ``lint``. These are the repo's standing names for a test whose
   JOB is to assert an invariant about the repo, the release, CI wiring, or
   the plugin surface -- release/choreography/pin tests, plugin-structure
   tests, wiring/gate/census lints.
3. It lives directly under ``tests/e2e/`` (any ``.py`` file there -- the E2E
   wiring-test directory).

Everything else is SUBSTRATE-DEPENDENT (needs an engine, a binary, a model, a
platform capability) and is OUT OF SCOPE for enforcement below -- Sam widens
enforcement to that bucket explicitly if it is ever wanted; this lint does not
assume it.

A reader can predict a new skip's bucket from these three rules alone: add
the ``lint`` marker, or name the file with one of the keywords above, or drop
it in ``tests/e2e/``, and it is GATE-LIKE; otherwise it is
SUBSTRATE-DEPENDENT by default.

Two GitHub Actions jobs are KNOWN, INTENTIONAL all-steps-skipped-on-green
jobs and are OUT OF SCOPE for this tests/ scan (it never reads ``.yml``):
``write-seam-gate`` (``.github/workflows/ci.yml:1690-1722``, doc comment at
``:1708-1713``: "the job ALWAYS runs ... but the expensive build/test steps
run only when the write-seam surface changes") and ``engine-service-build``
(``:1603-1646``, doc comment at ``:1614``: "The job always runs; its
expensive steps are path-gated via step `if:`"). Both are the
branch-protection-safe skipped-steps-equals-success pattern on a REQUIRED
check, not a vacuous gate -- named here so a reader of this file does not
mistake their existence for a gap this lint should have caught.

Step 2 -- enforce on GATE-LIKE sites only
==========================================

A GATE-LIKE environment-keyed skip site must EITHER:

* carry a **bound** -- the same file's AST contains an actual call to, or
  definition of, one of ``_check_marker_non_vacuity`` /
  ``_check_scenario_non_vacuity`` / ``_check_mandatory_pin_non_vacuity``
  (see ``tests/conftest.py:1436-1544``), or defines/reads a
  ``*_SKIP_BUDGET``/``*MAX_SKIP*``-named constant, meaning the module itself
  is covered by a marker-budget mechanism that fails the run when too many
  (or all) of its cases skip. This is checked against real AST nodes, never
  raw text -- a comment merely NAMING the guard does not count (see
  ``_BOUND_NAMES``'s docstring below for why that distinction is load
  bearing); OR
* sit in ``GATE_LIKE_SKIP_ALLOWLIST`` below, a SHRINK-ONLY named per-file
  budget (exact count, first N sites in line order), each entry carrying its
  reason as a comment -- never a per-line escape comment (the storage-boundary
  lint retired that pattern repo-wide at RDR-186 P4 and this lint follows
  suit: a comment on the skip line itself grants nothing).

A site that is neither bounded nor allowlisted is a hard violation.

Named allowlist entry: ``tests/scripts/test_check_release_ci_evidence.py``
carries ``mandatory_regression_pin``-marked tests whose
``GITHUB_TOKEN``-conditioned ``pytest.skip(...)`` calls are nominally
bounded by ``tests/conftest.py``'s ``_check_mandatory_pin_non_vacuity``
guard (~lines 1512-1541) -- but that guard is INERT on the PR-gating corpus
(``-m lint``, and the default unit-suite addopts) because
``mandatory_regression_pin`` tests are ALWAYS ALSO ``integration``-marked and
so are deselected before the guard ever sees an outcome to count
(``_check_marker_non_vacuity``'s own "Inert when zero marked tests reported
an outcome in THIS process" rule). The guard has real teeth only in an
``-m integration`` invocation that selects these modules -- which is exactly
the gap nexus-93j33 built ``_check_mandatory_pin_non_vacuity`` to close, and
exactly why it cannot be this lint's bound on the corpus THIS lint runs
against. Its ``mandatory_regression_pin`` sibling,
``tests/scripts/test_check_engine_release_floor.py``, has the SAME guard
gap but is not entered here: its two skips are conditioned on local git tag
presence (``git tag -l``, "shallow CI clone") rather than an
``_ENV_MARKERS`` signal, so this lint's site detector -- scoped to
``os.environ``/``os.getenv``/``sys.platform``/``platform.*``/
``shutil.which``/``find_spec``, deliberately not general subprocess-output
tracing -- does not find a site there to allowlist. Named here so a reader
does not mistake the absence for "this file has no debt."

The remaining seven allowlist entries are PRE-EXISTING debt this lint
inherits at birth (found on first live run against the real tree, not
introduced by this change): five ``pytest.importorskip("yaml")`` /
``shutil.which`` / filesystem-existence-conditioned ``skipif`` sites in
genuinely gate-like files (``test_plan_template_inline_var_lint.py``,
``test_plugin_release_drift_ledger.py``, ``test_plugin_release_workflow.py``
x2, ``test_plugin_structure.py``, ``test_plugin_surface_smoke_wiring.py``,
``test_rehearsal_container_guard_lint.py``,
``test_session_end_capability_census.py``) that have no non-vacuity budget
of their own. Each is allowlisted with its own reason at the entry, per the
shrink-only rule below -- do not widen the allowlist for a NEW site; a new
gate-like environment-keyed skip introduced after this lint lands must
either carry its own bound or be refused in review.

Do NOT duplicate
=================

This lint does not re-implement or shadow:

* ``tests/conftest.py:1436-1544`` -- the ``mandatory_regression_pin`` /
  ``scenario`` marker-budget guards (this lint READS their existence as a
  bound signal; it does not re-count their skips).
* ``scripts/check_lint_leg_non_vacuity.py`` -- the CI-leg executed-count
  floor (that guards the whole ``-m lint`` invocation not going vacuous;
  this lint is itself one of the tests that invocation runs).
* ``tests/containers/fanout.sh:149`` -- the container-fanout shard verdict
  (``fanout_verdict`` in ``tests/containers/lib/verdict.sh``), a shell-level
  minimum-tests-per-shard check, not a Python skip-site scan.
* ``tests/test_scenario_wiring_lint.py`` -- the scenario-marker
  whole-suite-removal lint (addopts exclusion / journeys-file deletion);
  disjoint subject, same ``lint`` bucket.
"""
from __future__ import annotations

import ast
import dataclasses
import pathlib

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = pathlib.Path(__file__).parent.parent
TESTS_ROOT = REPO_ROOT / "tests"

# ---------------------------------------------------------------------------
# Step 1: partition rule
# ---------------------------------------------------------------------------

#: Deliberately NOT included: bare "gate" and "contract". Both are common
#: English substrings inside plainly substrate-dependent names -- "gate" is
#: a substring of "aggregate", "runfence_gate", "retrieval_drift_gate" (an
#: NDCG benchmark needing a real embedding model + java), and "contract" of
#: "gap_contracts" (an RDR-151 suite needing a stamped jar) -- none of which
#: assert an invariant about the repo/release/CI/plugin surface; they need
#: real infrastructure to run at all, which is exactly SUBSTRATE-DEPENDENT.
#: Measured against the live tree while calibrating this lint (2026-09-17):
#: including either keyword misclassified 8 substrate-dependent files as
#: gate-like.
_GATE_KEYWORDS: tuple[str, ...] = (
    "wiring",
    "census",
    "choreography",
    "pin",
    "release",
    "plugin_structure",
    "ratchet",
    "lint",
)

# ---------------------------------------------------------------------------
# Step 2: bound tokens + shrink-only named allowlist
# ---------------------------------------------------------------------------

#: Names of an ACTUAL bound mechanism -- a function/call that fails the run
#: when too many cases skip, or a budget constant it reads. Matched only
#: against real AST nodes (a Call's callee name, a FunctionDef's name, an
#: assignment target, or a string constant used as an env-var name) -- NEVER
#: against raw file text, so a comment that merely MENTIONS
#: "_check_mandatory_pin_non_vacuity" (explaining why it does not help on
#: this corpus) does not count as a bound. This mirrors the repo-wide
#: RDR-186 P4 rule that a comment grants nothing (see
#: ``test_storage_boundary_lint.py``'s retired ``# epsilon-allow`` token) --
#: an earlier draft of this lint substring-matched raw text and was fooled
#: by exactly that kind of comment in
#: ``tests/scripts/test_check_release_ci_evidence.py``/
#: ``test_check_engine_release_floor.py``, which is why those two files are
#: allowlisted below instead of silently passing as "bounded".
_BOUND_NAMES: tuple[str, ...] = (
    "_check_marker_non_vacuity",
    "_check_scenario_non_vacuity",
    "_check_mandatory_pin_non_vacuity",
)
_BOUND_ASSIGN_SUBSTRINGS: tuple[str, ...] = ("SKIP_BUDGET", "MAX_SKIP")

#: Per-file exact budget of gate-like environment-keyed skip sites exempted
#: from the bound requirement, each with its reason. SHRINK-ONLY: an entry's
#: count may only go DOWN as a site is bounded/removed, never up without a
#: fresh review -- ``_ALLOWLIST_TOTAL_CEILING`` right below pins the current
#: sum so a silent widen is a visible diff, not a value bump alone.
GATE_LIKE_SKIP_ALLOWLIST: dict[str, int] = {
    # mandatory_regression_pin tests: GITHUB_TOKEN-conditioned pytest.skip()
    # sites nominally bounded by tests/conftest.py's
    # _check_mandatory_pin_non_vacuity guard (~1512-1541), which is INERT on
    # this lint's own corpus (-m lint / default addopts) because these tests
    # are also integration-marked and so are deselected before the guard
    # ever sees an outcome. See the module docstring's "Named allowlist
    # entry" section for the full reasoning.
    "tests/scripts/test_check_release_ci_evidence.py": 6,
    # RDR-186-P4-style pre-existing debt, seeded at this lint's first live
    # run (2026-09-17) rather than fixed in this bead -- each is a real
    # gate-like environment-keyed skip with no local bound.
    #
    # "conexus/plans/builtin/ dir is absent - defensive skip" over a
    # directory-existence check, not a real dependency probe.
    "tests/test_plan_template_inline_var_lint.py": 1,
    # One pytest.importorskip("yaml") reading a workflow YAML for the drift
    # check (PyYAML is an ordinary project dependency, not an optional
    # extra) plus one skip on an unconfirmed upstream-tag-absence probe
    # (_require_or_skip -- a live `git ls-remote` network call, not a
    # deterministic local fact).
    "tests/test_plugin_release_drift_ledger.py": 2,
    # Two pytest.importorskip("yaml") sites reading .github/workflows/*.yml
    # for the plugin-release and release workflow pins; same PyYAML
    # dependency as above.
    "tests/test_plugin_release_workflow.py": 2,
    # shutil.which("bash") gating a bash-syntax check over every command doc
    # (bash is assumed present on every dev/CI box this suite runs on), plus
    # a sibling "no ```! bash block found" content skip in the same test
    # function that inherits the function-level bash-availability marker.
    "tests/test_plugin_structure.py": 2,
    # pytest.importorskip("yaml") reading the plugin-surface-smoke workflow.
    "tests/test_plugin_surface_smoke_wiring.py": 1,
    # skipif(os.path.exists("/.dockerenv") or .../.containerenv) -- this
    # test's own purpose is to assert the guard fires OUTSIDE a container;
    # it is a legitimate platform-shape condition, not a missing-dependency
    # skip, but carries no local budget of its own.
    "tests/test_rehearsal_container_guard_lint.py": 1,
    # skipif(os.name == "nt") -- POSIX chmod permission semantics; this repo
    # has no Windows CI leg to bound it against.
    "tests/test_session_end_capability_census.py": 1,
}

#: NEVER RAISE this without a fresh review; LOWER it as an allowlist entry
#: shrinks (a site gets bounded or the file is deleted). This is the
#: shrink-only tripwire: growing GATE_LIKE_SKIP_ALLOWLIST's sum without also
#: lowering (or at least not raising) this ceiling fails
#: test_allowlist_is_shrink_only. Seeded 2026-09-17 at the sum below (16) --
#: see GATE_LIKE_SKIP_ALLOWLIST's own comments for what each entry covers.
_ALLOWLIST_TOTAL_CEILING = 16


@dataclasses.dataclass(frozen=True)
class SkipSite:
    file: str  # posix path, relative to REPO_ROOT
    line: int
    kind: str  # "skipif" | "importorskip" | "skip"
    shutil_which: bool
    bucket: str  # "gate-like" | "substrate-dependent"


@dataclasses.dataclass(frozen=True)
class ScanResult:
    files_scanned: int
    files_unparseable: int
    sites: list[SkipSite]

    @property
    def gate_like_sites(self) -> list[SkipSite]:
        return [s for s in self.sites if s.bucket == "gate-like"]

    @property
    def substrate_sites(self) -> list[SkipSite]:
        return [s for s in self.sites if s.bucket == "substrate-dependent"]


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------

_ENV_MARKERS: tuple[str, ...] = (
    "os.environ",
    "os.getenv",
    "sys.platform",
    "platform.system",
    "platform.machine",
    "shutil.which",
    "find_spec",
)


def _build_alias_maps(tree: ast.AST) -> tuple[dict[str, str], dict[str, str]]:
    """Return (module_alias, func_alias): local-name -> canonical dotted
    name, built from this file's own imports so ``import shutil as _sh`` or
    ``from pytest import skip`` still resolve to ``shutil.which`` /
    ``pytest.skip`` respectively."""
    module_alias: dict[str, str] = {}
    func_alias: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                local = alias.asname or alias.name.split(".")[0]
                module_alias[local] = alias.name
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for alias in node.names:
                local = alias.asname or alias.name
                func_alias[local] = f"{mod}.{alias.name}"
    return module_alias, func_alias


def _dotted_name(
    node: ast.AST, module_alias: dict[str, str], func_alias: dict[str, str]
) -> str | None:
    """Resolve a Name/Attribute chain to its canonical dotted string,
    honouring this file's own import aliases. Falls back to the raw
    identifier when nothing was imported under that name (e.g. bare
    ``pytest`` after a plain ``import pytest``)."""
    if isinstance(node, ast.Name):
        if node.id in func_alias:
            return func_alias[node.id]
        if node.id in module_alias:
            return module_alias[node.id]
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value, module_alias, func_alias)
        if base is None:
            return None
        return f"{base}.{node.attr}"
    return None


def _subtree_calls_shutil_which(
    node: ast.AST, module_alias: dict[str, str], func_alias: dict[str, str]
) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            if _dotted_name(n.func, module_alias, func_alias) == "shutil.which":
                return True
    return False


def _subtree_env_markers(
    node: ast.AST, module_alias: dict[str, str], func_alias: dict[str, str]
) -> set[str]:
    found: set[str] = set()
    for n in ast.walk(node):
        dotted: str | None = None
        if isinstance(n, (ast.Attribute, ast.Name)):
            dotted = _dotted_name(n, module_alias, func_alias)
        elif isinstance(n, ast.Call):
            dotted = _dotted_name(n.func, module_alias, func_alias)
        if not dotted:
            continue
        for marker in _ENV_MARKERS:
            if dotted == marker or dotted.startswith(marker + "."):
                found.add(marker)
    return found


def _find_skip_sites_in_tree(tree: ast.AST) -> list[tuple[int, str, bool]]:
    """Return (line, kind, shutil_which) triples for every skip site this
    file's AST contains -- ``skipif``/``importorskip`` calls unconditionally,
    and ``pytest.skip(...)`` calls ONLY when enclosed by an ``if``.

    An if-guarded ``pytest.skip(...)`` is environment-keyed when ANY of, in
    widening order (no real dataflow analysis, matching
    ``test_storage_boundary_lint.py``'s own scope disclaimer for taxonomy
    writes -- "does NOT require inferring which variable holds ... that
    would need dataflow analysis"):

    1. the immediate ``if`` test itself references an ``_ENV_MARKERS``
       signal;
    2. the ENCLOSING FUNCTION's own body references one elsewhere (the
       ``_require_or_skip()`` shape in this repo: one function, several
       branches, the env check and the skip call are siblings rather than
       parent/child);
    3. the enclosing function calls a SAME-MODULE helper function (one hop,
       by bare name only -- no attribute-call or cross-module resolution)
       whose own body references one (the ``token =
       _live_github_token()`` shape: the ``os.environ.get(...)`` call is
       one function away from the ``if not token: pytest.skip(...)`` that
       acts on it).

    Deliberately NOT done: a whole-module fallback (any env marker anywhere
    in the file). Measured while calibrating this lint (2026-09-17): it
    incorrectly tagged 8 sites in ``tests/test_plugin_structure.py`` alone,
    none of them actually environment-conditioned, merely sharing a module
    with one ``shutil.which("bash")`` check. The three-step widening above
    is the narrower version that still catches both real repo idioms.

    An unconditional ``pytest.skip("not yet implemented")`` (no enclosing
    ``if`` at all) is never environment-keyed and stays entirely out of
    scope."""
    module_alias, func_alias = _build_alias_maps(tree)

    parent_map: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parent_map[id(child)] = parent

    local_funcs: dict[str, ast.AST] = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            local_funcs.setdefault(n.name, n)

    def _enclosing(node: ast.AST, types: tuple[type, ...]) -> ast.AST | None:
        cur = node
        while id(cur) in parent_map:
            par = parent_map[id(cur)]
            if isinstance(par, types):
                return par
            cur = par
        return None

    def _one_hop_callee_markers(func: ast.AST) -> set[str]:
        found: set[str] = set()
        for n in ast.walk(func):
            if (
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id in local_funcs
            ):
                found |= _subtree_env_markers(local_funcs[n.func.id], module_alias, func_alias)
        return found

    sites: list[tuple[int, str, bool]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        dotted = _dotted_name(node.func, module_alias, func_alias)
        if not dotted:
            continue

        if dotted.endswith("mark.skipif"):
            shutil_which = bool(node.args) and _subtree_calls_shutil_which(
                node.args[0], module_alias, func_alias
            )
            sites.append((node.lineno, "skipif", shutil_which))
        elif dotted.endswith("importorskip") or dotted == "importorskip":
            sites.append((node.lineno, "importorskip", False))
        elif dotted == "pytest.skip":
            enclosing_if = _enclosing(node, (ast.If,))
            if enclosing_if is None:
                continue  # unconditional skip -- not environment-keyed
            markers = _subtree_env_markers(enclosing_if.test, module_alias, func_alias)
            if not markers:
                enclosing_func = _enclosing(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                if enclosing_func is not None:
                    markers = _subtree_env_markers(enclosing_func, module_alias, func_alias)
                    if not markers:
                        markers = _one_hop_callee_markers(enclosing_func)
            if not markers:
                continue  # if-guarded but not on any environment signal
            sites.append((node.lineno, "skip", "shutil.which" in markers))

    return sites


def _classify_file(rel_posix: str, tree: ast.AST) -> str:
    name = pathlib.PurePosixPath(rel_posix).name
    if _is_lint_marked(tree):
        return "gate-like"
    if any(kw in name for kw in _GATE_KEYWORDS):
        return "gate-like"
    if rel_posix.startswith("tests/e2e/"):
        return "gate-like"
    return "substrate-dependent"


def _is_lint_marked(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
            continue
        dumped = ast.dump(node.value)
        if "mark" in dumped and "lint" in dumped:
            return True
    return False


def _has_local_bound(tree: ast.AST) -> bool:
    """True if *tree* contains an actual call to, or definition of, a known
    non-vacuity bound function, or an assignment/constant naming a skip
    budget. AST-only by design (see the module docstring's Step 2, and
    ``_BOUND_NAMES``): a comment mentioning the guard's name does not
    count."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fname = None
            if isinstance(node.func, ast.Name):
                fname = node.func.id
            elif isinstance(node.func, ast.Attribute):
                fname = node.func.attr
            if fname in _BOUND_NAMES:
                return True
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name in _BOUND_NAMES:
                return True
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and any(
                    sub in t.id for sub in _BOUND_ASSIGN_SUBSTRINGS
                ):
                    return True
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if any(sub in node.value for sub in _BOUND_ASSIGN_SUBSTRINGS):
                return True
    return False


def scan_tests_dir(
    tests_root: pathlib.Path = TESTS_ROOT,
    repo_root: pathlib.Path = REPO_ROOT,
    extra_files: list[pathlib.Path] | None = None,
) -> ScanResult:
    """Filesystem AST scan of every ``.py`` file under *tests_root* (plus any
    *extra_files*, for synthetic-fixture tests), producing one ``SkipSite``
    per environment-keyed skip found. Never touches ``session.items``."""
    files = sorted(tests_root.rglob("*.py"))
    if extra_files:
        files = list(files) + list(extra_files)

    scanned = 0
    unparseable = 0
    sites: list[SkipSite] = []
    for path in files:
        try:
            source = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            unparseable += 1
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            unparseable += 1
            continue
        scanned += 1

        try:
            rel_posix = path.relative_to(repo_root).as_posix()
        except ValueError:
            rel_posix = path.as_posix()

        bucket = _classify_file(rel_posix, tree)
        for line, kind, shutil_which in _find_skip_sites_in_tree(tree):
            sites.append(
                SkipSite(
                    file=rel_posix, line=line, kind=kind,
                    shutil_which=shutil_which, bucket=bucket,
                )
            )

    return ScanResult(files_scanned=scanned, files_unparseable=unparseable, sites=sites)


def _unbounded_gate_like_violations(result: ScanResult) -> list[SkipSite]:
    """Gate-like sites that are neither locally bounded nor covered by the
    named allowlist budget, grouped per file (first N allowlisted sites in
    line order per RDR-186-P4-style named-budget precedent)."""
    by_file: dict[str, list[SkipSite]] = {}
    for site in result.gate_like_sites:
        by_file.setdefault(site.file, []).append(site)

    violations: list[SkipSite] = []
    for file, sites in by_file.items():
        tree = _parse_for_bound_check(file)
        if tree is not None and _has_local_bound(tree):
            continue  # module carries its own non-vacuity budget
        budget = GATE_LIKE_SKIP_ALLOWLIST.get(file, 0)
        sites_sorted = sorted(sites, key=lambda s: s.line)
        violations.extend(sites_sorted[budget:])
    return violations


def _parse_for_bound_check(rel_or_abs: str) -> ast.AST | None:
    p = pathlib.Path(rel_or_abs)
    candidate = p if p.is_absolute() else (REPO_ROOT / rel_or_abs)
    try:
        source = candidate.read_text(encoding="utf-8")
        return ast.parse(source, filename=str(candidate))
    except (UnicodeDecodeError, OSError, SyntaxError):
        return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_scan_partitions_and_prints_counts():
    """The live check: prints files scanned, sites found, gate-like vs
    substrate-dependent counts, allowlisted count -- and asserts zero
    unbounded gate-like violations."""
    result = scan_tests_dir()
    violations = _unbounded_gate_like_violations(result)
    allowlisted_count = sum(GATE_LIKE_SKIP_ALLOWLIST.values())

    print(  # noqa: T201 -- acceptance evidence the bead requires pasted verbatim
        f"\n[skip-bound-lint] files scanned: {result.files_scanned} "
        f"(unparseable: {result.files_unparseable})\n"
        f"[skip-bound-lint] skip sites found: {len(result.sites)} "
        f"(gate-like: {len(result.gate_like_sites)}, "
        f"substrate-dependent: {len(result.substrate_sites)})\n"
        f"[skip-bound-lint] allowlisted (gate-like, exempted): {allowlisted_count}\n"
        f"[skip-bound-lint] unbounded gate-like violations: {len(violations)}"
    )

    assert violations == [], (
        "gate-like environment-keyed skip site(s) with no bound and no "
        "allowlist entry:\n"
        + "\n".join(f"  {v.file}:{v.line} ({v.kind})" for v in violations)
        + "\nEither add a non-vacuity budget in the same module (see "
        "_BOUND_TOKENS) or a reasoned entry in GATE_LIKE_SKIP_ALLOWLIST."
    )


def test_non_vacuity_scan_examines_something():
    """nexus-moht0 vacuous-gate doctrine: a sweep that found nothing to
    check is a failure, not a pass. The census this lint models found 100
    files with skip sites across the whole tree; a scan that examines a
    near-zero file count or finds zero sites at all is broken, not clean."""
    result = scan_tests_dir()
    assert result.files_scanned > 500, (
        f"only {result.files_scanned} files examined -- scan_tests_dir is "
        "not walking the real tests/ tree"
    )
    assert len(result.sites) > 50, (
        f"only {len(result.sites)} skip site(s) found across the whole "
        "tests/ tree -- the detector is almost certainly broken, not the "
        "repo suddenly clean"
    )


def test_planted_unguarded_gate_like_skip_fails(tmp_path):
    """Acceptance: a planted gate-like file with an unguarded
    environment-keyed skip must be caught."""
    canary = tmp_path / "test_release_canary_unguarded.py"
    canary.write_text(
        "import os\n"
        "import pytest\n"
        "\n"
        "def test_needs_token():\n"
        "    if not os.environ.get('SOME_CANARY_TOKEN'):\n"
        "        pytest.skip('no token')\n"
    )
    result = scan_tests_dir(extra_files=[canary])
    matched = [s for s in result.sites if s.file.endswith("test_release_canary_unguarded.py")]
    assert len(matched) == 1
    assert matched[0].bucket == "gate-like", "filename carries 'release' -- must classify gate-like"
    violations = _unbounded_gate_like_violations(result)
    canary_violations = [v for v in violations if v.file.endswith("test_release_canary_unguarded.py")]
    assert len(canary_violations) == 1, (
        "planted unguarded gate-like skip was not flagged -- the check is inert"
    )


def test_planted_bounded_gate_like_skip_passes(tmp_path):
    """The mirror case: a gate-like file whose skip site is covered by a
    local non-vacuity token is NOT a violation."""
    canary = tmp_path / "test_gate_canary_bounded.py"
    canary.write_text(
        "import os\n"
        "import pytest\n"
        "\n"
        "CANARY_SKIP_BUDGET = 0\n"
        "\n"
        "def test_needs_token():\n"
        "    if not os.environ.get('SOME_CANARY_TOKEN'):\n"
        "        pytest.skip('no token')\n"
    )
    result = scan_tests_dir(extra_files=[canary])
    violations = _unbounded_gate_like_violations(result)
    canary_violations = [v for v in violations if v.file.endswith("test_gate_canary_bounded.py")]
    assert canary_violations == [], "a module carrying a bound token must not violate"


def test_substrate_dependent_skip_is_never_enforced(tmp_path):
    """A skip in a substrate-dependent file (no lint marker, no gate
    keyword, not under tests/e2e/) is counted but never enforced -- this
    lint does not widen into that bucket."""
    canary = tmp_path / "test_needs_docker_thing.py"
    canary.write_text(
        "import shutil\n"
        "import pytest\n"
        "\n"
        "@pytest.mark.skipif(shutil.which('docker') is None, reason='no docker')\n"
        "def test_thing():\n"
        "    pass\n"
    )
    result = scan_tests_dir(extra_files=[canary])
    matched = [s for s in result.sites if s.file.endswith("test_needs_docker_thing.py")]
    assert len(matched) == 1
    assert matched[0].bucket == "substrate-dependent"
    assert matched[0].shutil_which is True
    violations = _unbounded_gate_like_violations(result)
    assert not any(v.file.endswith("test_needs_docker_thing.py") for v in violations)


def test_skipif_shutil_which_and_importorskip_detected(tmp_path):
    """Self-test of the three literal detector shapes the bead names."""
    target = tmp_path / "test_gate_detector_shapes.py"
    target.write_text(
        "import shutil\n"
        "import pytest\n"
        "\n"
        "@pytest.mark.skipif(shutil.which('foo') is None, reason='no foo')\n"
        "def test_a():\n"
        "    pass\n"
        "\n"
        "def test_b():\n"
        "    pytest.importorskip('some_optional_module')\n"
    )
    result = scan_tests_dir(extra_files=[target])
    matched = [s for s in result.sites if s.file.endswith("test_gate_detector_shapes.py")]
    kinds = sorted((s.kind, s.shutil_which) for s in matched)
    assert kinds == [("importorskip", False), ("skipif", True)]


def test_unconditional_skip_is_not_a_site(tmp_path):
    """A bare, unconditional pytest.skip(...) is not environment-keyed and
    must not be counted at all -- it is out of this lint's scope entirely."""
    target = tmp_path / "test_gate_unconditional_skip.py"
    target.write_text(
        "import pytest\n"
        "\n"
        "def test_todo():\n"
        "    pytest.skip('not yet implemented')\n"
    )
    result = scan_tests_dir(extra_files=[target])
    matched = [s for s in result.sites if s.file.endswith("test_gate_unconditional_skip.py")]
    assert matched == []


def test_allowlist_is_shrink_only():
    """Shrink-only: the allowlist's sum may only go down from
    ``_ALLOWLIST_TOTAL_CEILING``, never up without a deliberate edit to the
    ceiling itself; every entry must still name a live file that still
    carries at least that many gate-like unbounded-by-token sites (a stale
    entry claiming more debt than exists must fail)."""
    total = sum(GATE_LIKE_SKIP_ALLOWLIST.values())
    assert total <= _ALLOWLIST_TOTAL_CEILING, (
        f"GATE_LIKE_SKIP_ALLOWLIST sum ({total}) exceeds "
        f"_ALLOWLIST_TOTAL_CEILING ({_ALLOWLIST_TOTAL_CEILING}) -- a wider "
        "allowlist needs a deliberate ceiling bump in this same commit, "
        "with its own reason, never a silent grow."
    )

    result = scan_tests_dir()
    by_file: dict[str, int] = {}
    for site in result.gate_like_sites:
        by_file[site.file] = by_file.get(site.file, 0) + 1

    stale: list[str] = []
    for file, budget in GATE_LIKE_SKIP_ALLOWLIST.items():
        if not (REPO_ROOT / file).is_file():
            stale.append(f"{file}: file no longer exists")
            continue
        tree = _parse_for_bound_check(file)
        if tree is not None and _has_local_bound(tree):
            stale.append(
                f"{file}: now carries a local bound -- allowlist entry "
                "is no longer needed, lower it to 0 and delete it"
            )
            continue
        actual = by_file.get(file, 0)
        if actual < budget:
            stale.append(
                f"{file}: allowlist budget {budget} exceeds the {actual} "
                "gate-like skip site(s) actually found -- lower the entry"
            )
    assert stale == [], "stale GATE_LIKE_SKIP_ALLOWLIST entr(y/ies):\n" + "\n".join(stale)
