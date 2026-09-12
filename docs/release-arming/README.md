# Release arming attestations

One JSON file per engine tag, at `docs/release-arming/engine-service-vX.Y.Z.json`,
written by **conexus** when a staged engine redeploy is armed. Read by
**nexus** at client-tag push, in `scripts/check_engine_release_floor.py`'s
paired-release battery (`check_release_arming`).

Bead: `nexus-h0fo3`. Origin: `nexus-1emxn` remedy (b).

## What it is for

Under the paired-release choreography a client release bumps
`REQUIRED_ENGINE_VERSION` to an engine tag whose deploy fires at client-tag
push. When the engine change is **not additive** — old client plus new engine
is unsafe — the deploy relay must already be armed before the client tag
pushes. Arming late is not a hygiene lapse: conexus's flip can refuse *after*
the nexus tag has pushed, and PyPI has by then published a client pinned to an
engine that never deployed. That is the GH #1402 shape, where cloud clients
refuse the managed service as below-identity.

The nexus gate is the last check before the irreversible step, so the
attestation is what makes "arm immediately before the paired tag" a contract
rather than advice. Two measured windows on 2026-08-29 (`engine-service-v0.1.88`
and `v0.1.89`) were both human-relay lapses; prose alone did not prevent them.

A file in the tagged tree, not a T2 row and not a live API call, for two
reasons. The gate that actually refuses is `release.yml` at tag push, whose
only cloud reach is an unauthenticated `GET /version` — it cannot read T2. And
a file needs no network at all, so the gate cannot fail open on a transport
error swallowed by some future `except`.

## Shape

The writer shipped on conexus main at their PR #336. This is the body as
it is actually written — JSON, 2-space indent, keys sorted, trailing newline.
Three fields are richer than the keys first settled, each for a stated reason.

```json
{
  "engine_tag": "engine-service-v0.1.117",
  "image_digest": "sha256:<64 hex>",
  "signature_verified": {
    "verified": true,
    "kms_key": "awskms:///alias/conexus-dev-image-signing",
    "image_ref": "conexus/engine:nexus-service-0.1.117@sha256:<64 hex>"
  },
  "ssm_param": "/conexus/dev/engine/image-tag",
  "ssm_param_version": 7,
  "redeploy_doc": "conexus-dev-engine-redeploy",
  "walk_rehearsed": "<T2 record title>",
  "armed_at": "2026-09-13T10:30:00Z",
  "armed_by": "sam@sha256:deadbeef"
}
```

`signature_verified` is an object rather than a bool because "verified, with
the KMS alias" is more than a bool can carry. `verified` is always true when
the file exists at all: a failed `cosign verify` refuses to arm instead of
writing false.

`walk_rehearsed` is a pointer — a T2 record title, or `not-required: <reason>`
— rather than a bool, because a bool would be unfalsifiable.

`armed_by` is `<principal-name>@sha256:<8>`, deliberately not an AWS caller
ARN. Hellblazer/nexus is public and conexus is private, and this repo's
`docs/` and `.github/` carry no 12-digit AWS account id today; an ARN would
mint that disclosure on every paired cut. Digests and signing cadence are not
new — every signed digest is already in the public Rekor log — which is why
only `armed_by` needed the treatment, and why the ECR registry URI stays out
of the body (the image is named `<repository>@<digest>`).

`armed_at` is UTC with a `Z` suffix. The reader also accepts `+00:00`.

The tag appears in both the filename and the body, so a mismatch between them
is detectable.

## Who checks what

Each condition is checked by the side that can observe it.

| Condition | Checked by | When |
|---|---|---|
| `engine_tag` equals the pairing's tag | nexus | client tag push |
| `armed_at` freshness | nexus | client tag push |
| live image digest still matches | conexus | at the flip |
| live SSM parameter version still matches | conexus | at the flip |

`image_digest` and `ssm_param_version` are deliberately **not** checked by
nexus. For a non-additive pairing the deploy is armed and held until the client
tag lands, so at tag time those fields are claims about a deploy that has not
happened yet and nobody can verify them. They belong at the flip.

### What this does not prove

Stated plainly, because the gate's name suggests more than it delivers.

The reader accepts on an attestation written *before* the flip. It does not
observe the deploy landing. A conexus flip that refuses after the nexus tag has
pushed still publishes a client pinned to an engine that never deployed — the
gate narrows that window to the arming freshness bound, it does not close it.
The backstop is the same one the module's tag-legitimacy bet already relies on:
the daily `engine-floor-verify` job in
`.github/workflows/scheduled-failure-watch.yml` (09:23 UTC, bare gate against
the real public endpoint) surfaces a still-stale cloud within 24 hours through
the tracked "scheduled workflows are failing silently" issue.

The flip-time half of the split is **not built yet** — conexus tracks it as
`conexus-9xny`. Until it is, the digest and parameter-version columns above
describe an intent, not a running check.

Writer identity rests on convention. Nothing in the file is signed, and
`armed_by` is read for the message and never validated; anyone who can commit
to this repo can write an attestation. That is accepted rather than overlooked:
the gate's value is that arming becomes a recorded, dated act, not that it
becomes unforgeable.

## Freshness

72 hours, the same window `--paired-tag-max-age-hours` applies to the paired
tag itself (`_DEFAULT_PAIRED_TAG_MAX_AGE_HOURS` in
`scripts/check_engine_release_floor.py`). The other acceptance facts are
stable — once a pairing is armed they stay true — so without a bound, reusing
an attestation on a later release would be accepted forever.

`armed_at` is wall clock from another machine, so an attestation dated in the
future is refused rather than read as very fresh. A short tolerance covers
ordinary NTP skew.

## When arming is required

Derived from `docs/wire-contract-pending.md`, never entered by hand. Arming is
required when either holds:

- any `## Unshipped` entry is not marked `[additive]`, whatever tag it names.
  An unshipped entry often names no concrete tag yet — the live one reads
  `engine tag \`TBD (next engine-service cut)\`` — so this half is not scoped
  by the pairing.
- any `## Shipped` entry whose engine tag equals the pairing's is not marked
  `[additive]`.

Both sections are read because the release PR moves the entry between them
before the tag exists. A non-additive `## Unshipped` entry fails
`release-ledger-gate` on every PR to main, so a non-additive paired release
must move its entry to `## Shipped` to merge at all — and a gate reading only
`## Unshipped` would therefore answer NOT-REQUIRED at every tag push, which is
exactly what the first version of this gate did.

An entry carrying neither token counts as not additive, the same fail-safe
reading `check_wire_contract_pairing.LedgerEntry.additive` documents. An
explicit `--ack-client-lag` does not make a change additive, so it does not
lift the arming requirement.

The `## Shipped` section's structural convention — the engine tag named once
as `engine half <tag>`, and the token leading a `--` segment — begins at
`SHIPPED_CONVENTION_FLOOR`, currently `engine-service-v0.1.92`, and is held by
a lint. That is a positive declaration of where the convention starts, not an
inference from the silence of older entries: the 16 entries below it carry no
token at all, and giving them one would mean retroactively adjudicating the
additivity of pairings nobody can now check.

The gate emits `ARMED`, `NOT-ARMED` or `NOT-REQUIRED` wherever it runs, and
emitting nothing is itself a failure.

It does not run everywhere. `--paired-deploy-auto`, which `release.yml` runs
at tag push, probes the cloud first and takes a pin-currency-only path when
the cloud already meets the floor — skipping the whole battery, ledger check
included, so no arming verdict is printed on that branch. The attended
`--paired-deploy` run at release Step 0 is the arming check; the tag-push run
is not a second one.

That is a ruling, not an oversight (`nexus-jv9h3`, 2026-09-12). The two-mode
asymmetry is deliberate and documented in
`docs/tables/release-choreography.toml`, and the safety property survives it:
the meets-floor branch fires only once the cloud is at or above the new floor,
which means the deploy has already happened, and arming is a claim about a
deploy that has not. What the branch costs is the audit trail, not the
protection.

## Retention

Files accumulate, one per tag. Nothing prunes them; an attestation for an old
tag is inert because the gate only ever looks up the tag the release pairs
with, and the freshness bound refuses a reused one.
