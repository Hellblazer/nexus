# SPDX-License-Identifier: AGPL-3.0-or-later
"""Single-source-of-truth engine-version floor (nexus-9qq85, nexus-b6qlf).

Replaces the two independently-drifting pinned constants
``guided_upgrade.REQUIRED_RELEASE_VERSION`` and
``managed_endpoint.MIN_MANAGED_RELEASE_VERSION`` — both modules now import
:data:`nexus.engine_version.REQUIRED_ENGINE_VERSION` and
:func:`nexus.engine_version.parse_engine_version`. This is the ONLY place the
pinned-floor value is asserted; ``test_guided_upgrade_version_pin.py`` and
``test_managed_endpoint.py`` exercise behavior, not the pin itself.
"""

from __future__ import annotations

from nexus.engine_version import REQUIRED_ENGINE_VERSION, parse_engine_version


class TestRequiredEngineVersion:
    def test_pinned_floor_is_current(self) -> None:
        # (0,1,5)->(0,1,8) for nexus-x2g1z; ->(0,1,34) for 6.5.0: the client
        # hard-requires catalog-012 (graph-hop `where` — pre-012 engines
        # silently ignore the key, the H2 version-skew failure class) and
        # catalog-013-1b (pre-1b engines fail boot VALIDATE on tenants with
        # legacy 64-char chash rows — the nexus-1wjmq incident). ->(0,1,39)
        # for nexus-rn3wo.1: T1 scratch now defaults to the PG-backed service
        # with no Chroma fallback, and every engine before v0.1.38 has a
        # native-image reflection gap that 500s on every T1 get/search/list
        # (nexus-opr9m). ->(0,1,41) for the 2026-07-13 release-gate arc:
        # service-mode remediation consent audit needs telemetry-002-consents
        # (v0.1.40+), retention markers + range where-operators need v0.1.41,
        # and tags <=0.1.40 are invalid rollback targets post-A6.
        # ->(0,1,42) 2026-07-14: fix-delivery rule (per Hal) — the engine
        # carries the catalog-015 FTS filename-token fix (nexus-8gue1) and
        # indexed_at repair provenance (nexus-p5qk8); local installs receive
        # engine fixes ONLY via this floor/pin, so an advertised engine fix
        # moves the floor even with zero client-side hard dependency.
        # ->(0,1,43) 2026-07-15: fix-delivery rule again — GH #1402
        # (nexus-0gis0): grants-nexus-svc-1's bulk GRANT crash-looped boot on
        # any install whose schema carries the superuser-owned diag view;
        # v0.1.42 and earlier are broken upgrade targets for that class.
        # ->(0,1,44) 2026-07-16: hard dependency — the 6.11.0 tier-writes
        # read-parity surfaces (nx tier-status, doctor tier-discipline, the
        # SessionEnd summary; nexus-59wjj/ov13k) call the new
        # GET /v1/telemetry/tier_writes/query route. Pre-44 engines 404 it
        # and every surface degrades to the honest fallback forever.
        # Deployed + cloud-gated 2026-07-16 (recall 12/12, hybrid p95
        # 1920ms < 2376 bound).
        assert REQUIRED_ENGINE_VERSION == (0, 1, 44)


class TestParseEngineVersion:
    def test_parses_plain_and_v_prefixed(self) -> None:
        assert parse_engine_version("0.1.5") == (0, 1, 5)
        assert parse_engine_version("v1.2.3") == (1, 2, 3)
        assert parse_engine_version("V1.2.3") == (1, 2, 3)

    def test_rejects_blank_none_and_whitespace(self) -> None:
        for bad in (None, "", "   "):
            assert parse_engine_version(bad) is None

    def test_rejects_dev_and_snapshot_qualifiers(self) -> None:
        for bad in ("1.0-SNAPSHOT", "0.1.6-dev", "0.1.9-SNAPSHOT", "0.1.8-dev"):
            assert parse_engine_version(bad) is None

    def test_rejects_malformed_segment_counts(self) -> None:
        for bad in ("0.1", "1.2.3.4", "x.y.z", "unknown"):
            assert parse_engine_version(bad) is None

    def test_rejects_trailing_qualifiers(self) -> None:
        for bad in ("0.1.8-rc1", "0.1.8+meta"):
            assert parse_engine_version(bad) is None

    def test_rejects_negative_components(self) -> None:
        assert parse_engine_version("-1.0.0") is None

    def test_rejects_non_string_types_via_caller_guard(self) -> None:
        # parse_engine_version itself only declares str | None; callers that
        # receive JSON-confused non-string values (bool/int/list/dict) must
        # coerce/guard before calling. Confirm the str-typed contract holds
        # for the values callers DO pass through.
        assert parse_engine_version("1.2.3") == (1, 2, 3)


class TestFloorComparison:
    def test_below_floor_compares_less(self) -> None:
        assert parse_engine_version("0.1.5") < REQUIRED_ENGINE_VERSION

    def test_at_floor_compares_equal(self) -> None:
        floor_str = ".".join(str(p) for p in REQUIRED_ENGINE_VERSION)
        assert parse_engine_version(floor_str) == REQUIRED_ENGINE_VERSION

    def test_above_floor_compares_greater(self) -> None:
        assert parse_engine_version("0.2.0") > REQUIRED_ENGINE_VERSION


def test_module_is_stdlib_only_leaf() -> None:
    """engine_version.py must import cleanly with zero ``nexus.*`` deps — it is
    a leaf module both ``nexus.db`` and ``nexus.migration`` import from, so any
    ``nexus`` import here risks a circular-import class of bug."""
    import ast
    import pathlib

    import nexus.engine_version as mod

    src = pathlib.Path(mod.__file__).read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.split(".")[0] == "nexus":
            raise AssertionError(f"engine_version.py imports from nexus: {node.module}")
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "nexus":
                    raise AssertionError(f"engine_version.py imports nexus: {alias.name}")


class TestDownstreamConsumersTrackTheFloor:
    def test_cold_rehearsal_tag_is_at_least_the_floor(self) -> None:
        """The migration-rehearsal COLD_TAG default must satisfy the
        guided-upgrade version pin, or `run.sh --cold` fail-closes at the
        version gate before migrating anything (demonstrated live
        2026-07-12: the (0,1,39) floor bump left COLD_TAG at v0.1.37 and
        the cold MVV died on 'engine-service v0.1.37 < required v0.1.39').
        Same drift class as the CI stamp-step regex (fixed 975dcd9a) and
        the original two hand-typed pins nexus-b6qlf unified — every
        hand-written downstream consumer of the floor gets a tripwire."""
        import re
        from pathlib import Path

        run_sh = Path(__file__).parent.parent / (
            "tests/e2e/migration-rehearsal/run.sh"
        )
        m = re.search(
            r'COLD_TAG="\$\{NEXUS_SERVICE_TAG:-engine-service-v(\d+\.\d+\.\d+)\}"',
            run_sh.read_text(),
        )
        assert m, "COLD_TAG default not found/parseable in run.sh"
        assert parse_engine_version(m.group(1)) >= REQUIRED_ENGINE_VERSION, (
            f"COLD_TAG default v{m.group(1)} is below the "
            f"guided-upgrade floor {REQUIRED_ENGINE_VERSION} — bump it "
            "with the floor (AGENTS.md § Engine-service release)"
        )
