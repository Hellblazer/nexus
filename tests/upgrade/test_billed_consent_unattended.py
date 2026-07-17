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

So this is that leg, in the cheapest form that still proves each property: a
real ``nx upgrade`` subprocess with ``stdin`` closed. No container, no service,
no money.

WHAT THIS FILE COVERS, precisely (critic, 2026-07-17 — the first draft's
docstring overclaimed "closes the gap"):

* r3's direction — a billed walk with no TTY DEFERS (exit 0, user-facing line,
  channel named) instead of crashing with an empty reason.
* r4's direction — the FREE pure-re-id shape (voyage-named + legacy ids, SC-2's
  "costs nothing" promise) sails past the gate unprompted; the r4 predicate
  regression made exactly this shape defer forever at exit 0.
* the GRANT channel — NX_ASSUME_YES carries a billed walk past the gate.

WHAT IT DELIBERATELY DOES NOT COVER: full convergence-and-verify of any leg.
The fixture's service is an unreachable address by design, so the grant/free
tests prove their walks reach the SERVICE BOUNDARY (which a deferred walk never
dials) and stop there. Asserting actual convergence needs a live voyage-capable
target — the era-hop covers the bge shapes; the voyage passthrough remains
ungated past this boundary.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import chromadb
import pytest

from nexus.catalog.catalog_db import CatalogDB

_LEGACY_COLLECTION = "knowledge__gate__minilm-l6-v2-384__v1"
#: SC-1+SC-2's own shape: voyage-NAMED, carrying pre-RDR-108 legacy ids. With a
#: key present this plans a PURE RE-ID passthrough — target == source,
#: needs_reembed False, billed False. No Voyage call, no money. 1024-dim vectors
#: deliberately: 768 would trip the measured-dim override and relabel it a
#: mislabel, which is a different shape with a different plan.
_FREE_REID_COLLECTION = "knowledge__gate__voyage-context-3__v1"


def _seed(chroma_path: pathlib.Path, name: str, dim: int) -> None:
    client = chromadb.PersistentClient(path=str(chroma_path))
    coll = client.create_collection(name)
    coll.add(
        ids=[f"legacy-id-{i:04d}" for i in range(3)],  # pre-RDR-108: not 32-char
        documents=[f"chunk {i}" for i in range(3)],
        embeddings=[[0.1] * dim for _ in range(3)],
    )


def _seed_billable_footprint(chroma_path: pathlib.Path) -> None:
    """A legacy-id collection on an UNWIRED model.

    With a Voyage key present that remaps to a voyage target, which is the only
    shape that reaches the cost gate: ``needs_reembed`` True (minilm is wired by
    nothing) and the target's declared model is billed.
    """
    _seed(chroma_path, _LEGACY_COLLECTION, 384)


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


def _isolated_env(tmp_path: pathlib.Path) -> dict[str, str]:
    """The isolation machinery both seeds share — config dir, catalog, env.

    Isolated by NEXUS_CONFIG_DIR + NX_LOCAL_CHROMA_PATH — this must never touch
    the developer's real install (feedback_dont_break_live_nexus_install).
    """
    chroma = tmp_path / "chroma"
    config_dir = tmp_path / "cfg"
    (config_dir / "catalog").mkdir(parents=True)
    # The catalog must EXIST or the t2-schema rung defers ("catalog absent —
    # retry deferred until catalog exists"), and a deferred rung HALTS the walk
    # — the substrate rung would never be reached and this gate would pass while
    # testing nothing. Found by the non-vacuity test below, which is why it is
    # here.
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


@pytest.fixture
def _billable_install(tmp_path: pathlib.Path) -> dict[str, str]:
    """An isolated install whose footprint plans exactly one BILLED leg."""
    _seed(tmp_path / "chroma", _LEGACY_COLLECTION, 384)
    return _isolated_env(tmp_path)


@pytest.fixture
def _free_reid_install(tmp_path: pathlib.Path) -> dict[str, str]:
    """An isolated install whose footprint plans exactly one FREE pure-re-id
    leg — SC-1+SC-2's shape, the one r4's predicate regression falsely billed."""
    _seed(tmp_path / "chroma", _FREE_REID_COLLECTION, 1024)
    return _isolated_env(tmp_path)


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

    # The line the USER reads — the `click.echo` at upgrade.py's deferred-only
    # branch — selected by STREAM and PREFIX, not grepped out of a merged blob.
    # Two drafts of this assertion were vacuous, one commit apart, for the same
    # reason: `--yes` also rides structlog WARNINGS (`substrate_cost_gate_
    # declined_no_tty`, then `ladder_rung_deferred`, which embeds the identical
    # detail), so any stdout+stderr search is backstopped by a log line nobody
    # reads. structlog goes to STDERR; the echo goes to STDOUT with a fixed
    # prefix. Asserting on a surface means selecting the stream and the prefix
    # (pins seven AND eight of this arc — the second found by mutating the echo
    # alone, which left the merged-blob version green via the stderr log).
    deferrals = [
        ln for ln in proc.stdout.splitlines()
        if ln.startswith("Upgrade ladder: rung ") and "deferred" in ln
    ]
    assert deferrals, f"the walk must SAY it deferred, to the user, on stdout:\n{out}"
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
    # Same stream-and-prefix discipline as the deferral assertion: the dry-run
    # pending line is a click.echo on STDOUT, and its pending_detail NAMES the
    # collections the planner gave legs. Asserting the seeded collection by
    # name proves the BILLED leg specifically was planned — "substrate-etl
    # appeared somewhere" would also match the stderr structlog stream.
    pending = [
        ln for ln in proc.stdout.splitlines()
        if ln.startswith("Upgrade ladder: rung 'substrate-etl'") and "pending" in ln
    ]
    assert pending, f"the substrate rung never engaged:\n{proc.stdout}\n{proc.stderr}"
    said = "\n".join(pending)
    assert _LEGACY_COLLECTION in said, (
        f"the seeded billable collection was never planned:\n{pending}"
    )
    # ...and planned as the shape that bills: the pending detail marks re-embed
    # legs explicitly. Without this, a plan that gave the collection some OTHER
    # leg (pure re-id, say) would still pass the name check.
    assert "(re-embed)" in said, f"the leg planned is not a re-embed:\n{pending}"


def test_standing_consent_is_honored_end_to_end(
    _billable_install: dict[str, str],
) -> None:
    """The GRANT channel, live: NX_ASSUME_YES=1 must carry the walk PAST the
    consent gate. A regression that stops honoring it would defer every
    unattended billed install forever — and nothing else drives the real
    process with the env set (critic, 2026-07-17: every other NX_ASSUME_YES
    test calls the gate function directly).

    What CAN be asserted here ends at the service boundary: this fixture's
    service is deliberately unreachable, so the walk proceeds past consent and
    then fails AT THE SERVICE — which is exactly the proof that consent was
    granted (a declined walk never dials it). Full convergence needs a live
    voyage-capable target; see the module docstring."""
    env = dict(_billable_install)
    env["NX_ASSUME_YES"] = "1"
    proc = _run_upgrade(env)
    out = proc.stdout + proc.stderr

    deferrals = [
        ln for ln in proc.stdout.splitlines()
        if ln.startswith("Upgrade ladder: rung ") and "deferred" in ln
    ]
    assert not deferrals, f"standing consent was ignored — the walk deferred:\n{out}"
    # The walk got past the gate and reached for the (unreachable) target:
    assert proc.returncode != 0 and "unreachable" in out, (
        f"the walk never reached the service boundary:\n{out}"
    )


def test_a_free_pure_reid_walk_never_reaches_the_consent_gate(
    _free_reid_install: dict[str, str],
) -> None:
    """r4's regression direction, live (critic, 2026-07-17). SC-2 promises the
    voyage-named legacy-ids shape costs nothing: a pure re-id passthrough,
    billed False, NOTHING to consent to. r4's widened predicate falsely billed
    exactly this shape, and with no terminal it then deferred forever at exit 0
    — reporting success while never converging, invisible to every gate.

    Under that regression this walk DEFERS (exit 0, deferral line). Under
    correct code it sails past the gate unprompted and fails only at this
    fixture's deliberately unreachable service — the same service-boundary
    proof as the grant test, with NO consent given: nothing needed asking."""
    proc = _run_upgrade(_free_reid_install)
    out = proc.stdout + proc.stderr

    deferrals = [
        ln for ln in proc.stdout.splitlines()
        if ln.startswith("Upgrade ladder: rung ") and "deferred" in ln
    ]
    assert not deferrals, (
        f"a FREE pure-re-id walk was asked for consent it does not need:\n{out}"
    )
    assert proc.returncode != 0 and "unreachable" in out, (
        f"the walk never reached the service boundary:\n{out}"
    )
