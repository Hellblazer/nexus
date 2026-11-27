# SPDX-License-Identifier: AGPL-3.0-or-later
"""``nx store expire`` names every note it retires, and ``--dry-run`` lists them without writing (nexus-6pfgb.15, 8.5.8 shakeout).

The 2026-11-26 cloud shakeout ran ``nx store expire`` and got "Expired 4 entries. 4 notes retired; 4 chunks deleted." with
no name among them, so the run could not be audited; the operator's pre-list read chunk-row expiry and missed the one
note the sweep retired by its catalog TTL. Real engine; the storage-free twin for the output shape is
``tests/test_store_cmd.py``.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from click.testing import CliRunner

from nexus.cli import main
from tests.integration.test_6pfgb15_expire_per_owner import _chash, _World

pytestmark = pytest.mark.integration


def test_dry_run_names_a_note_only_its_catalog_ttl_lapsed_and_writes_nothing(t2_service_env):
    w = _World(t2_service_env, "expire-audit-dry")
    body = "expire audit: a note whose catalog TTL lapsed and whose chunk row carries no TTL"
    w.put("audit-lapsed", body, ttl="1d", legacy_row=False)
    w.put("audit-permanent", "expire audit: a permanent note that must not be listed")
    w.age_note("audit-lapsed", 3)
    w.age_row(body, ttl_days=None, days=3)  # the row says nothing about a TTL and is not newer than the note's put
    row = w.client._note_rows(w.collection, [_chash(body)])[_chash(body)]
    assert not row.get("ttl_days"), "non-vacuity: the chunk row carries no TTL, so a chunk-row pre-list misses the note"

    dry = w.client.expire_report(dry_run=True)

    assert dry.dry_run and dry.chunks_deleted == 0 and dry.notes_tombstoned == 0
    assert [(n.collection, n.title) for n in dry.retired] == [(w.collection, "audit-lapsed")]
    expires = datetime.fromisoformat(dry.retired[0].expires_at)
    assert 1 <= (datetime.now(UTC) - expires).days <= 3, f"the note's TTL ran out about two days ago: {expires}"
    assert w.live("audit-lapsed") and w.chunk_present(body) and w.chunk_served(body), "a dry run writes nothing"
    assert w.live("audit-permanent")
    assert dry.candidate_chunks >= 1

    real = w.client.expire_report()

    assert not real.dry_run and real.notes_tombstoned == 1
    assert [(n.collection, n.tumbler, n.title, n.expires_at) for n in real.retired] == [
        (n.collection, n.tumbler, n.title, n.expires_at) for n in dry.retired
    ], "the real run retires exactly the notes the dry run named, by the same TTL"
    assert not w.live("audit-lapsed") and w.live("audit-permanent")


def test_dry_run_holds_a_note_the_stale_ttl_guard_holds_and_does_not_list_it_as_retired(t2_service_env):
    w = _World(t2_service_env, "expire-audit-stale")
    body = "expire audit: a note an older client re-put permanent"
    w.put("audit-stale", body, ttl="1d")
    w.age_note("audit-stale", 2)
    w.age_row(body, ttl_days=None, days=0)

    dry = w.client.expire_report(dry_run=True)

    assert dry.retired == () and [t for _, t in dry.catalog_ttl_stale] == ["audit-stale"]
    assert w.live("audit-stale")


def test_the_cli_prints_each_retired_note_before_the_totals_and_dry_run_prints_what_it_would(t2_service_env):
    w = _World(t2_service_env, "expire-audit-cli")
    w.put("cli-one", "expire audit cli body one", ttl="1d", legacy_row=False)
    w.put("cli-two", "expire audit cli body two", ttl="1d", legacy_row=False)
    w.age_note("cli-one", 3)
    w.age_note("cli-two", 3)

    dry = CliRunner().invoke(main, ["store", "expire", "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "Would retire 2 notes." in dry.stdout and "Dry run: nothing was written." in dry.stdout
    for title in ("cli-one", "cli-two"):
        assert f"would retire  {w.collection}  '{title}'" in dry.stdout, dry.stdout
    assert "Expired" not in dry.stdout
    assert w.live("cli-one") and w.live("cli-two")

    real = CliRunner().invoke(main, ["store", "expire"])
    assert real.exit_code == 0, real.output
    out = real.stdout
    for title in ("cli-one", "cli-two"):
        line = f"retired  {w.collection}  '{title}'"
        assert line in out, out
        assert out.index(line) < out.index("Expired "), "each note is named before the totals"
    assert "2 notes retired;" in out
    assert not w.live("cli-one") and not w.live("cli-two")
