# SPDX-License-Identifier: AGPL-3.0-or-later
"""``discover_pg_binaries`` must never resolve to a HOST PostgreSQL.

LOCKED POLICY (Hal, 2026-07-07 and restated 2026-07-27): nexus ALWAYS uses the
PostgreSQL it builds. Never Homebrew, never a distro package, never whatever is
on ``PATH``. Exactly two legs are sanctioned — the ``NEXUS_PG_BIN`` operator
override, and the extracted ship-alongside bundle under the config dir.

WHAT WAS REMOVED. Discovery used to carry two further legs after those: a list
of fixed candidate directories (``/opt/homebrew/opt/postgresql@{17,16,15}/bin``,
``/usr/lib/postgresql/{17,16,15}/bin``) and ``shutil.which("initdb")``.

WHY THEY WERE A CORRECTNESS BUG, not merely off-policy. The shipped bundle
carries exactly ``pg_trgm``, ``plpgsql`` and ``vector``; a host PostgreSQL
carries the full distro contrib set and often no pgvector at all. Falling
through to it substitutes a DIFFERENT database for the one nexus builds, so a
missing bundle surfaces late as ``CREATE EXTENSION vector`` failing at boot, or
as a green test run against a substrate no user has. The same fallback made
every test skip gate answer "does this BOX have a PostgreSQL?" rather than "can
we obtain OUR substrate?", which silently disabled 58 integration tests on any
box without a host install (measured 2026-07-27: 90 skipped -> 32 skipped,
307 passed -> 365 passed, with failures unchanged at 29).

WHY A TEST AND NOT A COMMENT. A fallback leg is re-added by exactly the kind of
well-meaning change that looks like a robustness improvement, and its effect is
invisible on any developer machine that happens to have Homebrew PostgreSQL —
which is where such a change would be written and tested. Nothing about a green
run on that box would reveal it.

Companion: tests/db/test_pg_gate_is_self_provisioning.py pins the TEST-side rule
(no skip gate may ask an ambient question). This module pins the PRODUCT-side
rule (discovery may not answer one).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from nexus.db import pg_provision
from nexus.db.pg_provision import PgBinaryNotFoundError, discover_pg_binaries

_SOURCE = Path(pg_provision.__file__)


def test_no_host_candidate_directories_remain() -> None:
    """No hardcoded host PG path may appear in the module at all."""
    src = _SOURCE.read_text()
    tree = ast.parse(src)

    # String CONSTANTS only — the docstrings deliberately name these paths to
    # explain what was removed and why, and a substring scan would fire on the
    # explanation rather than on a re-added leg.
    literals = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    code_literals = [s for s in literals if "\n" not in s]

    offenders = [
        s for s in code_literals
        if "/opt/homebrew" in s or "/usr/lib/postgresql" in s or "/usr/pgsql" in s
    ]
    assert offenders == [], (
        "a host-PostgreSQL path is back in discovery. nexus uses the PG it "
        f"builds; see _NO_HOST_FALLBACK in {_SOURCE.name}. Offenders: {offenders}"
    )
    assert not hasattr(pg_provision, "_CANDIDATE_DIRS"), (
        "_CANDIDATE_DIRS is back — that was the Homebrew/distro fallback leg"
    )
    assert pg_provision._NO_HOST_FALLBACK is True


def test_discovery_never_reaches_for_path() -> None:
    """``shutil.which`` must not be how a PostgreSQL gets chosen."""
    tree = ast.parse(_SOURCE.read_text())
    which_calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "which"
    ]
    assert which_calls == [], (
        "discovery resolves a binary off PATH again — PATH is the host's "
        "PostgreSQL, not ours"
    )


#: PostgreSQL executables. ``shutil.which`` on any of these is a host-PG leg;
#: ``which("java")`` and friends are not this rule's business.
_PG_TOOLS = frozenset({"initdb", "pg_ctl", "psql", "createdb", "pg_config", "postgres"})

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _python_sources() -> list[Path]:
    roots = [_REPO_ROOT / "src" / "nexus", _REPO_ROOT / "scripts"]
    return sorted(p for r in roots for p in r.rglob("*.py"))


def test_no_host_pg_leg_anywhere_in_product_or_tooling() -> None:
    """The rule is not confined to the discovery function.

    A PATH fallback re-appeared once in ``scripts/rdr152-sandbox/sandbox_helper.py``
    for a sympathetic reason (a discovery failure had silently produced an empty
    path, so every count check SKIPped and the run reported "all passed").
    Falling back to PATH fixed the silence but verified the sandbox's row counts
    against a DIFFERENT PostgreSQL — a quieter version of the same false pass.
    Failing loudly is the fix; reaching for the host's PG never is.
    """
    sources = _python_sources()
    assert len(sources) > 50, (
        f"only {len(sources)} python sources scanned — the rglob roots are wrong"
    )

    offenders: list[str] = []
    for path in sources:
        if path == Path(__file__):
            continue
        tree = ast.parse(path.read_text())
        rel = path.relative_to(_REPO_ROOT).as_posix()

        for node in ast.walk(tree):
            # shutil.which("psql") and friends.
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "which"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value in _PG_TOOLS
            ):
                offenders.append(f"{rel}:{node.lineno} which({node.args[0].value!r})")
            # Hardcoded host install paths, in code rather than prose.
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "\n" not in node.value
                and ("/opt/homebrew/opt/postgresql" in node.value
                     or "/usr/lib/postgresql" in node.value)
            ):
                offenders.append(f"{rel}:{node.lineno} {node.value!r}")

    assert offenders == [], (
        "host-PostgreSQL resolution outside the two sanctioned legs. nexus uses "
        "the PG it builds; fail loud instead of substituting the host's. "
        f"Offenders: {offenders}"
    )


def test_missing_bundle_fails_loud_and_names_the_bundle(monkeypatch, tmp_path) -> None:
    """The failure must send the reader to the bundle, never to a package manager.

    The old hint said ``brew install postgresql@17`` (apt/dnf on Linux). That
    was the message users actually saw on the one path where it mattered, and
    following it could not fix anything: a host PostgreSQL is not the bundle.
    """
    monkeypatch.delenv("NEXUS_PG_BIN", raising=False)
    # An empty config dir => no extracted bundle => both sanctioned legs miss.
    monkeypatch.setattr("nexus.config.nexus_config_dir", lambda: tmp_path)

    with pytest.raises(PgBinaryNotFoundError) as exc:
        discover_pg_binaries()

    msg = str(exc.value)
    assert "nx init --service" in msg, msg
    assert "NEXUS_PG_BIN" in msg, msg
    for forbidden in ("brew install", "apt-get install", "dnf install"):
        assert forbidden not in msg, (
            f"the failure hint tells the user to {forbidden!r} a host "
            f"PostgreSQL, which cannot resolve a missing bundle: {msg}"
        )


def test_a_host_pg_on_path_is_not_used(monkeypatch, tmp_path) -> None:
    """The falsification: a perfectly good host PG must still be refused.

    Builds a directory containing all four binaries, puts it on ``PATH``, and
    asserts discovery raises anyway. Under the old candidate-dirs/PATH legs this
    resolved happily — which is precisely the silent substitution being
    forbidden. Without this test the two AST checks above could both pass while
    some new leg resolved a host install by another route.
    """
    fake_host_pg = tmp_path / "host-pg" / "bin"
    fake_host_pg.mkdir(parents=True)
    for name in ("initdb", "pg_ctl", "psql", "createdb"):
        binary = fake_host_pg / name
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)

    monkeypatch.delenv("NEXUS_PG_BIN", raising=False)
    monkeypatch.setenv("PATH", f"{fake_host_pg}:{tmp_path}")
    monkeypatch.setattr("nexus.config.nexus_config_dir", lambda: tmp_path / "cfg")

    with pytest.raises(PgBinaryNotFoundError):
        discover_pg_binaries()


def test_the_two_sanctioned_legs_still_work(monkeypatch, tmp_path) -> None:
    """Non-vacuity: the rule above must not have broken discovery outright.

    A module that always raised would satisfy every assertion in this file, so
    the override leg is exercised positively here.
    """
    bundle_bin = tmp_path / "bundle" / "bin"
    bundle_bin.mkdir(parents=True)
    for name in ("initdb", "pg_ctl", "psql", "createdb"):
        binary = bundle_bin / name
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)

    monkeypatch.setenv("NEXUS_PG_BIN", str(bundle_bin))
    bins = discover_pg_binaries()
    assert bins.initdb == bundle_bin / "initdb"
    assert bins.all_present()
