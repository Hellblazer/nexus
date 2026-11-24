// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import dev.nexus.service.CollectionRouteFixture.Resp;
import dev.nexus.service.db.CollectionIds;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.TestInstance;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.MethodSource;

import java.util.stream.Stream;

import static dev.nexus.service.CollectionRouteFixture.label;
import static dev.nexus.service.jooq.nexus.Tables.CATALOG_COLLECTIONS;
import static org.assertj.core.api.Assertions.assertThat;

/**
 * RDR-229 R7 (bead nexus-p2m1x.25, linda's ruling 2026-11-23): a READ by label of a retired collection that names no live successor
 * answers the 409 {@code collection_superseded} the same read by id answers: the same reason and fields (the retired label and id), no
 * successor field, no new field and no new reason. One row per reader class the ruling names (vector search and its scoped variant,
 * get, store-get, store-list, the topics, the aspects); the re-embed source and target are {@link ReembedRetiredNoSuccessorTest}'s.
 *
 * <p>Each row runs four cases against the real engine: the label refused, the id refused with the SAME body, a live label (and id) that
 * still reads, and a tombstone WITH a live successor that still reads by label as it did and is still refused by id naming the successor.
 * The fixture of a tombstone whose successor has no row is {@code CatalogDualWriteIdTest}'s and {@code CollectionRenameTupleTest}'s: the
 * row's {@code superseded_by_id} is its own id and its {@code superseded_by} text a label nothing bears.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class RetiredLabelReadersTest {

    /** How a route carries the collection: a body with one label or a list of them, or a query string. */
    enum Shape { BODY_ONE, BODY_MANY, QUERY }

    /** One reader class's route: its verb, path and what the request carries beside the collection. */
    record Reader(String name, boolean post, String path, Shape shape, String extra, int reads) {
        Reader(String name, boolean post, String path, Shape shape, String extra) {
            this(name, post, path, shape, extra, 200);
        }

        String request(String selector) {
            return switch (shape) {
                case BODY_ONE -> "{" + selector + extra + "}";
                case BODY_MANY -> "{" + selector + extra + "}";
                case QUERY -> selector + extra;
            };
        }

        String byLabel(String label) {
            return request(switch (shape) {
                case BODY_ONE -> "\"collection\":\"" + label + "\"";
                case BODY_MANY -> "\"collections\":[\"" + label + "\"]";
                case QUERY -> "collection=" + label;
            });
        }

        String byId(long id) {
            return request(switch (shape) {
                case BODY_ONE -> "\"collection_id\":" + id;
                case BODY_MANY -> "\"collection_ids\":[" + id + "]";
                case QUERY -> "collection_id=" + id;
            });
        }

        @Override
        public String toString() {
            return name;
        }
    }

    static Stream<Arguments> readers() {
        return Stream.of(
            new Reader("vector search", true, "/v1/vectors/search", Shape.BODY_MANY, ",\"query\":\"p35a\""),
            new Reader("vector query", true, "/v1/vectors/query", Shape.BODY_MANY, ",\"query\":\"p35a\""),
            new Reader("vector search, metadata-scoped", true, "/v1/vectors/search-metadata-scoped", Shape.BODY_MANY, ",\"query\":\"p35a\""),
            new Reader("vector get", true, "/v1/vectors/get", Shape.BODY_ONE, ""),
            new Reader("vector store-get", true, "/v1/vectors/store-get", Shape.BODY_ONE, ""),
            new Reader("vector store-list", true, "/v1/vectors/store-list", Shape.BODY_ONE, ""),
            new Reader("taxonomy topics", false, "/v1/taxonomy/topics", Shape.QUERY, ""),
            new Reader("aspect get", false, "/v1/aspects/get", Shape.QUERY, "&source_path=p35a.md", 404),
            new Reader("aspect list_by_collection", false, "/v1/aspects/list_by_collection", Shape.QUERY, ""))
            .map(r -> Arguments.of(r));
    }

    CollectionRouteFixture fx;
    int n;

    @BeforeAll
    void startAll() throws Exception {
        fx = new CollectionRouteFixture();
    }

    @AfterAll
    void stopAll() {
        if (fx != null) fx.close();
    }

    private Resp send(Reader reader, String request) {
        return reader.post() ? fx.post(reader.path(), request) : fx.get(reader.path() + "?" + request);
    }

    private String live(String stem) {
        String l = label("r7-" + stem + "-" + (n++));
        fx.register(l);
        return l;
    }

    /** A retired row whose successor has no row (R7): its own id in {@code superseded_by_id}, a dead label in {@code superseded_by}. */
    private String retiredNoSuccessor() {
        String dead = label("r7-dead-" + (n++));
        String l = live("nosucc");
        fx.asSuperuser(ctx -> ctx.update(CATALOG_COLLECTIONS)
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY, dead)
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, CATALOG_COLLECTIONS.COLLECTION_ID)
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.NAME.eq(l)).execute());
        return l;
    }

    /** A retired row whose successor {@code successor} is live. */
    private String retiredTo(String successor) {
        String l = live("succ");
        fx.asSuperuser(ctx -> ctx.update(CATALOG_COLLECTIONS)
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY, successor)
            .set(CATALOG_COLLECTIONS.SUPERSEDED_BY_ID, CollectionIds.idOf(fx.tenant, successor))
            .where(CATALOG_COLLECTIONS.TENANT_ID.eq(fx.tenant)).and(CATALOG_COLLECTIONS.NAME.eq(l)).execute());
        return l;
    }

    @ParameterizedTest(name = "{0}")
    @MethodSource("readers")
    void aLabelOfARetiredCollectionWithNoSuccessor_isRefusedWithTheBodyTheIdGets(Reader reader) {
        String retired = retiredNoSuccessor();
        long id = fx.idOf(retired);

        Resp byLabel = send(reader, reader.byLabel(retired));
        Resp byId = send(reader, reader.byId(id));

        assertThat(byLabel.status()).as("by label: %s", byLabel.body()).isEqualTo(409);
        assertThat(byLabel.reason()).isEqualTo("collection_superseded");
        assertThat(byLabel.json().path("error").asText()).isEqualTo("collection_superseded");
        assertThat(byLabel.json().path("collection").asText()).isEqualTo(retired);
        assertThat(byLabel.json().path("collection_id").asLong(-1)).isEqualTo(id);
        assertThat(byLabel.json().has("superseded_by")).as("no successor fields: %s", byLabel.body()).isFalse();
        assertThat(byLabel.json().has("successor_collection_id")).isFalse();
        assertThat(byId.status()).as("by id: %s", byId.body()).isEqualTo(409);
        assertThat(byLabel.json()).as("the one body, whichever way the collection was named").isEqualTo(byId.json());
    }

    @ParameterizedTest(name = "{0}")
    @MethodSource("readers")
    void aLiveLabelStillReads_byLabelAndById(Reader reader) {
        String live = live("live");

        Resp byLabel = send(reader, reader.byLabel(live));
        Resp byId = send(reader, reader.byId(fx.idOf(live)));

        assertThat(byLabel.status()).as(byLabel.body()).isEqualTo(reader.reads());   // an aspect get of an unfiled document is its own 404
        assertThat(byId.status()).as(byId.body()).isEqualTo(reader.reads());
        assertThat(byLabel.reason()).isNotEqualTo("collection_superseded");
    }

    @ParameterizedTest(name = "{0}")
    @MethodSource("readers")
    void aLabelOfARetiredCollectionWithALiveSuccessor_readsAsItDid_andItsIdIsRefusedNamingTheSuccessor(Reader reader) {
        String successor = live("heir");
        String retired = retiredTo(successor);

        Resp byLabel = send(reader, reader.byLabel(retired));
        Resp byId = send(reader, reader.byId(fx.idOf(retired)));

        assertThat(byLabel.status()).as("a tombstone with a successor is read by label as before: %s", byLabel.body()).isEqualTo(reader.reads());
        assertThat(byId.status()).as(byId.body()).isEqualTo(409);
        assertThat(byId.reason()).isEqualTo("collection_superseded");
        assertThat(byId.json().path("superseded_by").asText()).isEqualTo(successor);
        assertThat(byId.json().path("successor_collection_id").asLong(-1)).isEqualTo(fx.idOf(successor));
    }

    /** A label no row has keeps the handler's own answer: the refusal is for a retired row, not an unknown label. */
    @ParameterizedTest(name = "{0}")
    @MethodSource("readers")
    void anUnregisteredLabelIsNotThisRefusal(Reader reader) {
        Resp r = send(reader, reader.byLabel(label("r7-never-registered-" + (n++))));

        assertThat(r.reason()).as(r.body()).isNotEqualTo("collection_superseded");
    }

    /** A request that names a live row and the retired one is refused whole, as the same list by id is. */
    @ParameterizedTest(name = "{0}")
    @MethodSource("manyReaders")
    void aListThatNamesARetiredLabelBesideALiveOneIsRefused(Reader reader) {
        String live = live("pair-live");
        String retired = retiredNoSuccessor();

        Resp r = send(reader, "{\"collections\":[\"" + live + "\",\"" + retired + "\"],\"query\":\"p35a\"}");

        assertThat(r.status()).as(r.body()).isEqualTo(409);
        assertThat(r.json().path("collection").asText()).isEqualTo(retired);
    }

    static Stream<Arguments> manyReaders() {
        return readers().filter(a -> ((Reader) a.get()[0]).shape() == Shape.BODY_MANY);
    }
}
