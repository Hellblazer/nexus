# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Unit coverage for ``tests/e2e/lib/admission_load.py`` (nexus-u2mlh.9).

Two layers, deliberately separated:

* The PURE core (counter extraction/comparison, ramp-stop decision,
  non-vacuity check, nonce/subject/document generation, response
  summarization) is exercised directly here, in-process, no network.
* The wrapper's mode detection (``tests/e2e/admission-load-gate.sh``
  refuses on a local-mode box) is exercised as a real subprocess with an
  isolated, credential-free environment — still no network, since a
  local-mode refusal is the FIRST thing the script does, before any
  driver invocation.

Never runs the real ramp: that would drive concurrent embed load against
the operator's live engine, which is exactly what this gate is for and
exactly what a test suite must never do on its own initiative.
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
_MODULE_PATH = REPO_ROOT / "tests" / "e2e" / "lib" / "admission_load.py"
_SCRIPT_PATH = REPO_ROOT / "tests" / "e2e" / "admission-load-gate.sh"


def _load_module():
    spec = importlib.util.spec_from_file_location("admission_load", _MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


admission_load = _load_module()


# ── Counters ──────────────────────────────────────────────────────────────


def test_snapshot_counters_extracts_the_named_embedder() -> None:
    status = {
        "embedder_activity": {
            "voyage-context-3": {
                "admission_refusals_total": 3,
                "deadline_aborts_total": 1,
                "queue_depth": 5,
                "chunks_done_total": 42,
                "thread_width": 12,
            },
            "voyage-code-3": {"admission_refusals_total": 999},
        }
    }
    counters = admission_load.snapshot_counters(status, "voyage-context-3")
    assert counters.admission_refusals_total == 3
    assert counters.deadline_aborts_total == 1
    assert counters.queue_depth == 5
    assert counters.chunks_done_total == 42
    assert counters.thread_width == 12


@pytest.mark.parametrize(
    "status",
    [None, {}, {"embedder_activity": None}, {"embedder_activity": {}}, {"embedder_activity": {"voyage-context-3": None}}],
)
def test_snapshot_counters_is_fail_closed_to_zero(status) -> None:
    assert admission_load.snapshot_counters(status, "voyage-context-3") == admission_load.EmbedderCounters()


def test_counters_moved_true_on_admission_refusal_increase() -> None:
    before = admission_load.EmbedderCounters(admission_refusals_total=0, deadline_aborts_total=0)
    after = admission_load.EmbedderCounters(admission_refusals_total=1, deadline_aborts_total=0)
    assert admission_load.counters_moved(before, after) is True


def test_counters_moved_true_on_deadline_abort_increase() -> None:
    before = admission_load.EmbedderCounters(admission_refusals_total=5, deadline_aborts_total=0)
    after = admission_load.EmbedderCounters(admission_refusals_total=5, deadline_aborts_total=1)
    assert admission_load.counters_moved(before, after) is True


def test_counters_moved_false_when_neither_advances() -> None:
    before = admission_load.EmbedderCounters(admission_refusals_total=5, deadline_aborts_total=2, chunks_done_total=10)
    after = admission_load.EmbedderCounters(admission_refusals_total=5, deadline_aborts_total=2, chunks_done_total=99)
    assert admission_load.counters_moved(before, after) is False


def test_counters_delta_reports_the_three_named_fields() -> None:
    before = admission_load.EmbedderCounters(admission_refusals_total=1, deadline_aborts_total=2, chunks_done_total=3)
    after = admission_load.EmbedderCounters(admission_refusals_total=4, deadline_aborts_total=2, chunks_done_total=10)
    assert admission_load.counters_delta(before, after) == {
        "admission_refusals_total": 3,
        "deadline_aborts_total": 0,
        "chunks_done_total": 7,
    }


# ── Ramp planning + non-vacuity ────────────────────────────────────────────


def test_parse_ramp_steps_accepts_a_strictly_increasing_list() -> None:
    assert admission_load.parse_ramp_steps("32,64,128,256") == [32, 64, 128, 256]


def test_parse_ramp_steps_tolerates_whitespace() -> None:
    assert admission_load.parse_ramp_steps(" 32, 64 ,128 ") == [32, 64, 128]


@pytest.mark.parametrize("raw", ["", "  ", "0,-1", "64,32", "32,32,64"])
def test_parse_ramp_steps_refuses_non_increasing_or_empty(raw: str) -> None:
    with pytest.raises(ValueError):
        admission_load.parse_ramp_steps(raw)


def test_decide_ramp_outcome_stops_at_first_movement() -> None:
    outcome = admission_load.decide_ramp_outcome([32, 64, 128], [False, True, True])
    assert outcome.stopped_at_step == 64
    assert outcome.stopped_at_index == 1
    assert outcome.tried == (32, 64, 128)


def test_decide_ramp_outcome_none_when_nothing_moved() -> None:
    outcome = admission_load.decide_ramp_outcome([32, 64], [False, False])
    assert outcome.stopped_at_step is None
    assert outcome.stopped_at_index is None


def test_decide_ramp_outcome_refuses_a_length_mismatch() -> None:
    with pytest.raises(ValueError):
        admission_load.decide_ramp_outcome([32, 64], [False])


def test_require_non_vacuous_passes_when_something_moved() -> None:
    outcome = admission_load.decide_ramp_outcome([32, 64], [False, True])
    admission_load.require_non_vacuous(outcome)  # must not raise


def test_require_non_vacuous_names_every_step_tried_when_nothing_moved() -> None:
    outcome = admission_load.decide_ramp_outcome([32, 64, 128], [False, False, False])
    with pytest.raises(admission_load.AdmissionLoadVacuousError) as excinfo:
        admission_load.require_non_vacuous(outcome)
    message = str(excinfo.value)
    assert "32" in message and "64" in message and "128" in message


# ── Nonce, subject, document generation ────────────────────────────────────


def test_make_run_nonce_default_is_lowercase_alnum() -> None:
    nonce = admission_load.make_run_nonce()
    assert nonce == nonce.lower()
    assert nonce.isalnum()
    assert len(nonce) > 0


def test_make_run_nonce_two_default_calls_differ() -> None:
    # Not a hard uniqueness guarantee (uuid4 collisions are astronomically
    # unlikely, not impossible) — this is the practical non-collision check.
    assert admission_load.make_run_nonce() != admission_load.make_run_nonce()


def test_make_run_nonce_accepts_an_injected_source() -> None:
    assert admission_load.make_run_nonce(lambda: "abc123") == "abc123"


def test_make_run_nonce_refuses_a_non_alnum_source() -> None:
    with pytest.raises(ValueError):
        admission_load.make_run_nonce(lambda: "not-alnum!")


def test_load_collection_subject_is_conformant_with_docs_collections_rule_3() -> None:
    subject = admission_load.load_collection_subject("abc123")
    assert subject == "u2mlh-load-abc123"
    assert subject == subject.lower()
    assert admission_load._SUBJECT_RE.match(subject)


def test_load_collection_subject_rejects_a_shape_the_grammar_refuses() -> None:
    # A nonce that already contains a hyphen would push the subject past
    # the documented two-to-three-word shape once decomposed by the
    # dash-separated grammar; guard by construction rather than trusting
    # every caller to pre-validate.
    with pytest.raises(ValueError):
        admission_load.load_collection_subject("has-a-hyphen-in-it")


def test_generate_document_is_unique_per_nonce_and_tag() -> None:
    a = admission_load.generate_document("nonce1", "0-0")
    b = admission_load.generate_document("nonce1", "0-1")
    c = admission_load.generate_document("nonce2", "0-0")
    assert len({a, b, c}) == 3


def test_generate_document_is_deterministic_for_the_same_inputs() -> None:
    assert admission_load.generate_document("n", "t") == admission_load.generate_document("n", "t")


def test_generate_document_respects_the_target_byte_floor() -> None:
    text = admission_load.generate_document("n", "t", target_bytes=admission_load.MIN_DOCUMENT_BYTES)
    assert len(text.encode("utf-8")) >= admission_load.MIN_DOCUMENT_BYTES


@pytest.mark.parametrize(
    "target_bytes",
    [admission_load.MIN_DOCUMENT_BYTES - 1, admission_load.MAX_DOCUMENT_BYTES + 1],
)
def test_generate_document_refuses_out_of_range_sizes(target_bytes: int) -> None:
    with pytest.raises(ValueError):
        admission_load.generate_document("n", "t", target_bytes=target_bytes)


def test_load_document_id_is_the_sha256_hex_of_the_generated_document() -> None:
    text = admission_load.generate_document("n", "t")
    expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert admission_load.load_document_id("n", "t") == expected
    assert len(expected) == 64


# ── Raw-503 evidence rollup ──────────────────────────────────────────────


def test_summarize_responses_counts_by_status_and_outcome() -> None:
    responses = [
        admission_load.RawResponse(status_code=200, retry_after=None, deadline_outcome=None, elapsed_s=0.1),
        admission_load.RawResponse(status_code=503, retry_after=5.0, deadline_outcome="refused", elapsed_s=0.05),
        admission_load.RawResponse(status_code=503, retry_after=5.0, deadline_outcome="aborted", elapsed_s=50.0),
        admission_load.RawResponse(status_code=0, retry_after=None, deadline_outcome=None, elapsed_s=1.0, error="boom"),
    ]
    summary = admission_load.summarize_responses(responses)
    assert summary["count"] == 4
    assert summary["by_status"] == {"200": 1, "503": 2, "0": 1}
    assert summary["by_deadline_outcome"] == {"refused": 1, "aborted": 1}
    assert summary["transport_errors"] == 1


def test_summarize_responses_of_an_empty_step_is_not_vacuous_silently() -> None:
    summary = admission_load.summarize_responses([])
    assert summary["count"] == 0
    assert summary["by_status"] == {}
    assert summary["by_deadline_outcome"] == {}


# ── dry-run plan is fully network-free ─────────────────────────────────────


def test_run_gate_dry_run_touches_no_network_and_reports_a_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Isolate from this box's real ~/.config/nexus/config.yml: resolve_collection_name
    # (called inside the dry-run branch) is network-free but reads local
    # config for the embed-model intent, and a real box's config could be
    # voyage-shaped with no key reachable from this test process. An empty
    # scratch config dir makes the resolution deterministic everywhere.
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    result = admission_load.run_gate(dry_run=True, nonce_source=lambda: "dryrunnonce")
    assert result["passed"] is True
    assert result["dry_run"] is True
    plan = result["plan"]
    assert plan["nonce"] == "dryrunnonce"
    assert plan["subject"] == "u2mlh-load-dryrunnonce"
    assert plan["ramp_steps"] == list(admission_load.DEFAULT_RAMP_STEPS)
    # resolve_collection_name is pure/network-free with t3=None (see its own
    # docstring) -- a dry run can still name the exact collection it would
    # write into.
    assert plan["collection_name"].startswith("knowledge__u2mlh-load-dryrunnonce__")


# ── Wrapper mode detection (real subprocess, still no network) ────────────


def test_wrapper_refuses_on_a_local_mode_box(tmp_path: Path) -> None:
    """The FIRST thing the wrapper does is mode detection; a box with no
    configured service_url must refuse (exit 2) before anything resembling
    the driver ever runs -- so this subprocess never reaches the network
    regardless of what credentials happen to be live in this box's own
    environment."""
    env = dict(os.environ)
    for var in ("NX_SERVICE_URL", "NX_SERVICE_TOKEN", "NX_SERVICE_HOST", "NX_SERVICE_PORT"):
        env.pop(var, None)
    env["NEXUS_CONFIG_DIR"] = str(tmp_path)  # empty scratch dir: no config.yml, no lease to discover

    result = subprocess.run(
        ["bash", str(_SCRIPT_PATH)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    assert "not applicable" in result.stdout
