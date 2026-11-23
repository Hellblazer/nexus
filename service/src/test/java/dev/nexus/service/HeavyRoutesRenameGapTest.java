// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.CollectionRouteFixture.Resp;
import dev.nexus.service.db.Chash;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.DynamicNode;
import org.junit.jupiter.api.DynamicTest;
import org.junit.jupiter.api.TestFactory;
import org.junit.jupiter.api.TestInstance;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.function.BiFunction;
import java.util.function.Consumer;
import java.util.stream.Stream;

import static dev.nexus.service.CollectionRouteFixture.json;
import static dev.nexus.service.CollectionRouteFixture.label;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENT_CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static dev.nexus.service.jooq.nexus.Tables.EMBEDDING_PROFILE;
import static dev.nexus.service.jooq.nexus.Tables.TOPIC_ASSIGNMENTS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5a (nexus-p2m1x.23): the rename-in-the-gap check for the HEAVY writes routes, the ones the table-driven generic test
 * ({@code CollectionRoutesByIdTest}) cannot run because an empty request answers before it reads the registry row (they need a document, a
 * stored chunk, a topic, a quarantine sibling or a source collection first). For each, a request by {@code collection_id} whose row is
 * renamed in place just before the write transaction's registry read (and its old label taken by a new row) must LAND under the row's
 * CURRENT label with its id, and put nothing under the new holder of the old label.
 *
 * <p>One case per route ({@link #CASES}). Each case seeds what the route needs, names what to store under the row's current label once the
 * hook has renamed it (a chunk, a manifest row, a topic, a quarantine sibling's chunk), sends the request by id, and then reads two things:
 * the schema-wide invariant of {@link RenameGapInvariant} (every row that carries the renamed id carries its current label; no row carries
 * the new holder's id) and what the route itself was meant to do, read from the tables. The gc routes also store a decoy for the new holder
 * of the old label (an orphan, a manifest row, a tagged chunk) that a writer which went back to the label would have moved, restored or
 * expired.
 *
 * <p>The one-row rename (P3.7) is not on this base, so the rename is the in-place update of the registry row and the fixture drops, on its
 * own throwaway databases, what that rename would drop: every inbound foreign key on a name-bearing registry key (as
 * {@code RenameGapSeededChildRowsTest} does), and the two chunk foreign keys ({@code fk_catalog_chunks_chunk},
 * {@code topic_assignments_chunk_fk}), so a manifest or assignment row can be seeded for a chunk that lives in a quarantine sibling or
 * nowhere. Nothing here is engine-run by its author; a case that cannot reach the registry read fails on {@code gap.ran()}, loudly.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class HeavyRoutesRenameGapTest {

    private static final String STAMP = "2026-01-01T00:00:00Z";
    private static final String BGE = "bge-base-en-v15-768";

    /** What one case brings to the shared run: the request, what to store after the rename, what to read after it. */
    record Plan(String body, int status, Consumer<CollectionRouteFixture.RenameGap> afterRename, Set<String> skipTables,
                boolean checkNewHolders, Consumer<Outcome> verify) {
    }

    /** What a case reads after the request: the row's id, its old and current label, the new holder's id and the answer. */
    record Outcome(CollectionRouteFixture f, String old, String current, long id, long newHolder, Resp resp) {
    }

    /** One route: its path, whether it needs the engine with an embedder and a re-embed runner, and its plan. */
    record Case(String path, boolean local, BiFunction<Ctx, String, Plan> plan) {
    }

    /** The fixture, the source's label and id, and the helpers a plan uses. */
    final class Ctx {
        final CollectionRouteFixture f;
        final String b;
        final long x;
        final String stem;

        Ctx(CollectionRouteFixture f, String b, long x, String stem) {
            this.f = f;
            this.b = b;
            this.x = x;
            this.stem = stem;
        }

        String host() {
            String h = label(stem + "-host");
            f.register(h);
            return h;
        }

        void ownerDoc(String doc, String host) {
            f.asSuperuser(c -> {
                CollectionIdSeeds.ownerDocument(c, f.tenant, doc, host);
                return null;
            });
        }

        void chunk(String collection, String hex, Map<String, Object> metadata) {
            f.asSuperuser(c -> {
                PgContainerHelper.insertChunks(c, f.tenant, collection, List.of(hex), List.of("rename gap text " + hex),
                    List.of(new float[384]), List.of(metadata));
                return null;
            });
        }

        void agedChunk(String collection, String hex) {
            chunk(collection, hex, Map.of());
            ReapableFixtures.agePastGrace(f.pg, f.tenant, collection);
        }

        void manifestRow(String doc, String collection, String hex) {
            f.asSuperuser(c -> {
                CollectionIdSeeds.manifestRows(c, f.tenant, doc, collection, 0, hex);
                return null;
            });
        }

        org.jooq.Condition chunkAt(String collection, String hex) {
            return CHUNKS.TENANT_ID.eq(f.tenant).and(CHUNKS.COLLECTION.eq(collection)).and(CHUNKS.CHASH.eq(Chash.fromHex(hex).toBytes()));
        }

        int chunksIn(String collection, String hex) {
            Integer n = f.asSuperuser(c -> c.fetchCount(CHUNKS, chunkAt(collection, hex)));
            return n;
        }

        Long chunkId(String collection, String hex) {
            return f.asSuperuser(c -> c.select(CHUNKS.COLLECTION_ID).from(CHUNKS).where(chunkAt(collection, hex)).fetchOptional(CHUNKS.COLLECTION_ID).orElse(null));
        }

        /** The named columns of the rows of {@code table} whose columns equal the key/value pairs, read as the superuser; each value binds as its own class. */
        @SuppressWarnings({"unchecked", "rawtypes"})
        List<Map<String, Object>> rows(String table, List<String> columns, Object... keyValues) {
            return f.asSuperuser(c -> {
                org.jooq.Condition where = org.jooq.impl.DSL.trueCondition();
                for (int i = 0; i < keyValues.length; i += 2) {
                    org.jooq.Field field = org.jooq.impl.DSL.field(org.jooq.impl.DSL.name((String) keyValues[i]), keyValues[i + 1].getClass());
                    where = where.and(field.eq(keyValues[i + 1]));
                }
                return c.select(columns.stream().map(n -> org.jooq.impl.DSL.field(org.jooq.impl.DSL.name(n))).toList())
                    .from(org.jooq.impl.DSL.table(org.jooq.impl.DSL.name("nexus", table))).where(where).fetch().intoMaps();
            });
        }
    }

    static String rowBody(String chash) {
        return "{\"position\":0,\"chash\":\"" + chash + "\",\"chunk_index\":0}";
    }

    // ---- the cases ------------------------------------------------------------------------------------------------------

    /** A manifest write to one document: the document is owned by another collection, the chunk is stored under the current label. */
    private static BiFunction<Ctx, String, Plan> oneDocumentManifestWrite(String answer) {
        return (t, route) -> {
            String doc = "hv-doc-" + t.stem;
            String hex = Chash.ofText("hv-" + t.stem).toHex();
            t.ownerDoc(doc, t.host());
            return new Plan("{\"doc_id\":" + json(doc) + ",\"collection_id\":" + t.x + ",\"rows\":[" + rowBody(hex) + "]}", 200,
                gap -> t.chunk(gap.currentLabelOf().get(t.x), hex, Map.of()), Set.of(), true, o -> {
                    assertThat(o.resp().body()).as("the answer").contains(answer);
                    var rows = t.rows("catalog_document_chunks", List.of("collection", "collection_id"), "tenant_id", o.f().tenant, "doc_id", doc);
                    assertThat(rows).as("the manifest row landed, once").hasSize(1);
                    assertThat(rows.get(0).get("collection")).as("under the renamed row's current label").isEqualTo(o.current());
                    assertThat(((Number) rows.get(0).get("collection_id")).longValue()).as("with its id").isEqualTo(o.id());
                });
        };
    }

    private static final List<Case> CASES = List.of(
        new Case("/v1/catalog/manifest/write", false, oneDocumentManifestWrite("\"ok\":true")),
        new Case("/v1/catalog/manifest/append", false, oneDocumentManifestWrite("\"count\"")),
        new Case("/v1/catalog/import/chunk", false, oneDocumentManifestWrite("\"imported\":1")),
        new Case("/v1/catalog/manifest/append_many", false, (t, route) -> {
            String doc = "hv-doc-" + t.stem;
            String hex = Chash.ofText("hv-" + t.stem).toHex();
            t.ownerDoc(doc, t.host());
            return new Plan("{\"collection_id\":" + t.x + ",\"docs\":[{\"doc_id\":" + json(doc) + ",\"rows\":[" + rowBody(hex) + "]}]}", 200,
                gap -> t.chunk(gap.currentLabelOf().get(t.x), hex, Map.of()), Set.of(), true, o -> {
                    assertThat(o.resp().json().path("failed_doc_ids").size()).as("no document failed: " + o.resp().body()).isZero();
                    var rows = t.rows("catalog_document_chunks", List.of("collection", "collection_id"), "tenant_id", o.f().tenant, "doc_id", doc);
                    assertThat(rows).as("the manifest row landed, once").hasSize(1);
                    assertThat(rows.get(0).get("collection")).isEqualTo(o.current());
                    assertThat(((Number) rows.get(0).get("collection_id")).longValue()).isEqualTo(o.id());
                });
        }),
        new Case("/v1/vectors/gc/quarantine-orphans", false, (t, route) -> {
            String sib = "quarantine-" + t.b;
            String mine = Chash.ofText("hv-orphan-mine-" + t.stem).toHex();
            String decoy = Chash.ofText("hv-orphan-decoy-" + t.stem).toHex();
            return new Plan("{\"collection_id\":" + t.x + ",\"quarantine_collection\":" + json(sib) + ",\"quarantined_at\":\"" + STAMP + "\"}", 200,
                gap -> {
                    t.agedChunk(gap.currentLabelOf().get(t.x), mine);
                    t.agedChunk(t.b, decoy);   // the new holder of the old label: a writer that went back to the label would quarantine it
                }, Set.of(), false, o -> {
                    assertThat(o.resp().json().path("moved").asLong(-1)).as("one orphan moved: " + o.resp().body()).isEqualTo(1);
                    assertThat(t.chunksIn(sib, mine)).as("the renamed collection's orphan is in the quarantine sibling").isEqualTo(1);
                    assertThat(t.chunksIn(o.current(), mine)).as("and no longer in the collection, under its current label").isZero();
                    assertThat(t.chunksIn(t.b, decoy)).as("the new holder's orphan was not touched").isEqualTo(1);
                    assertThat(t.chunksIn(sib, decoy)).as("and did not reach the sibling").isZero();
                });
        }),
        new Case("/v1/vectors/gc/restore-rereferenced", false, (t, route) -> {
            String sib = "quarantine-" + t.b;
            t.f.register(sib);
            String host = t.host();
            String mine = Chash.ofText("hv-rr-mine-" + t.stem).toHex();
            String decoy = Chash.ofText("hv-rr-decoy-" + t.stem).toHex();
            t.ownerDoc("hv-rr-doc-" + t.stem, host);
            t.ownerDoc("hv-rr-doc2-" + t.stem, host);
            return new Plan("{\"origin_collection_id\":" + t.x + ",\"quarantine_collection\":" + json(sib) + "}", 200,
                gap -> {
                    String c = gap.currentLabelOf().get(t.x);
                    t.chunk(sib, mine, Map.of());
                    t.manifestRow("hv-rr-doc-" + t.stem, c, mine);   // re-referenced by the renamed collection
                    t.chunk(sib, decoy, Map.of());
                    t.manifestRow("hv-rr-doc2-" + t.stem, t.b, decoy);   // re-referenced by the NEW holder of the old label
                }, Set.of(), false, o -> {
                    assertThat(t.chunksIn(o.current(), mine)).as("the re-referenced chunk was restored into the renamed collection").isEqualTo(1);
                    assertThat(t.chunkId(o.current(), mine)).as("with its id").isEqualTo(o.id());
                    assertThat(t.chunksIn(sib, mine)).as("and left the sibling").isZero();
                    assertThat(t.chunksIn(t.b, decoy)).as("the new holder's chunk was not restored into it").isZero();
                    assertThat(t.chunksIn(sib, decoy)).as("it stays in the sibling").isEqualTo(1);
                });
        }),
        new Case("/v1/vectors/gc/expire-quarantine", false, (t, route) -> {
            String sib = "quarantine-" + t.b;
            t.f.register(sib);
            String mine = Chash.ofText("hv-exp-mine-" + t.stem).toHex();
            String decoy = Chash.ofText("hv-exp-decoy-" + t.stem).toHex();
            Map<String, Object> aged = Map.of("quarantined_at", STAMP);
            Map<String, Object> taggedForTheNewHolder = Map.of("quarantined_at", STAMP, "origin_collection", t.b);
            return new Plan("{\"origin_collection_id\":" + t.x + ",\"quarantine_collection\":" + json(sib) + ",\"cutoff\":\"2999-01-01T00:00:00Z\"}", 200,
                gap -> {
                    t.chunk(sib, mine, aged);   // untagged: the origin's, whichever label it bears
                    t.chunk(sib, decoy, taggedForTheNewHolder);   // tagged for the label the NEW holder bears
                }, Set.of(), true, o -> {
                    assertThat(t.chunksIn(sib, mine)).as("the renamed collection's expired chunk is gone: " + o.resp().body()).isZero();
                    assertThat(t.chunksIn(sib, decoy)).as("the chunk tagged for the new holder of the old label was not expired").isEqualTo(1);
                    var audit = t.rows("gc_audit", List.of("details"), "tenant_id", o.f().tenant, "operation", "gc_expire_quarantine");
                    assertThat(audit).as("one expiry audited").isNotEmpty();
                    assertThat(audit.get(audit.size() - 1).get("details").toString()).as("naming the origin by its CURRENT label")
                        .contains(o.current()).doesNotContain("\"" + t.b + "\"");
                });
        }),
        new Case("/v1/vectors/gc/quarantine-restore", false, (t, route) -> {
            String sib = "quarantine-" + t.b;
            t.f.register(sib);
            String mine = Chash.ofText("hv-qr-mine-" + t.stem).toHex();
            String decoy = Chash.ofText("hv-qr-decoy-" + t.stem).toHex();
            return new Plan("{\"origin_collection_id\":" + t.x + ",\"chashes\":[\"" + mine + "\",\"" + decoy + "\"]}", 200,
                gap -> {
                    t.chunk(sib, mine, Map.of());   // untagged
                    t.chunk(sib, decoy, Map.of("origin_collection", t.b));   // tagged for the label the NEW holder bears
                }, Set.of(), true, o -> {
                    assertThat(t.chunksIn(o.current(), mine)).as("restored into the renamed collection under its current label: " + o.resp().body()).isEqualTo(1);
                    assertThat(t.chunkId(o.current(), mine)).as("with its id").isEqualTo(o.id());
                    assertThat(t.chunksIn(sib, mine)).as("and out of the sibling").isZero();
                    assertThat(t.chunksIn(t.b, decoy)).as("the chunk tagged for the new holder was not restored into it").isZero();
                    assertThat(t.chunksIn(sib, decoy)).as("it stays in the sibling").isEqualTo(1);
                });
        }),
        new Case("/v1/vectors/reembed/jobs", true, (t, route) -> {
            // the profile model has no registered collection for this owner yet: the request names the target it registers
            String target = "docs__" + t.stem + "__" + BGE + "__v1";
            return new Plan("{\"source_collection_id\":" + t.x + ",\"target_collection\":" + json(target) + "}", 201, null, Set.of(), true, o -> {
                var jobs = t.rows("reembed_jobs", List.of("source_collection", "source_collection_id"), "tenant_id", o.f().tenant, "source_collection_id", o.id());
                assertThat(jobs).as("the job was filed: " + o.resp().body()).hasSize(1);
                assertThat(jobs.get(0).get("source_collection")).as("under the renamed row's current label").isEqualTo(o.current());
            });
        }),
        new Case("/v1/taxonomy/assignments/assign", false, (t, route) -> {
            String hex = Chash.ofText("hv-assign-" + t.stem).toHex();
            long topic = t.f.post("/v1/taxonomy/topics/insert", "{\"collection\":" + json(t.b) + ",\"label\":\"hv-topic\",\"doc_count\":0}").json().path("id").asLong(-1);
            assertThat(topic).as("setup: the topic").isPositive();
            // the topic was filed before the rename, so it bears the old label by construction
            return new Plan("{\"doc_id\":\"" + hex + "\",\"topic_id\":" + topic + ",\"assigned_by\":\"manual\",\"source_collection_id\":" + t.x + "}", 200,
                null, Set.of("topics"), true, o -> {
                    var rows = t.rows("topic_assignments", List.of("source_collection", "source_collection_id"), "tenant_id", o.f().tenant,
                        "doc_id", Chash.fromHex(hex).toBytes());
                    assertThat(rows).as("the assignment landed: " + o.resp().body()).hasSize(1);
                    assertThat(rows.get(0).get("source_collection")).as("under the current label").isEqualTo(o.current());
                    assertThat(((Number) rows.get(0).get("source_collection_id")).longValue()).isEqualTo(o.id());
                });
        }),
        new Case("/v1/taxonomy/topics/persist_split", false, (t, route) -> {
            long parent = t.f.post("/v1/taxonomy/topics/insert", "{\"collection\":" + json(t.b) + ",\"label\":\"hv-parent\",\"doc_count\":0}").json().path("id").asLong(-1);
            assertThat(parent).as("setup: the parent topic").isPositive();
            String spec = "{\"label\":\"hv-child\",\"doc_count\":0,\"created_at\":\"2026-10-22T00:00:00Z\",\"terms_json\":\"[]\",\"doc_ids\":[]}";
            return new Plan("{\"topic_id\":" + parent + ",\"collection_id\":" + t.x + ",\"child_specs\":[" + spec + "]}", 200, null, Set.of("topics"), true, o -> {
                var kids = t.rows("topics", List.of("collection", "collection_id"), "tenant_id", o.f().tenant, "parent_id", parent);
                assertThat(kids).as("the child landed: " + o.resp().body()).hasSize(1);
                assertThat(kids.get(0).get("collection")).as("under the current label").isEqualTo(o.current());
                assertThat(((Number) kids.get(0).get("collection_id")).longValue()).isEqualTo(o.id());
            });
        }),
        new Case("/v1/aspects/highlights/upsert", false, (t, route) -> {
            String doc = "hv-hl-" + t.stem;
            t.ownerDoc(doc, t.host());
            return new Plan("{\"collection_id\":" + t.x + ",\"doc_id\":" + json(doc) + ",\"highlights_md\":\"## key\\nfinding\",\"ingested_at\":\"2026-10-22T00:00:00.000000Z\"}",
                200, null, Set.of(), true, o -> {
                    var rows = t.rows("document_highlights", List.of("collection", "collection_id"), "tenant_id", o.f().tenant, "doc_id", doc);
                    assertThat(rows).as("the highlight landed: " + o.resp().body()).hasSize(1);
                    assertThat(rows.get(0).get("collection")).as("under the current label").isEqualTo(o.current());
                    assertThat(((Number) rows.get(0).get("collection_id")).longValue()).isEqualTo(o.id());
                });
        }),
        new Case("/v1/remap/record_batch", false, (t, route) -> {
            String target = label(t.stem + "-remap-target");
            t.f.register(target);
            long targetId = t.f.idOf(target);
            String chash = Chash.ofText("hv-remap-" + t.stem).toHex();
            return new Plan("{\"source_collection_id\":" + t.x + ",\"entries\":[{\"old_id\":\"hv-old\",\"new_chash\":\"" + chash
                + "\",\"target_collection_id\":" + targetId + ",\"provenance\":\"hv\"}]}", 200, null, Set.of(), true, o -> {
                    var rows = t.rows("chash_remap", List.of("source_collection", "source_collection_id", "target_collection", "target_collection_id"),
                        "tenant_id", o.f().tenant, "old_id", "hv-old");
                    assertThat(rows).as("the fact landed: " + o.resp().body()).hasSize(1);
                    assertThat(rows.get(0).get("source_collection")).as("the source under its current label").isEqualTo(o.current());
                    assertThat(((Number) rows.get(0).get("source_collection_id")).longValue()).isEqualTo(o.id());
                    assertThat(rows.get(0).get("target_collection")).as("the target, which nobody renamed, as it was").isEqualTo(target);
                });
        }),
        new Case("/v1/pipeline/create", false, (t, route) -> {
            String hash = "hv-" + t.stem;
            return new Plan("{\"collection_id\":" + t.x + ",\"content_hash\":" + json(hash) + ",\"pdf_path\":\"/hv.pdf\"}", 200, null, Set.of(), true, o -> {
                var rows = t.rows("pdf_pipeline", List.of("collection"), "tenant_id", o.f().tenant, "content_hash", hash);
                assertThat(rows).as("the pipeline landed: " + o.resp().body()).hasSize(1);
                assertThat(rows.get(0).get("collection")).as("under the current label").isEqualTo(o.current());
            });
        }));

    /** The route paths this class covers: the final test of {@code CollectionRoutesByIdTest} checks each HEAVY WRITES route is one of them or named elsewhere. */
    static final Set<String> COVERED_PATHS = CASES.stream().map(Case::path).collect(java.util.stream.Collectors.toUnmodifiableSet());

    // ---- the shared run -------------------------------------------------------------------------------------------------

    CollectionRouteFixture fx;
    CollectionRouteFixture fxLocal;
    int n;

    @BeforeAll
    void startAll() throws Exception {
        fx = prepared(new CollectionRouteFixture());
        fxLocal = prepared(new CollectionRouteFixture(true));
        // the re-embed start: the tenant's profile names a model other than the source's, and the engine is told it serves it
        fxLocal.asSuperuser(ctx -> {
            ctx.deleteFrom(EMBEDDING_PROFILE).where(EMBEDDING_PROFILE.TENANT_ID.eq(fxLocal.tenant)).and(EMBEDDING_PROFILE.CONTENT_TYPE.eq("docs")).execute();
            ctx.insertInto(EMBEDDING_PROFILE, EMBEDDING_PROFILE.TENANT_ID, EMBEDDING_PROFILE.CONTENT_TYPE, EMBEDDING_PROFILE.EMBEDDING_MODEL,
                EMBEDDING_PROFILE.DIMENSION).values(fxLocal.tenant, "docs", BGE, 768).execute();
            return null;
        });
        fxLocal.service.setReembedServedModelsForTests(() -> List.of(BGE, "minilm-l6-v2-384"));
    }

    /** The one-row rename's own preconditions, on this fixture's throwaway database (see the class comment). */
    private static CollectionRouteFixture prepared(CollectionRouteFixture f) {
        f.buildIdTwins();
        f.asSuperuser(ctx -> {
            for (var key : CATALOG_COLLECTIONS.getKeys()) {
                if (!key.getFields().contains(CATALOG_COLLECTIONS.NAME)) continue;
                for (var fk : key.getReferences()) {
                    ctx.alterTable(fk.getTable()).dropConstraintIfExists(fk.getName()).execute();
                }
            }
            ctx.alterTable(CATALOG_COLLECTIONS).dropConstraint(CATALOG_COLLECTIONS.getPrimaryKey().getName()).cascade().execute();
            // the registry keeps its live arbiter: the upserts that register a collection (the re-embed target's) name (tenant_id, name) in
            // their ON CONFLICT, which the dropped primary key was; a unique index on the same columns serves, and an in-place rename keeps it unique
            // (after P4.1 drops the primary key, that conflict target needs the live-label index's WHERE clause: a P4.1 item, not this one). Both fixtures
            // run through prepared(), so the index is on the one the re-embed case uses too.
            ctx.createUniqueIndex("hv_catalog_collections_tenant_name").on(CATALOG_COLLECTIONS, CATALOG_COLLECTIONS.TENANT_ID,
                CATALOG_COLLECTIONS.NAME).execute();
            ctx.alterTable(CATALOG_DOCUMENT_CHUNKS).dropConstraintIfExists("fk_catalog_chunks_chunk").execute();
            ctx.alterTable(TOPIC_ASSIGNMENTS).dropConstraintIfExists("topic_assignments_chunk_fk").execute();
            return null;
        });
        return f;
    }

    @AfterAll
    void stopAll() {
        if (fx != null) fx.close();
        if (fxLocal != null) fxLocal.close();
    }

    private void run(Case cs) {
        CollectionRouteFixture f = cs.local() ? fxLocal : fx;
        String stem = "hv" + (n++);
        String b = label(stem);
        f.register(b);
        long x = f.idOf(b);
        Ctx t = new Ctx(f, b, x, stem);
        Plan plan = cs.plan().apply(t, cs.path());
        Resp r;
        CollectionRouteFixture.RenameGap gap = f.renameInPlaceBeforeTheWritesRead(List.of(x), List.of(b), stem, plan.afterRename());
        try {
            r = f.post(cs.path(), plan.body());
        } finally {
            gap.close();
        }
        assertThat(gap.ran()).as("%s reached its registry read by id in a write transaction (answered %d: %s)", cs.path(), r.status(), r.body()).isTrue();
        assertThat(r.status()).as("%s answered: %s", cs.path(), r.body()).isEqualTo(plan.status());
        String current = gap.currentLabelOf().get(x);
        long newHolder = gap.newHolders().iterator().next();
        List<String> violations = new ArrayList<>();
        RenameGapInvariant.findings(f, gap.currentLabelOf(), gap.newHolders(), violations, plan.skipTables(), plan.checkNewHolders());
        assertThat(violations).as("%s by id after a rename in the gap", cs.path()).isEmpty();
        plan.verify().accept(new Outcome(f, b, current, x, newHolder, r));
    }

    @TestFactory
    Stream<DynamicNode> everyHeavyWritesRouteLandsUnderTheCurrentLabelAfterARenameInTheGap() {
        List<DynamicNode> nodes = new ArrayList<>();
        for (Case cs : CASES) {
            nodes.add(DynamicTest.dynamicTest(cs.path(), () -> run(cs)));
        }
        nodes.add(DynamicTest.dynamicTest("non-vacuity: one case per route, none repeated", () -> {
            assertThat(CASES).as("the cases: one per HEAVY writes route this class covers").hasSize(14);
            assertThat(COVERED_PATHS).as("one case per route").hasSize(CASES.size());
        }));
        return nodes.stream();
    }
}
