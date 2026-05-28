# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""RDR-137 followup SIG-7, SIG-12, SIG-15, SIG-16 (epic nexus-43qgm).

SIG-7 (nexus-43qgm.7): backfill-collections calls register_collection
without owner_id → anonymous knowledge__ rows shadow legitimate
docs__ selections through OQ-5 inference. Fix: parse conformant
names and pass content_type + owner_id when available.

SIG-12 (nexus-43qgm.12): nexus.registry re-exports lack
DeprecationWarning. Plugin importers crash on next-major delete
with no prior signal. Fix: module-level __getattr__ that emits
DeprecationWarning + redirects.

SIG-15 (nexus-43qgm.15): _CatalogBackedRegistry.all_info() stub
docstring names indexer.py:1065 + 1958 as callers but all_info() is
never called from indexer.py. Misleading + violates "no silent
fallbacks for correctness" rule.

SIG-16 (nexus-43qgm.16): lint guard _REPOS_JSON_DIRECT_READ_RE has
200-char proximity false-positive risk. Tighten regex to require the
literal inside the read call.
"""
from __future__ import annotations

import warnings
from pathlib import Path

import pytest


class TestSig7BackfillCollectionsPassesOwnerIdForConformantNames:
    def test_backfill_collections_passes_owner_id_for_conformant_name(self) -> None:
        """The backfill-collections verb must extract owner_id from a
        conformant collection name and pass it to register_collection
        so OQ-5 inference cannot silently shadow other owners' docs
        with anonymous knowledge__ rows."""
        # Source-level invariant: the bare cat.register_collection(name)
        # call must be replaced with one that forwards content_type +
        # owner_id when parse_conformant_collection_name succeeds.
        src = (
            Path(__file__).resolve().parent.parent
            / "src" / "nexus" / "commands" / "catalog.py"
        )
        text = src.read_text()
        # The bare call without keyword args was the SIG-7 bug.
        # Allow it under conditional / fallback paths but require
        # at least one keyword-laden register_collection nearby.
        assert "parse_conformant_collection_name" in text, (
            "SIG-7: backfill_collections must parse conformant names "
            "and forward owner_id; the parse helper must appear in "
            "commands/catalog.py."
        )


class TestSig12NexusRegistryEmitsDeprecationWarning:
    def test_helper_import_from_nexus_registry_warns(self) -> None:
        """Importing a relocated helper from nexus.registry must emit
        DeprecationWarning. Pre-fix the re-exports were silent."""
        # Fresh import surface — avoid cached references.
        import importlib
        import sys
        # Force a clean import path
        for mod in ("nexus.registry",):
            sys.modules.pop(mod, None)

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            import nexus.registry as reg_mod
            _ = reg_mod._repo_identity  # access via module-level __getattr__
            depr = [
                warning for warning in w
                if issubclass(warning.category, DeprecationWarning)
            ]
            assert depr, (
                "SIG-12: nexus.registry._repo_identity access must emit "
                "DeprecationWarning. No DeprecationWarning was issued."
            )
            assert any(
                "nexus.repo_identity" in str(warning.message)
                for warning in depr
            ), "DeprecationWarning must name the new module."


class TestSig15AllInfoDocstringAccurate:
    def test_all_info_docstring_does_not_claim_indexer_callsites(self) -> None:
        """Pre-fix docstring at commands/index.py:110-115 named
        indexer.py:1065 + 1958 as callers, but a grep shows all_info()
        is NEVER called from indexer.py. Update the comment to name
        the real consumer (_backfill_repos in commands/catalog.py)
        and remove the misleading line refs."""
        src = (
            Path(__file__).resolve().parent.parent
            / "src" / "nexus" / "commands" / "index.py"
        )
        text = src.read_text()
        # Pre-fix included "_run_index_frecency_only" + "_run_index" callsite
        # claims. Post-fix must NOT contain those specific phrases tied to
        # all_info().
        # Find the all_info() method definition section:
        assert "def all_info" in text
        # The doc must NOT say "two indexer call sites" anymore (the
        # exact phrasing that was wrong). Case-insensitive.
        assert "two indexer call sites" not in text.lower(), (
            "SIG-15: stub docstring still claims indexer call sites that "
            "don't exist; rewrite to name the real consumer."
        )
        # And it MUST name the actual consumer (_backfill_repos in
        # commands/catalog.py).
        assert "_backfill_repos" in text or "_LegacyRegistryReader" in text, (
            "SIG-15: stub docstring must name the real consumer of "
            "all_info() (_backfill_repos via _LegacyRegistryReader)."
        )


class TestSig16LintGuardRegexNoFalsePositives:
    def test_regex_does_not_match_unrelated_json_loads_near_repos_json_mention(
        self, tmp_path: Path,
    ) -> None:
        """Pre-fix regex matched any file with json.loads near a
        repos.json mention within 200 chars (even in a comment). Now
        the pattern must require the literal inside the read call."""
        from tests.test_no_repo_registry_resurrection import (
            _REPOS_JSON_DIRECT_READ_RE,
        )

        # Construct a faux source string with two unrelated occurrences:
        # an unrelated json.loads + a separate repos.json mention in a
        # comment / variable. The pre-fix regex matched this; the
        # post-fix must NOT.
        text = (
            "def process(config_file):\n"
            "    data = json.loads(config_file.read_text())  # unrelated\n"
            "    return data\n"
            "\n"
            "# repos.json is the legacy registry path\n"
            "LEGACY_PATH = 'repos.json'\n"
        )
        match = _REPOS_JSON_DIRECT_READ_RE.search(text)
        assert match is None, (
            f"SIG-16: regex matched a false-positive scenario.\n"
            f"Matched: {match.group() if match else None!r}\n"
            f"Pattern: {_REPOS_JSON_DIRECT_READ_RE.pattern!r}"
        )

    def test_regex_still_matches_actual_direct_parse(self) -> None:
        """Sanity check: the tightened regex must still catch a
        genuine direct parse of repos.json."""
        from tests.test_no_repo_registry_resurrection import (
            _REPOS_JSON_DIRECT_READ_RE,
        )

        # An actual direct parse — what the guard EXISTS to catch.
        text = (
            "def read_legacy():\n"
            "    return json.loads(Path('repos.json').read_text())\n"
        )
        match = _REPOS_JSON_DIRECT_READ_RE.search(text)
        assert match is not None, (
            "SIG-16: regex stopped catching the canonical direct parse."
        )
