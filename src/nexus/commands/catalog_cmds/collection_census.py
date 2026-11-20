# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx catalog collections-census`` (RDR-229 P1.2, nexus-p2m1x.2): the read-only census of registry rows against
their names, one tenant per run. The computation is :mod:`nexus.catalog.collection_row_census`; this is the single read
(``list_collections()``, every lifecycle state, no filter: the dangling-tombstone count needs the tombstones) and the
printing. It writes nothing and never walks the catalog.

``--json`` prints the whole report, the form to store (``nexus_rdr/229-p1.2-census-<date>``); the text form prints the
counts and the first rows of each list. ``--tenant-label`` names the tenant in the report so two runs are told apart.
"""
from __future__ import annotations

import datetime
import json
from typing import Any

import click

#: Rows of each list the text form prints; ``--json`` carries every row.
TEXT_LIST_LIMIT = 20


def _shown(lines: list[str], items: list[Any], fmt: Any) -> None:
    for item in items[:TEXT_LIST_LIMIT]:
        lines.append("    " + fmt(item))
    if len(items) > TEXT_LIST_LIMIT:
        lines.append(f"    ... {len(items) - TEXT_LIST_LIMIT} more (use --json for every row)")


def render_text(rep: dict) -> list[str]:
    """The report as text lines: every count of the five items, and the first rows of each list."""
    reg, rv = rep["registry"], rep["row_vs_name"]
    lines = [f"collections-census {rep['census']} tenant={rep['tenant_label'] or '(unlabelled)'} at {rep['generated_at']}",
             f"registry: {reg['rows']} row(s): {reg['live']} live, {reg['tombstones']} tombstone(s)"]
    if rep["examined_nothing"]:
        lines.append("  the registry is empty: nothing was examined, so no count below is a finding.")
    lines.append(f"1. row vs name (live rows {rv['live_rows']}: {rv['name_conformant']} conformant, "
                 f"{rv['name_quarantine_conformant']} quarantine-conformant, {rv['name_not_conformant']} not conformant)")
    for field in ("content_type", "embedding_model", "model_version"):
        lines.append(f"  {field}: {rv[field]['mismatch']} mismatch of {rv[field]['compared']} compared")
    lc = rv["lifecycle"]
    lines.append(f"  lifecycle vs quarantine- prefix: {lc['mismatch']} mismatch of {lc['checked']} "
                 f"({lc['quarantine_state_without_prefix']} quarantine state without the prefix, "
                 f"{lc['prefix_without_quarantine_state']} prefix without the state)")
    od = rv["owner_differences"]
    lines.append(f"  owner_id differences (expected, counted apart): {od['differ']} of {od['compared']}")
    lines.append(f"  rows that disagree with their name: {len(rv['rows_that_disagree'])}")
    _shown(lines, rv["rows_that_disagree"], lambda x: f"{x['name']}: {json.dumps(x['differences'], sort_keys=True)}")
    if rv["not_conformant_names"]:
        lines.append(f"  live names that do not parse (compared on lifecycle only): {rv['name_not_conformant']}")
        _shown(lines, rv["not_conformant_names"], str)
    lc2 = rep["label_clash"]
    lines.append(f"2. label clash under a partial UNIQUE on live (tenant_id, name): {lc2['clashing_names']} of "
                 f"{lc2['live_names']} live names")
    _shown(lines, lc2["clashes"], lambda x: f"{x['name']} held by {x['rows']} live rows")
    mt = rep["multi_candidate_tuples"]
    lines.append("3. multi-candidate (content_type, owner_id, embedding_model) tuples: "
                 f"{mt['with_grandfathered_filter']['count']} with the grandfathered filter (today), "
                 f"{mt['without_grandfathered_filter']['count']} without it (D5); "
                 f"{mt['ambiguous_only_without_the_filter']} ambiguous only without it; "
                 f"{mt['rows_without_a_complete_tuple']} live row(s) with an incomplete tuple")
    _shown(lines, mt["without_grandfathered_filter"]["tuples"],
           lambda x: f"({x['content_type']}, {x['owner_id']}, {x['embedding_model']}): {', '.join(x['candidates'])}")
    mn = rep["minted_form_names"]
    lines.append(f"4. live names matching ^c[0-9]+$: {mn['count']}")
    _shown(lines, mn["names"], str)
    dg = rep["dangling_tombstones"]
    lines.append(f"5. dangling tombstones (superseded_by names no row): {dg['dangling']} of {dg['tombstones']} tombstone(s)")
    _shown(lines, dg["rows"], lambda x: f"{x['name']} -> {x['superseded_by']}")
    return lines


@click.command("collections-census")
@click.option("--tenant-label", default="", show_default=False,
              help="Name the tenant in the report (so two runs can be told apart); not sent anywhere.")
@click.option("--json", "as_json", is_flag=True, help="Print the whole report as JSON (the form to store).")
def collections_census_cmd(tenant_label: str, as_json: bool) -> None:
    """RDR-229 P1.2: read-only census of this tenant's collection rows against their names.

    One read of the collection registry (every state), no write, no catalog walk. Counts: row vs name for
    content_type, embedding_model, model_version and the quarantine lifecycle (owner differences apart); live label
    clashes; live (content_type, owner_id, embedding_model) tuples with more than one candidate row, with and without
    the grandfathered filter; live names matching ^c[0-9]+$; and tombstones whose superseded_by names no row.
    """
    from nexus.catalog.collection_row_census import census  # noqa: PLC0415 — command-local import deferred to avoid CLI startup cost
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    try:
        rows = _cat_cmd._get_catalog().list_collections()
    except Exception as exc:  # noqa: BLE001 — an unread registry is an error, never an empty census
        raise click.ClickException(f"could not read the collection registry: {exc}") from exc
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rep = census(rows, tenant_label=tenant_label, generated_at=now)
    if as_json:
        click.echo(json.dumps(rep, indent=2, sort_keys=True))
        return
    for line in render_text(rep):
        click.echo(line)


def register(group: click.Group) -> None:
    """Attach ``collections-census`` to the shared ``catalog`` group."""
    group.add_command(collections_census_cmd)
