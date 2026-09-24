# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.

"""Ratchet: capture+timeout ``subprocess`` calls route through ``run_bounded``.

THE SHAPE THIS WATCHES::

    subprocess.run(argv, capture_output=True, timeout=5)

:mod:`nexus.bounded_subprocess` carries the full mechanism, including the
correction that matters for reading this lint's severity: the unbounded
post-kill drain is Windows-only in CPython 3.12, so on POSIX this shape is
an ORPHAN LEAK (the timed-out call reaps its direct child and leaves the
descendants running) while on Windows it is a genuine unbounded HANG. Both
are worth removing; only one hangs a user's session.

WHY A RATCHET AND NOT A FLAG DAY, and what happened to it. The census
that produced ``run_bounded`` found 81 of these in ``src/nexus``, and
nexus-t10nc's own judgement — which Sam accepted — is that converting them
one at a time is what produced them in the first place, while converting
all of them in one change is a large blind diff across daemon, catalog,
indexer and CLI paths. So the count was pinned at 57 and drained.

IT IS NOW EMPTY, and the paragraph above is the design that says it
should not have been. Read this as a DISCLOSURE, not as a justification.

Sam named nexus-t10nc as the next task. That was read as "drain it
deliberately" and all 57 were converted in one sitting, in subsystem
batches, each run against the tests of the modules it touched. SAM DID
NOT CONFIRM THAT READING BEFORE THE WORK; the bead carried no comment
either way, and the standing critic was right to say so. The cadence
above was accepted; a dedicated sweep is a change to it, and whether the
change was wanted is Sam's to settle, not this docstring's. If the answer
is no, the remedy is a cadence rule, not a revert -- the conversions
themselves are faithful and reviewed.

What the sweep can claim on its own evidence is narrower than "reviewed
batches" suggests: it landed as three commits of 12, 7 and 26 files,
and the 26-file one spans db, daemon, CLI and tests in a single landing.
That is closer in SHAPE to the blind diff the paragraph above rejected
than the batching makes it sound. What it had that a blind diff does not
is a scoped test run per batch, a full suite, and both standing
reviewers -- and the full suite and the reviewers each caught a real
defect the scoped runs did not (see _CANNOT_IMPORT_NEXUS and
_RUN_ALIASES below).

Two files are exempt rather than converted, in
:data:`_CANNOT_IMPORT_NEXUS`, and the reason is worth reading before
adding a third: the ``_install`` cores run during an install, with the
``nexus`` package absent, so importing ``run_bounded`` from it is the
exact failure their own bootstrap tests exist to refuse. Converting them
passed every scoped run and 725 targeted tests, and broke 27 assertions
across five files in the FULL suite. A gate whose subject is "works when
nexus is not importable" cannot be reached by any selection that runs
with nexus importable.

An empty map is a STRONGER gate than a drained one, not a retired one:
every file is now allowed zero, so any new capture+timeout call anywhere
in scope fails :func:`test_unconverted_subprocess_calls_match_the_ratchet`
by name. Do not delete this file when you notice the map is empty.

"Allowed zero" is a claim about what this file's SCAN can see, and the
scan has two known blind spots, both found only after the map emptied and
there was nothing else left to look at: a call that unpacks ``**kwargs``
(:data:`_KWARGS_FUNNELS`) and a spawner bound to a name
(:data:`_RUN_ALIASES`). Each now has its own sweep. Neither was in the
57.

The ceiling is EXACT EQUALITY, per file, never ``<=``. An inequality
ceiling silently accepts a file that converted one site and added two, and
this repo's ratchets (``test_mode_declarations_are_explicit.py``, RDR-109;
``test_pipefail_early_exit_consumer_lint.py``) are all exact for that
reason. Per FILE rather than per line because line numbers move under
ordinary editing and a line-keyed ratchet reds on a rename.

WHAT THE EMPTY MAP COST, and what pays for it.
:func:`test_unconverted_subprocess_calls_match_the_ratchet` used to prove
the detector worked simply by passing: it reproduced 57 counts across 28
files, which a broken :func:`_capture_with_timeout_calls` could not have
done. At zero, a detector that silently matched NOTHING would pass exactly
as loudly. :func:`test_scan_is_not_vacuous` does not close that — it
proves the scan reached files, not that it recognises the shape inside
one. :func:`test_detector_recognises_the_shapes_it_claims_to` is the
replacement: a positive control over synthetic sources, including the
near-misses that must NOT match.

SCOPE IS NOW EVERY ``.py`` UNDER ``src/nexus``. ``hooks/`` was held out
while nexus-t9klx moved hook entry points between modules, because a
per-file ratchet over a directory being restructured reds on the
restructuring rather than on a defect. t9klx closed, nexus-zptvf
converted the 26 sites there, and the exclusion machinery was DELETED
rather than switched off: a disabled exclusion is a thing that gets
re-enabled by accident, and an exclusion never reports what it skipped,
so nobody would notice.

Those 26 were worth waiting for. They are the sites that hang a user's
SESSION rather than a CLI command they can interrupt, and they hold the
only MEASURED instance of the whole defect class:
``hooks/verification_config.py`` running ``git rev-parse
--git-common-dir`` with a 5.0 s timeout, wired on Stop, sampled still
blocked at 25 s on qwentescence — which is why a Windows session answers
and then sits.

ONE THING DIFFERS IN ``hooks/`` AND A FUTURE CONVERTER WILL TRIP ON IT.
The import is DEFERRED into the spawning function, not placed at module
scope like everywhere else in ``src/nexus``. Hooks fire on every tool
call, and a module-scope
``from nexus.bounded_subprocess import run_bounded`` pulls structlog and
about 231 further modules: measured on ``hooks/verification_config``,
14 ms and 106 modules becomes 62-84 ms and 337. The deferred form pays
that only on the rare path that actually shells out, and the measurement
is in the noqa comment at each site so it is not re-litigated from
memory.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

pytestmark = pytest.mark.lint

REPO_ROOT = pathlib.Path(__file__).parent.parent
SRC_ROOT = REPO_ROOT / "src" / "nexus"

#: Files that CANNOT route through ``run_bounded``, with the reason. This
#: is not a deferral like ``hooks/`` -- these are permanent, and the
#: obligation they carry is to stay small and stay explained.
#:
#: The ``_install`` cores are sourced by census.sh and the installer, which
#: run with NOTHING installed. ``run_bounded`` lives in the ``nexus``
#: package, so importing it is exactly the "simplification" that
#: ``tests/test_install_*_core_is_bootstrap_safe.py`` exists to refuse:
#: it works in every test session, because every test session has nexus
#: importable, and fails only during a real install. nexus-t10nc converted
#: both, and the full suite caught it -- 27 failures across five bootstrap
#: files. The sites are left on the stock call deliberately.
_CANNOT_IMPORT_NEXUS: dict[str, str] = {
    "src/nexus/_install/census_core.py": (
        "bootstrap-safe: runs with nexus absent, so it cannot import "
        "run_bounded. See tests/test_install_census_core_is_bootstrap_safe.py."
    ),
    "src/nexus/_install/layout_core.py": (
        "bootstrap-safe: runs with nexus absent, so it cannot import "
        "run_bounded. See tests/test_install_layout_core_is_bootstrap_safe.py."
    ),
}

#: Exact per-file count of unconverted capture+timeout subprocess calls.
#: Pinned at 57 across 28 files on 2026-09-22; DRAINED TO ZERO on
#: 2026-09-23 (nexus-t10nc). Numbers only ever go DOWN. A file that reaches
#: zero is deleted from this map, and a file absent from it — which is now
#: every file in scope — is allowed zero.
_UNCONVERTED: dict[str, int] = {}


def _in_scope(path: pathlib.Path, root: pathlib.Path = SRC_ROOT) -> bool:
    """Every ``.py`` under *root*. There is no held-out directory any more.

    ``src/nexus/hooks/`` was excluded until nexus-zptvf; the ``root``
    parameter stays because :func:`test_the_census_walk_finds_a_planted_file`
    walks a synthetic tree.
    """
    del root  # only ``__pycache__`` is filtered now
    return "__pycache__" not in path.parts


def _aliases_of_subprocess_run(source: str) -> list[int]:
    """Line numbers where ``subprocess.run``/``check_output`` is bound to a
    NAME instead of being called.

    ``_capture_with_timeout_calls`` matches the call ``subprocess.run(...)``.
    It cannot match ``run(...)`` where ``run`` arrived as
    ``run: Runner = subprocess.run`` -- an injected-spawner default, which
    is a deliberate and reasonable seam, and which reads at the call site as
    a bare name with no module on it.

    ``plugin_lockstep`` did exactly that, at four call sites passing
    capture+timeout, and it was invisible for this lint's whole life. Both
    standing reviewers found it independently, and only once the ratchet had
    drained to zero and there was nothing else left to look at. So the
    binding is what gets watched: an alias is cheap to spot and there are
    very few of them, where chasing every bare ``run(`` call is neither.
    """
    found: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "subprocess"
            and node.attr in {"run", "check_output"}
            and isinstance(node.ctx, ast.Load)
        ):
            continue
        found.append(node.lineno)
    # Drop the ones that ARE calls; those are the other detector's job.
    called = {
        n.func.lineno
        for n in ast.walk(ast.parse(source))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    return sorted(set(found) - called)


def _capture_with_timeout_calls(source: str) -> list[int]:
    """Line numbers of ``subprocess.run``/``check_output`` calls that both
    capture output and pass a timeout."""
    found: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
            and func.attr in {"run", "check_output"}
        ):
            continue
        kwargs = {kw.arg for kw in node.keywords if kw.arg}
        captures = (
            "capture_output" in kwargs
            or "stdout" in kwargs
            or func.attr == "check_output"
        )
        if captures and "timeout" in kwargs:
            found.append(node.lineno)
    return found


def _census(
    root: pathlib.Path = SRC_ROOT,
    rel_to: pathlib.Path = REPO_ROOT,
    detect=_capture_with_timeout_calls,
) -> dict[str, list[int]]:
    """Walk *root* and report every file holding a watched call.

    ``root``/``rel_to``/``detect`` are parameters ONLY so the walk itself
    can be tested against a synthetic tree — see
    :func:`test_the_census_walk_finds_a_planted_file`. Production callers
    pass nothing. Without them the exclusion and reporting logic here was
    reachable by no test at all, which the standing critic pointed out once
    ``_UNCONVERTED`` went empty and the real tree stopped exercising the
    reporting branch.
    """
    out: dict[str, list[int]] = {}
    for path in sorted(root.rglob("*.py")):
        if not _in_scope(path, root):
            continue
        try:
            lines = detect(path.read_text())
        except (
            SyntaxError
        ):  # pragma: no cover - a syntax error is another test's failure
            continue
        if lines:
            out[path.relative_to(rel_to).as_posix()] = lines
    return out


def test_scan_is_not_vacuous() -> None:
    """The sweep reached a real tree.

    A lint whose scan found nothing passes for two very different reasons —
    the defect is gone, or the scan is broken — and this repo has already
    paid for one gate that could not tell them apart (the nexus-moht0
    vacuous-gate doctrine). The floor is deliberately loose: it only has to
    distinguish "scanned the codebase" from "scanned nothing".
    """
    scanned = [p for p in SRC_ROOT.rglob("*.py") if _in_scope(p)]
    assert len(scanned) > 200, (
        f"only {len(scanned)} files in scope under {SRC_ROOT}; the scan root or the "
        "exclusion filter is wrong, so a green result here means nothing"
    )


def test_bootstrap_safe_files_still_cannot_import_nexus() -> None:
    """The exemption stays tied to the fact that earns it.

    A file listed in :data:`_CANNOT_IMPORT_NEXUS` is exempt because it must
    run with the ``nexus`` package absent. If it ever gains a ``nexus``
    import for some other reason, the exemption has outlived its reason and
    the site should convert -- so this fails rather than letting a stale
    entry keep waving the file through.
    """
    for rel, reason in _CANNOT_IMPORT_NEXUS.items():
        path = REPO_ROOT / rel
        assert path.is_file(), f"{rel} is exempt from this lint but does not exist"
        assert "bootstrap" in reason, f"{rel}'s exemption does not state its reason"
        # MODULE SCOPE only, which is the level the exemption is about: a
        # module-scope import runs at import time, when nexus is absent. A
        # guarded deferred import inside a function is a different thing and
        # layout_core legitimately has one (nexus.errors, with a plain-
        # Exception fallback), so walking the whole tree would fail on the
        # very file whose own bootstrap test passes.
        tree = ast.parse(path.read_text())
        imports_nexus = any(
            (isinstance(n, ast.Import) and any(a.name.split(".")[0] == "nexus" for a in n.names))
            or (
                isinstance(n, ast.ImportFrom)
                and n.level == 0
                and (n.module or "").split(".")[0] == "nexus"
            )
            for n in tree.body
        )
        assert not imports_nexus, (
            f"{rel} is exempt from routing through run_bounded because it cannot "
            "import nexus, but it now imports nexus at module scope. Either that "
            "import is the bootstrap bug its own test guards against, or the "
            "exemption is stale and the subprocess call should convert."
        )


def test_unconverted_subprocess_calls_match_the_ratchet() -> None:
    """Exact per-file equality against the pinned census."""
    actual = {
        path: len(lines)
        for path, lines in _census().items()
        if path not in _CANNOT_IMPORT_NEXUS
    }

    new_files = sorted(set(actual) - set(_UNCONVERTED))
    assert not new_files, (
        "these files gained a capture+timeout subprocess call and are not in the "
        f"ratchet: {new_files}. Route it through nexus.bounded_subprocess.run_bounded "
        "rather than adding an entry -- the ratchet only ever counts DOWN."
    )

    regressions = {
        p: (_UNCONVERTED[p], actual[p]) for p in actual if actual[p] > _UNCONVERTED[p]
    }
    assert not regressions, (
        "these files gained capture+timeout subprocess calls (pinned, actual): "
        f"{regressions}. Use nexus.bounded_subprocess.run_bounded."
    )

    improvements = {
        p: (_UNCONVERTED[p], actual.get(p, 0))
        for p in _UNCONVERTED
        if actual.get(p, 0) < _UNCONVERTED[p]
    }
    assert not improvements, (
        "these files have FEWER unconverted calls than the ratchet records "
        f"(pinned, actual): {improvements}. That is good -- lower the numbers in "
        "_UNCONVERTED to match, or delete the entry if it reached zero, so the "
        "ratchet cannot drift back up."
    )


#: Sources the detector must flag, as (label, source, expected line count).
#: Every shape here was a real site drained by nexus-t10nc.
_MUST_MATCH: tuple[tuple[str, str, int], ...] = (
    ("capture_output", "subprocess.run(a, capture_output=True, timeout=5)\n", 1),
    ("stdout pipe", "subprocess.run(a, stdout=subprocess.PIPE, timeout=5)\n", 1),
    ("stdout devnull", "subprocess.run(a, stdout=subprocess.DEVNULL, timeout=5)\n", 1),
    ("check_output", "subprocess.check_output(a, text=True, timeout=5)\n", 1),
    (
        "multiline, kwargs spread over lines",
        "subprocess.run(\n    argv,\n    capture_output=True,\n    text=True,\n    timeout=10,\n)\n",
        1,
    ),
    (
        "two in one file",
        "subprocess.run(a, capture_output=True, timeout=1)\n"
        "subprocess.check_output(b, timeout=2)\n",
        2,
    ),
)

#: Sources the detector must NOT flag. A detector that matched these would
#: make the empty ratchet unmaintainable by failing on correct code.
_MUST_NOT_MATCH: tuple[tuple[str, str], ...] = (
    ("capture without timeout", "subprocess.run(a, capture_output=True)\n"),
    ("timeout without capture", "subprocess.run(a, timeout=5)\n"),
    ("already converted", "run_bounded(a, timeout=5)\n"),
    ("Popen, not run", "subprocess.Popen(a, stdout=subprocess.PIPE)\n"),
    ("a different module's run", "other.run(a, capture_output=True, timeout=5)\n"),
)


def test_detector_recognises_the_shapes_it_claims_to() -> None:
    """Positive control for :func:`_capture_with_timeout_calls`.

    With ``_UNCONVERTED`` drained to ``{}``, a detector that matched
    nothing at all would make every other test in this file pass. Before
    the drain the map itself was the proof — reproducing 57 counts across
    28 files is not something a broken matcher does — and deleting the map
    deleted that proof with it. This restores it without depending on any
    site staying unconverted.
    """
    for label, source, expected in _MUST_MATCH:
        found = _capture_with_timeout_calls(source)
        assert len(found) == expected, (
            f"the detector missed the {label!r} shape: expected {expected} "
            f"call(s), found {found}. Every shape in _MUST_MATCH was a real "
            "site this lint was written to catch, so a miss here means the "
            "empty ratchet is passing vacuously."
        )

    for label, source in _MUST_NOT_MATCH:
        found = _capture_with_timeout_calls(source)
        assert not found, (
            f"the detector flagged {label!r}, which is not the watched shape "
            f"(line {found}). A false positive here fails correct code and is "
            "how a lint gets deleted rather than fixed."
        )


#: ``subprocess.run``/``check_output`` calls whose kwargs are UNPACKED, so
#: a static scan cannot read whether they carry capture+timeout. Each is
#: listed with what was determined by reading it, because the alternative
#: is that the scan skips them in silence. Found by nexus-t10nc AFTER the
#: drain reached zero, which is when a funnel stops being invisible.
#:
#: This map has shrunk to empty twice now, each time by its own guard
#: rather than by anyone remembering: ``db/pg_provision.py`` was listed
#: here as "unbounded, a different defect", nexus-9dkxu then bounded it a
#: few hours later; ``daemon/installer.py`` was listed the same way for
#: its three untimed ``_run_manager`` callers, nexus-k9i56 then bounded
#: those too. Both times the stale-entry assertion in
#: :func:`test_kwargs_funnels_are_named_not_silently_skipped` failed the
#: lint bucket until the entry was deleted. That is the behaviour to
#: preserve if this map is ever refactored -- an exemption that outlives
#: its reason is how a real funnel gets waved through later.
_KWARGS_FUNNELS: dict[str, str] = {}


def test_kwargs_funnels_are_named_not_silently_skipped() -> None:
    """A ``**kwargs`` call is unreadable to the scan, so it must be listed.

    :func:`_capture_with_timeout_calls` reads keywords off the call node.
    A site that assembles its kwargs into a dict and unpacks them -- or
    takes ``**kwargs`` from its own caller -- presents no ``timeout``
    keyword to the AST and is skipped, whatever it does at runtime.

    That is not hypothetical. ``command_context._check_output`` was
    exactly this: it set BOTH ``timeout`` and ``stderr`` via
    ``setdefault`` and then unpacked, so it was unconditionally the
    watched shape and the lint never saw it, while every preamble git, bd,
    gh and nx call in the repo funnelled through it. It was invisible for
    as long as the ratchet was non-empty, because a non-empty ratchet
    looks like work remaining rather than like a gap.

    So every funnel in scope is enumerated with what reading it found. A
    NEW one fails here, which forces the same read rather than allowing
    the silence back.
    """
    found: dict[str, list[int]] = {}
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if not _in_scope(path):
            continue
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover - another test's failure
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"
                and func.attr in {"run", "check_output"}
            ):
                continue
            if any(kw.arg is None for kw in node.keywords):
                rel = path.relative_to(REPO_ROOT).as_posix()
                found.setdefault(rel, []).append(node.lineno)

    unlisted = {p: lines for p, lines in found.items() if p not in _KWARGS_FUNNELS}
    assert not unlisted, (
        "these subprocess calls unpack their kwargs, so this file's scan cannot "
        f"tell whether they are capture+timeout: {unlisted}. Read each one. If it "
        "is the watched shape, route it through run_bounded; if it is not, add it "
        "to _KWARGS_FUNNELS saying what it does instead. Leaving it unlisted means "
        "the ratchet's zero is not covering it."
    )

    gone = sorted(set(_KWARGS_FUNNELS) - set(found))
    assert not gone, (
        f"_KWARGS_FUNNELS lists files with no unpacked subprocess call left: {gone}. "
        "Delete the entry -- a stale exemption is how a real funnel gets waved "
        "through later."
    )


#: Files that bind ``subprocess.run``/``check_output`` to a NAME rather
#: than calling it, with what reading that binding found. An alias is the
#: one shape :func:`_capture_with_timeout_calls` structurally cannot see,
#: because the call it produces is a bare ``run(...)``.
_RUN_ALIASES: dict[str, str] = {}


def test_aliases_of_subprocess_run_are_listed() -> None:
    """An aliased spawner cannot hide the watched shape.

    nexus-t10nc drained this ratchet to zero and BOTH standing reviewers
    then found, independently, that ``plugin_lockstep`` had been calling
    the watched shape at four sites the whole time. It took a
    ``run: Runner = subprocess.run`` default parameter, so every call site
    read as ``run(...)`` and no ``subprocess`` attribute appeared on any of
    them. The empty map had claimed "every file in scope is allowed zero",
    and for that file it was not true.

    Sweeping for the BINDING closes it: an alias is rare and cheap to
    spot, where resolving every bare ``run(`` call to its origin is
    neither. Both live aliases were converted (plugin_lockstep's injected
    Runner, and commands/upgrade's ``_run``), so the expected set is
    empty; a new one has to be read and listed.
    """
    found: dict[str, list[int]] = {}
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if not _in_scope(path):
            continue
        try:
            lines = _aliases_of_subprocess_run(path.read_text())
        except SyntaxError:  # pragma: no cover - another test's failure
            continue
        if lines:
            found[path.relative_to(REPO_ROOT).as_posix()] = lines

    unlisted = {p: lines for p, lines in found.items() if p not in _RUN_ALIASES}
    assert not unlisted, (
        f"these files bind subprocess.run/check_output to a name: {unlisted}. The "
        "call sites will read as a bare run(...), which this file's AST scan "
        "cannot match, so the ratchet's zero does not cover them. Read each one: "
        "if any call site passes capture+timeout, point the alias at "
        "nexus.bounded_subprocess.run_bounded; otherwise add it to _RUN_ALIASES "
        "saying what its call sites actually pass."
    )

    gone = sorted(set(_RUN_ALIASES) - set(found))
    assert not gone, f"_RUN_ALIASES lists files with no alias left: {gone}. Delete them."


def test_the_alias_detector_reproduces_the_miss() -> None:
    """Positive control: the shape that got past this lint for its whole life.

    Verbatim from ``plugin_lockstep`` as it stood before nexus-t10nc. If
    :func:`_aliases_of_subprocess_run` stops matching this, the sweep above
    passes over the exact defect it was written for.
    """
    source = (
        "import subprocess\n"
        "from collections.abc import Callable\n"
        "Runner = Callable[..., subprocess.CompletedProcess[str]]\n"
        "def converge(*, run: Runner = subprocess.run) -> None:\n"
        "    run(['claude'], capture_output=True, text=True, timeout=30, check=False)\n"
    )
    assert _aliases_of_subprocess_run(source) == [4], (
        "the alias detector no longer sees an injected subprocess.run default -- "
        "the exact shape that hid four live capture+timeout call sites"
    )
    # And the call inside it is invisible to the OTHER detector, which is
    # the whole reason this one exists.
    assert _capture_with_timeout_calls(source) == [], (
        "if the call detector can see a bare run(...) now, say so here rather "
        "than keeping two detectors for one shape"
    )
    # A plain call must NOT read as an alias.
    assert _aliases_of_subprocess_run(
        "import subprocess\nsubprocess.run(a, capture_output=True, timeout=5)\n"
    ) == []


def test_the_census_walk_finds_a_planted_file(tmp_path: pathlib.Path) -> None:
    """End-to-end over the WALK, not just the matcher.

    :func:`test_detector_recognises_the_shapes_it_claims_to` proves the AST
    matcher works on a string. It says nothing about ``_census`` reaching
    files, honouring ``_in_scope``, or reporting the right relative path --
    and with ``_UNCONVERTED`` empty, the real tree no longer exercises the
    reporting branch at all, so a regression there would be silent. The
    standing critic raised exactly that.
    """
    root = tmp_path / "nexus"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "watched.py").write_text(
        "import subprocess\nsubprocess.run(a, capture_output=True, timeout=5)\n"
    )
    (root / "pkg" / "clean.py").write_text("import subprocess\nsubprocess.run(a)\n")
    (root / "pkg" / "__pycache__").mkdir()
    (root / "pkg" / "__pycache__" / "stale.py").write_text(
        "import subprocess\nsubprocess.run(a, capture_output=True, timeout=5)\n"
    )

    census = _census(root=root, rel_to=tmp_path)

    assert census == {"nexus/pkg/watched.py": [2]}, (
        f"the census walk is wrong: {census}. It must find the planted call, skip "
        "the clean file, skip __pycache__, and key the result on the path "
        "relative to the repo root."
    )


def test_hooks_is_in_scope_and_stays_there() -> None:
    """``src/nexus/hooks/`` is watched like everything else (nexus-zptvf).

    It was held out while nexus-t9klx moved hook entry points between
    modules, because a per-file ratchet over a directory being
    restructured reds on the restructuring rather than on a defect. That
    bead closed, the 26 sites converted, and the exclusion machinery was
    DELETED rather than switched off -- a disabled exclusion is a thing
    that gets re-enabled by accident, and an exclusion never reports what
    it skipped, so nobody would notice.

    This pins the outcome rather than the mechanism: the most dangerous
    files in the census are reachable by the scan. They are the ones that
    hang a user's SESSION rather than a CLI command they can interrupt --
    the one measured Windows hang was hooks/verification_config.py, wired
    on Stop, sampled still blocked at 25 s on a 5.0 s timeout.
    """
    hooks = SRC_ROOT / "hooks"
    assert hooks.is_dir(), "src/nexus/hooks is gone; this test needs rewriting"
    assert _in_scope(hooks / "verification_config.py"), (
        "hooks/ is filtered out of _in_scope again. It holds the only measured "
        "instance of the defect this lint watches; if it must be held out again, "
        "say which bead ends the deferral and assert it still excludes something."
    )
    scanned = {p.name for p in hooks.rglob("*.py") if _in_scope(p)}
    assert "verification_config.py" in scanned and len(scanned) > 5, (
        f"the walk reaches only {len(scanned)} file(s) under hooks/"
    )