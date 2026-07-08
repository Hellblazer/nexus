---
title: "Content-Address Chunks by a Canonical 32-Byte Binary chash: One Digest, Stored as Bytes, Hex Only at the Boundary"
id: RDR-180
type: Architecture
status: draft
priority: high
author: Hal Hildebrand
created: 2026-07-04
related_issues: [nexus-z4skl, nexus-kmb6]
related: [RDR-108, RDR-152, RDR-155, RDR-156]
---

# RDR-180: Content-Address Chunks by a Canonical 32-Byte Binary chash

## Problem Statement

`chash` — the content address of a chunk — has **two incompatible width conventions** living in the tree at once, bridged only by a silent truncation. The system has been storing *half* of a SHA-256 while its citation grammar advertises the whole thing, and nobody caught it because nothing ever throws.

### Enumerated gaps to close

- G1 — **Two widths.** The chunk natural-ID (`chunk_identity.py`, `CHUNK_ID_LEN = 32`, `sha256(text).hexdigest()[:32]`) is **32 hex chars = 128 bits = 16 bytes**, and the DB `CHECK (length(chash) = 32)` on `catalog_document_chunks` + `chunks_{384,768,1024}` enforces it. The catalog citation grammar (`catalog.py`, `catalog_spans.py`) is `chash:[0-9a-f]{64}` — **64 hex chars = 256 bits = 32 bytes = the full digest.** These cannot be equal.
- G2 — **A truncation seam papers over G1.** `indexer.py:2146` (`chash = (meta.get("chunk_text_hash") or "")[:32]`) and `chunk_id_from_hash()` ("for sites that ALSO need the full 64-char hash") slice the full digest down to 128 bits at join points. Content-addressed citation resolution therefore effectively runs at 128 bits while the grammar claims 256.
- G3 — **Units are conflated in the storage type itself.** `chash` is a `text` column; "32" silently means *hex characters*. A caller passing the natural, correct 32-byte SHA-256 (64 hex chars) is rejected by a `CHECK` deep inside a per-row transaction, swallowed into `failed_doc_ids` with no reason. This cost three deploy-gate iterations on the v0.1.24 batch-endpoint probe (2026-07-04) before "64 vs 32" was found.
- G4 — **Storage format == interchange format.** Storing the hex string couples "how we key rows" to "how we render a citation." Hex is 2× the byte width (64 chars for a 32-byte digest); it belongs on the wire, not in the key column.

## Context

### Background

- RDR-108 D1 (bead nexus-kmb6) chose `sha256(text)[:32]` as the canonical chunk natural ID and centralized it in `chunk_identity.py` as "the single source of truth" — deliberately a compact 128-bit id, appropriate for a Chroma/pgvector record id, in the Chroma-era where record ids are strings.
- RDR-152/155/156 moved all persistent state behind the Java engine service on Postgres (pgvector, RLS). The chunk tables (`chunks_{384,768,1024}`) and `catalog_document_chunks` carry `chash` as `text` with the `length=32` CHECK (catalog-002-2-chash-checks, NOT VALID).
- The catalog span/citation grammar (`chash:<64hex>`, optionally `:<start>-<end>`) predates the service move and speaks the *full* digest — it was designed as a human-pasteable, greppable content citation.
- These two lineages (compact chunk-id vs full content-citation) were never reconciled; the same word `chash` names both.

### Technical Environment

- Java 25 engine (`service/`), jOOQ over HikariCP → PG17 + pgvector, Liquibase-managed schema, RLS on tenant tables. GraalVM native image.
- Postgres `BYTEA` binds natively to `byte[]` in jOOQ/JDBC; H2 (local/test) uses `BINARY(32)`/`VARBINARY`. Both index binary keys in a btree without ceremony.
- Producer side (Python `nexus`): chunk hashing in `chunk_identity.py`; chunk text (`chunk_text` / `chunk_text_hash` metadata) is retained, so the full digest is recomputable from stored content without re-embedding.

## Research Findings

### Investigation

Traced every `chash` site during the v0.1.24 batch-endpoint gate (2026-07-04): the producer (`chunk_identity.py`), the storage CHECK (catalog-002-hygiene changelog), the citation grammar (`catalog.py:290-291`, `catalog_spans.py:64-67`), the bridge (`indexer.py:2146`, `chunk_id_from_hash`), and the consumers (`ChashHandler` upsert/upsert_many/import, `PgVectorRepository` upsert paths + the ad-hoc `chashStr.length() != 32` read guard at `PgVectorRepository:2229`).

### Key Discoveries (evidence-grade)

1. **The stored value is 128-bit, not 256-bit.** `CHUNK_ID_LEN = 32` (hex chars). Confirmed against `chunk_identity.py:20,25` and the DB `CHECK(length(chash)=32)`.
2. **The citation grammar is 256-bit.** `chash:[0-9a-f]{64}` confirmed at `catalog.py:290-291` and `catalog_spans.py:64,67`.
3. **The invariant is enforced only writer-side by the DB, and mutely.** No HTTP handler validates chash width; the CHECK fires mid-transaction and batch endpoints fold it into `failed_doc_ids` with a `log.debug`.
4. **The full digest is recoverable by rehash, not re-embed.** Because `chunk_text` is retained, migrating to full 32-byte keys is a SHA-256 pass over stored text + an id remap — no Voyage calls, vectors reused via the old→new id map.
5. **Hex is 2× bytes.** 32 bytes = 64 hex = 44 base64 chars; 32 hex = 16 bytes. The storage-vs-interchange conflation is the root of the recurring 32/64 confusion.
6. **A-1 rehash coverage is total (live audit, 2026-07-04).** Across 246,995 chunk rows in both real tenants, 0 lack `chunk_text` and all 242,970 distinct chashes are rehashable — the migration drops nothing. Every stored chash is exactly 32 hex chars (128 bits): the truncation is corpus-wide, confirming the downgrade is real, not a fixture artifact.

### Critical Assumptions

- A-1: `chunk_text` is present for effectively all live chunks, so full-digest rehash covers the corpus. **VERIFIED (2026-07-04, live cloud corpus, tenants gate-xr789 + nexus):** 246,995 chunk rows across `chunks_{384,768,1024}`; **0** reference-only (null/empty `chunk_text`) rows; 242,970 distinct chashes, **all** rehashable; **residue = 0**. Full-digest rehash covers 100% of the corpus. Also confirmed on live data: `min_len = max_len = 32`, `rows_len_ne_32 = 0` — every stored chash is exactly 32 hex chars (128 bits), so the truncation is universal, not sampled. Residue handling (per operator, 2026-07-04): null-`chunk_text` rows are dropped-or-synthesized — kept as a defensive fallback in the ETL though the current residue is zero.
- A-2: `BYTEA(32)` keys perform at parity with `text(32)` for the PK/btree lookups and the pgvector chunk-id join. **Verify:** EXPLAIN a chash PK lookup + a manifest join on both before/after on a representative table.
- A-3: pgvector/Chroma record-id compatibility does not require a *string* id. If any external consumer keys on the hex-string chunk id, it must accept hex-encoded-at-boundary while storage is binary. **Verify:** enumerate external consumers of the chunk record id.
- A-4: Emitting hex at the JSON/citation boundary keeps the `chash:<hex>` wire format byte-compatible for existing clients (they never see the binary). **Verify:** a round-trip test (bytes→hex JSON→bytes) and that the widened citation is the full 64-hex.
- A-5: The migration is offline / freeze-gated (it rekeys content-addressed rows). **Verify:** confirm no online writer races the remap; sequence it in the managed-migration machinery.

## Proposed Solution

### Approach

1. **Item1 — Canonical definition.** A chash IS the 32-byte SHA-256 digest of chunk text. Full digest, not truncated. Binary is the storage form; hex is the interchange form. (Closes G1, G4.)
2. **Item2 — Store binary.** Change `chash` columns to `BYTEA` with `CHECK (octet_length(chash) = 32)` on `catalog_document_chunks` and `chunks_{384,768,1024}` (and `chash_index`). (Closes G3.)
3. **Item3 — One value type.** Introduce a `Chash` value type backed by `byte[32]`, the sole construction/enforcement point; `fromHex`/`toHex`(+`toBase64`) at boundaries; `@JsonValue` emits hex so wire + citations stay string-compatible. Repositories take `Chash`, not `String`; the DB CHECK demotes to belt-and-suspenders. Rewrites the provisional String-backed draft. (Closes G3.)
4. **Item4 — Producer emits full digest.** `chunk_identity.py`: `chunk_id` returns the full 64-hex (interchange) / 32-byte (storage) digest; retire the `[:32]` slice and the `indexer.py:2146` truncation seam. (Closes G2.)
5. **Item5 — Boundary validation.** HTTP handlers parse `chash` fields through the type at bind time → uniform 400 with the offending length, before any transaction (mirror the fk-001 typed-error pattern in `CatalogHandlerManifestFkTest`). (Closes G3.)
6. **Item6 — Offline reindex/remap.** Rehash stored `chunk_text` to the full digest; build old-128bit-hex → new-32byte map; migrate chunk tables, manifests, `chash_index`, **and `topic_assignments`** (its `doc_id` is a chunk chash — nexus-sa14p; the taxonomy-001 header's "doc tumblers" comment is stale — an unremapped assignment dangles and topic membership is silently lost); reuse vectors via the map (no re-embed). Freeze-gated. **Persist the old→new map as a permanent `chash_alias` table** (old 128-bit hex → new key): legacy 32-hex references live OUTSIDE the remapped tables too — bead comments, T2 memories, plan_json step ids, prose citations — and without a persisted alias they become forever unresolvable; with it, a resolver can accept either width indefinitely. (Closes G1, G2; alias + topic_assignments added 2026-07-08 review.)
7. **Item7 — Grammar stays 64-hex.** The catalog citation grammar `chash:[0-9a-f]{64}` is now *correct* against storage; no regex change needed (it was the 128-bit storage that was wrong). Add a resolver test proving a 64-hex citation resolves to a stored chunk. (Closes G1.)
8. **Item8 — Null/orphaned `chunk_text` disposition (defined regardless of current zero residue).** A-1 measured residue = 0 on the two cloud tenants, but local users and future tenants may hold reference-only / null-text chunks; the ETL MUST have a correct, tested answer before it runs against any tenant. Every chunk gets one of three dispositions, in priority order — see Technical Design. (Closes G2 robustly; prevents the dangling-manifest failure mode.)

### Technical Design

- **Schema:** Liquibase changeset converting `chash text` → `chash bytea`, `CHECK octet_length=32`; drop the old `length=32` text checks; VALIDATE the new checks post-backfill. Binary btree PK `(tenant_id, chash[, ...])` unchanged in shape.
- **Java:** `record Chash(byte[] value)` in `dev.nexus.service.db` — canonical constructor asserts `value.length == 32`; `equals`/`hashCode` over contents (`Arrays.equals`/`Arrays.hashCode`); `Chash.fromHex(String)` / `toHex()` (lowercase) / `fromSha256Bytes(byte[])`; `@JsonCreator fromHex` + `@JsonValue toHex` so the wire form is hex. jOOQ `bytea ↔ byte[]`.
- **Python producer:** `chunk_identity.chunk_id` returns full digest; a single `to_storage_bytes()` / `to_citation_hex()` pair mirrors the Java type's boundary discipline. Remove `[:32]` at all sites (`mcp/core.py`, `commands/store.py`, `commands/memory.py`, `db/t3.py`, `indexer.py:2146`).
- **Migration ETL:** stream chunks, recompute `sha256(chunk_text)`, write the new binary key, record old→new; repoint manifests + `chash_index`; verify counts equal pre/post.

- **Null/orphaned `chunk_text` disposition (Item8) — three-way, in priority order.** The migration keys off the union old→new map (`old_chash → sha256(chunk_text)`), built from all content-bearing rows across every dim:
  1. **Rehashable** — the row has non-empty `chunk_text`: new key = `sha256(chunk_text)` (32 bytes). Primary path (100% of the current corpus).
  2. **Reference-only, recoverable** — the row's `chunk_text` is null/empty BUT its `old_chash` appears with content on some *other* row (a legitimate cross-collection reference): remap via the old→new map to the sibling's new key. Never dropped, never synthesized — this preserves genuine reference chunks.
  3. **Orphaned** — null/empty `chunk_text` AND `old_chash` has no content-bearing source anywhere: apply the configured `orphan_policy`:
     - `drop` (default): delete the chunk row **and cascade** to every `catalog_document_chunks` / `chash_index` pointer at that `old_chash` (else the migration creates dangling manifest rows — see Failure Modes). Emit a per-tenant count; never silent.
     - `synthesize` (opt-in, when a pointer MUST survive for referential integrity): mint a deterministic surrogate key `sha256("nexus:synthetic-chash:v1|" + tenant + "|" + collection + "|" + old_chash)` (32 bytes), and stamp `metadata->>'chash_origin' = 'synthetic'` on the row so nothing downstream mistakes a surrogate for a real content address. The bytes are indistinguishable from a content digest by construction (that's fine — uniqueness holds); the metadata flag is the honest signal, NOT the byte pattern.
  The policy is a per-run ETL flag (default `drop`), logged with the disposition counts (rehashed / remapped / dropped / synthesized) so a run against a text-sparse tenant is auditable, not silently lossy.

### Existing Infrastructure Audit

- Reuse: the `CHECK`-constraint + NOT-VALID/VALIDATE pattern (catalog-002); the fk-001 typed-error handler pattern; the managed-migration ETL machinery (RDR-176/178) for the offline remap; `chunk_identity.py` as the already-centralized single producer site.
- Replace: the `length=32` text checks; the `[:32]` slices; the ad-hoc `PgVectorRepository:2229` length guard.

### Decision Rationale

Store the digest as 32 raw bytes because content-addressable storage keys on the *value*, not its rendering. Binary makes the width unambiguous (`octet_length=32` bytes — bytes are not characters, so the 32/64 confusion cannot recur), halves the key width vs 64-hex text, and lets hex/base64 be what they are: interchange. Choosing the *full* 256-bit digest (vs blessing the 128-bit truncation) aligns storage with the citation grammar that already assumes it and removes a silent security/collision downgrade, at a one-time rehash cost that is cheap because it reuses embeddings.

## Alternatives Considered

### Alternative 1: Bless the 128-bit truncation, keep text
Keep `[:32]` and `text` storage; tighten the catalog grammar regex from `{64}` to `{32}` so both subsystems agree at 128 bits. **Pro:** no reindex, smallest diff. **Con:** cements a half-SHA-256 as the content address, keeps storage==interchange coupling, and still models the key as a string where "32" is ambiguous — the units bug can recur at the next new table. Rejected: it makes the code honest about the wrong thing.

### Alternative 2: Full 256-bit, stored as text(64)
Full digest but kept as a 64-char hex `text` column. **Pro:** simplest migration shape (widen check 32→64, rehash). **Con:** 2× key width on disk/index, and preserves the storage-is-interchange conflation that caused G3/G4. Rejected in favor of binary.

### Briefly Rejected
- Base64 storage (44 chars): still text, still 2.75× the byte width, less greppable. No.
- Keep both a 128-bit `chunk_id` and a 256-bit `content_hash` as distinct columns/types: viable if a genuine compact-id need exists, but doubles the surface for no demonstrated benefit; revisit only if A-3 surfaces a hard string-id dependency.

## Trade-offs

### Consequences
- The 32-vs-64 bug class is structurally eliminated (bytes have no char-count ambiguity; one type is the only constructor).
- Content citations become truthful full-SHA-256 and actually resolve at 256 bits.
- Smaller, cleaner key columns; a single boundary for all encode/decode.

### Risks and Mitigations
- R1 — **Reindex rekeys content-addressed rows.** Mitigate: offline/freeze-gated ETL, old→new map, count-verify pre/post, reuse vectors (no re-embed).
- R2 — **Chunks lacking recomputable text** (relics). Mitigate: A-1 audit; define drop-vs-backfill before migration.
- R3 — **External string-id consumers.** Mitigate: A-3 enumeration; hex-at-boundary keeps wire compatibility.

### Failure Modes
- A partial migration leaving mixed 128-bit/256-bit keys → citations silently fail. Mitigate: single atomic cutover per tenant with count-verify gate; no dual-width window.
- **Dangling topic assignment:** `topic_assignments.doc_id` is a chunk chash with NO FK (soft reference by design), so the rekey cannot be caught by constraint — an ETL that misses this table silently strips every document's topic memberships. Mitigate: `topic_assignments` is in the Item6 remap inventory; the post-ETL orphan-pointer scan must include an assignments-vs-chunks join. (Added 2026-07-08: found by the "no information loss?" review — the table was absent from the original inventory.)
- **Legacy 32-hex references outside the DB:** historical artifacts (bead comments, T2 memories, plan_json, prose) cite the old 128-bit hex; they are not remappable in place. Mitigate: the persisted `chash_alias` table (Item6) keeps them resolvable; resolvers accept 32-hex via alias lookup and 64-hex directly.
- **Dangling manifest pointer:** dropping an orphaned chunk (Item8 disposition 3, `drop`) without cascading to its `catalog_document_chunks` / `chash_index` pointers leaves rows referencing a vanished key. Mitigate: `drop` MUST cascade the pointers in the same transaction; a post-ETL orphan-pointer scan is part of count-verify.
- **Synthetic key mistaken for content address:** a `synthesize`d surrogate is byte-indistinguishable from a real digest. Mitigate: the `metadata.chash_origin='synthetic'` flag is the authority; any consumer that must treat content-addressed rows specially checks the flag, not the bytes.

## Implementation Plan

### Prerequisites
- A-1 corpus audit (recomputable-text coverage), A-3 consumer enumeration, A-2 EXPLAIN parity check.

### Minimum Viable Validation
- On a throwaway tenant: index N chunks, migrate to `bytea(32)` full digest, prove a `chash:<64hex>` citation resolves and manifests/vectors survive with counts equal.

### Phase 1: Code Implementation
- `Chash` type (`byte[32]`) + test; producer full-digest + boundary helpers; handler bind-time validation; schema changeset (add bytea column, backfill, swap, VALIDATE); repositories typed.

### Phase 2: Operational Activation
- Freeze-gated per-tenant ETL remap; count-verify; flip.

### Day 2 Operations
- Monitor citation-resolution errors post-cutover; the boundary 400s now name the offending length.

### New Dependencies
- None (BYTEA/byte[] are native).

## Test Plan
- Unit: `Chash` rejects non-32-byte, hex round-trips, `@JsonValue`/`@JsonCreator` bind.
- Handler: a 64-hex (correct) chash writes 200; a 63/65-hex or non-hex → 400 with length in message.
- Resolver: `chash:<64hex>` citation resolves to a stored chunk.
- Migration: counts equal pre/post; a sampled chunk's new key == `sha256(chunk_text)`.
- `topic_assignments` remap: assign topics to chunks, migrate, assert every assignment resolves to a live new-key chunk (and the orphan scan covers the assignments join).
- `chash_alias`: after migration, a legacy 32-hex reference resolves through the alias table to the same chunk its 64-hex resolves to directly.
- Null/orphaned disposition (Item8), on a synthetic fixture tenant (since live residue is 0): (a) rehashable row → `sha256(chunk_text)`; (b) reference-only row whose old_chash has a content sibling → remapped to the sibling's new key, not dropped; (c) orphaned row under `drop` → row gone AND its manifest/chash_index pointers cascaded (no dangling scan hit); (d) orphaned row under `synthesize` → surrogate 32-byte key present with `metadata.chash_origin='synthetic'`, pointer preserved. Disposition counts logged and asserted.

## Validation

### Testing Strategy
- Real Postgres IT (existing manifest/chash IT harness) + H2 for the type. No mocks at the DB boundary.

### Performance Expectations
- Chash PK lookup + manifest join at parity or better vs text(32) (A-2 EXPLAIN). Key column shrinks from 64 to 32 bytes.

## Finalization Gate

### Contradiction Check
- Grammar `{64}` now matches storage (full digest) — the prior contradiction (G1) is resolved by construction, not by loosening either side.

### Assumption Verification
- A-1..A-5 each carry an explicit verify step above; none may remain unchecked at accept.

### Scope Verification
- Items 1–8 each map to a closing bead at planning; the offline ETL (Item6) and the null/orphaned disposition (Item8) are the silent-scope-reduction risks — each MUST get its own bead, not be folded into "schema change." Item8 in particular is defined regardless of the current zero residue, precisely so a later text-sparse tenant does not surface it as unplanned work mid-migration.

### Cross-Cutting Concerns
- Security: full 256-bit restores the intended collision resistance; note it explicitly as a (minor) posture improvement.
- Tenancy: all rekeys run under RLS `withTenant`; no cross-tenant key collision (keys are tenant-scoped).

### Proportionality
- One value type + one schema change + one offline ETL. Proportional to eliminating a whole recurring bug class and a silent hash-width downgrade.

## References
- RDR-108 (chunk identity D1, nexus-kmb6); RDR-152/155/156 (Postgres service tier); catalog-002-hygiene changelog (chash CHECK); `chunk_identity.py`, `catalog.py:290-291`, `catalog_spans.py:64-67`, `indexer.py:2146`, `PgVectorRepository:2229`, `CatalogHandlerManifestFkTest` (typed-error pattern).
- Origin: v0.1.24 engine batch-endpoint deploy gate, 2026-07-04 (the 64-vs-32 probe rabbit hole). Tracking bead: nexus-z4skl.

## Appendix A: Provisional reference draft (String-backed — to be rewritten `byte[32]`)

These two files were drafted during the origin investigation as a *String-backed* `Chash` (`LENGTH = 32` hex chars). They are kept here as reference only. **Per Item3, the accepted implementation must be rewritten `byte[32]`-backed** (canonical constructor asserts `value.length == 32` bytes; `fromHex`/`toHex`; `@JsonValue` emits hex); the String/hex form below would reject the correct 64-hex citations and cements the 128-bit width. Do NOT land the version below verbatim — it exists to show the boundary-validation and Jackson-binding shape, not the storage type.

`service/src/main/java/dev/nexus/service/db/Chash.java` (provisional):

```java
// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.fasterxml.jackson.annotation.JsonCreator;
import com.fasterxml.jackson.annotation.JsonValue;

import java.util.Locale;

/**
 * A content hash: exactly 32 lowercase hex characters (the first 32 chars of a
 * SHA-256 digest). PROVISIONAL — RDR-180 Item3 rewrites this byte[32]-backed.
 */
public record Chash(String value) {

    public static final int LENGTH = 32;

    public Chash {
        if (value == null) {
            throw new IllegalArgumentException(
                "chash must be non-null, exactly " + LENGTH + " lowercase hex chars");
        }
        if (value.length() != LENGTH) {
            throw new IllegalArgumentException(
                "chash must be exactly " + LENGTH + " lowercase hex chars; got length "
                    + value.length() + " (a full SHA-256 hex is 64 — did you forget to take [:32]?)");
        }
        if (!isLowerHex(value)) {
            throw new IllegalArgumentException(
                "chash must match [0-9a-f]{" + LENGTH + "}; got '" + value + "'");
        }
    }

    @JsonCreator
    public static Chash parse(String s) { return new Chash(s); }

    public static Chash fromSha256(String sha256Hex) {
        if (sha256Hex == null || sha256Hex.length() < LENGTH) {
            throw new IllegalArgumentException(
                "sha256 hex must be >= " + LENGTH + " chars to derive a chash; got "
                    + (sha256Hex == null ? "null" : "length " + sha256Hex.length()));
        }
        return new Chash(sha256Hex.substring(0, LENGTH).toLowerCase(Locale.ROOT));
    }

    @JsonValue
    @Override
    public String value() { return value; }

    @Override
    public String toString() { return value; }

    private static boolean isLowerHex(String s) {
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            boolean ok = (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
            if (!ok) { return false; }
        }
        return true;
    }
}
```

`service/src/test/java/dev/nexus/service/db/ChashTest.java` (provisional):

```java
// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;
import static org.assertj.core.api.Assertions.catchThrowableOfType;

final class ChashTest {

    private static final String VALID = "a".repeat(32);
    private static final ObjectMapper MAPPER = new ObjectMapper();

    @Test
    void accepts_canonical_32_lower_hex() {
        assertThat(new Chash(VALID).value()).isEqualTo(VALID);
        assertThat(Chash.parse("0123456789abcdef0123456789abcdef").value())
            .isEqualTo("0123456789abcdef0123456789abcdef");
    }

    @Test
    void rejects_full_sha256_64_hex_with_actual_length_in_message() {
        var ex = catchThrowableOfType(
            () -> new Chash("a".repeat(64)), IllegalArgumentException.class);
        assertThat(ex).hasMessageContaining("length 64");
    }

    @Test
    void rejects_null_short_and_uppercase_and_nonhex() {
        assertThatThrownBy(() -> new Chash(null)).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> new Chash("a".repeat(31))).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> new Chash("A".repeat(32))).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> new Chash("g".repeat(32))).isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void fromSha256_truncates_and_lowercases() {
        String sha = "ABCDEF0123456789".repeat(4);
        assertThat(Chash.fromSha256(sha).value())
            .isEqualTo("abcdef0123456789abcdef0123456789");
        assertThatThrownBy(() -> Chash.fromSha256("tooshort"))
            .isInstanceOf(IllegalArgumentException.class);
    }

    @Test
    void jackson_round_trips_as_a_bare_string() throws Exception {
        Chash c = MAPPER.readValue('"' + VALID + '"', Chash.class);
        assertThat(c.value()).isEqualTo(VALID);
        assertThat(MAPPER.writeValueAsString(c)).isEqualTo('"' + VALID + '"');
    }

    @Test
    void jackson_rejects_a_64_char_field_at_bind_time() {
        assertThatThrownBy(() -> MAPPER.readValue('"' + "a".repeat(64) + '"', Chash.class))
            .hasRootCauseInstanceOf(IllegalArgumentException.class);
    }
}
```

## Revision History
- 2026-07-04: draft created (RDR-180).
- 2026-07-04: A-1 audit recorded (residue 0, truncation corpus-wide); Item8 null/orphaned disposition added; provisional String-backed reference draft relocated from `service/src` into Appendix A to keep the engine build tree clean.
