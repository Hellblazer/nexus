// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import com.zaxxer.hikari.HikariConfig;
import com.zaxxer.hikari.HikariDataSource;
import dev.nexus.service.db.Chash;
import dev.nexus.service.db.CollectionIds;
import dev.nexus.service.db.PgSession;
import dev.nexus.service.db.TenantScope;
import dev.nexus.service.jooq.binding.Vector;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.MethodSource;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.time.OffsetDateTime;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeSet;
import java.util.stream.Collectors;

import static dev.nexus.service.jooq.nexus.Tables.*;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 P3.5b (nexus-p2m1x.24, linda 2026-11-23): the id twins {@code <name>_by_id} and the two form B views, on a store whose
 * chunks, manifest rows, topics and aspects all carry their collection id, in all three embedding dimensions.
 *
 * <p><b>Row parity, every twin.</b> Each of the 38 twins (the dimension families at 384, 768 and 1024, plus
 * {@code manifest_qualifying_hits} and {@code chunk_live_owners}) returns its name function's rows for the same collection: the
 * original's columns, value for value (in order where the function orders, as a set where it does not), and the twin's trailing
 * {@code collection_id} column holds the registry id. Every comparison is non-vacuous (the name function returns rows) and the
 * list of cases is checked against {@code pg_proc}, so a twin that has no case here fails the class.
 *
 * <p><b>A one-row rename (P3.7).</b> P3.7 renames a collection by updating its registry row alone; every child's name column keeps
 * the old label until P4.1. The class drops the name foreign keys (as P3.7's move does), renames the registry row, and requires
 * the twins' {@code collection} column, {@code nexus.collection_vector_stats} and {@code nexus.live_chunks} to show the NEW label
 * and the id. The engine's own readers of the stats view are called too: {@code PgVectorRepository.collectionStats} and the
 * predicate {@code CatalogRepository}'s ghost sweep puts on the view ({@code collection = <registry name>}).
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class Rdr229P35bTwinParityIntegrationTest {

    static final String T = "p35b-parity";
    static final String GATE = "zebracorn";
    static final String TOPIC = "Parity Topic";
    static final int[] DIMS = {384, 768, 1024};
    static final int K = 10;
    static final OffsetDateTime SEED_TS = OffsetDateTime.parse("2026-01-01T00:00:00+00:00");

    /** The chunk keys, nearest the query first: s1, x, s2 are held by sensitive documents; n1..n6 follow. */
    static final String S1 = "s1", S2 = "s2", X = "x";
    static final List<String> PLAIN_KEYS = List.of("n1", "n2", "n3", "n4", "n5", "n6");
    static final Map<String, Double> ANGLE = new LinkedHashMap<>();
    static {
        ANGLE.put(S1, 0.0);
        ANGLE.put(X, 0.02);
        ANGLE.put(S2, 0.05);
        for (int i = 0; i < PLAIN_KEYS.size(); i++) ANGLE.put(PLAIN_KEYS.get(i), 0.2 + 0.1 * i);
    }
    /** document stem to the chunk key it owns; sx is sensitive, nx is not, and both own the shared chunk x. */
    static final Map<String, String> OWNS = new LinkedHashMap<>();
    static {
        for (String n : PLAIN_KEYS) OWNS.put(n, n);
        OWNS.put("s1", S1);
        OWNS.put("s2", S2);
        OWNS.put("sx", X);
        OWNS.put("nx", X);
    }
    static final Set<String> SENSITIVE_DOCS = Set.of("s1", "s2", "sx");

    /** Every family of the id twins; the dimension families run at 384, 768 and 1024. */
    enum Fam {
        PLAIN("plain_search", true, 2), MANIFEST("manifest_search", true, 2), HITS("manifest_qualifying_hits", false, 1),
        METADATA("search_metadata_scoped", true, 2), ASPECT("search_aspect_scoped", true, 2),
        GRAPH("search_graph_hop", true, 2), TOPIC("search_topic_scoped", true, 2), TOPIC_SET("search_topic_set", true, 2),
        ALLOWED("text_gated_search_allowed", true, 2), BY_CHASH("text_gated_search_by_chash", true, 2),
        HNSW_FIRST("text_gated_search_hnsw_first", true, 2),
        PROBE("text_gate_probe", true, -1), PROBE_BY_COLLECTION("text_gate_probe_by_collection", true, 0),
        LIVE_OWNERS("chunk_live_owners", false, -1);

        final String base;
        final boolean perDim;
        /** The index of the label column in the original's rows, or -1 where the function returns none. */
        final int labelColumn;

        Fam(String base, boolean perDim, int labelColumn) {
            this.base = base;
            this.perDim = perDim;
            this.labelColumn = labelColumn;
        }

        /** The search functions order their rows by distance; the others return a set. */
        boolean ordered() {
            return labelColumn == 2;
        }
    }

    record Case(Fam fam, int dim) {
        String original() {
            return fam.base + (dim > 0 ? "_" + dim : "");
        }

        String twin() {
            return original() + "_by_id";
        }

        @Override
        public String toString() {
            return original();
        }
    }

    PostgreSQLContainer<?> pg;
    HikariDataSource ds;
    TenantScope scope;
    PgVectorRepository repo;
    final Map<Integer, String> models = new LinkedHashMap<>();
    final Map<Integer, Long> ids = new LinkedHashMap<>();
    final Map<Integer, Long> topicIds = new LinkedHashMap<>();

    static String coll(int dim) {
        return switch (dim) {
            case 384 -> "knowledge__p35bparity-384__minilm-l6-v2-384__v1";
            case 768 -> "knowledge__p35bparity-768__bge-base-en-v15-768__v1";
            default -> "knowledge__p35bparity-1024__voyage-context-3__v1";
        };
    }

    static String renamed(int dim) {
        return coll(dim).replace("p35bparity", "p35brenamed");
    }

    static String text(String key) {
        return "chunk " + key + " " + GATE;
    }

    static byte[] chash(String key) {
        return Chash.ofText(text(key)).toBytes();
    }

    static String doc(String stem, int dim) {
        return stem + "-" + dim;
    }

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            DSLContext dsl = DSL.using(su, SQLDialect.POSTGRES);
            for (int dim : DIMS) seedDim(dsl, dim);
            assertThat(ByIdRestoreSupport.dropRegistryNameKeys(dsl))
                .as("non-vacuity: the name keys P3.7's one-row rename would trip on").isNotEmpty();
        }
        var cfg = new HikariConfig();
        cfg.setJdbcUrl(pg.getJdbcUrl());
        cfg.setUsername(PgContainerHelper.SVC_USERNAME);
        cfg.setPassword(PgContainerHelper.SVC_PASSWORD);
        cfg.setMaximumPoolSize(4);
        cfg.setAutoCommit(true);
        ds = new HikariDataSource(cfg);
        scope = new TenantScope(ds);
        var embedder = new PgVectorRepositoryContractTest.FakeEmbedder(384);
        repo = new PgVectorRepository(scope, embedder, embedder);
    }

    @AfterAll
    void stopAll() {
        if (ds != null) ds.close();
        if (pg != null) pg.stop();
    }

    // -- fixture -----------------------------------------------------------------------------------------

    private void seedDim(DSLContext dsl, int dim) {
        String collection = coll(dim);
        PgContainerHelper.insertCollection(dsl, T, collection);
        String model = PgContainerHelper.collectionModel(dsl, T, collection);
        long id = CollectionIds.require(dsl, T, collection);
        models.put(dim, model);
        ids.put(dim, id);
        long topic = dsl.insertInto(TOPICS)
            .set(TOPICS.TENANT_ID, T).set(TOPICS.LABEL, TOPIC).set(TOPICS.COLLECTION, collection).set(TOPICS.COLLECTION_ID, id)
            .set(TOPICS.CREATED_AT, SEED_TS)
            .returningResult(TOPICS.ID).fetchOne(TOPICS.ID);
        topicIds.put(dim, topic);
        for (var e : ANGLE.entrySet()) {
            byte[] chash = chash(e.getKey());
            double theta = e.getValue();
            dsl.insertInto(CHUNKS)
                .set(CHUNKS.TENANT_ID, T).set(CHUNKS.COLLECTION, collection).set(CHUNKS.COLLECTION_ID, id).set(CHUNKS.CHASH, chash)
                .set(CHUNKS.EMBEDDING_MODEL, model).set(CHUNKS.CHUNK_TEXT, text(e.getKey()))
                .set(VectorSeeds.chunkEmbedding(dim), VectorSeeds.vecXY(dim, (float) Math.cos(theta), (float) Math.sin(theta)))
                .execute();
            dsl.insertInto(TOPIC_ASSIGNMENTS)
                .set(TOPIC_ASSIGNMENTS.TENANT_ID, T).set(TOPIC_ASSIGNMENTS.DOC_ID, chash)
                .set(TOPIC_ASSIGNMENTS.TOPIC_ID, topic).set(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION, collection)
                .set(TOPIC_ASSIGNMENTS.SOURCE_COLLECTION_ID, id)
                .set(TOPIC_ASSIGNMENTS.ASSIGNED_AT, SEED_TS).set(TOPIC_ASSIGNMENTS.EMBEDDING_MODEL, model)
                .execute();
        }
        for (var e : OWNS.entrySet()) {
            String doc = doc(e.getKey(), dim);
            dsl.insertInto(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.TENANT_ID, T).set(CATALOG_DOCUMENTS.TUMBLER, doc)
                .set(CATALOG_DOCUMENTS.TITLE, "Doc " + doc).set(CATALOG_DOCUMENTS.CONTENT_TYPE, "paper")
                .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, collection).set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, id)
                .set(CATALOG_DOCUMENTS.SENSITIVE, SENSITIVE_DOCS.contains(e.getKey()))
                .execute();
            dsl.insertInto(CATALOG_DOCUMENT_CHUNKS)
                .set(CATALOG_DOCUMENT_CHUNKS.TENANT_ID, T).set(CATALOG_DOCUMENT_CHUNKS.DOC_ID, doc)
                .set(CATALOG_DOCUMENT_CHUNKS.POSITION, 0).set(CATALOG_DOCUMENT_CHUNKS.CHASH, chash(e.getValue()))
                .set(CATALOG_DOCUMENT_CHUNKS.COLLECTION, collection).set(CATALOG_DOCUMENT_CHUNKS.COLLECTION_ID, id)
                .set(CATALOG_DOCUMENT_CHUNKS.EMBEDDING_MODEL, model)
                .execute();
            dsl.insertInto(DOCUMENT_ASPECTS)
                .set(DOCUMENT_ASPECTS.TENANT_ID, T).set(DOCUMENT_ASPECTS.COLLECTION, collection).set(DOCUMENT_ASPECTS.COLLECTION_ID, id)
                .set(DOCUMENT_ASPECTS.SOURCE_PATH, doc).set(DOCUMENT_ASPECTS.PROPOSED_METHOD, "gradient descent")
                .set(DOCUMENT_ASPECTS.CONFIDENCE, 0.9).set(DOCUMENT_ASPECTS.EXTRACTED_AT, SEED_TS)
                .set(DOCUMENT_ASPECTS.MODEL_VERSION, "p35bparity").set(DOCUMENT_ASPECTS.EXTRACTOR_NAME, "p35bparity")
                .set(DOCUMENT_ASPECTS.DOC_ID, doc).set(DOCUMENT_ASPECTS.SOURCE_URI, "p35bparity://" + doc)
                .execute();
        }
        // the graph-hop seed reaches every document by a cites edge
        String seed = doc("seed", dim);
        dsl.insertInto(CATALOG_DOCUMENTS)
            .set(CATALOG_DOCUMENTS.TENANT_ID, T).set(CATALOG_DOCUMENTS.TUMBLER, seed)
            .set(CATALOG_DOCUMENTS.TITLE, "Seed " + dim).set(CATALOG_DOCUMENTS.CONTENT_TYPE, "paper")
            .set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION, collection).set(CATALOG_DOCUMENTS.PHYSICAL_COLLECTION_ID, id)
            .execute();
        for (String stem : OWNS.keySet()) {
            dsl.insertInto(CATALOG_LINKS, CATALOG_LINKS.TENANT_ID, CATALOG_LINKS.FROM_TUMBLER, CATALOG_LINKS.TO_TUMBLER,
                    CATALOG_LINKS.LINK_TYPE, CATALOG_LINKS.CREATED_BY)
                .values(T, seed, doc(stem, dim), "cites", "test")
                .execute();
        }
    }

    // -- the cases and the calls ---------------------------------------------------------------------------

    static List<Case> cases() {
        List<Case> out = new ArrayList<>();
        for (Fam f : Fam.values()) {
            if (f.perDim) {
                for (int dim : DIMS) out.add(new Case(f, dim));
            } else {
                out.add(new Case(f, 0));
            }
        }
        return out;
    }

    /** The cases whose function returns a collection label. */
    static List<Case> labelCases() {
        return cases().stream().filter(c -> c.fam().labelColumn >= 0).toList();
    }

    private byte[][] allChashes() {
        List<String> keys = new ArrayList<>(ANGLE.keySet());
        byte[][] out = new byte[keys.size()][];
        for (int i = 0; i < out.length; i++) out[i] = chash(keys.get(i));
        return out;
    }

    /** The name function of {@code c} over the fixture's collection of its dimension, as PgVectorRepository calls it. */
    private Table<?> original(Case c) {
        int d = c.dim();
        String[] colls = {coll(d == 0 ? 384 : d)};
        Vector q = VectorSeeds.vecXY(d == 0 ? 384 : d, 1f, 0f);
        String m = models.get(d == 0 ? 384 : d);
        byte[][] all = allChashes();
        String seed = doc("seed", d == 0 ? 384 : d);
        return switch (c.fam()) {
            case PLAIN -> switch (d) {
                case 384 -> PLAIN_SEARCH_384.call(q, colls, null, null, K, m, T, true);
                case 768 -> PLAIN_SEARCH_768.call(q, colls, null, null, K, m, T, true);
                default -> PLAIN_SEARCH_1024.call(q, colls, null, null, K, m, T, true);
            };
            case MANIFEST -> switch (d) {
                case 384 -> MANIFEST_SEARCH_384.call(q, null, null, colls, null, null, null, null, K, m, T, true);
                case 768 -> MANIFEST_SEARCH_768.call(q, null, null, colls, null, null, null, null, K, m, T, true);
                default -> MANIFEST_SEARCH_1024.call(q, null, null, colls, null, null, null, null, K, m, T, true);
            };
            case HITS -> MANIFEST_QUALIFYING_HITS.call(colls, null, null, null, null, 100, m, T, null, true);
            case METADATA -> switch (d) {
                case 384 -> SEARCH_METADATA_SCOPED_384.call(q, colls, null, null, null, null, null, null, null, null, K, m, T, true);
                case 768 -> SEARCH_METADATA_SCOPED_768.call(q, colls, null, null, null, null, null, null, null, null, K, m, T, true);
                default -> SEARCH_METADATA_SCOPED_1024.call(q, colls, null, null, null, null, null, null, null, null, K, m, T, true);
            };
            case ASPECT -> switch (d) {
                case 384 -> SEARCH_ASPECT_SCOPED_384.call(q, colls, null, null, null, null, null, null, K, m, T, true);
                case 768 -> SEARCH_ASPECT_SCOPED_768.call(q, colls, null, null, null, null, null, null, K, m, T, true);
                default -> SEARCH_ASPECT_SCOPED_1024.call(q, colls, null, null, null, null, null, null, K, m, T, true);
            };
            case GRAPH -> switch (d) {
                case 384 -> SEARCH_GRAPH_HOP_384.call(q, new String[] {seed}, colls, "cites", 1, "out", null, null, null, K, m, T, true);
                case 768 -> SEARCH_GRAPH_HOP_768.call(q, new String[] {seed}, colls, "cites", 1, "out", null, null, null, K, m, T, true);
                default -> SEARCH_GRAPH_HOP_1024.call(q, new String[] {seed}, colls, "cites", 1, "out", null, null, null, K, m, T, true);
            };
            case TOPIC -> switch (d) {
                case 384 -> SEARCH_TOPIC_SCOPED_384.call(q, TOPIC, colls[0], K, m, T, true);
                case 768 -> SEARCH_TOPIC_SCOPED_768.call(q, TOPIC, colls[0], K, m, T, true);
                default -> SEARCH_TOPIC_SCOPED_1024.call(q, TOPIC, colls[0], K, m, T, true);
            };
            case TOPIC_SET -> {
                Long[] topics = {topicIds.get(d)};
                yield switch (d) {
                    case 384 -> SEARCH_TOPIC_SET_384.call(q, topics, colls[0], K, m, T, true);
                    case 768 -> SEARCH_TOPIC_SET_768.call(q, topics, colls[0], K, m, T, true);
                    default -> SEARCH_TOPIC_SET_1024.call(q, topics, colls[0], K, m, T, true);
                };
            }
            case ALLOWED -> switch (d) {
                case 384 -> TEXT_GATED_SEARCH_ALLOWED_384.call(q, GATE, all, colls, null, null, K, m, T, true);
                case 768 -> TEXT_GATED_SEARCH_ALLOWED_768.call(q, GATE, all, colls, null, null, K, m, T, true);
                default -> TEXT_GATED_SEARCH_ALLOWED_1024.call(q, GATE, all, colls, null, null, K, m, T, true);
            };
            case BY_CHASH -> switch (d) {
                case 384 -> TEXT_GATED_SEARCH_BY_CHASH_384.call(q, all, colls, null, null, K, m, T, true);
                case 768 -> TEXT_GATED_SEARCH_BY_CHASH_768.call(q, all, colls, null, null, K, m, T, true);
                default -> TEXT_GATED_SEARCH_BY_CHASH_1024.call(q, all, colls, null, null, K, m, T, true);
            };
            case HNSW_FIRST -> switch (d) {
                case 384 -> TEXT_GATED_SEARCH_HNSW_FIRST_384.call(q, GATE, colls, null, null, K, m, T, true);
                case 768 -> TEXT_GATED_SEARCH_HNSW_FIRST_768.call(q, GATE, colls, null, null, K, m, T, true);
                default -> TEXT_GATED_SEARCH_HNSW_FIRST_1024.call(q, GATE, colls, null, null, K, m, T, true);
            };
            case PROBE -> switch (d) {
                case 384 -> TEXT_GATE_PROBE_384.call(GATE, colls, null, null, 100, m, T);
                case 768 -> TEXT_GATE_PROBE_768.call(GATE, colls, null, null, 100, m, T);
                default -> TEXT_GATE_PROBE_1024.call(GATE, colls, null, null, 100, m, T);
            };
            case PROBE_BY_COLLECTION -> switch (d) {
                case 384 -> TEXT_GATE_PROBE_BY_COLLECTION_384.call(GATE, colls, null, null, 100, m, T);
                case 768 -> TEXT_GATE_PROBE_BY_COLLECTION_768.call(GATE, colls, null, null, 100, m, T);
                default -> TEXT_GATE_PROBE_BY_COLLECTION_1024.call(GATE, colls, null, null, 100, m, T);
            };
            case LIVE_OWNERS -> CHUNK_LIVE_OWNERS.call(T, colls[0], chash(X));
        };
    }

    /** The twin of {@code c} over the same collection's id. */
    private Table<?> twin(Case c) {
        int d = c.dim();
        int dm = d == 0 ? 384 : d;
        Long id = ids.get(dm);
        Long[] idArr = {id};
        Vector q = VectorSeeds.vecXY(dm, 1f, 0f);
        String m = models.get(dm);
        byte[][] all = allChashes();
        String seed = doc("seed", dm);
        return switch (c.fam()) {
            case PLAIN -> switch (d) {
                case 384 -> PLAIN_SEARCH_384_BY_ID.call(q, idArr, null, null, K, m, T, true);
                case 768 -> PLAIN_SEARCH_768_BY_ID.call(q, idArr, null, null, K, m, T, true);
                default -> PLAIN_SEARCH_1024_BY_ID.call(q, idArr, null, null, K, m, T, true);
            };
            case MANIFEST -> switch (d) {
                case 384 -> MANIFEST_SEARCH_384_BY_ID.call(q, null, null, idArr, null, null, null, null, K, m, T, true);
                case 768 -> MANIFEST_SEARCH_768_BY_ID.call(q, null, null, idArr, null, null, null, null, K, m, T, true);
                default -> MANIFEST_SEARCH_1024_BY_ID.call(q, null, null, idArr, null, null, null, null, K, m, T, true);
            };
            case HITS -> MANIFEST_QUALIFYING_HITS_BY_ID.call(idArr, null, null, null, null, 100, m, T, null, true);
            case METADATA -> switch (d) {
                case 384 -> SEARCH_METADATA_SCOPED_384_BY_ID.call(q, idArr, null, null, null, null, null, null, null, null, K, m, T, true);
                case 768 -> SEARCH_METADATA_SCOPED_768_BY_ID.call(q, idArr, null, null, null, null, null, null, null, null, K, m, T, true);
                default -> SEARCH_METADATA_SCOPED_1024_BY_ID.call(q, idArr, null, null, null, null, null, null, null, null, K, m, T, true);
            };
            case ASPECT -> switch (d) {
                case 384 -> SEARCH_ASPECT_SCOPED_384_BY_ID.call(q, idArr, null, null, null, null, null, null, K, m, T, true);
                case 768 -> SEARCH_ASPECT_SCOPED_768_BY_ID.call(q, idArr, null, null, null, null, null, null, K, m, T, true);
                default -> SEARCH_ASPECT_SCOPED_1024_BY_ID.call(q, idArr, null, null, null, null, null, null, K, m, T, true);
            };
            case GRAPH -> switch (d) {
                case 384 -> SEARCH_GRAPH_HOP_384_BY_ID.call(q, new String[] {seed}, idArr, "cites", 1, "out", null, null, null, K, m, T, true);
                case 768 -> SEARCH_GRAPH_HOP_768_BY_ID.call(q, new String[] {seed}, idArr, "cites", 1, "out", null, null, null, K, m, T, true);
                default -> SEARCH_GRAPH_HOP_1024_BY_ID.call(q, new String[] {seed}, idArr, "cites", 1, "out", null, null, null, K, m, T, true);
            };
            case TOPIC -> switch (d) {
                case 384 -> SEARCH_TOPIC_SCOPED_384_BY_ID.call(q, TOPIC, id, K, m, T, true);
                case 768 -> SEARCH_TOPIC_SCOPED_768_BY_ID.call(q, TOPIC, id, K, m, T, true);
                default -> SEARCH_TOPIC_SCOPED_1024_BY_ID.call(q, TOPIC, id, K, m, T, true);
            };
            case TOPIC_SET -> {
                Long[] topics = {topicIds.get(d)};
                yield switch (d) {
                    case 384 -> SEARCH_TOPIC_SET_384_BY_ID.call(q, topics, id, K, m, T, true);
                    case 768 -> SEARCH_TOPIC_SET_768_BY_ID.call(q, topics, id, K, m, T, true);
                    default -> SEARCH_TOPIC_SET_1024_BY_ID.call(q, topics, id, K, m, T, true);
                };
            }
            case ALLOWED -> switch (d) {
                case 384 -> TEXT_GATED_SEARCH_ALLOWED_384_BY_ID.call(q, GATE, all, idArr, null, null, K, m, T, true);
                case 768 -> TEXT_GATED_SEARCH_ALLOWED_768_BY_ID.call(q, GATE, all, idArr, null, null, K, m, T, true);
                default -> TEXT_GATED_SEARCH_ALLOWED_1024_BY_ID.call(q, GATE, all, idArr, null, null, K, m, T, true);
            };
            case BY_CHASH -> switch (d) {
                case 384 -> TEXT_GATED_SEARCH_BY_CHASH_384_BY_ID.call(q, all, idArr, null, null, K, m, T, true);
                case 768 -> TEXT_GATED_SEARCH_BY_CHASH_768_BY_ID.call(q, all, idArr, null, null, K, m, T, true);
                default -> TEXT_GATED_SEARCH_BY_CHASH_1024_BY_ID.call(q, all, idArr, null, null, K, m, T, true);
            };
            case HNSW_FIRST -> switch (d) {
                case 384 -> TEXT_GATED_SEARCH_HNSW_FIRST_384_BY_ID.call(q, GATE, idArr, null, null, K, m, T, true);
                case 768 -> TEXT_GATED_SEARCH_HNSW_FIRST_768_BY_ID.call(q, GATE, idArr, null, null, K, m, T, true);
                default -> TEXT_GATED_SEARCH_HNSW_FIRST_1024_BY_ID.call(q, GATE, idArr, null, null, K, m, T, true);
            };
            case PROBE -> switch (d) {
                case 384 -> TEXT_GATE_PROBE_384_BY_ID.call(GATE, idArr, null, null, 100, m, T);
                case 768 -> TEXT_GATE_PROBE_768_BY_ID.call(GATE, idArr, null, null, 100, m, T);
                default -> TEXT_GATE_PROBE_1024_BY_ID.call(GATE, idArr, null, null, 100, m, T);
            };
            case PROBE_BY_COLLECTION -> switch (d) {
                case 384 -> TEXT_GATE_PROBE_BY_COLLECTION_384_BY_ID.call(GATE, idArr, null, null, 100, m, T);
                case 768 -> TEXT_GATE_PROBE_BY_COLLECTION_768_BY_ID.call(GATE, idArr, null, null, 100, m, T);
                default -> TEXT_GATE_PROBE_BY_COLLECTION_1024_BY_ID.call(GATE, idArr, null, null, 100, m, T);
            };
            case LIVE_OWNERS -> CHUNK_LIVE_OWNERS_BY_ID.call(T, id, chash(X));
        };
    }

    /** Every row of {@code fn} on the exact route (index scans off), the row's values in column order. */
    private List<List<Object>> fetch(Table<?> fn) {
        return scope.withTenant(T, ctx -> {
            PgSession.disableIndexScanForExactFallback(ctx);
            PgSession.setSearchPlanCacheMode(ctx);
            List<List<Object>> out = new ArrayList<>();
            for (Record r : ctx.selectFrom(fn).fetch()) {
                out.add(r.intoList());
            }
            return out;
        });
    }

    private static String show(Object v) {
        return v instanceof byte[] b ? HexFormat.of().formatHex(b) : String.valueOf(v);
    }

    /** The first {@code columns} values of every row, as text. */
    private static List<String> prefix(List<List<Object>> rows, int columns) {
        return rows.stream().map(r -> r.subList(0, columns).stream().map(Rdr229P35bTwinParityIntegrationTest::show)
            .collect(Collectors.joining("|"))).toList();
    }

    // -- row parity ----------------------------------------------------------------------------------------

    @ParameterizedTest(name = "{0}: the twin returns the name function's rows")
    @MethodSource("cases")
    void theTwinReturnsTheNameFunctionsRows(Case c) {
        List<List<Object>> named = fetch(original(c));
        List<List<Object>> twin = fetch(twin(c));
        int columns = named.isEmpty() ? 0 : named.get(0).size();
        assertThat(named).as("%s: non-vacuity, the name function finds rows", c).isNotEmpty();
        assertThat(twin).as("%s: the twin finds rows too", c).isNotEmpty();
        boolean hasId = c.fam().labelColumn >= 0;
        assertThat(twin.get(0)).as("%s: the twin has the original's columns%s", c, hasId ? " and a trailing collection id" : "")
            .hasSize(columns + (hasId ? 1 : 0));
        if (c.fam().ordered()) {
            assertThat(prefix(twin, columns)).as("%s: the twin's rows, in order, are the name function's", c)
                .containsExactlyElementsOf(prefix(named, columns));
        } else {
            assertThat(prefix(twin, columns)).as("%s: the twin's rows are the name function's", c)
                .containsExactlyInAnyOrderElementsOf(prefix(named, columns));
        }
        if (hasId) {
            int dm = c.dim() == 0 ? 384 : c.dim();
            assertThat(twin.stream().map(r -> r.get(columns)).collect(Collectors.toSet()))
                .as("%s: the trailing column is the registry id of the collection", c).containsExactly(ids.get(dm));
        }
    }

    @Test
    void everyTwinInTheCatalogHasACase() throws Exception {
        Set<String> inCatalog = new TreeSet<>();
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            Table<?> p = DSL.table(DSL.name("pg_catalog", "pg_proc")).as("p");
            Table<?> n = DSL.table(DSL.name("pg_catalog", "pg_namespace")).as("n");
            Field<String> name = DSL.field(DSL.name("p", "proname"), String.class);
            for (String f : ctx.select(name).from(p)
                    .join(n).on(DSL.field(DSL.name("n", "oid")).eq(DSL.field(DSL.name("p", "pronamespace"))))
                    .where(DSL.field(DSL.name("n", "nspname"), String.class).eq("nexus"))
                    .and(name.like("%\\_by\\_id")).fetch(name)) {
                inCatalog.add(f);
            }
        }
        Set<String> covered = cases().stream().map(Case::twin).collect(Collectors.toCollection(TreeSet::new));
        assertThat(covered).as("one case per id twin: 12 dimension families x 3, manifest_qualifying_hits and chunk_live_owners")
            .hasSize(38).isEqualTo(inCatalog);
    }

    // -- a one-row rename ----------------------------------------------------------------------------------

    private void renameRegistryRow(int dim, String to) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            int n = DSL.using(su, SQLDialect.POSTGRES).update(CATALOG_COLLECTIONS).set(CATALOG_COLLECTIONS.NAME, to)
                .where(CATALOG_COLLECTIONS.TENANT_ID.eq(T)).and(CATALOG_COLLECTIONS.COLLECTION_ID.eq(ids.get(dim)))
                .execute();
            assertThat(n).as("the one-row rename of the registry row").isEqualTo(1);
        }
    }

    @ParameterizedTest(name = "{0}: after a one-row rename the twin returns the new label")
    @MethodSource("labelCases")
    void afterAOneRowRename_theTwinReturnsTheNewLabel(Case c) throws Exception {
        int dm = c.dim() == 0 ? 384 : c.dim();
        List<List<Object>> before = fetch(twin(c));
        assertThat(before).as("%s: non-vacuity", c).isNotEmpty();
        assertThat(before.stream().map(r -> show(r.get(c.fam().labelColumn))).collect(Collectors.toSet()))
            .as("%s: before the rename the label is the collection's own", c).containsExactly(coll(dm));
        renameRegistryRow(dm, renamed(dm));
        try {
            List<List<Object>> after = fetch(twin(c));
            assertThat(after.stream().map(r -> show(r.get(c.fam().labelColumn))).collect(Collectors.toSet()))
                .as("%s: the chunks' own name columns still say %s, the twin says what the registry says", c, coll(dm))
                .containsExactly(renamed(dm));
            assertThat(after.stream().map(r -> r.get(r.size() - 1)).collect(Collectors.toSet()))
                .as("%s: and the id", c).containsExactly(ids.get(dm));
            assertThat(after).as("%s: the rename changes no row of the result, only the label", c).hasSameSizeAs(before);
        } finally {
            renameRegistryRow(dm, coll(dm));
        }
    }

    @Test
    void afterAOneRowRename_theViewsAndTheEnginesStatsShowTheNewLabel() throws Exception {
        int dim = 384;
        long id = ids.get(dim);
        renameRegistryRow(dim, renamed(dim));
        try {
            checkViewsAndEngine(dim, id);
        } finally {
            renameRegistryRow(dim, coll(dim));
        }
    }

    private void checkViewsAndEngine(int dim, long id) throws Exception {
        try (Connection su = pg.createConnection("")) {
            DSLContext ctx = DSL.using(su, SQLDialect.POSTGRES);
            // the children keep the old label: that is the state P3.7 leaves
            assertThat(ctx.fetchCount(CHUNKS, CHUNKS.TENANT_ID.eq(T).and(CHUNKS.COLLECTION.eq(coll(dim))).and(CHUNKS.COLLECTION_ID.eq(id))))
                .as("non-vacuity: the chunks still name %s", coll(dim)).isEqualTo(ANGLE.size());

            var stats = ctx.select(COLLECTION_VECTOR_STATS.COLLECTION, COLLECTION_VECTOR_STATS.DIM,
                    COLLECTION_VECTOR_STATS.CHUNK_COUNT, COLLECTION_VECTOR_STATS.STORED_COUNT, COLLECTION_VECTOR_STATS.COLLECTION_ID)
                .from(COLLECTION_VECTOR_STATS)
                .where(COLLECTION_VECTOR_STATS.TENANT_ID.eq(T)).and(COLLECTION_VECTOR_STATS.COLLECTION_ID.eq(id))
                .fetch();
            assertThat(stats).as("collection_vector_stats: one row for the id").hasSize(1);
            assertThat(stats.get(0).value1()).as("collection_vector_stats shows the registry's label").isEqualTo(renamed(dim));
            assertThat(stats.get(0).value3()).as("live chunks").isEqualTo((long) ANGLE.size());
            assertThat(stats.get(0).value4()).as("stored chunks").isEqualTo((long) ANGLE.size());
            assertThat(ctx.fetchCount(COLLECTION_VECTOR_STATS, COLLECTION_VECTOR_STATS.TENANT_ID.eq(T)
                    .and(COLLECTION_VECTOR_STATS.COLLECTION.eq(coll(dim)))))
                .as("and no row under the old label").isZero();

            var live = ctx.selectDistinct(LIVE_CHUNKS.COLLECTION, LIVE_CHUNKS.COLLECTION_ID).from(LIVE_CHUNKS)
                .where(LIVE_CHUNKS.TENANT_ID.eq(T)).and(LIVE_CHUNKS.COLLECTION_ID.eq(id)).fetch();
            assertThat(live).as("live_chunks: one (label, id) for the collection").hasSize(1);
            assertThat(live.get(0).value1()).as("live_chunks shows the registry's label").isEqualTo(renamed(dim));
            assertThat(ctx.fetchCount(LIVE_CHUNKS, LIVE_CHUNKS.TENANT_ID.eq(T).and(LIVE_CHUNKS.COLLECTION_ID.eq(id))))
                .as("every chunk is live").isEqualTo(ANGLE.size());

            // CatalogRepository's ghost sweep asks the view for the dimensions of a registry name
            assertThat(ctx.select(COLLECTION_VECTOR_STATS.DIM).from(COLLECTION_VECTOR_STATS)
                    .where(COLLECTION_VECTOR_STATS.TENANT_ID.eq(T)).and(COLLECTION_VECTOR_STATS.COLLECTION.eq(renamed(dim)))
                    .fetch(COLLECTION_VECTOR_STATS.DIM))
                .as("the sweep's read by the registry name finds the stored dimension, so a renamed live collection is not marked dormant")
                .containsExactly(dim);
        }

        // the engine's own reader: the view joined to the registry by name
        List<Map<String, Object>> engine = repo.collectionStats(T, "live");
        Map<String, Object> row = engine.stream().filter(m -> renamed(dim).equals(m.get("name"))).findFirst().orElse(null);
        assertThat(row).as("collectionStats lists the collection under its new label: %s", engine).isNotNull();
        assertThat(row.get("count")).as("its live count").isEqualTo((long) ANGLE.size());
        assertThat(row.get("dim")).isEqualTo(dim);
        assertThat(row.get("lifecycle_state")).as("the registry row joined by the new name answers").isEqualTo("live");
        assertThat(row.get("content_type")).as("and so does its content type").isNotNull();
        assertThat(engine.stream().map(m -> m.get("name")).toList())
            .as("no entry under the old label").doesNotContain(coll(dim));
    }
}
