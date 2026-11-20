# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""``nx catalog collections-census`` (RDR-229 P1.2, nexus-p2m1x.2): the read-only census of registry rows against
their names, one tenant per run. The computation is :mod:`nexus.catalog.rdr229.collection_row_census`; this is the single read
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
    if rv["not_conformant_rows"]:
        lines.append(f"  live names that do not parse (compared on lifecycle only): {rv['name_not_conformant']}")
        _shown(lines, rv["not_conformant_rows"], lambda x: (
            f"{x['name']}: content_type={x['content_type'] or '(empty)'} embedding_model={x['embedding_model'] or '(empty)'} "
            f"model_version={x['model_version'] or '(empty)'} legacy_grandfathered={x['legacy_grandfathered']}"
            + (f"  EMPTY: {', '.join(x['empty_fields'])}" if x["empty_fields"] else "")))
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
           lambda x: f"({x['content_type']}, {x['owner_id']}, {x['embedding_model']}): "
                     + ", ".join(f"{c['name']}@{c['model_version'] or '(empty)'}" for c in x["candidates"]))
    for label, grp in (("with", mt["tied_at_top_version"]["with_grandfathered_filter"]),
                       ("without", mt["tied_at_top_version"]["without_grandfathered_filter"])):
        lines.append(f"   tuples tied at the TOP version {label} the grandfathered filter (settled by NAME ASC): {grp['count']}")
        _shown(lines, grp["tuples"], lambda x: f"({x['content_type']}, {x['owner_id']}, {x['embedding_model']}) at "
                                               f"{x['top_version']}: {', '.join(x['tied'])} -> {x['winner_name_asc']}")
    wc = mt["winner_changes_when_filter_dropped"]
    lines.append(f"   tuples whose winner changes when the grandfathered filter is dropped (D5): {wc['count']} "
                 f"({wc['tuples_that_only_gain_a_winner']['count']} more only gain a winner)")
    _shown(lines, wc["tuples"], lambda x: f"({x['content_type']}, {x['owner_id']}, {x['embedding_model']}): "
                                          f"{x['winner_with_filter']} -> {x['winner_without_filter']}")
    p14 = mt["p14_created_at_tie_break"]
    d = p14["created_at_winner_differs"]
    lines.append(f"   P1.4 TIE-BREAK: of {p14['tied_tuples_with_filter']} tuple(s) tied at the top version, "
                 f"created_at-then-NAME picks a different winner than NAME ASC in {d['count']} "
                 f"({p14['created_at_winner_is_legacy_or_nonconformant_count']} of them a legacy or non-conformant "
                 f"created_at winner; {p14['tied_candidates_without_created_at']} tied candidate(s) have no created_at). "
                 "The P1.4 switch holds unless this count is zero. NAME ASC winners are computed in codepoint "
                 "order; the cloud database collation is unverified, so check each listed tie against the engine.")
    _shown(lines, d["tuples"], lambda x: f"({x['content_type']}, {x['owner_id']}, {x['embedding_model']}): NAME ASC "
                                         f"{x['winner_name_asc']}, created_at {x['winner_created_at_then_name']}"
                                         + (" [legacy/non-conformant]" if x["created_at_winner_is_legacy_or_nonconformant"] else "")
                                         + (f"; conformant sibling(s): {', '.join(x['conformant_siblings'])}"
                                            if x["conformant_siblings"] else ""))
    uv = rep["unusable_model_versions"]
    lines.append(f"   live rows whose model_version is empty or not v<digits>: {uv['count']} ({uv['empty']} empty, "
                 f"{uv['malformed']} malformed); P1.4's client falls back to the name for an empty value until this is zero")
    _shown(lines, uv["rows"], lambda x: f"{x['name']}: model_version={x['model_version'] or '(empty)'}")
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
    from nexus.catalog.rdr229.collection_row_census import census  # noqa: PLC0415 — command-local import deferred to avoid CLI startup cost
    from nexus.commands import catalog as _cat_cmd  # noqa: PLC0415 — module-routed helper access keeps import acyclic + monkeypatch-visible

    try:
        rows = _cat_cmd._get_catalog().list_collections()
    except Exception as exc:  # noqa: BLE001 — an unread registry is an error, never an empty census
        raise click.ClickException(f"could not read the collection registry: {exc}") from exc
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rep = census(rows, tenant_label=tenant_label, generated_at=now)
    # The engine binds the tenant to the bearer token and no public route echoes it (GET /v1/_whoami is not part of the
    # public surface), so the label is the operator's word and the report says so.
    rep["tenant_label_verified"] = False
    click.echo(
        "warning: the tenant label is unverified (the engine does not echo the tenant on a public route); check which "
        "endpoint and token this run used before storing the report.", err=True)
    if as_json:
        click.echo(json.dumps(rep, indent=2, sort_keys=True))
        return
    for line in render_text(rep):
        click.echo(line)


def register(group: click.Group) -> None:
    """Attach ``collections-census`` to the shared ``catalog`` group."""
    group.add_command(collections_census_cmd)
