// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.db.SchemaViolationException;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TooLargeException;
import dev.nexus.service.db.TupleLimits;
import dev.nexus.service.db.TupleRepository;
import dev.nexus.service.tuples.TemplateRegistry;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.util.Map;
import java.util.Optional;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/**
 * RDR-205 amendment (bead nexus-r7xao, Sam's decision 2026-09-13) —
 * boundary tests for the engine-side per-field size checks: {@code
 * TupleRepository}'s {@code out}/{@code ackWithReply}/{@code rd}/{@code
 * rdp}/{@code in}/{@code inp}/{@code ack}/{@code nack}/{@code renew} all
 * refuse an over-limit {@code subspace}, {@code keys}/{@code dims}/{@code
 * keys_pattern} value, {@code nonce}, {@code claimant}, {@code claim_id} or
 * {@code body} with {@link TooLargeException}, at exactly the boundary
 * named in {@link TupleLimits} — one byte over refuses, exactly at the cap
 * succeeds (or at least reaches the NEXT validation stage, proving the size
 * check itself did not fire).
 *
 * <p>Every multibyte case uses a 2-byte UTF-8 character ("é",
 * {@code é}) repeated so the boundary is genuinely on BYTES, not on
 * {@code String#length()} (UTF-16 code units) — a body of 2048 "é"
 * characters is 4096 bytes but {@code length() == 2048}, so a char-count
 * check would wrongly accept it as "half the limit".
 *
 * <p>Uses its own dedicated Postgres container and a synthetic {@code
 * probecap/<id>} template ({@code max_body_bytes: 10}) loaded the same way
 * {@code TupleRepositoryTest}'s {@code probe/<id>} fixture is, so the
 * per-template body cap can be exercised without touching the two v1
 * production templates.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class TupleSizeLimitsTest {

    private static final String TENANT = "tuple-size-limits-tenant";
    private static final String SVC_ROLE = "svc_tuple_size_limits_test";
    private static final String SVC_PASS = "svc_tuple_size_limits_test_pass";

    /** A 2-byte UTF-8 character, so a byte-count boundary and a char-count
     *  boundary disagree -- exactly what a length()-based bug would miss. */
    private static final String TWO_BYTE_CHAR = "é";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    TenantScope tenantScope;
    TemplateRegistry registry;
    TupleRepository repo;

    @BeforeAll
    void startAll(@org.junit.jupiter.api.io.TempDir Path extraTemplateDir) throws Exception {
        pg = PgContainerHelper.start();

        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }

        var cfg = new com.zaxxer.hikari.HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(SVC_ROLE);
        cfg.setPassword(SVC_PASS);
        cfg.setMaximumPoolSize(10);
        cfg.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(cfg);

        tenantScope = new TenantScope(svcDs);
        Files.writeString(extraTemplateDir.resolve("probecap.yaml"), """
                name: probecap/<id>
                keys:
                  - owner
                id_from: keys
                take:
                  enabled: true
                  max_attempts: 3
                  max_lease_seconds: 300
                retention_seconds: 3600
                max_body_bytes: 10
                """, StandardCharsets.UTF_8);
        registry = TemplateRegistry.loadAtBoot(extraTemplateDir.toString(), null,
                NexusService.SWEEP_INTERVAL_HOURS * 3600L);
        repo = new TupleRepository(tenantScope, registry,
                TupleRepository.DEFAULT_READ_MAX, TupleRepository.DEFAULT_CLAIM_PASSES,
                /* timeoutCapSeconds */ 10, /* parkCapPerClaimant */ 4, /* parkCapGlobal */ 16);
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) {
            svcDs.close();
        }
        if (pg != null) {
            pg.stop();
        }
    }

    // ── body: global 4096-byte cap ───────────────────────────────────────────

    @Test
    void out_bodyAtGlobalCap_succeeds() {
        String body = "x".repeat(TupleLimits.MAX_BODY_BYTES);
        byte[] id = repo.out(TENANT, "mailbox/size-body-ok", Map.of("to", "size-body-ok"),
                Map.of("from", "sender"), body, "nonce-body-ok", null);
        assertThat(id).isNotNull();
    }

    @Test
    void out_bodyOneByteOverGlobalCap_refusedAsTooLarge() {
        String body = "x".repeat(TupleLimits.MAX_BODY_BYTES + 1);
        assertThatThrownBy(() -> repo.out(TENANT, "mailbox/size-body-over",
                Map.of("to", "size-body-over"), Map.of("from", "sender"), body, "nonce-body-over", null))
                .isInstanceOf(TooLargeException.class)
                .satisfies(e -> assertThat(((TooLargeException) e).field()).isEqualTo("body"))
                .satisfies(e -> assertThat(((TooLargeException) e).limitBytes())
                        .isEqualTo(TupleLimits.MAX_BODY_BYTES));
    }

    @Test
    void out_bodyMultibyteAtCap_countsBytesNotChars() {
        // 2048 two-byte characters == 4096 bytes, exactly the cap -- must succeed.
        String body = TWO_BYTE_CHAR.repeat(TupleLimits.MAX_BODY_BYTES / 2);
        byte[] id = repo.out(TENANT, "mailbox/size-body-multibyte-ok",
                Map.of("to", "size-body-multibyte-ok"), Map.of("from", "sender"), body,
                "nonce-body-mb-ok", null);
        assertThat(id).isNotNull();
    }

    @Test
    void out_bodyMultibyteOneCharOverCap_refused() {
        // 2049 two-byte characters == 4098 bytes -- over the cap by byte count, even
        // though String#length() (4098/2 rounded... 2049 UTF-16 units) is nowhere near
        // a naive char-count cap of 4096.
        String body = TWO_BYTE_CHAR.repeat(TupleLimits.MAX_BODY_BYTES / 2 + 1);
        assertThatThrownBy(() -> repo.out(TENANT, "mailbox/size-body-multibyte-over",
                Map.of("to", "size-body-multibyte-over"), Map.of("from", "sender"), body,
                "nonce-body-mb-over", null))
                .isInstanceOf(TooLargeException.class);
    }

    // ── body: per-template max_body_bytes (lower ceiling) ───────────────────

    @Test
    void out_bodyAtTemplateCap_succeeds() {
        String body = "x".repeat(10);
        byte[] id = repo.out(TENANT, "probecap/size-tpl-ok", Map.of("owner", "size-tpl-ok"),
                Map.of(), body, null, null);
        assertThat(id).isNotNull();
    }

    @Test
    void out_bodyOneByteOverTemplateCap_refused() {
        String body = "x".repeat(11);
        assertThatThrownBy(() -> repo.out(TENANT, "probecap/size-tpl-over",
                Map.of("owner", "size-tpl-over"), Map.of(), body, null, null))
                .isInstanceOf(TooLargeException.class)
                .satisfies(e -> assertThat(((TooLargeException) e).limitBytes()).isEqualTo(10L));
    }

    @Test
    void out_bodyUnderGlobalCapButOverTemplateCap_stillRefused() {
        // 200 bytes is well under the GLOBAL 4096-byte cap, but over probecap's
        // own 10-byte ceiling -- proves the template cap actually lowers the
        // effective limit rather than being advisory.
        String body = "x".repeat(200);
        assertThatThrownBy(() -> repo.out(TENANT, "probecap/size-tpl-under-global",
                Map.of("owner", "size-tpl-under-global"), Map.of(), body, null, null))
                .isInstanceOf(TooLargeException.class);
    }

    @Test
    void ledgerTemplate_maxBodyBytesZero_nullAndEmptyPass_nonEmptyRefused() {
        byte[] idNullBody = repo.out(TENANT, "ledger/size-ledger-session",
                Map.of("agent_id", "a1", "kind", "start"), Map.of("agent_type", "developer"), null, null, null);
        assertThat(idNullBody).isNotNull();

        byte[] idEmptyBody = repo.out(TENANT, "ledger/size-ledger-session-2",
                Map.of("agent_id", "a2", "kind", "start"), Map.of("agent_type", "developer"), "", null, null);
        assertThat(idEmptyBody).isNotNull();

        assertThatThrownBy(() -> repo.out(TENANT, "ledger/size-ledger-session-3",
                Map.of("agent_id", "a3", "kind", "start"), Map.of("agent_type", "developer"), "x", null, null))
                .isInstanceOf(TooLargeException.class)
                .satisfies(e -> assertThat(((TooLargeException) e).limitBytes()).isEqualTo(0L));
    }

    // ── subspace: 256-byte cap ───────────────────────────────────────────────

    @Test
    void out_subspaceOverCap_refusedBeforeUnknownSubspace() {
        // A subspace this long resolves to no template AND is over the cap --
        // TooLarge must fire, not UnknownSubspace, proving size checks run first.
        String subspace = "mailbox/" + "a".repeat(TupleLimits.MAX_SUBSPACE_BYTES);
        assertThatThrownBy(() -> repo.out(TENANT, subspace, Map.of("to", "x"), Map.of("from", "s"),
                null, "n", null))
                .isInstanceOf(TooLargeException.class)
                .satisfies(e -> assertThat(((TooLargeException) e).field()).isEqualTo("subspace"));
    }

    @Test
    void rd_subspaceOverCap_refused() {
        String subspace = "a".repeat(TupleLimits.MAX_SUBSPACE_BYTES + 1);
        assertThatThrownBy(() -> repo.rd(TENANT, subspace, Map.of(), 1, null, 0))
                .isInstanceOf(TooLargeException.class);
    }

    @Test
    void in_subspaceOverCap_refused() {
        String subspace = "a".repeat(TupleLimits.MAX_SUBSPACE_BYTES + 1);
        assertThatThrownBy(() -> repo.in(TENANT, subspace, Map.of(), "claimant", 60, 0))
                .isInstanceOf(TooLargeException.class);
    }

    // ── keys / dims / keys_pattern: 256-byte value cap ──────────────────────

    @Test
    void out_keyValueAtCap_succeeds() {
        // Subspace address is short and unrelated to the "to" key's own value --
        // the two are independent fields, so this isolates the KEY-VALUE boundary
        // from the subspace boundary.
        String value = "a".repeat(TupleLimits.MAX_FIELD_VALUE_BYTES);
        byte[] id = repo.out(TENANT, "mailbox/size-key-at-cap", Map.of("to", value),
                Map.of("from", "s"), null, "n-key-ok", null);
        assertThat(id).isNotNull();
    }

    @Test
    void out_keyValueOneByteOverCap_refused() {
        String value = "a".repeat(TupleLimits.MAX_FIELD_VALUE_BYTES + 1);
        assertThatThrownBy(() -> repo.out(TENANT, "mailbox/size-key-over", Map.of("to", value),
                Map.of("from", "s"), null, "n-key-over", null))
                .isInstanceOf(TooLargeException.class)
                .satisfies(e -> assertThat(((TooLargeException) e).field()).isEqualTo("keys.to"));
    }

    @Test
    void out_dimValueOneByteOverCap_refused() {
        String value = "a".repeat(TupleLimits.MAX_FIELD_VALUE_BYTES + 1);
        assertThatThrownBy(() -> repo.out(TENANT, "mailbox/size-dim-over", Map.of("to", "addr"),
                Map.of("from", value), null, "n-dim-over", null))
                .isInstanceOf(TooLargeException.class)
                .satisfies(e -> assertThat(((TooLargeException) e).field()).isEqualTo("dims.from"));
    }

    @Test
    void rd_patternValueOneByteOverCap_refused() {
        String value = "a".repeat(TupleLimits.MAX_FIELD_VALUE_BYTES + 1);
        assertThatThrownBy(() -> repo.rd(TENANT, "mailbox/size-pattern-over",
                Map.of("to", value), 1, null, 0))
                .isInstanceOf(TooLargeException.class)
                .satisfies(e -> assertThat(((TooLargeException) e).field()).isEqualTo("keys_pattern.to"));
    }

    @Test
    void in_patternValueOneByteOverCap_refused() {
        String value = "a".repeat(TupleLimits.MAX_FIELD_VALUE_BYTES + 1);
        assertThatThrownBy(() -> repo.in(TENANT, "probecap/size-in-pattern-over",
                Map.of("owner", value), "claimant", 60, 0))
                .isInstanceOf(TooLargeException.class);
    }

    // ── nonce: 128-byte cap ──────────────────────────────────────────────────

    @Test
    void out_nonceAtCap_succeeds() {
        String nonce = "n".repeat(TupleLimits.MAX_NONCE_BYTES);
        byte[] id = repo.out(TENANT, "mailbox/size-nonce-ok", Map.of("to", "size-nonce-ok"),
                Map.of("from", "s"), null, nonce, null);
        assertThat(id).isNotNull();
    }

    @Test
    void out_nonceOneByteOverCap_refused() {
        String nonce = "n".repeat(TupleLimits.MAX_NONCE_BYTES + 1);
        assertThatThrownBy(() -> repo.out(TENANT, "mailbox/size-nonce-over",
                Map.of("to", "size-nonce-over"), Map.of("from", "s"), null, nonce, null))
                .isInstanceOf(TooLargeException.class)
                .satisfies(e -> assertThat(((TooLargeException) e).field()).isEqualTo("nonce"));
    }

    // ── claimant: 128-byte cap ───────────────────────────────────────────────

    @Test
    void in_claimantOneByteOverCap_refused() {
        String claimant = "c".repeat(TupleLimits.MAX_CLAIMANT_BYTES + 1);
        assertThatThrownBy(() -> repo.in(TENANT, "probecap/size-claimant-over",
                Map.of("owner", "x"), claimant, 60, 0))
                .isInstanceOf(TooLargeException.class)
                .satisfies(e -> assertThat(((TooLargeException) e).field()).isEqualTo("claimant"));
    }

    @Test
    void ack_claimantOneByteOverCap_refusedBeforeAnyClaimLookup() {
        String claimant = "c".repeat(TupleLimits.MAX_CLAIMANT_BYTES + 1);
        assertThatThrownBy(() -> repo.ack(TENANT, "some-claim-id", claimant))
                .isInstanceOf(TooLargeException.class);
    }

    // ── claim_id: 128-byte cap ───────────────────────────────────────────────

    @Test
    void ack_claimIdOneByteOverCap_refused() {
        String claimId = "c".repeat(TupleLimits.MAX_CLAIM_ID_BYTES + 1);
        assertThatThrownBy(() -> repo.ack(TENANT, claimId, "claimant"))
                .isInstanceOf(TooLargeException.class)
                .satisfies(e -> assertThat(((TooLargeException) e).field()).isEqualTo("claim_id"));
    }

    @Test
    void nack_claimIdOneByteOverCap_refused() {
        String claimId = "c".repeat(TupleLimits.MAX_CLAIM_ID_BYTES + 1);
        assertThatThrownBy(() -> repo.nack(TENANT, claimId, "claimant"))
                .isInstanceOf(TooLargeException.class);
    }

    @Test
    void renew_claimIdOneByteOverCap_refused() {
        String claimId = "c".repeat(TupleLimits.MAX_CLAIM_ID_BYTES + 1);
        assertThatThrownBy(() -> repo.renew(TENANT, claimId, "claimant", 60))
                .isInstanceOf(TooLargeException.class);
    }

    // ── ack-with-reply: size checks run before the transaction opens ───────

    @Test
    void ackWithReply_oversizedReplyBody_refusedAndClaimStaysClaimedAndAckable() {
        byte[] id = repo.out(TENANT, "mailbox/size-reply-source",
                Map.of("to", "size-reply-source"), Map.of("from", "sender"), null,
                "n-reply-source", null);
        Optional<TupleRepository.ClaimedTuple> claimed = repo.inp(TENANT,
                "mailbox/size-reply-source", Map.of("to", "size-reply-source"), "reply-claimant", 300);
        assertThat(claimed).isPresent();
        String claimId = claimed.get().claimId();

        String oversizedReplyBody = "x".repeat(TupleLimits.MAX_BODY_BYTES + 1);
        TupleRepository.ReplySpec reply = new TupleRepository.ReplySpec(
                "mailbox/size-reply-target", Map.of("to", "size-reply-target"), Map.of("from", "sender"),
                oversizedReplyBody, null);

        assertThatThrownBy(() -> repo.ackWithReply(TENANT, claimId, "reply-claimant", reply))
                .isInstanceOf(TooLargeException.class);

        // RDR-206 contract (bead nexus-r7xao extends it to a size refusal): a
        // refused reply must leave the request STILL CLAIMED and STILL ACKABLE by
        // the same claimant -- a plain ack (no reply) must still succeed.
        repo.ack(TENANT, claimId, "reply-claimant");
    }

    @Test
    void ackWithReply_replyClaimantOverCap_refusedBeforePrepareOut() {
        byte[] id = repo.out(TENANT, "mailbox/size-reply-claimant-source",
                Map.of("to", "size-reply-claimant-source"), Map.of("from", "sender"), null,
                "n-reply-claimant-source", null);
        Optional<TupleRepository.ClaimedTuple> claimed = repo.inp(TENANT,
                "mailbox/size-reply-claimant-source", Map.of("to", "size-reply-claimant-source"),
                "reply-claimant-2", 300);
        assertThat(claimed).isPresent();
        String claimId = claimed.get().claimId();

        String tooLongClaimant = "c".repeat(TupleLimits.MAX_CLAIMANT_BYTES + 1);
        assertThatThrownBy(() -> repo.ackWithReply(TENANT, claimId, tooLongClaimant, null))
                .isInstanceOf(TooLargeException.class);

        // Still claimed by the ORIGINAL claimant, since the oversized-claimant
        // ack never reached consumeClaim.
        repo.ack(TENANT, claimId, "reply-claimant-2");
    }

    // ── never echoes the oversized value ─────────────────────────────────────

    @Test
    void tooLargeException_neverEchoesTheOversizedValue() {
        String secretLookingBody = "SECRET-" + "x".repeat(TupleLimits.MAX_BODY_BYTES);
        try {
            repo.out(TENANT, "mailbox/size-no-echo", Map.of("to", "size-no-echo"),
                    Map.of("from", "s"), secretLookingBody, "n-no-echo", null);
            throw new AssertionError("expected TooLargeException");
        } catch (TooLargeException e) {
            assertThat(e.getMessage()).doesNotContain("SECRET");
            assertThat(e.getMessage()).contains("body");
        }
    }

    // ── SchemaViolation still fires when size is fine but shape is not ──────

    @Test
    void out_sizeOk_butUnknownKey_stillRaisesSchemaViolation_notTooLarge() {
        assertThatThrownBy(() -> repo.out(TENANT, "mailbox/size-shape-still-checked",
                Map.of("to", "size-shape-still-checked", "bogus", "v"), Map.of("from", "s"), null,
                "n-shape", null))
                .isInstanceOf(SchemaViolationException.class);
    }
}
