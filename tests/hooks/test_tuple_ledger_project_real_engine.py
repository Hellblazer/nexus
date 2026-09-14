# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Real-engine proof for the checkable-report VERIFY dims (bead
nexus-cnzei.6 item 2, engine half nexus-d9k5h).

``tests/hooks/test_tuple_ledger_project.py``'s VERIFY-dims coverage runs
entirely against a mocked ``/v1/tuples/out`` -- including the schema-
fallback tests, whose mock is HAND-TOLD to answer 400 for
commit/t2_ref/verify. That proves the PROJECTOR's own retry logic given a
400, but nothing there proves a 400 is actually what a real engine
returns for an undeclared dim, nor that the dev jar built from THIS
worktree really does declare commit/t2_ref/verify (nexus-d9k5h landed on
develop, but a mock can't prove that landed jar behaves as assumed).
"Our rule is that a fixture MVV is not the live path" (bead
nexus-cnzei.6 dispatch brief) -- this file is the live-path check that
rule requires: a real transcript fixture, run through the real
production code path, against a REAL engine booted from a dev jar built
via ``scripts/build-gate-jar.sh`` (never a bare ``mvn``/``mvnw``, per
AGENTS.md).

Two things are proven here that the mocked suite cannot:

1. The dev jar's ``ledger.yaml`` really does declare commit/t2_ref/verify
   (nexus-d9k5h) -- a real ``out`` naming them succeeds, end to end,
   through the actual ``tuple_ledger_project.py`` subprocess reading a
   real transcript.
2. A dim NO template will ever declare really does provoke HTTP 400 from
   the real engine -- confirming ``_post_via_urllib``'s ``status == 400``
   ``_SchemaViolation`` detection matches genuine wire behavior, not an
   assumption baked into the mock's ``reject_extra_dims`` knob.

Skips (never fails) when the service JAR is not built -- same contract
as every other engine-substrate test (``t2_service_env``, ``ensure_engine``).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.hooks.test_tuple_ledger_project import (
    _assistant_text_entry,
    _data_token_digest,
    _write_transcript,
)

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "conexus"
    / "hooks"
    / "scripts"
    / "tuple_ledger_project.py"
)

SESSION_ID = "sess-tuple-proj-real"
AGENT_ID = "arealengine1234567890abcdef"
AGENT_TYPE = "developer"


def _real_engine_state():
    from tests._engine_substrate import ensure_engine
    from tests.db._service_fixture import jar_freshness_skip_reason

    reason = jar_freshness_skip_reason()
    if reason is not None:
        pytest.skip(f"engine substrate: {reason}")
    try:
        return ensure_engine()
    except RuntimeError as exc:
        pytest.skip(f"engine substrate unavailable: {exc}")


def _mint(state: dict) -> tuple[str, str]:
    from tests._engine_substrate import mint_test_tenant

    return mint_test_tenant(state)


def _write_data_token_lease(config_dir: Path, *, base_url: str, token: str, tenant: str) -> None:
    digest = _data_token_digest(base_url, tenant)
    record = {
        "format_version": 1,
        "token": token,
        "tenant": tenant,
        "base_url_digest": digest,
        "expires_at": time.time() + 3600.0,
        "ttl_seconds": 3600.0,
        "minted_by_pid": os.getpid(),
    }
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / f"data_token_lease.{digest}").write_text(json.dumps(record))


def test_real_engine_accepts_the_verify_dims_end_to_end(tmp_path: Path) -> None:
    """The full production path: a real transcript with VERIFY lines, the
    real ``tuple_ledger_project.py`` subprocess, a real engine booted from
    this worktree's dev jar. Proves the dev jar's ledger.yaml actually
    declares commit/t2_ref/verify (nexus-d9k5h) -- not merely that the
    projector WOULD send them."""
    state = _real_engine_state()
    # HttpTupleStore's DEFAULT_TENANT -- the tenant this script always
    # resolves to (see tuple_ledger_project.py's _RESOLVED_TENANT).
    tenant, token = _mint(state)
    # The default tenant name IS what _RESOLVED_TENANT names on the
    # client side; the minted tenant's OWN token is bound to whatever
    # tenant mint_test_tenant actually created, so present that lease
    # under the digest the script computes for _RESOLVED_TENANT explicitly
    # by writing the lease keyed on that name -- exercised by is_local_
    # supervisor=False's config.yml leg for a deterministic base_url.
    config_dir = tmp_path / "config"
    import importlib.util

    spec = importlib.util.spec_from_file_location("tuple_ledger_project_real", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    resolved_tenant = module._RESOLVED_TENANT
    _write_data_token_lease(config_dir, base_url=state["base_url"], token=token, tenant=resolved_tenant)

    transcript = _write_transcript(tmp_path, [
        {"type": "user", "message": {"role": "user", "content": "implement the thing"}},
        _assistant_text_entry(
            "Implementation complete.\n"
            "VERIFY: commit=1234567\n"
            "VERIFY: uv run pytest tests/hooks/test_tuple_ledger_project.py => rc=0 41 passed\n"
            "VERIFY: t2=nexus/cnzei6-real-engine-proof\n"
        ),
    ])

    env = {k: v for k, v in os.environ.items() if not k.startswith("NX_SERVICE_")}
    env["NEXUS_CONFIG_DIR"] = str(config_dir)
    env["XDG_STATE_HOME"] = str(tmp_path / "state")
    env["NX_SERVICE_URL"] = state["base_url"]
    payload = json.dumps({
        "session_id": SESSION_ID,
        "hook_event_name": "SubagentStop",
        "agent_id": AGENT_ID,
        "agent_type": AGENT_TYPE,
        "agent_transcript_path": str(transcript),
    })
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "report"],
        input=payload, capture_output=True, text=True, env=env, timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    log = tmp_path / "state" / "nexus" / "orchestration" / f"{SESSION_ID}.tuple-projection.log"
    log_text = log.read_text() if log.exists() else ""
    assert "SKIP" not in log_text, f"expected the real engine to accept the VERIFY dims, got: {log_text}"
    assert "SCHEMA_FALLBACK" not in log_text, (
        "the dev jar built from this worktree should already declare commit/t2_ref/verify "
        f"(nexus-d9k5h) -- a fallback here means the checked-out ledger.yaml regressed: {log_text}"
    )


def test_real_engine_returns_http_400_for_an_undeclared_dimension(tmp_path: Path) -> None:
    """Confirms the wire contract ``_post_via_urllib``'s 400-detection
    depends on, against the genuine engine -- not the mock's hand-told
    ``reject_extra_dims`` behavior. No template past, present, or future
    is expected to declare this dimension name, so this is a stable
    proof of "undeclared dim -> HTTP 400", independent of nexus-d9k5h's
    own three specific dims ever being retired or renamed."""
    state = _real_engine_state()
    tenant, token = _mint(state)

    import importlib.util

    spec = importlib.util.spec_from_file_location("tuple_ledger_project_real2", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    body = {
        "subspace": f"ledger/{SESSION_ID}-undeclared-dim-probe",
        "keys": {"agent_id": AGENT_ID, "kind": "report"},
        "dims": {"agent_type": AGENT_TYPE, "nx_cnzei6_never_declared_probe_dim": "x"},
    }
    with pytest.raises(module._SchemaViolation):
        module._post_via_urllib(state["base_url"], token, body, is_local_supervisor=True)
