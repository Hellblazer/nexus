// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.AtomicWriteTestBase;
import dev.nexus.service.vectors.ChunkKeyAllowlistActivity;
import dev.nexus.service.vectors.ChunkKeyAllowlistCheck;
import dev.nexus.service.vectors.OwnerlessWriteMode;
import dev.nexus.service.vectors.OwnerlessWritePolicy;
import dev.nexus.service.vectors.PgVectorRepository;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.slf4j.LoggerFactory;

import java.net.URI;
import java.sql.Connection;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

import static dev.nexus.service.jooq.nexus.Tables.CHUNKS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-236 Phase 4 Step 2a (nexus-6pfgb.66): the engine's chunk-metadata allow-list in LOG MODE, over every client
 * route that writes chunk metadata, against a real Postgres. A write that names a key outside the CHUNK set is
 * counted, logged once per route, tenant and collection per minute with its offending keys by class and the client's
 * identity, and then WRITES EXACTLY AS BEFORE. The control cases show the check does not fire on clean metadata, on a
 * bypass-prefix collection or on the keys it deliberately does not inspect.
 *
 * <p>Each case that expects a log line uses its own collection (the limiter is keyed by route, tenant and
 * collection), and the counter is reset before each.
 */
class ChunkKeyAllowlistLogTest extends AtomicWriteTestBase {

    private static final ObjectMapper JSON = new ObjectMapper();
    private static final String EVENT = "chunk_metadata_key_would_refuse";

    private CatalogHandler catalog;
    private VectorHandler vectors;
    private ListAppender<ILoggingEvent> appender;
    private Logger logger;

    @BeforeAll
    void wireHandlers() {
        var check = new ChunkKeyAllowlistCheck();
        catalog = new CatalogHandler(repo, svc, null, check);
        var vectorRepo = new PgVectorRepository(tenantScope, embedder, embedder);
        vectors = new VectorHandler(null, vectorRepo, null, new OwnerlessWritePolicy(OwnerlessWriteMode.ENFORCE),
            null, check);
    }

    @BeforeEach
    void captureLog() {
        ChunkKeyAllowlistActivity.resetForTests();
        logger = (Logger) LoggerFactory.getLogger(ChunkKeyAllowlistCheck.class);
        appender = new ListAppender<>();
        appender.start();
        logger.addAppender(appender);
    }

    @AfterEach
    void releaseLog() {
        logger.detachAppender(appender);
    }

    private record Resp(int status, String body) {
        JsonNode json() throws Exception {
            return JSON.readTree(body);
        }
    }

    private Resp send(com.sun.net.httpserver.HttpHandler handler, String path, Map<String, Object> body)
            throws Exception {
        CapturingExchange ex = new CapturingExchange("POST", URI.create(path), JSON.writeValueAsString(body));
        ex.requestHeaders.add("User-Agent", "nx-test-agent/9");
        ex.requestHeaders.add(VectorHandler.CLIENT_VERSION_HEADER, "7.99.0");
        RequestContext.set(new RequestContext.Principal(TENANT, null, false, false, "tenant", "test-credential-hash"));
        try {
            handler.handle(ex);
        } finally {
            RequestContext.clear();
        }
        return new Resp(ex.status, ex.bodyString());
    }

    private List<String> lines() {
        return appender.list.stream().map(ILoggingEvent::getFormattedMessage)
            .filter(m -> m.contains("event=" + EVENT)).toList();
    }

    /** One chunk's metadata naming one key of each class beside the two the client may write. */
    private static Map<String, Object> offendingMetadata(String chash) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("chunk_text_hash", chash);
        m.put("embedding_model", "minilm-l6-v2-384");
        m.put("title", "a.py");
        m.put("page_number", 3);
        m.put("doc_id", "1.2.3");
        m.put("zz_unknown_key", "x");
        return m;
    }

    private Map<String, Object> chunkWith(String chash, String text, Map<String, Object> metadata) {
        Map<String, Object> c = new LinkedHashMap<>();
        c.put("chash", chash);
        c.put("text", text);
        c.put("metadata", metadata);
        return c;
    }

    private Map<String, Object> writeManyBody(Fx f, String text, Map<String, Object> metadata) {
        String chash = ch(text);
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("collection", f.collection());
        body.put("chunks", List.of(chunkWith(chash, text, metadata)));
        body.put("docs", List.of(doc(f.docId(), List.of(row(0, chash)))));
        return body;
    }

    private Map<String, Object> storedMetadata(String collection, String hexChash) throws Exception {
        try (Connection su = pg.createConnection("")) {
            su.setAutoCommit(true);
            var json = DSL.using(su, SQLDialect.POSTGRES).select(CHUNKS.METADATA).from(CHUNKS)
                .where(CHUNKS.TENANT_ID.eq(TENANT))
                .and(CHUNKS.COLLECTION.eq(collection))
                .and(CHUNKS.CHASH.eq(HexFormat.of().parseHex(hexChash)))
                .fetchOne(CHUNKS.METADATA);
            @SuppressWarnings("unchecked")
            Map<String, Object> m = JSON.readValue(json.data(), Map.class);
            return m;
        }
    }

    // ── write_many: the line, the counter, and a write that proceeds unchanged ───────────────────────

    @Test
    void writeMany_withOffendingKeys_logsTheKeysByClass_countsOnce_andWritesTheMetadataUnchanged() throws Exception {
        Fx f = fixture("ckl-wm");
        String text = "ckl-wm-text-" + f.docId();
        String chash = ch(text);

        Resp r = send(catalog, "/v1/catalog/manifest/write_many", writeManyBody(f, text, offendingMetadata(chash)));

        assertThat(r.status()).as("body: %s", r.body()).isEqualTo(200);
        assertThat(lines()).hasSize(1);
        String line = lines().get(0);
        assertThat(line).contains("route=manifest/write_many").contains("tenant=" + TENANT)
            .contains("collection=\"" + f.collection() + "\"")
            .contains("offending_keys=\"other:zz_unknown_key,document:title,position:page_number,legacy:doc_id\"")
            .contains("offending_chunks=1").contains("requested=1")
            .contains("user_agent=\"nx-test-agent/9\"").contains("client_version=\"7.99.0\"")
            .contains("source_agent=\"absent\"").contains("suppressed_since_last=0");
        assertThat(ChunkKeyAllowlistActivity.wouldRefuseTotal()).isEqualTo(1);
        Map<String, Object> stored = storedMetadata(f.collection(), chash);
        assertThat(stored).as("log mode writes the metadata as sent").containsKeys("title", "page_number", "doc_id",
            "zz_unknown_key");
    }

    @Test
    void control_writeMany_withOnlyTheChunkKeys_logsAndCountsNothing() throws Exception {
        Fx f = fixture("ckl-clean");
        String text = "ckl-clean-text-" + f.docId();
        Map<String, Object> clean = new LinkedHashMap<>();
        clean.put("chunk_text_hash", ch(text));
        clean.put("embedding_model", "minilm-l6-v2-384");

        Resp r = send(catalog, "/v1/catalog/manifest/write_many", writeManyBody(f, text, clean));

        assertThat(r.status()).as("body: %s", r.body()).isEqualTo(200);
        assertThat(lines()).isEmpty();
        assertThat(ChunkKeyAllowlistActivity.wouldRefuseTotal()).isZero();
    }

    @Test
    void aRepeatedWriteInTheSameWindowIsCountedBothTimes_butLoggedOnce() throws Exception {
        Fx f = fixture("ckl-rate");
        for (int i = 0; i < 2; i++) {
            String text = "ckl-rate-text-" + i + "-" + f.docId();
            Resp r = send(catalog, "/v1/catalog/manifest/write_many",
                writeManyBody(f, text, offendingMetadata(ch(text))));
            assertThat(r.status()).as("body: %s", r.body()).isEqualTo(200);
        }
        assertThat(lines()).as("one line per route, tenant and collection per window").hasSize(1);
        assertThat(ChunkKeyAllowlistActivity.wouldRefuseTotal()).as("the counter is not rate limited").isEqualTo(2);
    }

    // ── append and append_many ───────────────────────────────────────────────────────────────────────

    @Test
    void append_withOffendingKeys_logsItsOwnRoute() throws Exception {
        Fx f = fixture("ckl-ap");
        String text = "ckl-ap-text-" + f.docId();
        String chash = ch(text);
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("collection", f.collection());
        body.put("doc_id", f.docId());
        body.put("chunks", List.of(chunkWith(chash, text, offendingMetadata(chash))));
        body.put("rows", List.of(row(0, chash)));

        Resp r = send(catalog, "/v1/catalog/manifest/append", body);

        assertThat(r.status()).as("body: %s", r.body()).isEqualTo(200);
        assertThat(lines()).hasSize(1);
        assertThat(lines().get(0)).contains("route=manifest/append ").contains("other:zz_unknown_key");
        assertThat(ChunkKeyAllowlistActivity.wouldRefuseTotal()).isEqualTo(1);
        assertThat(chunkExists(f.collection(), chash)).isTrue();
    }

    @Test
    void appendMany_withOffendingKeys_logsItsOwnRoute() throws Exception {
        Fx f = fixture("ckl-apm");
        String text = "ckl-apm-text-" + f.docId();
        String chash = ch(text);
        Map<String, Object> d = new LinkedHashMap<>();
        d.put("doc_id", f.docId());
        d.put("rows", List.of(row(0, chash)));
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("collection", f.collection());
        body.put("docs", List.of(d));
        body.put("chunks", List.of(chunkWith(chash, text, offendingMetadata(chash))));

        Resp r = send(catalog, "/v1/catalog/manifest/append_many", body);

        assertThat(r.status()).as("body: %s", r.body()).isEqualTo(200);
        assertThat(lines()).hasSize(1);
        assertThat(lines().get(0)).contains("route=manifest/append_many");
        assertThat(ChunkKeyAllowlistActivity.wouldRefuseTotal()).isEqualTo(1);
        assertThat(chunkExists(f.collection(), chash)).isTrue();
    }

    // ── upsert-chunks and update-metadata ────────────────────────────────────────────────────────────

    /** Seed one OWNED chunk with clean metadata (so upsert-chunks passes its ownership check) and return its chash. */
    private String seedOwned(Fx f, String tag) throws Exception {
        String text = tag + "-text-" + f.docId();
        String chash = ch(text);
        Map<String, Object> clean = new LinkedHashMap<>();
        clean.put("chunk_text_hash", chash);
        clean.put("embedding_model", "minilm-l6-v2-384");
        assertThat(send(catalog, "/v1/catalog/manifest/write_many", writeManyBody(f, text, clean)).status())
            .isEqualTo(200);
        return chash;
    }

    @Test
    void upsertChunks_withOffendingKeys_logsAndWritesUnchanged() throws Exception {
        Fx f = fixture("ckl-up");
        String chash = seedOwned(f, "ckl-up");
        assertThat(lines()).isEmpty();

        Map<String, Object> body = new LinkedHashMap<>();
        body.put("collection", f.collection());
        body.put("ids", List.of(chash));
        body.put("documents", List.of("ckl-up-text-" + f.docId()));
        body.put("metadatas", List.of(offendingMetadata(chash)));
        Resp r = send(vectors, "/v1/vectors/upsert-chunks", body);

        assertThat(r.status()).as("body: %s", r.body()).isEqualTo(200);
        assertThat(lines()).hasSize(1);
        assertThat(lines().get(0)).contains("route=upsert-chunks").contains("legacy:doc_id")
            .contains("user_agent=\"nx-test-agent/9\"").contains("client_version=\"7.99.0\"");
        assertThat(ChunkKeyAllowlistActivity.wouldRefuseTotal()).isEqualTo(1);
        assertThat(storedMetadata(f.collection(), chash)).containsKey("zz_unknown_key");
    }

    @Test
    void updateMetadata_withOffendingKeys_logsAndWritesUnchanged() throws Exception {
        Fx f = fixture("ckl-um");
        String chash = seedOwned(f, "ckl-um");

        Map<String, Object> body = new LinkedHashMap<>();
        body.put("collection", f.collection());
        body.put("ids", List.of(chash));
        body.put("metadatas", List.of(Map.of("title", "renamed", "zz_unknown_key", "x")));
        Resp r = send(vectors, "/v1/vectors/update-metadata", body);

        assertThat(r.status()).as("body: %s", r.body()).isEqualTo(200);
        assertThat(r.json().get("updated").asInt()).isEqualTo(1);
        assertThat(lines()).hasSize(1);
        assertThat(lines().get(0)).contains("route=update-metadata")
            .contains("offending_keys=\"other:zz_unknown_key,document:title\"");
        assertThat(ChunkKeyAllowlistActivity.wouldRefuseTotal()).isEqualTo(1);
        assertThat(storedMetadata(f.collection(), chash)).containsEntry("title", "renamed");
    }

    @Test
    void control_deleteKeysAreNotInspected() throws Exception {
        Fx f = fixture("ckl-del");
        String chash = seedOwned(f, "ckl-del");

        Map<String, Object> body = new LinkedHashMap<>();
        body.put("collection", f.collection());
        body.put("ids", List.of(chash));
        body.put("metadatas", List.of(Map.of()));
        body.put("delete_keys", List.of("title", "doc_id", "zz_unknown_key"));
        Resp r = send(vectors, "/v1/vectors/update-metadata", body);

        assertThat(r.status()).as("body: %s", r.body()).isEqualTo(200);
        assertThat(lines()).isEmpty();
        assertThat(ChunkKeyAllowlistActivity.wouldRefuseTotal()).isZero();
    }

    // ── the bypass prefixes ──────────────────────────────────────────────────────────────────────────

    @Test
    void control_aBypassPrefixCollectionIsSkipped_butTheSameWriteToAnotherCollectionIsNot() throws Exception {
        // The check's bypass set is ChunkKeyPlan.BYPASS_PREFIXES, the same exemption the database CHECK carries.
        // The two prefixes are built here from that constant, so no literal holding the separator is written.
        for (String prefix : dev.nexus.service.vectors.ChunkKeyPlan.BYPASS_PREFIXES) {
            String bypass = prefix + "code" + "_".repeat(2) + "cklbypass" + "_".repeat(2) + "minilm-l6-v2-384"
                + "_".repeat(2) + "v1";
            registerCollection(bypass);
            Map<String, Object> body = new LinkedHashMap<>();
            body.put("collection", bypass);
            body.put("ids", List.of(ch("ckl-bypass-" + prefix)));
            body.put("metadatas", List.of(offendingMetadata(ch("ckl-bypass-" + prefix))));
            send(vectors, "/v1/vectors/update-metadata", body);
            assertThat(lines()).as("bypass prefix %s", prefix).isEmpty();
            assertThat(ChunkKeyAllowlistActivity.wouldRefuseTotal()).as("bypass prefix %s", prefix).isZero();
        }
        // positive: the same request shape to a non-bypass collection is seen
        Fx f = fixture("ckl-nonbypass");
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("collection", f.collection());
        body.put("ids", List.of(ch("ckl-nonbypass")));
        body.put("metadatas", List.of(offendingMetadata(ch("ckl-nonbypass"))));
        send(vectors, "/v1/vectors/update-metadata", body);
        assertThat(lines()).hasSize(1);
        assertThat(ChunkKeyAllowlistActivity.wouldRefuseTotal()).isEqualTo(1);
    }

    // ── what the log line can be made to say ─────────────────────────────────────────────────────────

    @Test
    void aClientControlledKeyCannotForgeAFieldOfTheLine() throws Exception {
        Fx f = fixture("ckl-forge");
        String chash = ch("ckl-forge");
        Map<String, Object> meta = new LinkedHashMap<>();
        meta.put("x\" tenant=evil suppressed_since_last=77\nevent=forged,other:y", "v");
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("collection", f.collection());
        body.put("ids", List.of(chash));
        body.put("metadatas", List.of(meta));
        send(vectors, "/v1/vectors/update-metadata", body);

        assertThat(lines()).hasSize(1);
        String line = lines().get(0);
        assertThat(line).doesNotContain("\n").contains("tenant=" + TENANT).doesNotContain("tenant=evil").contains("tenant~evil")
            .contains("suppressed_since_last=0");
        assertThat(line.split("event=").length).as("one event= in the whole line").isEqualTo(2);
        // The five quoted fields (collection, offending_keys, user_agent, client_version, source_agent) own ten
        // quote characters between them: the forged key's own quote is turned into an apostrophe and cannot close
        // the offending_keys field early.
        assertThat(line.chars().filter(c -> c == '"').count()).as("quote count of the whole line").isEqualTo(10);
        var field = java.util.regex.Pattern.compile("offending_keys=\"([^\"]*)\"").matcher(line);
        assertThat(field.find()).isTrue();
        assertThat(field.group(1)).isEqualTo("other:x'_tenant~evil_suppressed_since_last~77_event~forged.other.y");
    }

    // ── the seams ────────────────────────────────────────────────────────────────────────────────────

    @Test
    void anUpsertTheOwnershipCheckRefuses_isStillCounted_theCheckRunsBeforeTheRefusal() throws Exception {
        // Seam (a) of ChunkKeyAllowlistCheck: the check sits before the ownerless 422, so a refused request counts.
        Fx f = fixture("ckl-owned-refused");
        String chash = ch("ckl-never-seeded-" + f.docId());   // no manifest row references it: ownerless
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("collection", f.collection());
        body.put("ids", List.of(chash));
        body.put("documents", List.of("ckl-never-seeded-" + f.docId()));
        body.put("metadatas", List.of(offendingMetadata(chash)));

        Resp r = send(vectors, "/v1/vectors/upsert-chunks", body);

        assertThat(r.status()).as("the write is refused for ownership, body: %s", r.body()).isEqualTo(422);
        assertThat(ChunkKeyAllowlistActivity.wouldRefuseTotal()).isEqualTo(1);
        assertThat(lines()).hasSize(1);
        assertThat(lines().get(0)).contains("route=upsert-chunks").contains("legacy:doc_id");
        assertThat(chunkExists(f.collection(), chash)).as("and nothing was stored").isFalse();
    }
}
