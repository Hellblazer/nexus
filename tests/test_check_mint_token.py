# SPDX-License-Identifier: AGPL-3.0-or-later
"""nexus-wrwb7: ``nx doctor``'s mint_token self-minting check (health.py
``_check_mint_token``).

Degrades cleanly in all four states: unconfigured (loud skip, ok), endpoint
unreachable (soft warning), mint failure (soft warning), success (ok, with
the pre-cutover neutral-wording caveat). Never crashes, never false-clean.
"""
from __future__ import annotations


def _label(results):
    matches = [r for r in results if "mint_token" in r.label.lower() or "self-minting" in r.label.lower()]
    assert len(matches) == 1, results
    return matches[0]


def test_unconfigured_is_a_loud_ok_skip(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("NX_MINT_TOKEN", raising=False)

    from nexus.health import _check_mint_token

    result = _label(_check_mint_token())
    assert result.ok is True
    assert "not configured" in result.detail.lower()
    assert "optional" in result.detail.lower()


def test_configured_but_endpoint_unresolvable_warns(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("NX_MINT_TOKEN", "mint-cred")

    def _boom():
        raise RuntimeError("nexus-service endpoint is not resolvable")

    monkeypatch.setattr(
        "nexus.db.service_endpoint.resolve_service_endpoint_with_evidence_gate", _boom
    )

    from nexus.health import _check_mint_token

    result = _label(_check_mint_token())
    assert result.ok is False
    assert result.warn is True, "endpoint-unresolvable must be a soft warning, never fatal"
    assert "not resolvable" in result.detail.lower()


def test_configured_but_mint_fails_warns(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("NX_MINT_TOKEN", "mint-cred")

    monkeypatch.setattr(
        "nexus.db.service_endpoint.resolve_service_endpoint_with_evidence_gate",
        lambda: ("http://127.0.0.1:9999", "static-tok"),
    )

    from nexus.db.data_token import DataTokenMintError

    class _FailingManager:
        def has_live_token(self, base_url, tenant):
            return False

        def has_fresh_lease(self, base_url, tenant):
            return False

        def bearer_for(self, base_url, tenant):
            raise DataTokenMintError("data-token mint failed (401): invalid credential")

    # critic S3 (nexus-ssqk9): the check now routes through the process-wide
    # singleton accessor, not the DataTokenManager class directly.
    monkeypatch.setattr("nexus.db.data_token.get_data_token_manager", lambda: _FailingManager())

    from nexus.health import _check_mint_token

    result = _label(_check_mint_token())
    assert result.ok is False
    assert result.warn is True, "a mint failure must be a soft warning, never fatal, never false-clean"
    assert "failed" in result.detail.lower()
    assert "127.0.0.1:9999" in result.detail


def test_configured_and_mint_succeeds_reports_neutrally(monkeypatch, tmp_path):
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("NX_MINT_TOKEN", "mint-cred")

    monkeypatch.setattr(
        "nexus.db.service_endpoint.resolve_service_endpoint_with_evidence_gate",
        lambda: ("http://127.0.0.1:9999", "static-tok"),
    )

    class _SucceedingManager:
        def has_live_token(self, base_url, tenant):
            return False

        def has_fresh_lease(self, base_url, tenant):
            return False

        def bearer_for(self, base_url, tenant):
            return "minted-data-token"

        def granted_ttl_seconds(self, base_url, tenant):
            return 300.0

    monkeypatch.setattr("nexus.db.data_token.get_data_token_manager", lambda: _SucceedingManager())

    from nexus.health import _check_mint_token

    result = _label(_check_mint_token())
    assert result.ok is True
    assert "127.0.0.1:9999" in result.detail
    # Neutral wording: success through the edge does not (yet) prove THIS
    # credential's own authority pre-cutover (RDR-005 2a staged cutover).
    assert "does not" in result.detail.lower() or "does NOT" in result.detail
    assert "authority" in result.detail.lower()


def test_success_line_reports_minted_when_no_live_token_cached(monkeypatch, tmp_path):
    """critic S1: the success line must report the GRANTED TTL, matching
    what cli-reference.md and this check's own docstring already claim."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("NX_MINT_TOKEN", "mint-cred")
    monkeypatch.setattr(
        "nexus.db.service_endpoint.resolve_service_endpoint_with_evidence_gate",
        lambda: ("http://127.0.0.1:9999", "static-tok"),
    )

    class _FreshMintManager:
        def has_live_token(self, base_url, tenant):
            return False

        def has_fresh_lease(self, base_url, tenant):
            return False

        def bearer_for(self, base_url, tenant):
            return "minted-data-token"

        def granted_ttl_seconds(self, base_url, tenant):
            return 300.0

    monkeypatch.setattr("nexus.db.data_token.get_data_token_manager", lambda: _FreshMintManager())

    from nexus.health import _check_mint_token

    result = _label(_check_mint_token())
    assert result.ok is True
    assert "minted a fresh" in result.detail
    assert "300" in result.detail  # granted TTL


def test_success_line_reports_reused_when_a_live_token_is_cached(monkeypatch, tmp_path):
    """critic S3 (nexus-lgiqw residue class): the doctor check must not
    mint a fresh token on every invocation -- when the manager already has
    a live cached token, the success line must say REUSED, not MINTED."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("NX_MINT_TOKEN", "mint-cred")
    monkeypatch.setattr(
        "nexus.db.service_endpoint.resolve_service_endpoint_with_evidence_gate",
        lambda: ("http://127.0.0.1:9999", "static-tok"),
    )

    class _ReuseManager:
        def has_live_token(self, base_url, tenant):
            return True

        def bearer_for(self, base_url, tenant):
            return "cached-data-token"

        def granted_ttl_seconds(self, base_url, tenant):
            return 187.0

    monkeypatch.setattr("nexus.db.data_token.get_data_token_manager", lambda: _ReuseManager())

    from nexus.health import _check_mint_token

    result = _label(_check_mint_token())
    assert result.ok is True
    assert "reused the cached" in result.detail
    assert "187" in result.detail


def test_success_line_reports_reused_from_lease_file_distinctly(monkeypatch, tmp_path):
    """nexus-9c7t9: when the process has NO in-process cache hit but the
    cross-process lease file is fresh, the success line must say "reused
    the cached (lease file)" -- distinct from both the in-process-reuse
    and the minted-fresh wording, so a real `nx doctor` subprocess (whose
    in-process cache is always empty) can genuinely report reuse."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("NX_MINT_TOKEN", "mint-cred")
    monkeypatch.setattr(
        "nexus.db.service_endpoint.resolve_service_endpoint_with_evidence_gate",
        lambda: ("http://127.0.0.1:9999", "static-tok"),
    )

    class _LeaseReuseManager:
        def has_live_token(self, base_url, tenant):
            return False

        def has_fresh_lease(self, base_url, tenant):
            return True

        def bearer_for(self, base_url, tenant):
            return "leased-data-token"

        def granted_ttl_seconds(self, base_url, tenant):
            return 250.0

    monkeypatch.setattr("nexus.db.data_token.get_data_token_manager", lambda: _LeaseReuseManager())

    from nexus.health import _check_mint_token

    result = _label(_check_mint_token())
    assert result.ok is True
    assert "reused the cached (lease file)" in result.detail
    assert "(in-process)" not in result.detail
    assert "250" in result.detail


def test_success_line_reports_in_process_reuse_distinctly(monkeypatch, tmp_path):
    """The in-process-reuse wording must be distinguishable from the
    lease-file-reuse wording too (nexus-9c7t9)."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("NX_MINT_TOKEN", "mint-cred")
    monkeypatch.setattr(
        "nexus.db.service_endpoint.resolve_service_endpoint_with_evidence_gate",
        lambda: ("http://127.0.0.1:9999", "static-tok"),
    )

    class _InProcessReuseManager:
        def has_live_token(self, base_url, tenant):
            return True

        def has_fresh_lease(self, base_url, tenant):
            # Must never even be consulted when has_live_token is True —
            # returning True here would wrongly flip the wording if the
            # short-circuit in health.py regressed.
            raise AssertionError("has_fresh_lease must not be called when has_live_token is True")

        def bearer_for(self, base_url, tenant):
            return "cached-data-token"

        def granted_ttl_seconds(self, base_url, tenant):
            return 187.0

    monkeypatch.setattr("nexus.db.data_token.get_data_token_manager", lambda: _InProcessReuseManager())

    from nexus.health import _check_mint_token

    result = _label(_check_mint_token())
    assert result.ok is True
    assert "reused the cached (in-process)" in result.detail


def test_repeated_doctor_invocations_mint_at_most_once(monkeypatch, tmp_path):
    """critic S3 (nexus-lgiqw): end-to-end against a REAL DataTokenManager
    (not a fake) -- two `_check_mint_token()` calls back to back must mint
    exactly once, proving the fix routes through the process-wide singleton
    rather than constructing a throwaway manager per invocation."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("NX_MINT_TOKEN", "mint-cred")
    monkeypatch.setattr(
        "nexus.db.service_endpoint.resolve_service_endpoint_with_evidence_gate",
        lambda: ("http://127.0.0.1:9999", "static-tok"),
    )

    from nexus.db.data_token import DataTokenManager

    mint_calls: list[dict] = []

    def poster(url, headers, body):
        mint_calls.append(body)
        return 200, {"data_token": "tok-1", "expires_in_seconds": 300}, {}

    manager = DataTokenManager(poster=poster, mint_credential=lambda: "mint-cred")
    monkeypatch.setattr("nexus.db.data_token.get_data_token_manager", lambda: manager)

    from nexus.health import _check_mint_token

    first = _label(_check_mint_token())
    second = _label(_check_mint_token())

    assert len(mint_calls) == 1
    assert "minted a fresh" in first.detail
    assert "reused the cached" in second.detail


def test_doctor_reports_reused_from_lease_end_to_end(monkeypatch, tmp_path):
    """nexus-9c7t9 end-to-end: a REAL DataTokenManager (not a fake) whose
    in-process cache is empty but whose lease file (written by an earlier
    "process") is fresh reports "reused the cached (lease file)" -- the
    scenario a second `nx <cmd>` subprocess actually hits."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("NX_MINT_TOKEN", "mint-cred")
    monkeypatch.setattr(
        "nexus.db.service_endpoint.resolve_service_endpoint_with_evidence_gate",
        lambda: ("http://127.0.0.1:9999", "static-tok"),
    )

    from nexus.db.data_token import DataTokenManager

    mint_calls: list[dict] = []

    def poster(url, headers, body):
        mint_calls.append(body)
        return 200, {"data_token": "tok-1", "expires_in_seconds": 300}, {}

    # First "process": mints and writes the lease file.
    first_manager = DataTokenManager(poster=poster, mint_credential=lambda: "mint-cred")
    first_manager.bearer_for("http://127.0.0.1:9999", "default")
    assert len(mint_calls) == 1

    # Second "process": a FRESH manager (empty in-process cache), same
    # config_dir (real nexus_config_dir(), redirected by NEXUS_CONFIG_DIR
    # above) — must borrow the lease, never mint again.
    second_manager = DataTokenManager(poster=poster, mint_credential=lambda: "mint-cred")
    monkeypatch.setattr("nexus.db.data_token.get_data_token_manager", lambda: second_manager)

    from nexus.health import _check_mint_token

    result = _label(_check_mint_token())
    assert len(mint_calls) == 1  # no second mint
    assert result.ok is True
    assert "reused the cached (lease file)" in result.detail


def test_never_crashes_on_unexpected_exception(monkeypatch, tmp_path):
    """A non-DataTokenMintError exception from the endpoint-resolution half
    is caught by the broad best-effort guard; the check must degrade to a
    warning, never propagate and crash `nx doctor`."""
    monkeypatch.setenv("NEXUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("NX_MINT_TOKEN", "mint-cred")

    def _boom():
        raise ValueError("unexpected parse error")

    monkeypatch.setattr(
        "nexus.db.service_endpoint.resolve_service_endpoint_with_evidence_gate", _boom
    )

    from nexus.health import _check_mint_token

    result = _label(_check_mint_token())
    assert result.ok is False
    assert result.warn is True
