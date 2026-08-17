# SPDX-License-Identifier: AGPL-3.0-or-later
"""Parity pin between the TWO INDEPENDENT copies of the data-token
resolution seam (code-review Sig#2, nexus-wrwb7 fix pass, nexus-ssqk9).

``RefreshableHttpStoreMixin._apply_data_token_override``
(``nexus/db/t2/_refreshable_client.py``, ridden by the nine T2 store
adopters) and ``HttpScratchStore._apply_data_token_override``
(``nexus/db/http_scratch_store.py``, T1 -- does NOT ride the mixin, has
its own bespoke twin) implement the SAME contract in two places. Nothing
mechanically stops a future fix landing in one copy only -- this file is
that mechanism, mirroring the existing
``test_refreshable_client.py::test_gateway_constants_match_reference``
precedent (same file, same "two independent copies of one contract" shape,
just for the gateway-retry schedule instead of the data-token seam).
"""
from __future__ import annotations

from unittest.mock import patch

from nexus.db.http_scratch_store import HttpScratchStore
from nexus.db.t2._refreshable_client import RefreshableHttpStoreMixin


def _reset_manager() -> None:
    from nexus.db.data_token import reset_data_token_manager
    reset_data_token_manager()


class TestDataTokenSeamParity:
    """Mechanical parity guard between the mixin's and HttpScratchStore's
    independent ``_apply_data_token_override`` implementations."""

    def test_both_seams_tolerate_new_bypass_with_no_token_pinned_attr(self) -> None:
        """The EXACT AttributeError class the nexus-wrwb7 session fixed live:
        an instance built via ``cls.__new__(cls)`` (bypassing ``__init__`` --
        a real pattern in both stores' own test suites, e.g.
        ``tests/test_scratch_cmd_service_errors.py``) has NO
        ``_token_pinned`` attribute at all. BOTH override methods must
        default to pinned/no-op (``getattr(self, "_token_pinned", True)``),
        never raise ``AttributeError`` -- and this was fixed in the mixin
        PROACTIVELY (latent, not yet triggered there) precisely because the
        two copies share this failure mode. If a future edit reintroduces a
        bare ``self._token_pinned`` in EITHER copy, this test fails loudly
        for that copy specifically.
        """
        mixin_obj = RefreshableHttpStoreMixin.__new__(RefreshableHttpStoreMixin)
        mixin_obj._base_url = "http://127.0.0.1:9999"  # noqa: SLF001 — building a bare test double, mirrors __new__-bypass fixtures elsewhere
        mixin_obj._tenant = "default"
        mixin_obj._token = "original-token"

        scratch_obj = HttpScratchStore.__new__(HttpScratchStore)
        scratch_obj._base_url = "http://127.0.0.1:9999"  # noqa: SLF001
        scratch_obj._tenant = "default"
        scratch_obj._headers = {"Authorization": "Bearer original-token"}

        mixin_obj._apply_data_token_override()  # must not raise AttributeError
        scratch_obj._apply_data_token_override()  # must not raise AttributeError

        # getattr(..., True) means "treat as pinned" -- neither mutates.
        assert mixin_obj._token == "original-token"
        assert scratch_obj._headers["Authorization"] == "Bearer original-token"

    def test_both_seams_skip_when_explicitly_pinned(self) -> None:
        """Explicit ``_token_pinned = True`` (the normal "caller supplied a
        token" case) must no-op identically in both copies, even with a
        mint_token configured and ready to mint."""
        _reset_manager()
        try:
            with patch("nexus.db.data_token.get_data_token_manager") as mock_get_manager:
                mock_get_manager.return_value.bearer_for.return_value = "SHOULD-NOT-BE-USED"

                mixin_obj = RefreshableHttpStoreMixin.__new__(RefreshableHttpStoreMixin)
                mixin_obj._base_url = "http://127.0.0.1:9999"  # noqa: SLF001
                mixin_obj._tenant = "default"
                mixin_obj._token = "pinned-token"
                mixin_obj._token_pinned = True

                scratch_obj = HttpScratchStore.__new__(HttpScratchStore)
                scratch_obj._base_url = "http://127.0.0.1:9999"  # noqa: SLF001
                scratch_obj._tenant = "default"
                scratch_obj._headers = {"Authorization": "Bearer pinned-token"}
                scratch_obj._token_pinned = True

                mixin_obj._apply_data_token_override()
                scratch_obj._apply_data_token_override()

                assert mixin_obj._token == "pinned-token"
                assert scratch_obj._headers["Authorization"] == "Bearer pinned-token"
                mock_get_manager.return_value.bearer_for.assert_not_called()
        finally:
            _reset_manager()

    def test_both_seams_apply_the_minted_token_identically_when_unpinned(self) -> None:
        """The positive case: with ``_token_pinned = False`` and a
        configured mint_token, BOTH copies must substitute the manager's
        minted bearer -- proving the two independent implementations still
        agree on the CONTRACT, not just the crash-safety edge case above."""
        _reset_manager()
        try:
            with patch("nexus.db.data_token.get_data_token_manager") as mock_get_manager:
                mock_get_manager.return_value.bearer_for.return_value = "minted-data-token"

                mixin_obj = RefreshableHttpStoreMixin.__new__(RefreshableHttpStoreMixin)
                mixin_obj._base_url = "http://127.0.0.1:9999"  # noqa: SLF001
                mixin_obj._tenant = "default"
                mixin_obj._token = "stale-token"
                mixin_obj._token_pinned = False

                scratch_obj = HttpScratchStore.__new__(HttpScratchStore)
                scratch_obj._base_url = "http://127.0.0.1:9999"  # noqa: SLF001
                scratch_obj._tenant = "default"
                scratch_obj._headers = {"Authorization": "Bearer stale-token"}
                scratch_obj._token_pinned = False

                mixin_obj._apply_data_token_override()
                scratch_obj._apply_data_token_override()

                assert mixin_obj._token == "minted-data-token"
                assert scratch_obj._headers["Authorization"] == "Bearer minted-data-token"
        finally:
            _reset_manager()
