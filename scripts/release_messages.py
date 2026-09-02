#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hal Hildebrand. All rights reserved.
"""Message catalog for docs/tables/release-choreography.toml (RDR-201 P2.3,
nexus-j9z30.13).

Rows in the choreography table carry only an ``exit_code`` and a
``message_key`` -- the release-gate DECISION, not the operator-facing
prose. This module is the OTHER half: a flat ``dict[str, str]`` keyed by
the table's own row id (``f"{function}::{message_key}"``,
``scripts.enumerate_release_cells.cell_id``'s format, reused verbatim so
the table, the fixture, and this catalog all key on the same string),
holding a faithful rendering of the message text
``scripts/check_engine_release_floor.py`` / ``scripts/
check_client_release_precondition.py`` / ``scripts/
check_wire_contract_pairing.py`` print for that branch TODAY.

Entries are sourced from the real ``print(...)`` call sites (read
directly, not regenerated), collapsed to their INVARIANT wording -- the
real messages interpolate run-specific values (a tag string, an hours
figure, a commit list) that this static catalog cannot reproduce
byte-for-byte; where a message's identity rests on such an interpolated
detail, the entry keeps the surrounding fixed prose and marks the
variable span with a bracketed placeholder (e.g. ``[reason]``,
``[tag]``) rather than inventing a fake concrete value.

Consumed by BOTH gated scripts through ``release_choreography
.emit_choreography`` (RDR-201 P2.4/P2.5/P2.6, nexus-j9z30.14/.15/.16);
the placeholders are filled from the values each call site has in hand.
This catalog IS the operator-facing text now -- the inline prints it was
transcribed from were deleted at P2.6 after byte-for-byte parity was
proven over every enumerated cell of both scripts. ``tests/scripts/
test_release_table_parity.py`` asserts catalog<->table row-id parity and,
for every cell, that the real function leaves no placeholder unfilled --
a placeholder this catalog lacks (the ``[acked_suffix]`` the two
``ledger_additive_authorized`` entries carry, added at P2.5) is a fact
the operator stops seeing, not a cosmetic gap.

Where the real branch prints an exception's text, the entry carries a
bare ``[exc]`` placeholder -- never a paraphrase of what the exception
"would say" (the P2.4 fix round found six entries that invented one).

stdlib only.
"""
from __future__ import annotations

import check_client_release_precondition as _precond
import check_engine_release_floor as _floor

#: Row id (== the choreography table's own row id, ``f"{function}::
#: {message_key}"``) -> the message text that function's real branch
#: prints today. See the module docstring for the bracketed-placeholder
#: convention used where the real message interpolates a run-specific
#: value.
RELEASE_MESSAGES: dict[str, str] = {
    # -- check_pin_currency (check_engine_release_floor.py) ----------------
    "check_pin_currency::pin_currency_tags_unavailable": (
        "ENGINE PIN CHECK FAILED: could not read engine-service tags from "
        "git. Cannot verify that every gated engine tag is pinned -- treat "
        "as a failed gate, not a pass. In CI, actions/checkout needs "
        "`fetch-tags: true`."
    ),
    "check_pin_currency::pin_currency_zero_tags": (
        "ENGINE PIN CHECK FAILED: zero engine-service-v* tags visible. "
        "Either the checkout has no tags (CI: set `fetch-tags: true`) or "
        "the tag namespace changed. A gate that sees nothing must not "
        "report success."
    ),
    "check_pin_currency::pin_currency_stale_pin": (
        "ENGINE PIN CHECK FAILED: engine-service-v[newest] is published but "
        "this release pins v[floor]. Local-mode installs receive ONLY the "
        "pinned identity, so every engine fix between v[floor] and "
        "v[newest] reaches nobody.\n" + _floor._UNPINNED_REMEDY
    ),
    "check_pin_currency::pin_currency_current_at_floor": (
        "engine pin is current: REQUIRED_ENGINE_VERSION v[floor] == newest "
        "published tag"
    ),
    "check_pin_currency::pin_currency_current_below_floor": (
        "engine pin is current: REQUIRED_ENGINE_VERSION v[floor] == newest "
        "published tag"
    ),
    # -- check_source_ancestry (check_engine_release_floor.py) -------------
    "check_source_ancestry::ancestry_tag_unavailable": (
        "ENGINE SOURCE-ANCESTRY CHECK UNVERIFIABLE: could not confirm "
        "[pinned_tag] exists in git. Cannot compare source trees -- treat "
        "as a failed gate, not a pass. In CI, actions/checkout needs "
        "`fetch-depth: 0` (release.yml already sets this)."
    ),
    "check_source_ancestry::ancestry_tag_missing": (
        "ENGINE SOURCE-ANCESTRY CHECK UNVERIFIABLE: [pinned_tag] does not "
        "exist in this checkout's git history. Cannot compare source trees "
        "-- treat as a failed gate, not a pass."
    ),
    "check_source_ancestry::ancestry_diff_exception": (
        "ENGINE SOURCE-ANCESTRY CHECK UNVERIFIABLE: git diff failed "
        "([exc]). Cannot compare source trees -- treat as a failed gate, "
        "not a pass."
    ),
    "check_source_ancestry::ancestry_diff_nonzero": (
        "ENGINE SOURCE-ANCESTRY CHECK UNVERIFIABLE: `git diff [pinned_tag] "
        "HEAD -- [scope]` exited [returncode]: [stderr]"
    ),
    "check_source_ancestry::ancestry_drift": (
        "ENGINE SOURCE-ANCESTRY CHECK FAILED: this release ships [scope] "
        "source that its pinned engine tag ([pinned_tag]) does not "
        "contain:\n[diff]\nThe floor is version-CURRENT but SOURCE-STALE: "
        "a pin equal to the newest published tag can still predate shipped "
        "engine source (nexus-ajlz5). Cut a fresh engine tag carrying this "
        "source (or re-pin to a tag that already does) before releasing -- "
        "see AGENTS.md § Engine-service release, paired-release "
        "choreography."
    ),
    "check_source_ancestry::ancestry_clean": (
        "engine source is current: no [scope] drift between [pinned_tag] "
        "and HEAD"
    ),
    # -- check_client_lag_ledger (check_engine_release_floor.py) -----------
    "check_client_lag_ledger::ledger_clean": (
        "client-lag ledger clean: 0 unshipped both-halves commits in "
        "[ledger_path]"
    ),
    "check_client_lag_ledger::ledger_blocked": (
        "PAIRED DEPLOY BLOCKED: [n] both-halves commit(s) in "
        "[ledger_path] have an unshipped client half, no "
        "[additive] direction-safety token, and no acknowledgment:\n"
        "[entries]\n\nThis engine tag cannot deploy without an explicit "
        "paired-client acknowledgment (nexus-1vogq). Either pair this "
        "deploy with the client release carrying the listed commit(s), or "
        "pass --ack-client-lag <bead-id> for each entry above to deploy "
        "anyway."
    ),
    "check_client_lag_ledger::ledger_additive_authorized": (
        "client-lag ledger: [n] unacknowledged unshipped both-halves "
        "commit(s), all marked [additive] (old client + new engine safe) "
        "— deploy authorized ahead of the client tag (nexus-1emxn "
        "choreography (a)); pairing completes when the client release "
        "carrying [beads] bumps the floor.[acked_suffix]"
    ),
    "check_client_lag_ledger::ledger_acked": (
        "client-lag ledger: [n] unshipped both-halves commit(s), all "
        "explicitly acknowledged via --ack-client-lag: [beads]"
    ),
    # -- check_wire_contract_ledger (check_client_release_precondition.py) -
    "check_wire_contract_ledger::ledger_clean": (
        "wire-contract ledger: 0 unshipped entries in "
        "[ledger_path]"
    ),
    "check_wire_contract_ledger::ledger_blocked": (
        "BLOCKED: [n] both-halves commit(s) in [ledger_path] "
        "have an unshipped client half, no [additive] direction-safety "
        "token, and no acknowledgment:\n[entries]\n\n" + _precond._LEDGER_REMEDY
    ),
    "check_wire_contract_ledger::ledger_additive_authorized": (
        "wire-contract ledger: [n] unacknowledged unshipped both-halves "
        "commit(s), all marked [additive] (old client + new engine safe) "
        "— unpaired deploy authorized ahead of the client tag "
        "(nexus-1emxn choreography (a)); pairing completes when the client "
        "release carrying [beads] bumps the floor.[acked_suffix]"
    ),
    "check_wire_contract_ledger::ledger_acked": (
        "wire-contract ledger: [n] unshipped both-halves commit(s), all "
        "explicitly acknowledged via --ack-client-lag: [beads]"
    ),
    # -- check_paired_preconditions (check_engine_release_floor.py) --------
    "check_paired_preconditions::battery_bad_prefix": (
        "PAIRED MODE REJECTED: --paired-deploy [tag] is not an "
        "engine-service-v* tag."
    ),
    "check_paired_preconditions::battery_unparseable_tag": (
        "PAIRED MODE REJECTED: --paired-deploy [tag] does not parse as a "
        "version."
    ),
    "check_paired_preconditions::battery_exists_unavailable": (
        "PAIRED MODE UNVERIFIABLE: could not read git tags to confirm [tag] "
        "exists. Cannot verify the pairing -- treat as a failed gate, not "
        "a pass."
    ),
    "check_paired_preconditions::battery_tag_not_exists": (
        "PAIRED MODE REJECTED: [tag] does not exist in git. --paired-deploy "
        "must name a tag that has actually been pushed."
    ),
    # COUPLING (RDR-201 P2.4 fix round, critique T2 nexus/critique-nexus
    # -j9z30-14-2026-09-02 [24073] finding (c)): these two entries carry NO
    # fixed marker text of their own -- their [reason] placeholder is filled
    # at the check_paired_preconditions call site (check_engine_release_floor
    # .py) with _paired_tag_published()'s own reason string, which
    # enumerate_release_cells.py's _classify_paired_preconditions greps the
    # SUBSTITUTED text for a literal substring ("verify publication" /
    # "not published" respectively). If this [reason] placeholder is ever
    # removed, the classifier finds nothing to grep and raises loudly --
    # never a silent misroute -- but see that function's own COUPLING
    # comment, and tests/scripts/test_release_table_parity.py's
    # test_battery_*_catalog_reason_placeholder_drives_the_classifier.
    "check_paired_preconditions::battery_published_unavailable": (
        "PAIRED MODE UNVERIFIABLE: [reason]. Cannot verify publication -- "
        "treat as a failed gate, not a pass."
    ),
    "check_paired_preconditions::battery_not_published": (
        "PAIRED MODE REJECTED: [reason]."
    ),
    "check_paired_preconditions::battery_version_mismatch": (
        "PAIRED MODE REJECTED: --paired-deploy names v[tag] but "
        "REQUIRED_ENGINE_VERSION is v[floor] -- wrong pairing. The flag "
        "must name the exact tag this release pairs with."
    ),
    "check_paired_preconditions::battery_newest_unavailable": (
        "PAIRED MODE UNVERIFIABLE: could not read engine-service tags from "
        "git to confirm no newer tag exists."
    ),
    "check_paired_preconditions::battery_newest_none": (
        "PAIRED MODE REJECTED: newest published engine tag is v[newest], not "
        "v[tag] -- a newer engine tag exists than the one this release "
        "pairs with; unaccounted engine work. Keep the pin-currency red "
        "until it is pinned or explained."
    ),
    "check_paired_preconditions::battery_newest_mismatch": (
        "PAIRED MODE REJECTED: newest published engine tag is v[newest], "
        "not v[tag] -- a newer engine tag exists than the one this release "
        "pairs with; unaccounted engine work. Keep the pin-currency red "
        "until it is pinned or explained."
    ),
    "check_paired_preconditions::battery_age_unavailable": (
        "PAIRED MODE UNVERIFIABLE: could not determine [tag]'s commit age "
        "from git. Cannot verify the pairing is fresh -- treat as a failed "
        "gate, not a pass."
    ),
    "check_paired_preconditions::battery_too_old": (
        "PAIRED MODE REJECTED: [tag] is [age]h old, past the [max_age]h "
        "paired-tag freshness window. Deploy fires AT client-tag push -- a "
        "pairing this old is not THIS release's partner, and accepting it "
        "reopens the multi-release i5c2u drift class this gate exists to "
        "close. If this release genuinely lagged its engine tag, override "
        "explicitly with --paired-tag-max-age-hours."
    ),
    "check_paired_preconditions::battery_armed": (
        "paired mode ARMED: [tag] verified published, pinned to "
        "REQUIRED_ENGINE_VERSION, newest published engine tag, and [age]h "
        "old (within the [max_age]h window)."
    ),
    # -- record_deploy_from_gate_report_leg (check_engine_release_floor.py)
    "record_deploy_from_gate_report_leg::tracker_directory_error": (
        "TRACKER NOT RECORDED (exit 3): [exc]"
    ),
    "record_deploy_from_gate_report_leg::tracker_schema_error": (
        "TRACKER NOT RECORDED (exit 3): [exc]"
    ),
    "record_deploy_from_gate_report_leg::tracker_no_report_for_version": (
        "TRACKER NOT RECORDED (exit 3): [exc]"
    ),
    "record_deploy_from_gate_report_leg::tracker_gate_red": (
        "TRACKER NOT RECORDED (exit 3): [exc]"
    ),
    "record_deploy_from_gate_report_leg::tracker_version_mismatch": (
        "TRACKER NOT RECORDED (exit 3): [exc]"
    ),
    "record_deploy_from_gate_report_leg::tracker_live_version_mismatch": (
        "TRACKER NOT RECORDED (exit 3): [exc]"
    ),
    "record_deploy_from_gate_report_leg::tracker_managed_service_error": (
        "TRACKER NOT RECORDED (exit 3): the live /version re-read failed "
        "([exc])"
    ),
    "record_deploy_from_gate_report_leg::tracker_recorded": (
        "deployed-engine-version recorded from [report.basename]: [content]"
    ),
    # -- check_floor_bare (check_engine_release_floor.py, check_floor()) ---
    "check_floor_bare::bare_pin_blocks": (
        "delegates to check_pin_currency -- see the check_pin_currency::* "
        "entries above for the printed text of the specific pin-currency "
        "leaf reached."
    ),
    "check_floor_bare::bare_probe_unreachable": (
        "ENGINE FLOOR CHECK FAILED: managed service at [base] is "
        "unreachable ([exc]). Cannot verify the cloud engine version -- "
        "treat this as a failed gate, not a pass."
    ),
    "check_floor_bare::bare_probe_stale_via_exception": (
        "ENGINE FLOOR CHECK FAILED (required v[floor]): [exc]\n" + _floor._REMEDY
    ),
    "check_floor_bare::bare_probe_stale_via_success": (
        "ENGINE FLOOR CHECK FAILED: deployed engine at [base_url] reports "
        "release_version [release_version], required floor is v[floor]."
        "\n" + _floor._REMEDY
    ),
    "check_floor_bare::bare_probe_current": (
        "cloud engine is current: [base_url] release_version="
        "[release_version] (floor v[floor])"
    ),
    # -- check_floor_paired (check_engine_release_floor.py, check_floor()) -
    "check_floor_paired::paired_battery_blocks": (
        "delegates to the paired-precondition battery (client-lag ledger "
        "then check_paired_preconditions) -- see the "
        "check_client_lag_ledger::* / check_paired_preconditions::* "
        "entries above for the printed text of the specific battery leaf "
        "reached."
    ),
    "check_floor_paired::paired_probe_unreachable": (
        "ENGINE FLOOR CHECK FAILED: managed service at [base] is "
        "unreachable ([exc]). Cannot verify the cloud engine version -- "
        "treat this as a failed gate, not a pass."
    ),
    "check_floor_paired::paired_probe_unverifiable_exception": (
        "ENGINE FLOOR CHECK UNVERIFIABLE (required v[floor]): managed "
        "service at [base] probe failed without a genuine below-floor "
        "version reading ([exc]). Paired mode only ever accepts a "
        "GENUINE, parseable below-floor version report as 'deploy "
        "pending' -- an endpoint error or a malformed/unparseable "
        "response is never folded into that acceptance, paired or not. "
        "Treat as a failed gate, not a pass."
    ),
    "check_floor_paired::paired_probe_ack_via_exception": (
        "PAIRED MODE: cloud reports release_version [deployed], behind "
        "floor v[floor]. Expected pre-deploy under the paired-release "
        "choreography -- the deploy fires at client-tag push (AGENTS.md "
        "§ Cutting a release, step 0), not before this tag exists. "
        "Pairing named via --paired-deploy."
        "\nPOST-TAG VERIFY REQUIRED: re-run this script WITHOUT "
        "--paired-deploy once the deploy lands, to confirm the cloud "
        "engine actually converged -- escalate loudly (never silently "
        "re-accept) if it is still behind at that point."
    ),
    "check_floor_paired::paired_probe_unverifiable_success": (
        "ENGINE FLOOR CHECK UNVERIFIABLE (required v[floor]): managed "
        "service at [base] reported an unparseable release_version "
        "[release_version]. Paired mode only ever accepts a GENUINE, "
        "parseable below-floor version report as 'deploy pending' -- an "
        "endpoint error or a malformed/unparseable response is never "
        "folded into that acceptance, paired or not. Treat as a failed "
        "gate, not a pass."
    ),
    "check_floor_paired::paired_probe_ack_via_success": (
        "PAIRED MODE: cloud reports release_version [deployed], behind "
        "floor v[floor]. Expected pre-deploy under the paired-release "
        "choreography -- the deploy fires at client-tag push (AGENTS.md "
        "§ Cutting a release, step 0), not before this tag exists. "
        "Pairing named via --paired-deploy."
        "\nPOST-TAG VERIFY REQUIRED: re-run this script WITHOUT "
        "--paired-deploy once the deploy lands, to confirm the cloud "
        "engine actually converged -- escalate loudly (never silently "
        "re-accept) if it is still behind at that point."
    ),
    "check_floor_paired::paired_probe_current": (
        "cloud engine is current: [base_url] release_version="
        "[release_version] (floor v[floor])"
    ),
    # -- check_floor_auto_paired (check_engine_release_floor.py) -----------
    "check_floor_auto_paired::auto_probe_unreachable": (
        "ENGINE FLOOR CHECK FAILED: managed service at [base] is "
        "unreachable ([exc]). Cannot verify the cloud engine version -- "
        "treat this as a failed gate, not a pass."
    ),
    "check_floor_auto_paired::auto_probe_unverifiable_exception": (
        "ENGINE FLOOR CHECK UNVERIFIABLE (required v[floor]): managed "
        "service at [base] probe failed without a genuine below-floor "
        "version reading ([exc]). Paired mode only ever accepts a "
        "GENUINE, parseable below-floor version report as 'deploy "
        "pending' -- an endpoint error or a malformed/unparseable "
        "response is never folded into that acceptance, paired or not. "
        "Treat as a failed gate, not a pass."
    ),
    "check_floor_auto_paired::auto_below_via_exception_battery_blocks": (
        "delegates to _paired_below_floor_path -> the paired-precondition "
        "battery -- see the check_client_lag_ledger::* / "
        "check_paired_preconditions::* entries above."
    ),
    "check_floor_auto_paired::auto_below_via_exception_ack": (
        "PAIRED MODE: cloud reports release_version [deployed], behind "
        "floor v[floor]. Expected pre-deploy under the paired-release "
        "choreography -- the deploy fires at client-tag push (AGENTS.md "
        "§ Cutting a release, step 0), not before this tag exists. "
        "Pairing AUTO-derived from REQUIRED_ENGINE_VERSION "
        "(--paired-deploy-auto, nexus-gc9ir) -- no explicit --paired-deploy "
        "given."
        "\nPOST-TAG VERIFY REQUIRED: re-run this script WITHOUT "
        "--paired-deploy once the deploy lands, to confirm the cloud "
        "engine actually converged -- escalate loudly (never silently "
        "re-accept) if it is still behind at that point."
    ),
    "check_floor_auto_paired::auto_current_pin_blocks": (
        "delegates to check_pin_currency -- see the check_pin_currency::* "
        "entries above."
    ),
    "check_floor_auto_paired::auto_current": (
        "cloud engine is current: [base_url] release_version="
        "[release_version] (floor v[floor])"
    ),
    "check_floor_auto_paired::auto_probe_unverifiable_success": (
        "ENGINE FLOOR CHECK UNVERIFIABLE (required v[floor]): managed "
        "service at [base] reported an unparseable release_version "
        "[release_version]. Paired mode only ever accepts a GENUINE, "
        "parseable below-floor version report as 'deploy pending' -- an "
        "endpoint error or a malformed/unparseable response is never "
        "folded into that acceptance, paired or not. Treat as a failed "
        "gate, not a pass."
    ),
    "check_floor_auto_paired::auto_below_via_success_battery_blocks": (
        "delegates to _paired_below_floor_path -> the paired-precondition "
        "battery -- see the check_client_lag_ledger::* / "
        "check_paired_preconditions::* entries above."
    ),
    "check_floor_auto_paired::auto_below_via_success_ack": (
        "PAIRED MODE: cloud reports release_version [deployed], behind "
        "floor v[floor]. Expected pre-deploy under the paired-release "
        "choreography -- the deploy fires at client-tag push (AGENTS.md "
        "§ Cutting a release, step 0), not before this tag exists. "
        "Pairing AUTO-derived from REQUIRED_ENGINE_VERSION "
        "(--paired-deploy-auto, nexus-gc9ir) -- no explicit --paired-deploy "
        "given."
        "\nPOST-TAG VERIFY REQUIRED: re-run this script WITHOUT "
        "--paired-deploy once the deploy lands, to confirm the cloud "
        "engine actually converged -- escalate loudly (never silently "
        "re-accept) if it is still behind at that point."
    ),
    # -- main_dispatch (check_engine_release_floor.py, main()) -------------
    "main_dispatch::main_bare_check_floor_blocks": (
        "delegates to check_floor -- see the check_floor_bare::* entries "
        "above for the printed text of the specific leaf reached."
    ),
    "main_dispatch::main_bare_ancestry_blocks": (
        "delegates to check_source_ancestry -- see the "
        "check_source_ancestry::* entries above."
    ),
    "main_dispatch::main_bare_tracker_delegates": (
        "delegates to record_deploy_from_gate_report_leg -- see the "
        "record_deploy_from_gate_report_leg::* entries above."
    ),
    "main_dispatch::main_bare_tracker_opt_out": (
        _floor._TRACKER_OPT_OUT_NOTE + " Reason given: [reason]"
    ),
    "main_dispatch::main_bare_tracker_refusal": _floor._TRACKER_REFUSAL,
    "main_dispatch::main_paired_explicit_check_floor_blocks": (
        "delegates to check_floor -- see the check_floor_paired::* entries "
        "above."
    ),
    "main_dispatch::main_paired_explicit_ancestry_blocks": (
        "delegates to check_source_ancestry -- see the "
        "check_source_ancestry::* entries above."
    ),
    "main_dispatch::main_paired_explicit_non_bare_return": (
        "pre-deploy modes verify preconditions only; there is no "
        "post-deploy report to record from yet -- main() returns 0 with "
        "no further print beyond check_floor's own paired-acceptance "
        "message (check_floor_paired::paired_probe_ack_via_* above)."
    ),
    "main_dispatch::main_paired_auto_check_floor_blocks": (
        "delegates to check_floor -- see the check_floor_auto_paired::* "
        "entries above."
    ),
    "main_dispatch::main_paired_auto_ancestry_blocks": (
        "delegates to check_source_ancestry -- see the "
        "check_source_ancestry::* entries above."
    ),
    "main_dispatch::main_paired_auto_non_bare_return": (
        "pre-deploy modes verify preconditions only; there is no "
        "post-deploy report to record from yet -- main() returns 0 with "
        "no further print beyond _check_floor_auto_paired's own "
        "paired-acceptance message (check_floor_auto_paired::auto_*_ack "
        "above)."
    ),
    "main_dispatch::main_ledger_only_clean": (
        "delegates to check_client_lag_ledger -- see "
        "check_client_lag_ledger::ledger_clean above."
    ),
    "main_dispatch::main_ledger_only_blocked": (
        "delegates to check_client_lag_ledger -- see "
        "check_client_lag_ledger::ledger_blocked above."
    ),
    "main_dispatch::main_ledger_only_additive": (
        "delegates to check_client_lag_ledger -- see "
        "check_client_lag_ledger::ledger_additive_authorized above."
    ),
    "main_dispatch::main_ledger_only_acked": (
        "delegates to check_client_lag_ledger -- see "
        "check_client_lag_ledger::ledger_acked above."
    ),
    # -- check_composite (check_client_release_precondition.py, check()) --
    "check_composite::composite_latest_release_tag_error": (
        "CANNOT VERIFY: [exc]"
    ),
    "check_composite::composite_is_ancestor_error": (
        "CANNOT VERIFY [commit]: [exc]"
    ),
    "check_composite::composite_missing_commit": (
        "\nBLOCKED: [engine_tag] must not deploy — [n] required client "
        "commit(s) absent from the latest release [release]:\n[commits]"
        "\n\n" + _precond._REMEDY
    ),
    "check_composite::composite_both_vacuous": (
        "OK (VACUOUS -- 0 preconditions registered for [engine_tag] AND 0 "
        "entries in [ledger_path]'s ## Unshipped section): "
        "this run verified NOTHING from EITHER source. An empty hand table "
        "plus an empty ledger means either 'no known client coupling for "
        "this engine tag' or 'nobody added rows to either source' -- this "
        "script cannot tell the two apart. See ENGINE_CLIENT_PRECONDITIONS's "
        "module docstring before treating this as evidence the deploy is "
        "safe."
    ),
    "check_composite::composite_vacuous_table_ledger_blocks": (
        "delegates to check_wire_contract_ledger -- see "
        "check_wire_contract_ledger::ledger_blocked above (the hand table "
        "itself was vacuous, so the ledger's verdict is the whole story)."
    ),
    "check_composite::composite_vacuous_table_ledger_additive": (
        "delegates to check_wire_contract_ledger -- see "
        "check_wire_contract_ledger::ledger_additive_authorized above."
    ),
    "check_composite::composite_vacuous_table_ledger_acked": (
        "delegates to check_wire_contract_ledger -- see "
        "check_wire_contract_ledger::ledger_acked above."
    ),
    "check_composite::composite_table_satisfied_ledger_empty": (
        "OK: all client preconditions for [engine_tag] are in [release]\n"
        "(the ledger then confirms clean -- see "
        "check_wire_contract_ledger::ledger_clean above.)"
    ),
    "check_composite::composite_table_satisfied_ledger_blocks": (
        "OK: all client preconditions for [engine_tag] are in [release]\n"
        "then delegates to check_wire_contract_ledger -- see "
        "check_wire_contract_ledger::ledger_blocked above."
    ),
    "check_composite::composite_table_satisfied_ledger_additive": (
        "OK: all client preconditions for [engine_tag] are in [release]\n"
        "then delegates to check_wire_contract_ledger -- see "
        "check_wire_contract_ledger::ledger_additive_authorized above."
    ),
    "check_composite::composite_table_satisfied_ledger_acked": (
        "OK: all client preconditions for [engine_tag] are in [release]\n"
        "then delegates to check_wire_contract_ledger -- see "
        "check_wire_contract_ledger::ledger_acked above."
    ),
    # -- precond_main_dispatch (check_client_release_precondition.py) -----
    "precond_main_dispatch::precond_main_explicit_tag_with_ack": (
        "delegates to check() with an explicit --engine-tag and a "
        "matching --ack-client-lag -- see check_composite::* above for the "
        "printed text of the specific leaf reached (driven vacuous + "
        "acked ledger: composite_vacuous_table_ledger_acked)."
    ),
    "precond_main_dispatch::precond_main_default_tag_no_ack": (
        "delegates to check() with the default (pinned) --engine-tag and "
        "no --ack-client-lag -- see check_composite::* above (driven "
        "vacuous + blocking ledger: composite_vacuous_table_ledger_blocks)."
    ),
}


def get(row_id: str) -> str:
    """Message text for ``row_id`` (the choreography table's own row id).

    Raises ``KeyError`` on an unknown id rather than returning a silent
    placeholder -- a row with no catalog entry is a real authoring gap,
    not something to paper over at read time.
    """
    return RELEASE_MESSAGES[row_id]
