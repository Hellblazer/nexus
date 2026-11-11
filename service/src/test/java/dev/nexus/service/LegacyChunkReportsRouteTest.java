// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNK_ORPHANED_AT;
import static dev.nexus.service.jooq.nexus.Tables.FRECENCY;
import static dev.nexus.service.jooq.nexus.Tables.RELEVANCE_LOG;
import static dev.nexus.service.jooq.nexus.Tables.TOPICS;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.AdminConnection;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.ChunkReaper.Settings;
import dev.nexus.service.db.ChashConstraintValidator;
import dev.nexus.service.db.LegacyChunkSurvey;
import dev.nexus.service.db.LadderRepository;
import dev.nexus.service.db.Rdr192BackfillGate;
import dev.nexus.service.vectors.ReaperRepository;
import dev.nexus.service.db.TenantConstants;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.db.TokenStore;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.jooq.nexus.tables.records.ChunksRecord;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.DSLContext;
import org.jooq.JSONB;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.jooq.impl.SQLDataType;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.security.MessageDigest;
import java.sql.Connection;
import java.time.Clock;
import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HexFormat;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;

/**
 * nexus-f3yqd, engine 0.1.190: what the chash VALIDATE job's refusals and the doctor's opt-in {@code detail=legacy_chunks} say about
 * non-32-byte chunk and manifest rows: counts per reason, the bad chunks listed (bounded, key order, an exact capped flag) with
 * their owning documents, and no route named (the engine has no repair for them). Each test names the mutation it kills.
 */
class LegacyChunkReportsRouteTest extends LegacyChashFixture {

    private static byte[] key(int first, int second) {
        byte[] k = new byte[16];
        k[0] = (byte) first;
        k[1] = (byte) second;
        return k;
    }

    /**
     * The refusal of the chunks table counts its bad rows per reason, lists them with their owners and names NO route; the
     * manifest's refusal counts per reason by what each row's chunk is and carries no chunk list. Kills: a refusal without the
     * per-reason account; a list without owners; a next_step (or any route) in either refusal.
     */
    @Test
    void theValidateRefusal_countsPerReason_listsTheChunksWithOwners_andNamesNoRoute() throws Exception {
        byte[] held = HexFormat.of().parseHex("ef".repeat(16));       // no text, held by a manifest row
        byte[] wide = HexFormat.of().parseHex("cd".repeat(17));       // 17 bytes, no owner
        byte[] wrong = HexFormat.of().parseHex("ab".repeat(16));      // a 16-byte key that is not its text's digest prefix
        seed(ctx -> {
            document(ctx, TENANT_A, "1.1", COLLECTION, "complete");
            chunk(ctx, TENANT_A, COLLECTION, legacy("digest key"), "digest key", unit(1f), null, OffsetDateTime.now().minusHours(2));
            manifest(ctx, TENANT_A, COLLECTION, "1.1", 0, legacy("digest key"));
            chunk(ctx, TENANT_A, COLLECTION, wrong, "mismatched", unit(1f), null, OffsetDateTime.now().minusHours(2));
            referenceOnlyChunk(ctx, TENANT_A, COLLECTION, held);
            manifest(ctx, TENANT_A, COLLECTION, "1.1", 1, held);
            chunk(ctx, TENANT_A, COLLECTION, wide, "wide", unit(1f), null, OffsetDateTime.now().minusHours(2));
        });

        JsonNode job = validateJob();
        assertThat(job.get("all_validated").asBoolean()).isFalse();
        JsonNode leaf = refusalOf(job, "nexus.chunks");
        assertThat(counts(leaf.get("reasons"))).containsOnlyKeys("legacy_digest_key", "text_mismatch", "no_text", "unexpected_width")
            .containsEntry("legacy_digest_key", 1L).containsEntry("text_mismatch", 1L).containsEntry("no_text", 1L).containsEntry("unexpected_width", 1L);
        assertThat(leaf.get("chunks")).hasSize(4);
        assertThat(leaf.get("chunks_capped").asBoolean()).isFalse();
        for (JsonNode c : leaf.get("chunks")) {
            assertThat(c.get("tenant").asText()).isEqualTo(TENANT_A);
            switch (c.get("reason").asText()) {
                case "legacy_digest_key", "no_text" -> {
                    assertThat(c.get("owners")).hasSize(1);
                    assertThat(c.get("owners").get(0).get("doc_id").asText()).isEqualTo("1.1");
                    assertThat(c.get("owners").get(0).get("via").asText()).isEqualTo("manifest");
                }
                default -> {
                    assertThat(c.get("owners").isArray()).isTrue();
                    assertThat(c.get("owners")).as("an explicit no owner").isEmpty();
                }
            }
        }
        JsonNode manifestRefusal = refusalOf(job, "nexus.catalog_document_chunks");
        assertThat(counts(manifestRefusal.get("reasons"))).containsOnlyKeys("legacy_digest_key", "no_text");
        assertThat(manifestRefusal.has("chunks")).as("the manifest refusal carries counts, not a chunk list").isFalse();
        for (JsonNode r : new JsonNode[]{leaf, manifestRefusal}) {
            assertThat(r.fieldNames()).toIterable().noneMatch(n -> n.startsWith("next_step") || n.contains("rekey") || n.contains("route"));
            // no field NAME and no string VALUE anywhere in the refusal names a route or a repair
            assertThat(r.toString().toLowerCase()).doesNotContain("rekey", "re-key", "/v1/", "next step", "repair");
        }
    }

    /**
     * The chunk list is bounded at 20 with an exact capped flag, in the VALIDATE refusal and the doctor: 20 bad chunks list all
     * 20 with capped false, a 21st makes it true. Kills: a limit of cap rather than cap+1 (capped stays false at 21); a flag
     * computed from a filtered list.
     */
    @Test
    void theChunkList_isBoundedAtTwenty_withAnExactCappedFlag_inTheValidateRefusalAndTheDoctor() throws Exception {
        seed(ctx -> {
            for (int i = 1; i <= 20; i++) referenceOnlyChunk(ctx, TENANT_A, COLLECTION, key(0xee, i));
        });
        JsonNode leaf = refusalOf(validateJob(), "nexus.chunks");
        assertThat(leaf.get("chunks")).hasSize(20);
        assertThat(leaf.get("chunks_capped").asBoolean()).as("exactly 20: nothing beyond the list").isFalse();
        JsonNode doctor = get(TOKEN_A, "/v1/schema/chash-conformance?detail=legacy_chunks").get("legacy_chunks");
        assertThat(doctor.get("chunks")).hasSize(20);
        assertThat(doctor.get("chunks_capped").asBoolean()).isFalse();

        seed(ctx -> referenceOnlyChunk(ctx, TENANT_A, COLLECTION, key(0xee, 21)));
        JsonNode capped = refusalOf(validateJob(), "nexus.chunks");
        assertThat(capped.get("chunks")).hasSize(20);
        assertThat(capped.get("chunks_capped").asBoolean()).isTrue();
        JsonNode cappedDoctor = get(TOKEN_A, "/v1/schema/chash-conformance?detail=legacy_chunks").get("legacy_chunks");
        assertThat(cappedDoctor.get("chunks")).hasSize(20);
        assertThat(cappedDoctor.get("chunks_capped").asBoolean()).isTrue();
    }

    /**
     * Capped counts are the first rows in key order (the same sample every run), never whichever rows the scan met first: with the
     * cap at 3 the sample is the three no_text chunks that sort first, though a digest-key chunk was written before them. Kills:
     * counts taken without the ORDER BY.
     */
    @Test
    void cappedCounts_areADeterministicKeyOrderedSample() throws Exception {
        seed(ctx -> {
            document(ctx, TENANT_A, "1.1", COLLECTION, "complete");
            byte[] k = legacy("sorts after the sample");     // written first, so an unordered sample would meet it first
            org.assertj.core.api.Assertions.assertThat(Arrays.compareUnsigned(k, key(0, 3))).isGreaterThan(0);
            chunk(ctx, TENANT_A, COLLECTION, k, "sorts after the sample", unit(1f), null, OffsetDateTime.now().minusHours(2));
            for (int i = 0; i < 3; i++) referenceOnlyChunk(ctx, TENANT_A, COLLECTION, key(0, i));
        });
        String prior = System.getProperty(ChashConstraintValidator.PRECHECK_CAP_PROPERTY);
        System.setProperty(ChashConstraintValidator.PRECHECK_CAP_PROPERTY, "3");
        try {
            JsonNode first = refusalOf(validateJob(), "nexus.chunks");
            assertThat(counts(first.get("reasons"))).as("the first three bad rows in key order").containsOnlyKeys("no_text").containsEntry("no_text", 3L);
            assertThat(first.get("reasons_capped").asBoolean()).isTrue();
            assertThat(counts(refusalOf(validateJob(), "nexus.chunks").get("reasons"))).as("the same sample every run").isEqualTo(counts(first.get("reasons")));
        } finally {
            if (prior == null) System.clearProperty(ChashConstraintValidator.PRECHECK_CAP_PROPERTY);
            else System.setProperty(ChashConstraintValidator.PRECHECK_CAP_PROPERTY, prior);
        }
    }

    /**
     * The doctor route stays a catalog-only read by default; with detail=legacy_chunks it adds, for the caller's own tenant, the
     * per-reason counts and the chunks with their owners, bounded. The owners are found the ways the engine knows an owner: the
     * manifest, a note stamped with the chunk's hex, and none (an explicit empty list). Kills: the detail always on (a scan on
     * every doctor call); the detail reading another tenant's rows; an owners lookup that reads only the manifest.
     */
    @Test
    void theDoctorRoute_addsTheLegacyChunkDetailOnlyWhenAsked_forTheCallersOwnTenant_withOwnersByManifestNoteOrNone() throws Exception {
        byte[] held = HexFormat.of().parseHex("ef".repeat(16));
        byte[] stamped = HexFormat.of().parseHex("cd".repeat(17));
        byte[] nobody = HexFormat.of().parseHex("ee".repeat(16));
        seed(ctx -> {
            document(ctx, TENANT_A, "1.1", COLLECTION, "complete");
            referenceOnlyChunk(ctx, TENANT_A, COLLECTION, held);
            manifest(ctx, TENANT_A, COLLECTION, "1.1", 0, held);
            note(ctx, TENANT_A, "1.2", COLLECTION, hex(stamped));
            chunk(ctx, TENANT_A, COLLECTION, stamped, "seventeen byte key", unit(1f), null, OffsetDateTime.now().minusHours(2));
            referenceOnlyChunk(ctx, TENANT_A, COLLECTION, nobody);
            referenceOnlyChunk(ctx, TENANT_B, COLLECTION, HexFormat.of().parseHex("aa".repeat(16)));
        });
        assertThat(get(TOKEN_A, "/v1/schema/chash-conformance").has("legacy_chunks")).as("off by default").isFalse();
        assertThat(get(TOKEN_A, "/v1/schema/chash-conformance?detail=other").has("legacy_chunks")).as("and for any other value").isFalse();

        JsonNode d = get(TOKEN_A, "/v1/schema/chash-conformance?detail=legacy_chunks").get("legacy_chunks");

        assertThat(d.get("available").asBoolean()).isTrue();
        assertThat(d.get("deadline_exceeded").asBoolean()).isFalse();
        assertThat(counts(d.get("chunk_reasons"))).as("only tenant A's chunks").containsOnlyKeys("no_text", "unexpected_width")
            .containsEntry("no_text", 2L).containsEntry("unexpected_width", 1L);
        assertThat(counts(d.get("manifest_reasons"))).containsOnlyKeys("no_text").containsEntry("no_text", 1L);
        assertThat(d.get("chunks")).hasSize(3);
        assertThat(d.get("chunks_capped").asBoolean()).isFalse();
        for (JsonNode c : d.get("chunks")) {
            JsonNode owners = c.get("owners");
            switch (c.get("chash_hex").asText()) {
                case "efefefefefefefefefefefefefefefef" -> {
                    assertThat(owners).hasSize(1);
                    assertThat(owners.get(0).get("doc_id").asText()).isEqualTo("1.1");
                    assertThat(owners.get(0).get("title").asText()).isEqualTo("doc 1.1");
                    assertThat(owners.get(0).get("via").asText()).isEqualTo("manifest");
                }
                case "cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd" -> {
                    assertThat(c.get("reason").asText()).isEqualTo("unexpected_width");
                    assertThat(owners).hasSize(1);
                    assertThat(owners.get(0).get("doc_id").asText()).isEqualTo("1.2");
                    assertThat(owners.get(0).get("via").asText()).isEqualTo("note");
                }
                case "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee" -> {
                    assertThat(owners.isArray()).isTrue();
                    assertThat(owners).as("no owner is an explicit empty list").isEmpty();
                }
                default -> throw new AssertionError("another tenant's or an unexpected chunk: " + c);
            }
        }
    }

    /**
     * The doctor detail has one deadline over its scans and owner lookups: with none left it runs none of them, says partial, and
     * lists nothing; with time it is whole. Kills: per-statement bounds only (a spent deadline still scans).
     */
    @Test
    void theDoctorDetailHonoursOneRequestDeadline() throws Exception {
        seed(ctx -> {
            document(ctx, TENANT_A, "1.1", COLLECTION, "complete");
            referenceOnlyChunk(ctx, TENANT_A, COLLECTION, key(0xee, 1));
            manifest(ctx, TENANT_A, COLLECTION, "1.1", 0, key(0xee, 1));
        });
        var scope = new TenantScope(ds);
        var none = LegacyChunkSurvey.forTenant(scope, TENANT_A, 1000, 20, 5000, 0L);
        assertThat(none.partial()).isTrue();
        assertThat(none.chunks()).as("nothing was scanned past the deadline: unknown, not empty").isNull();
        assertThat(none.chunkCounts()).isNull();

        var whole = LegacyChunkSurvey.forTenant(scope, TENANT_A, 1000, 20, 5000, 60_000L);
        assertThat(whole.partial()).isFalse();
        assertThat(whole.chunks()).hasSize(1);
        assertThat(whole.chunks().get(0).owners().owners()).hasSize(1);
    }

    // ── fix round: exact flags, deadlines, tenant fences, honesty ───────────

    private static byte[] key(int first, int second, int third) {
        byte[] k = new byte[16];
        k[0] = (byte) first;
        k[1] = (byte) second;
        k[2] = (byte) third;
        return k;
    }

    private static void withProperty(String name, String value, Runnable body) {
        String prior = System.getProperty(name);
        System.setProperty(name, value);
        try {
            body.run();
        } finally {
            if (prior == null) System.clearProperty(name);
            else System.setProperty(name, prior);
        }
    }

    /** N reference-only chunks (keys 0xdd, i), each named by a manifest row of its own document. */
    private void ownedNoTextChunks(String tenant, int from, int to) throws Exception {
        seed(ctx -> {
            for (int i = from; i <= to; i++) {
                if (ctx.fetchCount(CATALOG_DOCUMENTS, CATALOG_DOCUMENTS.TENANT_ID.eq(tenant).and(CATALOG_DOCUMENTS.TUMBLER.eq("1." + i))) == 0) {
                    document(ctx, tenant, "1." + i, COLLECTION, "complete");
                }
                referenceOnlyChunk(ctx, tenant, COLLECTION, key(0xdd, i));
                manifest(ctx, tenant, COLLECTION, "1." + i, 0, key(0xdd, i));
            }
        });
    }

    /**
     * The capped flags are exact at the cap in every account: with the cap at 3, exactly 3 bad rows are not capped and a 4th makes
     * them so, for the pre-check's bad_rows, the VALIDATE refusal's reasons (chunks and manifest) and the doctor's chunk and
     * manifest reasons. Kills: a flag read as count >= cap (true at exactly the cap), in LegacyChunkSurvey and in the pre-check.
     */
    @Test
    void theCappedFlags_areExactAtTheCap_inThePrecheck_theValidateRefusalAndTheDoctorCounts() throws Exception {
        ownedNoTextChunks(TENANT_A, 1, 3);
        withProperty(ChashConstraintValidator.PRECHECK_CAP_PROPERTY, "3", () -> {
            try {
                JsonNode job = validateJob();
                for (String table : new String[]{"nexus.chunks", "nexus.catalog_document_chunks"}) {
                    JsonNode r = refusalOf(job, table);
                    assertThat(r.get("bad_rows").asLong()).as(table).isEqualTo(3);
                    assertThat(r.get("bad_rows_capped").asBoolean()).as(table + ": exactly the cap").isFalse();
                    assertThat(r.get("reasons_capped").asBoolean()).as(table + ": exactly the cap").isFalse();
                }
                var exact = LegacyChunkSurvey.forTenant(new TenantScope(ds), TENANT_A, 3, 20, 5000, 60_000L);
                assertThat(exact.chunkCounts().capped()).isFalse();
                assertThat(exact.manifestCounts().capped()).isFalse();
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        });
        ownedNoTextChunks(TENANT_A, 4, 4);
        withProperty(ChashConstraintValidator.PRECHECK_CAP_PROPERTY, "3", () -> {
            try {
                JsonNode job = validateJob();
                for (String table : new String[]{"nexus.chunks", "nexus.catalog_document_chunks"}) {
                    JsonNode r = refusalOf(job, table);
                    assertThat(r.get("bad_rows").asLong()).as(table).isEqualTo(3);
                    assertThat(r.get("bad_rows_capped").asBoolean()).as(table + ": more rows than the cap").isTrue();
                    assertThat(r.get("reasons_capped").asBoolean()).as(table + ": more rows than the cap").isTrue();
                    assertThat(counts(r.get("reasons")).values().stream().mapToLong(Long::longValue).sum()).as("at most the cap").isEqualTo(3);
                }
                var over = LegacyChunkSurvey.forTenant(new TenantScope(ds), TENANT_A, 3, 20, 5000, 60_000L);
                assertThat(over.chunkCounts().capped()).isTrue();
                assertThat(over.manifestCounts().capped()).isTrue();
                assertThat(over.chunkCounts().byReason().values().stream().mapToLong(Long::longValue).sum()).isEqualTo(3);
            } catch (Exception e) {
                throw new IllegalStateException(e);
            }
        });
    }

    /**
     * A chunk with exactly 5 owners lists them with owners_capped false; a 6th makes it true, listing 5. Kills: an owners cap read
     * as >= (true at 5), or a list that is not cut.
     */
    @Test
    void ownersAreCappedAtFive_withAnExactFlag() throws Exception {
        byte[] shared = key(0xcc, 1, 1);
        seed(ctx -> {
            referenceOnlyChunk(ctx, TENANT_A, COLLECTION, shared);
            for (int i = 1; i <= 5; i++) {
                document(ctx, TENANT_A, "2." + i, COLLECTION, "complete");
                manifest(ctx, TENANT_A, COLLECTION, "2." + i, 0, shared);
            }
        });
        JsonNode five = get(TOKEN_A, "/v1/schema/chash-conformance?detail=legacy_chunks").get("legacy_chunks").get("chunks").get(0);
        assertThat(five.get("owners")).hasSize(5);
        assertThat(five.get("owners_capped").asBoolean()).isFalse();

        seed(ctx -> {
            document(ctx, TENANT_A, "2.6", COLLECTION, "complete");
            manifest(ctx, TENANT_A, COLLECTION, "2.6", 0, shared);
        });
        JsonNode six = get(TOKEN_A, "/v1/schema/chash-conformance?detail=legacy_chunks").get("legacy_chunks").get("chunks").get(0);
        assertThat(six.get("owners")).hasSize(5);
        assertThat(six.get("owners_capped").asBoolean()).isTrue();
        JsonNode leaf = refusalOf(validateJob(), "nexus.chunks");
        assertThat(leaf.get("chunks").get(0).get("owners_capped").asBoolean()).as("and in the VALIDATE refusal").isTrue();
    }

    /**
     * Tenant B has a document with the SAME tumbler (1.1, its own title), a manifest row and a note on the very collection, model
     * and chash of one of A's chunks. Run on a session that row-level security does not apply to (a superuser's), so the explicit
     * tenant predicates are the only fence: the owners lookup (its manifest-to-document join included), the doctor's counts, chunk
     * list and documents, and the VALIDATE refusal's per-chunk owners and manifest documents report only A's titles for A. Kills:
     * dropping the tenant predicate from the owners lookup or from the join of a manifest row to its document (tenant A's 1.1
     * would also carry tenant B's title), from the chunk and manifest counts, or from the document list.
     */
    @Test
    void anotherTenantsDocumentsOnTheSameChash_areNeverReported_withNoRowLevelSecurityToHideThem() throws Exception {
        byte[] shared = key(0xbb, 1, 1);
        seed(ctx -> {
            document(ctx, TENANT_A, "1.1", COLLECTION, "complete");
            ctx.insertInto(CATALOG_DOCUMENTS).set(CATALOG_DOCUMENTS.TENANT_ID, TENANT_B).set(CATALOG_DOCUMENTS.TUMBLER, "1.1")
                .set(CATALOG_DOCUMENTS.TITLE, "tenant B one").set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, COLLECTION).execute();
            for (String t : new String[]{TENANT_A, TENANT_B}) {
                referenceOnlyChunk(ctx, t, COLLECTION, shared);
                manifest(ctx, t, COLLECTION, "1.1", 0, shared);
            }
            note(ctx, TENANT_B, "9.8", COLLECTION, hex(shared));
            note(ctx, TENANT_A, "1.2", COLLECTION, hex(shared));
        });

        try (Connection su = pg.createConnection("")) {
            var owners = LegacyChunkSurvey.owners(DSL.using(su, SQLDialect.POSTGRES), TENANT_A, COLLECTION, MODEL, shared);
            assertThat(owners.owners()).extracting(LegacyChunkSurvey.Owner::docId, LegacyChunkSurvey.Owner::title)
                .containsExactlyInAnyOrder(org.assertj.core.api.Assertions.tuple("1.1", "doc 1.1"), org.assertj.core.api.Assertions.tuple("1.2", "note 1.2"));
        }
        var detail = LegacyChunkSurvey.forTenant(superuserScope(), TENANT_A, 1000, 20, 5000, 60_000L);
        assertThat(detail.chunkCounts().byReason()).as("only A's chunk").containsOnlyKeys("no_text").containsEntry("no_text", 1L);
        assertThat(detail.manifestCounts().byReason()).containsEntry("no_text", 1L);
        assertThat(detail.chunks()).hasSize(1);
        assertThat(detail.chunks().get(0).owners().owners()).extracting(LegacyChunkSurvey.Owner::docId, LegacyChunkSurvey.Owner::title)
            .containsExactlyInAnyOrder(org.assertj.core.api.Assertions.tuple("1.1", "doc 1.1"), org.assertj.core.api.Assertions.tuple("1.2", "note 1.2"));
        assertThat(detail.manifestDocs().get("no_text").docs())
            .extracting(LegacyChunkSurvey.DocRef::tenant, LegacyChunkSurvey.DocRef::docId, LegacyChunkSurvey.DocRef::title)
            .containsExactly(org.assertj.core.api.Assertions.tuple(TENANT_A, "1.1", "doc 1.1"));

        var original = new ChashConstraintValidator(new AdminConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword()), new TenantScope(ds),
            () -> List.of(TENANT_A, TENANT_B));
        service.chashConstraintValidator(new ChashConstraintValidator(new AdminConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword()),
            superuserScope(), () -> List.of(TENANT_A, TENANT_B)));
        try {
            JsonNode job = validateJob();
            boolean sawA = false;
            for (JsonNode r : job.get("refused")) {
                if (!r.get("table").asText().startsWith("nexus.chunks")) continue;
                for (JsonNode c : r.get("chunks")) {
                    var owners = new ArrayList<String>();
                    c.get("owners").forEach(o -> owners.add(o.get("doc_id").asText() + "/" + o.get("title").asText()));
                    if (c.get("tenant").asText().equals(TENANT_A)) {
                        sawA = true;
                        assertThat(owners).containsExactlyInAnyOrder("1.1/doc 1.1", "1.2/note 1.2");
                    } else {
                        assertThat(owners).containsExactlyInAnyOrder("1.1/tenant B one", "9.8/note 9.8");
                    }
                }
            }
            assertThat(sawA).isTrue();
            JsonNode manifest = refusalOf(job, "nexus.catalog_document_chunks");
            var docs = new ArrayList<String>();
            manifest.get("documents").get("no_text").get("docs")
                .forEach(d -> docs.add(d.get("tenant").asText() + "/" + d.get("doc_id").asText() + "/" + d.get("title").asText()));
            assertThat(docs).containsExactlyInAnyOrder(TENANT_A + "/1.1/doc 1.1", TENANT_B + "/1.1/tenant B one");
        } finally {
            service.chashConstraintValidator(original);
        }
    }

    /** Neither report writes: every row of the chunk, manifest and document tables is the same after the VALIDATE job and the doctor detail. */
    @Test
    void neitherReportChangesAnyRow() throws Exception {
        seed(ctx -> {
            document(ctx, TENANT_A, "1.1", COLLECTION, "complete");
            referenceOnlyChunk(ctx, TENANT_A, COLLECTION, key(0xaa, 1, 1));
            manifest(ctx, TENANT_A, COLLECTION, "1.1", 0, key(0xaa, 1, 1));
            chunk(ctx, TENANT_A, COLLECTION, legacy("a digest key"), "a digest key", unit(1f), null, OffsetDateTime.now().minusHours(2));
            note(ctx, TENANT_A, "1.2", COLLECTION, hex(legacy("a digest key")));
        });
        List<String> before = snapshot();
        assertThat(before).isNotEmpty();

        validateJob();
        get(TOKEN_A, "/v1/schema/chash-conformance?detail=legacy_chunks");

        assertThat(snapshot()).isEqualTo(before);
    }

    /**
     * The owners lookup asks its gate before EACH of its two statements: a gate that grants one and refuses the next, or refuses
     * the first, makes the whole answer unknown (null owners, never an empty list), and a gate that grants both reads the chunk
     * as having none. Kills: a gate asked once per lookup (the second statement overshoots); an empty list for a lookup that did
     * not finish.
     */
    @Test
    void theOwnersLookupAsksItsGateBeforeEveryStatement_andAnUnfinishedLookupIsNullNotEmpty() throws Exception {
        byte[] nobody = key(0x99, 1, 1);
        seed(ctx -> referenceOnlyChunk(ctx, TENANT_A, COLLECTION, nobody));
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            int[] asked = {0};
            var second = LegacyChunkSurvey.owners(ctx, TENANT_A, COLLECTION, MODEL, nobody, 5, c -> ++asked[0] < 2);
            assertThat(asked[0]).as("the note arm is a second statement, gated too").isEqualTo(2);
            assertThat(second.owners()).as("refused before the second statement: unknown, not none").isNull();
            assertThat(LegacyChunkSurvey.owners(ctx, TENANT_A, COLLECTION, MODEL, nobody, 5, c -> false).owners()).isNull();
            assertThat(LegacyChunkSurvey.owners(ctx, TENANT_A, COLLECTION, MODEL, nobody, 5, c -> true).owners()).as("both ran: no owner").isEmpty();
        }
    }

    /**
     * A statement cut by the request deadline inside the doctor detail does not discard what finished: with the MANIFEST locked so
     * its scans wait past their (short) bound, the chunk stages finish and keep their answers, the manifest stages are cut (null,
     * unknown, never an empty answer), the owners the cut lookups never reached are null, and the result says partial. Kills: a
     * cut statement that throws (everything lost); a cut that aborts or blanks the stages that finished; a cut stage given as an
     * empty answer.
     */
    @Test
    void aStatementCutByTheDeadline_keepsTheStagesThatFinished_rendersTheCutOnesUnknown_andSaysPartial() throws Exception {
        seed(ctx -> {
            document(ctx, TENANT_A, "1.1", COLLECTION, "complete");
            referenceOnlyChunk(ctx, TENANT_A, COLLECTION, key(0x88, 1, 1));
            manifest(ctx, TENANT_A, COLLECTION, "1.1", 0, key(0x88, 1, 1));
        });
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            PgContainerHelper.lockTable(holder, CATALOG_DOCUMENT_CHUNKS, PgContainerHelper.LockMode.ACCESS_EXCLUSIVE);
            var d = LegacyChunkSurvey.forTenant(new TenantScope(ds), TENANT_A, 1000, 20, 400, 60_000L);
            assertThat(d.partial()).isTrue();
            assertThat(d.chunkCounts().byReason()).as("the chunk stages finished").containsEntry("no_text", 1L);
            assertThat(d.chunks()).hasSize(1);
            assertThat(d.chunks().get(0).reason()).isEqualTo("no_text");
            assertThat(d.chunks().get(0).owners().owners()).as("the owner lookup was cut: unknown, not none").isNull();
            assertThat(d.manifestCounts()).as("the manifest stages were cut: unknown").isNull();
            assertThat(d.manifestDocs()).isNull();
            holder.rollback();
        }
        var whole = LegacyChunkSurvey.forTenant(new TenantScope(ds), TENANT_A, 1000, 20, 400, 60_000L);
        assertThat(whole.partial()).isFalse();
        assertThat(whole.manifestCounts().byReason()).containsEntry("no_text", 1L);
        assertThat(whole.chunks().get(0).owners().owners()).hasSize(1);
    }

    /**
     * A lock the request could not get within its bound (55P03) is the request running out of time like a statement timeout: the
     * stages that did not get their lock are unknown, the result is partial, and the detail still answers instead of failing
     * whole. Kills: a lock timeout that propagates (available:false).
     */
    @Test
    void aLockTheRequestCouldNotGet_isPartial_notAFailure() throws Exception {
        seed(ctx -> referenceOnlyChunk(ctx, TENANT_A, COLLECTION, key(0x87, 1, 1)));
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            PgContainerHelper.lockTable(holder, CHUNKS, PgContainerHelper.LockMode.ACCESS_EXCLUSIVE);
            var d = LegacyChunkSurvey.forTenant(new TenantScope(ds), TENANT_A, 1000, 20, 30_000, 60_000L);   // the 2 s lock bound ends it, not the statement's
            assertThat(d.partial()).isTrue();
            assertThat(d.chunkCounts()).isNull();
            assertThat(d.chunks()).isNull();
            assertThat(d.manifestCounts()).as("the manifest account reads each row's chunk too: also unknown").isNull();
            assertThat(d.manifestDocs()).isNull();
            holder.rollback();
        }
    }

    /**
     * The manifest refusal carries the documents that hold the bad rows, per reason, with titles and an exact cap of 20; the
     * reasons are marked capped when a tenant with bad rows was not counted (no token: here tenant C, which the validator does not
     * know) rather than a short sum passing as complete. Kills: no documents; a cap read as >=; a short sum with reasons_capped
     * false.
     */
    @Test
    void theManifestRefusalListsItsDocuments_withAnExactCap_andFlagsATenantItDidNotCount() throws Exception {
        ownedNoTextChunks(TENANT_A, 1, 20);
        JsonNode docs = refusalOf(validateJob(), "nexus.catalog_document_chunks").get("documents").get("no_text");
        assertThat(docs.get("docs")).hasSize(20);
        assertThat(docs.get("capped").asBoolean()).as("exactly 20 documents").isFalse();
        assertThat(docs.get("docs").get(0).get("title").asText()).isEqualTo("doc 1.1");

        ownedNoTextChunks(TENANT_A, 21, 21);
        JsonNode over = refusalOf(validateJob(), "nexus.catalog_document_chunks");
        assertThat(over.get("documents").get("no_text").get("docs")).hasSize(20);
        assertThat(over.get("documents").get("no_text").get("capped").asBoolean()).isTrue();

        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            PgContainerHelper.ensureTenantPartitions(ctx, "t-legacy-c");
            PgContainerHelper.insertCollection(ctx, "t-legacy-c", COLLECTION);
        }
        seed(ctx -> {
            document(ctx, "t-legacy-c", "7.1", COLLECTION, "complete");
            referenceOnlyChunk(ctx, "t-legacy-c", COLLECTION, key(0x77, 1, 1));
            manifest(ctx, "t-legacy-c", COLLECTION, "7.1", 0, key(0x77, 1, 1));
        });
        // clean the tenant A rows so only the uncounted tenant's row differs from the sum
        seed(ctx -> {
            ctx.deleteFrom(CATALOG_DOCUMENT_CHUNKS).where(CATALOG_DOCUMENT_CHUNKS.TENANT_ID.eq(TENANT_A)).execute();
            ctx.deleteFrom(CHUNKS).where(CHUNKS.TENANT_ID.eq(TENANT_A)).execute();
        });
        seed(ctx -> {
            document(ctx, TENANT_A, "1.1", COLLECTION, "complete");
            referenceOnlyChunk(ctx, TENANT_A, COLLECTION, key(0x66, 1, 1));
            manifest(ctx, TENANT_A, COLLECTION, "1.1", 0, key(0x66, 1, 1));
        });
        JsonNode uncounted = refusalOf(validateJob(), "nexus.catalog_document_chunks");
        assertThat(uncounted.get("bad_rows").asLong()).as("the admin session sees both tenants' rows").isEqualTo(2);
        assertThat(counts(uncounted.get("reasons")).values().stream().mapToLong(Long::longValue).sum()).as("the counted tenants have one").isEqualTo(1);
        assertThat(uncounted.get("reasons_capped").asBoolean()).as("a tenant not counted: the sum is a floor").isTrue();
    }

    /**
     * A manifest row with no chunk row is reported as manifest_without_chunk, with the document that holds it: possible only where
     * the manifest-to-chunk foreign key was dropped too (a validated key makes it unreachable), which this test does. Kills: a row
     * with no chunk silently left out of the reasons.
     */
    @Test
    void aManifestRowWithNoChunk_isReportedAsFkGuarded() throws Exception {
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.dropCatalogChunksFk(su);
        }
        try {
            seed(ctx -> {
                document(ctx, TENANT_A, "1.1", COLLECTION, "complete");
                manifest(ctx, TENANT_A, COLLECTION, "1.1", 0, key(0x55, 1, 1));
            });
            JsonNode manifest = refusalOf(validateJob(), "nexus.catalog_document_chunks");
            assertThat(counts(manifest.get("reasons"))).containsOnlyKeys("manifest_without_chunk").containsEntry("manifest_without_chunk", 1L);
            assertThat(manifest.get("documents").get("manifest_without_chunk").get("docs").get(0).get("doc_id").asText()).isEqualTo("1.1");
        } finally {
            try (Connection su = pg.createConnection("")) {
                su.setAutoCommit(true);
                DSL.using(su, SQLDialect.POSTGRES).deleteFrom(CATALOG_DOCUMENT_CHUNKS).execute();
                PgContainerHelper.readdCatalogChunksFkNotValid(su);
            }
        }
    }

    /** Waits until a session is waiting for a lock (the first detail call is parked on the locked table), then returns. */
    private void awaitALockWaiter() throws Exception {
        long end = System.nanoTime() + java.time.Duration.ofSeconds(20).toNanos();
        try (Connection su = pg.createConnection("")) {
            var ctx = DSL.using(su, SQLDialect.POSTGRES);
            var locks = DSL.table(DSL.name("pg_catalog", "pg_locks"));
            while (System.nanoTime() < end) {
                if (ctx.fetchCount(locks, DSL.field(DSL.name("granted"), Boolean.class).isFalse()) > 0) return;
                Thread.onSpinWait();
            }
        }
        throw new AssertionError("no session ever waited for the lock");
    }

    /**
     * One detail at a time per tenant: with the chunks table locked, the first call parks on its lock, and a call that arrives
     * meanwhile answers busy without scanning; the first ends partial once its lock bound passes, and the flight is cleared.
     * Driven on the handler directly (an HTTP request's own work on the locked table would wait too). Kills: no single flight (the
     * second call scans and waits as well); a flight never cleared.
     */
    @Test
    void theDoctorDetail_isSingleFlightPerTenant() throws Exception {
        seed(ctx -> referenceOnlyChunk(ctx, TENANT_A, COLLECTION, key(0x44, 1, 1)));
        var handler = new dev.nexus.service.http.ChashConformanceHandler(
            new dev.nexus.service.db.ChashConformanceConstraints(superuserScope()), superuserScope());
        try (Connection holder = pg.createConnection("")) {
            holder.setAutoCommit(false);
            PgContainerHelper.lockTable(holder, CHUNKS, PgContainerHelper.LockMode.ACCESS_EXCLUSIVE);
            var first = java.util.concurrent.CompletableFuture.supplyAsync(() -> handler.legacyChunks(TENANT_A));
            awaitALockWaiter();
            var busy = handler.legacyChunks(TENANT_A);
            assertThat(busy).containsEntry("available", false).containsEntry("busy", true);
            var ended = first.get(30, java.util.concurrent.TimeUnit.SECONDS);
            assertThat(ended).as("the lock it could not get is a partial answer").containsEntry("available", true).containsEntry("deadline_exceeded", true);
            holder.rollback();
        }
        assertThat(handler.legacyChunks(TENANT_A)).as("and the flight is cleared").containsEntry("available", true)
            .containsEntry("deadline_exceeded", false);
    }

    /** A read that fails for any other reason (here its database is gone) answers available:false. Kills: a failure that is not available:false. */
    @Test
    void aDetailThatFailsOutright_isAvailableFalse() throws Exception {
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(pg.getUsername());
        cfg.setPassword(pg.getPassword());
        cfg.setMaximumPoolSize(1);
        var gone = new HikariDataSource(cfg);
        var scope = new TenantScope(gone);
        gone.close();
        var handler = new dev.nexus.service.http.ChashConformanceHandler(new dev.nexus.service.db.ChashConformanceConstraints(scope), scope);
        assertThat(handler.legacyChunks(TENANT_A)).containsEntry("available", false).doesNotContainKey("busy");
    }

    /**
     * A spent request deadline (the property is read when the handler is built) is deadline_exceeded with every stage unknown (null,
     * never an empty answer); and over HTTP the detail is absent once every width CHECK is validated. Kills: a deadline that never
     * shows; a cut stage given as empty; the detail on a validated store.
     */
    @Test
    void aSpentDeadline_rendersEveryStageUnknown_andTheDetailIsAbsentOverHttpWhenValidated() throws Exception {
        seed(ctx -> referenceOnlyChunk(ctx, TENANT_A, COLLECTION, key(0x44, 1, 1)));
        withProperty("nexus.doctor.legacy_detail_deadline_ms", "0", () -> {
            var handler = new dev.nexus.service.http.ChashConformanceHandler(
                new dev.nexus.service.db.ChashConformanceConstraints(superuserScope()), superuserScope());
            var spent = handler.legacyChunks(TENANT_A);
            assertThat(spent).containsEntry("available", true).containsEntry("deadline_exceeded", true);
            assertThat(spent).containsEntry("chunk_reasons", null).containsEntry("manifest_reasons", null).containsEntry("chunks", null)
                .containsEntry("chunks_capped", null);
        });
        seed(ctx -> ctx.deleteFrom(CHUNKS).execute());
        assertThat(validateJob().get("all_validated").asBoolean()).isTrue();
        assertThat(get(TOKEN_A, "/v1/schema/chash-conformance?detail=legacy_chunks").has("legacy_chunks"))
            .as("nothing to report once every width CHECK is validated").isFalse();
    }
}
