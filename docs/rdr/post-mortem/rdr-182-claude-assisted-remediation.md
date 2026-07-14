# Post-Mortem: RDR-182 — Claude-Assisted Upgrade and Remediation

**Closed**: 2026-07-13 (implemented) · **Epic**: nexus-ykzbj (all 20 P0-P5 sub-beads closed; five residuals tracked with priorities)

## Outcome vs design

All five Gaps shipped: the default-off opt-in gate (A4), the consent audit
(grant/revoke + read surface), the shared playbook emitter, the pre-emission
read-only SQL lint, the `nexus_diag` SELECT-only+BYPASSRLS diagnostic role +
single connection choke point, the MCP `forensics`/`remediate` tools, the
`nx forensics`/`nx remediate` CLI, and both MVV proofs. Each phase passed a
stacked review; the final holistic pass (two reviewers APPROVE, test-validator
validated, the latter proving non-vacuity by mutating production code) verified
all five Gaps and the taxonomy amendments consistent across code, docs, tests.
Full suite green (13058).

## Divergences from the accepted text (all disclosed at the time)

1. **Taxonomy amendment A5** (2026-07-13): the RDR framed the CLI as ungated
   ("printing text a human copies is the consent act"). That precedent was a
   STATIC paste-prompt; the shipped commands additionally run a credentialed
   BYPASSRLS store probe. The critic flagged an ungated bypass of the MCP
   gate; the taxonomy was split — static text stays ungated, the live probe
   honors the flag on both transports. RDR §Risks A5 records it.
2. **H1 release gate** (extension of A5): the CLI `remediate` release + its
   `granted=True` audit row were gated only by `click.confirm`, so a piped
   `y` could forge a human-looking consent row. Resolved (Hal decision): the
   release also requires the durable flag. Both amendments were user decisions,
   not silent code changes.
3. **MVV proof #2 re-scope**: the "walk §8.1 → re-run upgrade succeeds" leg is
   the guided-upgrade rehearsal E2E's job (container tier); the P5 test proves
   the product's half (consented, audited handoff). Disclosed in the test
   docstring after the final critic flagged the original close as over-claiming.
4. **Gap 1 credit correction**: the automatic edge-interception that actually
   catches the destructive reflex (`_emit_chash_poison_gate` → `install-binary`)
   PRE-DATES this epic (nexus-pnwu0). RDR-182's contribution is the generalized
   emitter + self-serve surfaces + Gaps 2-5 machinery, not a second automatic
   gate. RDR Gap 1 now carries a delivery-honesty note.

## What the process caught (worth repeating)

- The **`.nexus.yml` consent-laundering** hole: the gate honored `load_config()`'s
  merged view, so a `git`-pulled repo file could flip consent for the
  mutation-authorizing tool AND mint a false `granted=True` row. Fixed
  structurally (global-config-only reader). This was a carried-forward
  limitation the critic refused to let ride into the phase where it turned
  dangerous.
- The **fast-idempotency role gap**: `nexus_diag` was never created on the
  re-provision path (the steady state for every existing install), making the
  whole P2.1 deliverable inert for the population RDR-182 targets. The reviewer
  cited the existing pgvector-backfill precedent in the same function.
- The **lint bypass on unqualified targets**: `SELECT content FROM chunks_768`
  (no schema prefix) slipped past the content check, saved only by no
  search_path override — an operational accident. Hardened to fail-closed.
- **nexus-vounk** (discovered while enumerating the P2.1 decision surface, fixed
  2026-07-13): the shipped `nx doctor`/`install-binary` chash-poison probe ran
  as `nexus_admin` with no tenant GUC, counting 0 under FORCE RLS — the gate was
  vacuous in production, reporting clean on exactly the poisoned stores it
  existed to block. Rewired onto the `nexus_diag` BYPASSRLS path; the health
  probe and the forensics topic now share one chash-table constant so they
  cannot drift.

## Residuals (tracked, prioritized)

- **nexus-ng2sy (P0)** — service-mode `record_consent` parity. `remediate` is
  local-mode-only until it lands (service mode is the primary deployment).
- **nexus-s4a98 (P1)** — `set_config_value` crashes on a pre-existing flat
  scalar; the enable command every refusal names is unreliable for that subset.
- **nexus-9bufb (P2)** — structural DB-level content scoping (count views) for
  `nexus_diag`, so the content boundary is DB-enforced, not just choke-point.
- **nexus-wnp3c (P2)** — forensics/remediate topics for the 3 remaining incident
  classes; Gap 1 is populated for `chash-poison` only.

## Lesson

Consent and read-only-diagnostic boundaries are worth stating with brutal
precision about which surface enforces what: the reviews' highest-value catches
were all "the mechanism enforces less than the claim says" (`.nexus.yml`
provenance, forgeable CLI audit, the vacuous RLS probe, the describe/runbook
information-vs-audit distinction). Gate shape + carried-forward limitations:
T2 `nexus/rdr182-a4-spike-gate-shape.md`.
