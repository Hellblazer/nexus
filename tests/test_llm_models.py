# SPDX-License-Identifier: AGPL-3.0-or-later
"""The price table, the alias map and the shared usage helper (nexus-2k66t.3 P2)."""
from __future__ import annotations

import pytest

from nexus.operators import model_tiers
from nexus.operators.llm_models import (
    FORCED_TOOL_UNSUPPORTED,
    PRICES,
    canonical_model_id,
    price_for,
    supports_forced_tool_choice,
)
from nexus.operators.llm_types import (
    DispatchUsage,
    OperatorAPIError,
    OperatorError,
    ambient_usage_sink,
    empty_usage,
    record_usage,
)
from nexus.operators.model_tiers import _API_MODEL_IDS, STRONG_DEFAULT_ALIAS, resolve_api_model


class TestPriceTable:
    def test_list_prices_for_the_five_five_family(self) -> None:
        # Pinned to the published list (Context7 pricing page, 2026-10-23). A drift here
        # is a deliberate edit of llm_models.PRICES plus this test, never an accident.
        got = {m: (p.input, p.output, p.cache_write, p.cache_read) for m, p in PRICES.items()}
        assert got["claude-haiku-5-5"] == (0.10, 0.50, 0.125, 0.01)
        assert got["claude-sonnet-5-5"] == (2.0, 10.0, 2.5, 0.10)
        assert got["claude-opus-5-5"] == (4.0, 20.0, 5.0, 0.20)
        assert got["claude-fable-5-1"] == (10.0, 50.0, 12.5, 0.25)
        assert got["claude-haiku-4-5"] == (1.0, 5.0, 1.25, 0.10)

    def test_every_alias_target_is_priced(self) -> None:
        # An alias that maps to an unpriced id would make every call's cost None.
        for alias, model_id in _API_MODEL_IDS.items():
            assert price_for(model_id) is not None, (alias, model_id)

    def test_cost_arithmetic(self) -> None:
        p = price_for("claude-sonnet-5-5")
        assert p.cost(input_tokens=1_000_000, output_tokens=0) == pytest.approx(2.0)
        assert p.cost(input_tokens=0, output_tokens=1_000_000) == pytest.approx(10.0)
        assert p.cost(input_tokens=0, output_tokens=0, cache_creation_tokens=1_000_000) == pytest.approx(2.5)
        assert p.cost(input_tokens=0, output_tokens=0, cache_read_tokens=1_000_000) == pytest.approx(0.10)

    def test_haiku_five_five_prices_a_long_prompt_at_the_higher_row(self) -> None:
        p = price_for("claude-haiku-5-5")
        short = p.cost(input_tokens=100_000, output_tokens=1000)
        long = p.cost(input_tokens=100_001, output_tokens=1000)
        assert short == pytest.approx((100_000 * 0.10 + 1000 * 0.50) / 1e6)
        assert long == pytest.approx((100_001 * 0.50 + 1000 * 2.50) / 1e6)
        # cached tokens count toward the prompt length that picks the row
        assert p.cost(input_tokens=10, output_tokens=0, cache_read_tokens=100_000) == pytest.approx(
            (10 * 0.50 + 100_000 * 0.05) / 1e6)

    def test_unknown_and_blank_ids_have_no_price(self) -> None:
        assert price_for("claude-nonexistent-9") is None and price_for("") is None and price_for(None) is None

    def test_dated_snapshots_resolve_to_their_base(self) -> None:
        assert canonical_model_id("claude-haiku-4-5-20251001") == "claude-haiku-4-5"
        assert price_for("claude-haiku-4-5-20251001") is price_for("claude-haiku-4-5")
        assert canonical_model_id("claude-opus-5-5") == "claude-opus-5-5"

    def test_forced_tool_choice_capability(self) -> None:
        for m in FORCED_TOOL_UNSUPPORTED:
            assert supports_forced_tool_choice(m) is False
        assert supports_forced_tool_choice("claude-haiku-5-5") is True
        assert supports_forced_tool_choice("claude-opus-5") is True  # the older opus accepts it
        assert supports_forced_tool_choice("claude-sonnet-5-5") is False


class TestAliasMap:
    def test_aliases_map_to_current_ids(self) -> None:
        assert resolve_api_model("haiku") == "claude-haiku-5-5"
        assert resolve_api_model("sonnet") == "claude-sonnet-5-5"
        assert resolve_api_model("opus") == "claude-opus-5-5"
        assert resolve_api_model("Fable") == "claude-fable-5-1"

    def test_none_and_blank_are_the_strong_default(self) -> None:
        assert resolve_api_model(None) == resolve_api_model("") == resolve_api_model("  ") == resolve_api_model(STRONG_DEFAULT_ALIAS)

    def test_full_ids_pass_through(self) -> None:
        assert resolve_api_model("claude-haiku-4-5-20251001") == "claude-haiku-4-5-20251001"
        assert resolve_api_model("some-pinned-id") == "some-pinned-id"

    def test_the_map_lives_in_model_tiers(self) -> None:
        assert model_tiers._API_MODEL_IDS is _API_MODEL_IDS


class TestRecordUsage:
    @staticmethod
    def _usage(model="claude-haiku-5-5") -> DispatchUsage:
        return DispatchUsage(model=model, cost_usd=0.5, input_tokens=1, output_tokens=2,
                             cache_creation_input_tokens=None, cache_read_input_tokens=None,
                             duration_ms=1, duration_api_ms=1, num_turns=1)

    def test_noop_with_no_sink(self) -> None:
        u = self._usage()
        assert record_usage(u, usage_sink=None, requested_model=None, operator=None) is u

    def test_one_instance_to_both_sinks(self) -> None:
        explicit, ambient = [], []
        u = self._usage()
        with ambient_usage_sink(ambient):
            record_usage(u, usage_sink=explicit, requested_model="haiku", operator="op")
        assert explicit == ambient == [u] and explicit[0] is ambient[0] is u

    def test_ambient_alone(self) -> None:
        ambient = []
        with ambient_usage_sink(ambient):
            record_usage(self._usage(), usage_sink=None, requested_model=None, operator=None)
        assert len(ambient) == 1

    def test_absent_usage_records_the_absent_factory_and_returns_none(self) -> None:
        sink = []
        sentinel = self._usage("sentinel")
        assert record_usage(None, usage_sink=sink, requested_model="haiku", operator=None, absent=lambda: sentinel) is None
        assert sink == [sentinel]

    def test_absent_usage_without_a_factory_is_all_none_never_zero(self) -> None:
        sink = []
        record_usage(None, usage_sink=sink, requested_model=None, operator=None)
        assert sink == [empty_usage()] and sink[0].cost_usd is None and sink[0].input_tokens is None

    def test_absent_factory_is_not_called_when_no_sink_is_active(self) -> None:
        record_usage(None, usage_sink=None, requested_model=None, operator=None, absent=lambda: 1 / 0)

    def test_drift_fires_with_or_without_a_sink_and_only_for_a_requested_model(self) -> None:
        seen = []
        drift = lambda req, usage, op: seen.append((req, usage.model, op))  # noqa: E731
        record_usage(self._usage("claude-sonnet-5-5"), usage_sink=None, requested_model="haiku", operator="o", warn_drift=drift)
        record_usage(self._usage(), usage_sink=None, requested_model=None, operator="o", warn_drift=drift)
        record_usage(None, usage_sink=None, requested_model="haiku", operator="o", warn_drift=drift)
        assert seen == [("haiku", "claude-sonnet-5-5", "o")]

    def test_default_drift_warning_names_the_family_mismatch(self) -> None:
        import structlog

        with structlog.testing.capture_logs() as logs:
            record_usage(self._usage("claude-sonnet-5-5"), usage_sink=None, requested_model="haiku", operator="op")
            record_usage(self._usage("claude-haiku-5-5"), usage_sink=None, requested_model="haiku", operator="op")
        drift = [e for e in logs if e["event"] == "model_family_drift"]
        assert len(drift) == 1 and drift[0]["canonical_model"] == "claude-sonnet-5-5"


class TestApiErrorClassification:
    @pytest.mark.parametrize("status,transient", [
        (None, True), (429, True), (529, True), (500, True), (502, True), (503, True), (504, True),
        (400, False), (401, False), (402, False), (403, False), (404, False), (413, False),
    ])
    def test_transient_is_exactly_what_a_retry_can_cure(self, status, transient) -> None:
        assert OperatorAPIError("x", status=status).transient is transient

    def test_the_providers_should_retry_header_overrides(self) -> None:
        assert OperatorAPIError("x", status=500, retryable=False).transient is False
        assert OperatorAPIError("x", status=400, retryable=True).transient is True

    def test_it_is_an_operator_error(self) -> None:
        assert issubclass(OperatorAPIError, OperatorError)


class TestThePriceTableSaysWhenItWasRead:
    def test_prices_as_of_is_an_iso_date(self) -> None:
        import datetime

        from nexus.operators.llm_models import PRICES_AS_OF

        assert datetime.date.fromisoformat(PRICES_AS_OF).year >= 2026
