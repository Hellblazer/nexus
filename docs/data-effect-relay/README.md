# Data-effect relay attestations

One JSON file per engine tag, at `docs/data-effect-relay/<engine-tag>.json`,
written and read by **nexus** — both halves of this relay's own record live
in this one repo, unlike `docs/release-arming/`, where conexus writes and
nexus reads. Written by `scripts/list_data_effects.py --record-relay-
attestation` (usually run manually, right after the table has been pasted
into the conexus handoff); read by
`scripts/list_data_effects.py --verify-relay-attestation`, which a release
battery runs.

Bead: `nexus-iu43o`. Follow-up from `nexus-f7dwp` (T2
`nexus/f7dwp-critic-pass-2026-09-13`).

## What it is for

The engine-release skill's handoff step already says: run
`scripts/list_data_effects.py <prev-tag> <tag>`, paste its markdown table
into the relay verbatim. That paste was a prose step with nothing checking
it actually happened — a human could run the script, read the table, and
still forget to include it in what conexus receives. This file is the
mechanical record that it was included, checked the same way
`docs/release-arming/` is checked: a battery step refuses without it,
instead of trusting that a described procedure was followed.

## Shape

```json
{
  "changeset_ids": [
    "catalog-016-source-uri-unique.xml:catalog-016-0"
  ],
  "engine_tag": "engine-service-v0.1.130",
  "from_tag": "engine-service-v0.1.129",
  "recorded_at": "2026-09-23T19:20:00Z"
}
```

`changeset_ids` is `file:changeset_id` for every data-effecting changeset
`find_added_data_effecting_changesets(from_tag, engine_tag)` finds in this
exact range — the SAME computation the verifier re-runs at check time, so a
stale attestation (a later commit adds a new data-effecting changeset after
this was recorded) is caught as a mismatch, not trusted. `from_tag` is
checked too: an attestation recorded for a differently-scoped range that
happens to share the same `engine_tag` name must not silently pass.

`recorded_at` is UTC with a `Z` suffix, written but not currently bounded by
age the way `docs/release-arming/`'s `armed_at` is — this attestation is
about *did the table land in the relay*, not about a deploy that can go
stale while waiting.

## When there is nothing to attest

A range with zero data-effecting changesets never gets a file here at all —
`--verify-relay-attestation` reports `NOT-APPLICABLE` (exit 0) and moves on,
per the vacuous-gate doctrine: a real gate with nothing to check passes
loudly, never silently.
