# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Every runtime dependency carries an upper bound (nexus-l2ku5 class, gap 3
of T2 ``nexus/release-protocol-gap-audit-2026-08-14`` [22511]).

THE BUG CLASS this guards. ``mcp>=1.0`` (unbounded) resolved fresh to
``mcp==2.0.0`` on 2026-07-28 -- a MAJOR release that removed
``mcp.server.fastmcp`` -- and every fresh install got it, since ``uv tool
install`` re-resolves the environment at install time and does not honour
this repo's committed ``uv.lock``. Both MCP servers were dead for 4 days
(nexus-l2ku5) before anyone noticed; no dev-venv gate could see it, because a
dev checkout's own ``uv.lock`` pins the working resolution and never
re-resolves against PyPI on its own.

``mcp`` was fixed with a targeted ``<2`` cap plus an MVV assertion
(``tests/e2e/fresh-install-mvv.sh:305-312``). That closed ONE door. The
2026-08-14 audit found 19 further runtime dependencies with the identical
unbounded-floor shape (two, ``uvicorn`` and ``mineru``, had no version
constraint at all) -- the same failure class, just not yet triggered.

THIS LINT is the structural half of the fix: every ``[project.dependencies]``
entry must carry an upper-bound specifier (``<`` or ``<=``), or appear on
``_EXEMPTIONS`` below with a reason. It does not re-resolve against PyPI --
that is the separate weekly drift watch
(``scripts/check_dependency_drift.py``); this lint only proves the *shape* of
the constraint is closed, cheaply and offline, on every push.
"""
from __future__ import annotations

import pathlib
import tomllib

import pytest
from packaging.requirements import Requirement

pytestmark = pytest.mark.lint

_REPO_ROOT = pathlib.Path(__file__).parent.parent
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

#: Package names permitted to ship without an upper-bound specifier, each
#: with a reason string. Empty by design (nexus-l2ku5 gap-3 closure, 2026-08-14)
#: -- every runtime dependency was bounded rather than exempted. A future
#: addition here must carry a genuine reason (e.g. a package with no stable
#: release cadence to bound against), not "didn't get to it yet".
_EXEMPTIONS: dict[str, str] = {}

#: Non-vacuity floor: proves this lint is actually parsing the real
#: dependency list, not silently iterating zero entries after a refactor
#: moved `[project.dependencies]` or renamed the table. 24 runtime deps
#: existed at gap-3 closure (2026-08-14); this floor tolerates future
#: additions/removals without needing a bump for every dependency edit.
_MIN_EXPECTED_DEPENDENCY_COUNT = 20


def _load_runtime_dependencies() -> list[str]:
    with _PYPROJECT.open("rb") as f:
        data = tomllib.load(f)
    return data["project"]["dependencies"]


def _has_upper_bound(requirement_string: str) -> bool:
    req = Requirement(requirement_string)
    return any(spec.operator in ("<", "<=") for spec in req.specifier)


def test_dependency_list_is_non_vacuous() -> None:
    """A parser that silently sees zero (or a handful of) dependencies would
    make every other test in this module pass vacuously. Pin a floor so a
    broken parse -- or a `[project.dependencies]` table move -- fails loud
    instead of quietly checking nothing."""
    deps = _load_runtime_dependencies()
    assert len(deps) >= _MIN_EXPECTED_DEPENDENCY_COUNT, (
        f"only parsed {len(deps)} runtime dependencies from {_PYPROJECT}, "
        f"expected at least {_MIN_EXPECTED_DEPENDENCY_COUNT} -- the parser "
        "may be reading the wrong table, or the dependency list genuinely "
        "shrank and this floor needs a deliberate lowering."
    )


def test_every_runtime_dependency_is_bounded_or_exempt() -> None:
    """The actual gate: every ``[project.dependencies]`` entry either carries
    an upper-bound specifier, or is named in ``_EXEMPTIONS`` with a reason.
    A dependency failing both is exactly the nexus-l2ku5 shape -- an
    unbounded floor one major release away from resolving something breaking
    into every fresh install."""
    deps = _load_runtime_dependencies()
    unbounded_unexempt = []
    for dep in deps:
        req = Requirement(dep)
        if _has_upper_bound(dep):
            continue
        reason = _EXEMPTIONS.get(req.name)
        if reason and reason.strip():
            continue
        unbounded_unexempt.append(dep)

    assert not unbounded_unexempt, (
        "runtime dependencies with no upper bound and no documented "
        f"exemption: {unbounded_unexempt} -- add a next-major cap "
        "(`pkg>=X.Y,<NEXT_MAJOR`) in pyproject.toml, or add a reasoned "
        "entry to _EXEMPTIONS in this file (see nexus-l2ku5)."
    )


def test_exemptions_carry_real_reasons() -> None:
    """An exemption with an empty or whitespace-only reason is a silent
    escape hatch -- guard against that shape even though _EXEMPTIONS is
    empty today, since the exemption list is the sanctioned way to loosen
    this lint and its own discipline must hold."""
    for name, reason in _EXEMPTIONS.items():
        assert reason.strip(), f"exemption for {name!r} has no reason string"


# ---------------------------------------------------------------------------
# Kill control: a synthetic unbounded, unexempt entry must be caught by the
# same logic the production test above uses. Proves the detector actually
# detects, rather than vacuously passing because pyproject.toml today
# happens to be clean.
# ---------------------------------------------------------------------------


def test_kill_control_unbounded_synthetic_entry_is_detected() -> None:
    synthetic_deps = ["click>=8.1,<9", "some-unbounded-package>=1.0"]
    flagged = [
        dep
        for dep in synthetic_deps
        if not _has_upper_bound(dep)
        and not _EXEMPTIONS.get(Requirement(dep).name, "").strip()
    ]
    assert flagged == ["some-unbounded-package>=1.0"], (
        "kill-control synthetic unbounded entry was not flagged -- the "
        "bound-detection logic is broken, which means the production test "
        "above would pass vacuously on a real regression too"
    )


def test_kill_control_fully_unconstrained_entry_is_detected() -> None:
    """The two originally-fully-unconstrained deps (uvicorn, mineru) had no
    specifier at all, not merely a missing upper bound -- confirm that shape
    is caught too, not just the `>=X` case."""
    assert not _has_upper_bound("some-package[extra]")
    assert not _has_upper_bound("some-package")


@pytest.mark.parametrize(
    "requirement_string,expected",
    [
        ("click>=8.1,<9", True),
        ("mcp>=1.0,<2", True),
        ("llama-index-core>=0.12.7,<0.15", True),
        ("sigstore>=3.0,<5", True),
        ("uuid7-standard>=1.1.0,<2; python_version<'3.14'", True),
        ("click>=8.1", False),
        ("uvicorn[standard]", False),
        ("mineru[pipeline]", False),
        ("some-package==1.2.3", False),
        ("some-package<=2.0", True),
    ],
)
def test_has_upper_bound_classification(requirement_string: str, expected: bool) -> None:
    assert _has_upper_bound(requirement_string) is expected


def test_live_pyproject_dependencies_all_parse() -> None:
    """Every entry in the live pyproject.toml must be a valid PEP 508
    requirement string -- a malformed entry would silently break `uv sync`
    and every other test in this module without this check."""
    deps = _load_runtime_dependencies()
    for dep in deps:
        Requirement(dep)  # raises InvalidRequirement on malformed strings


# ── Shape-sensitive dependencies confine to the locked minor ──────────────────
#
# nexus-jd8fi drift (2026-09-03). An upper bound at the next MAJOR is the right
# shape for most dependencies, but for a package whose OUTPUT the fixtures lock
# (MinerU markdown, the bloom-filter text length, the virgo table) a minor bump
# is a behaviour change nothing gated: ``mineru>=3.1.11,<4`` admitted 3.4.5,
# every fresh install got it from 2026-08-14 on, and uv.lock plus every test
# stayed on 3.1.11. For the packages below the pyproject specifier must admit
# the locked version and refuse the next minor, so a fresh install can only
# land where the gates ran. Bumping is deliberate: raise the lock and the cap
# together, on a green ``-m slow`` MinerU run and shakedown.
_SHAPE_SENSITIVE: dict[str, str] = {
    "mineru": "MinerU markdown shape locks tests/test_pdf_subsystem.py's slow fixtures and the visual-marker regexes",
    "docling-slim": "docling (declared as docling-slim[convert-core,format-pdf,models-local], nexus-jpsn1) is the auto-mode formula screen and the fallback extractor; its table export and markdown shape are gated by the MVV and shakedown",
}

_LOCK = _REPO_ROOT / "uv.lock"


def _locked_version(name: str) -> str:
    with _LOCK.open("rb") as f:
        lock = tomllib.load(f)
    for pkg in lock["package"]:
        if pkg["name"] == name:
            return pkg["version"]
    raise AssertionError(f"{name} is not in uv.lock")


@pytest.mark.parametrize("name", sorted(_SHAPE_SENSITIVE))
def test_shape_sensitive_dependency_confines_to_the_locked_minor(name: str) -> None:
    from packaging.version import Version

    specs = {Requirement(d).name: Requirement(d).specifier for d in _load_runtime_dependencies()}
    assert name in specs, f"{name} is in _SHAPE_SENSITIVE but not a runtime dependency"
    locked = Version(_locked_version(name))
    spec = specs[name]
    assert locked in spec, f"{name}: uv.lock has {locked}, outside pyproject specifier {spec}"
    # The next PATCH, not only the next minor: mineru 3.1.15 was gated on
    # 2026-09-03 and refused (spurious inline math on prose, a split word), so
    # a patch is a behaviour change here too. The cap is the exact version.
    next_patch = Version(f"{locked.major}.{locked.minor}.{locked.micro + 1}")
    assert next_patch not in spec, (
        f"{name}: pyproject specifier {spec} admits {next_patch} while uv.lock and every "
        f"gate run {locked}. A fresh install would resolve past what was tested "
        f"({_SHAPE_SENSITIVE[name]}). Cap at <{next_patch}, or bump lock and cap together "
        "on a green slow-gate run."
    )


# nexus-mt1tj (Sam, 2026-09-04): CPU-only torch on Linux by default. The lock
# routes torch/torchvision for Linux to the PyTorch CPU index, which is what
# drops the 15 nvidia-* dists and triton from every Linux sync. A future
# `uv lock --upgrade` that loses the [tool.uv.sources] routing would bring the
# CUDA tree back silently; this pins the lock's shape.


def _lock_packages() -> list[dict]:
    with _LOCK.open("rb") as f:
        return tomllib.load(f)["package"]


def test_lock_carries_no_cuda_tree() -> None:
    names = sorted(p["name"] for p in _lock_packages())
    cuda = [n for n in names if n.startswith("nvidia-") or n == "triton"]
    assert not cuda, f"CUDA payload is back in uv.lock: {cuda} (nexus-mt1tj)"


def test_linux_torch_resolves_from_the_cpu_index() -> None:
    torches = [p for p in _lock_packages() if p["name"] in ("torch", "torchvision")]
    assert torches, "torch is not in uv.lock at all"
    cpu = [p for p in torches if "download.pytorch.org/whl/cpu" in p.get("source", {}).get("registry", "")]
    assert cpu, "no torch/torchvision entry resolves from the PyTorch CPU index (nexus-mt1tj)"
    for p in cpu:
        assert p["version"].endswith("+cpu") or p["name"] == "torchvision", (p["name"], p["version"])
