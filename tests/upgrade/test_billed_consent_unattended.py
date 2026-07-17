# SPDX-License-Identifier: AGPL-3.0-or-later
"""RDR-185 P5 (nexus-dnnbl): the billed-consent gate, driven as a REAL process.

THE GAP THIS CLOSES. Six rounds of consent-gate changes shipped with zero live
coverage, and every Critical in them was found by a reviewer rather than by a
test:

* r3 — ``click.Abort`` escaped the gate, so an unattended walk exited 1 with an
  EMPTY reason (``did not converge — substrate-etl: failed (converge raised: )``)
  and never named the flag that fixes it.
* r4 — the billed predicate was widened, so SC-1's own pure-re-id shape deferred
  forever at exit 0, reporting success while never converging.

Both are *process-level* failures: an exit code, a message, a TTY that is not
there. Unit tests call ``_default_cost_gate`` directly and cannot see any of it —
under pytest there is no TTY either way, and a monkeypatched ``click.confirm``
never produces a real ``Abort``.

WHY NOT THE ERA-HOP (bead nexus-dnnbl's original premise, disproven 2026-07-17).
The bead proposed exporting a dummy ``NX_VOYAGE_API_KEY`` into
``rehearse_era_hop.sh``. That cannot work, and the reason is structural rather
than fixable:

* ``voyage_key_available``'s docstring is explicit that the service wires voyage
  iff the key is set AT LAUNCH (``Main.java:111``). A key exported AFTER the
  era-hop's ``nx init --service --embedder bge-768`` makes the planner believe
  voyage is wired while the running service serves only bge — every remapped leg
  then 422s.
* A key set BEFORE launch makes the service wire "voyage", and any real
  re-embed then calls Voyage with a fake key: 401.

Either way the era-hop's minilm shapes must re-embed into voyage, so the
consent-GRANTED path is not container-testable without real Voyage spend. The
consent-DECLINED path is, because a deferred walk stops before any embed call —
but a walk that defers does not converge, and "converges unattended" is the one
thing the era-hop exists to assert. They cannot share a leg.

So this is that leg, in the cheapest form that still proves the property: a real
``nx upgrade`` subprocess with ``stdin`` closed. No container, no service, no
money — the walk defers before it would need any of them.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

_LEGACY_COLLECTION = "knowledge__gate__minilm-l6-v2-384__v1"


def _seed_billable_footprint(chroma_path: pathlib.Path) -> None:
    """A legacy-id collection on an UNWIRED model.

    With a Voyage key present that remaps to a voyage target, which is the only
    shape that reaches the cost gate: ``needs_reembed`` True (minilm is wired by
    nothing) and the target's declared model is billed.
    """
    import chromadb

    client = chromadb.PersistentClient(path=str(chroma_path))
    coll = client.create_collection(_LEGACY_COLLECTION)
    coll.add(
        ids=[f"legacy-id-{i:04d}" for i in range(3)],  # pre-RDR-108: not 32-char
        documents=[f"chunk {i}" for i in range(3)],
        embeddings=[[0.1] * 384 for _ in range(3)],
    )


def _run_upgrade(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    """`nx upgrade` as a REAL process with NO terminal.

    `stdin=DEVNULL` is the whole point: it is what a hook, a cron job, and a CI
    runner actually present, and what no in-process test can fake — pytest's
    captured stdin raises OSError rather than behaving like a closed terminal.
    """
    # --skip-t3 suppresses the engine INSTALL and the daemon cycle (a binary
    # download that has nothing to do with consent and hangs an isolated box) —
    # it does NOT skip the ladder walk, which still reaches the substrate rung
    # and its cost gate. That is the whole path under test.
    return subprocess.run(
        [sys.executable, "-m", "nexus.cli", "upgrade", "--skip-t3", *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
    )


@pytest.fixture
def _billable_install(tmp_path: pathlib.Path) -> dict[str, str]:
    """An isolated install whose footprint plans exactly one BILLED leg.

    Isolated by NEXUS_CONFIG_DIR + NX_LOCAL_CHROMA_PATH — this must never touch
    the developer's real install (feedback_dont_break_live_nexus_install).
    """
    import os

    chroma = tmp_path / "chroma"
    _seed_billable_footprint(chroma)
    config_dir = tmp_path / "cfg"
    (config_dir / "catalog").mkdir(parents=True)
    # The catalog must EXIST or the t2-schema rung defers ("catalog absent —
    # retry deferred until catalog exists"), and a deferred rung HALTS the walk
    # — the substrate rung would never be reached and this gate would pass while
    # testing nothing. Found by the non-vacuity test below, which is why it is
    # here.
    from nexus.catalog.catalog_db import CatalogDB

    CatalogDB(config_dir / "catalog" / ".catalog.db").close()

    env = dict(os.environ)
    env.update(
        NEXUS_CONFIG_DIR=str(config_dir),
        NX_LOCAL_CHROMA_PATH=str(chroma),
        # A configured service_url satisfies the provisioning precondition from
        # on-disk evidence alone (preconditions._default_provisioned), so the
        # walk reaches the rung without standing up a real stack. It is never
        # dialled: the rung defers before it would migrate, and the target-count
        # probe reads an unreachable service as "could not tell", which is never
        # "converged".
        NX_SERVICE_URL="http://127.0.0.1:1",
        NX_SERVICE_TOKEN="t",
        NX_VOYAGE_API_KEY="dummy-key-never-dialled",  # presence is the signal
        NX_MIGRATION_NOTICE="1",
    )
    env.pop("NX_ASSUME_YES", None)
    # SCRUB the developer's real cloud credentials. `dict(os.environ)` inherits
    # them, and `open_read_legs` then opens a REAL Chroma Cloud read leg — which
    # is both a live-install hazard and, on this box, an outright failure
    # (`detect raised: Permission denied.`, chroma /auth/identity). Only the
    # absent-leg sentinels are swallowed by design; a permission error is meant
    # to propagate loud. NEXUS_CONFIG_DIR isolates config.yml but NOT the env.
    for leaked in ("CHROMA_API_KEY", "CHROMA_TENANT", "CHROMA_DATABASE"):
        env.pop(leaked, None)
    return env


def test_a_billed_walk_defers_unattended_instead_of_crashing_or_hanging(
    _billable_install: dict[str, str],
) -> None:
    """THE gate. An unattended install with a billable leg must DEFER: exit 0,
    say so, and name the way through — never crash with an empty reason (r3),
    never hang on a prompt nothing can answer, never bill.

    Falsifiable against r3 verbatim: letting click.Abort escape turns this exit
    code into 1 and the reason into the empty string."""
    proc = _run_upgrade(_billable_install)
    out = proc.stdout + proc.stderr

    assert proc.returncode == 0, f"an unattended billed walk must not fail:\n{out}"

    # The line the USER reads — `Upgrade ladder: rung '...' deferred — <detail>`.
    # Asserted on that line specifically, NOT on the whole output: the cost
    # gate's structlog WARNING also names `--yes`, so a whole-output search
    # passes while the user-facing message says nothing useful. Falsification
    # caught exactly that — stripping the channel from the deferral left this
    # test green via the log line (the seventh vacuous pin of this arc).
    deferrals = [ln for ln in out.splitlines() if "deferred" in ln.lower() and "rung" in ln]
    assert deferrals, f"the walk must SAY it deferred, to the user:\n{out}"
    said = "\n".join(deferrals)
    assert "--yes" in said or "NX_ASSUME_YES" in said, (
        f"the deferral the user reads must name the consent channel:\n{said}"
    )


def test_the_deferral_is_not_a_no_op_report(
    _billable_install: dict[str, str],
) -> None:
    """Non-vacuity: the walk must actually have PLANNED the billed leg, not
    skipped the footprint and reported 'deferred' about something else. Without
    this, a walk that saw no collections at all would satisfy the test above."""
    proc = _run_upgrade(_billable_install, "--dry-run")
    out = (proc.stdout + proc.stderr).lower()
    assert "substrate-etl" in out, f"the substrate rung never engaged:\n{out}"
