// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.SweepBounds;
import org.jooq.Condition;
import org.jooq.DSLContext;
import org.jooq.Field;
import org.jooq.Record;
import org.jooq.Result;
import org.jooq.SQLDialect;
import org.jooq.Table;
import org.jooq.impl.DSL;
import org.jooq.exception.DataAccessException;
import org.jooq.impl.SQLDataType;
import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

import javax.sql.DataSource;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.sql.Connection;
import java.sql.SQLException;
import java.time.Duration;
import java.time.Instant;
import java.time.format.DateTimeParseException;
import java.time.temporal.ChronoUnit;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * RDR-227 Step 2 (nexus-43ulx.11): the names of the per-collection HNSW indexes ({@code pci_...}) and the read of
 * the catalog that says which of them exist, on which leaf, for which collection, and whether each is valid.
 *
 * <p><b>Name.</b> {@link #indexName} is {@code pci_} plus the first 24 hex digits of SHA-256 over
 * {@code model NUL tenant NUL collection} (28 characters, well under the 63-byte identifier limit). The hash is the
 * only thing the name says; the collection is read back from the index's predicate, never from its name.
 *
 * <p><b>Catalog read.</b> {@link #read()} makes one statement, as whatever role the pool connects as ({@code nexus_svc}
 * in the engine; the catalogs it reads are not row-secured). It walks the layout RDR-225 made
 * ({@code vectors-030-model-tenant-partition-functions.xml}): {@code nexus.chunks} is LIST-partitioned by
 * {@code embedding_model} into model partitions, each LIST-partitioned by {@code tenant_id} into tenant leaves. There
 * IS a model level above the tenant leaves, so a leaf's model is the bound of its parent and its tenant is its own
 * bound. Both bounds are read with {@code pg_get_expr(relpartbound, oid)} and parsed here; {@code
 * nexus.partition_bound_value} does the same job server-side but is granted to no role. One statement means one
 * snapshot, so a leaf is never listed with indexes from two different instants.
 *
 * <p><b>The read is bounded</b> (nexus-u9zkn convention). It runs on ONE borrowed connection inside a transaction
 * that opens with {@code set_config('statement_timeout', '<bound> ms', true)} through
 * {@link SweepBounds#applyStatementTimeout}, which also gives the connection a network (socket read) timeout of the
 * bound plus {@code NX_PG_SOCKET_TIMEOUT_MARGIN_SECONDS}. A statement stuck behind a lock ends at the bound with
 * SQLSTATE 57014; a server gone silent (no RST, the 2026-10-05 failover) ends at bound plus margin. Either way the
 * read throws, which the sweep treats as a failed read. The bound is {@link #DEFAULT_READ_BOUND}, the engine's
 * established bound for a background sweep statement.
 *
 * <p><b>Unparsed rule</b> (the RDR leaves it open; Sam's decision 4, T2 {@code nexus_rdr/227-planner-decisions-confirmed},
 * extended by the session in nexus-43ulx.11's fix round). An index is <i>parsed</i> only when ALL of these hold: its
 * name matches {@code ^pci_[0-9a-f]{24}$} (the shape {@link #indexName} makes), its access method is {@code hnsw}
 * ({@code pg_am} through {@code pg_class.relam}), its predicate is exactly {@code (collection = '<name>'::text)}, and
 * its leaf's own and parent bounds are single-value LIST bounds. Every other index whose name starts with
 * {@code pci_} is <i>unparsed</i>: an operator-made index ({@code pci_foo}), a non-hnsw index, a hand-edited
 * predicate, a valid REINDEX CONCURRENTLY product ({@code pci_<hash>_ccnew}), or a PostgreSQL that deparses
 * differently. An unparsed index is listed with a {@code null} collection and counted by
 * {@link Snapshot#unparsedCount()} (the status object reports it), whatever its validity. It is never routed to
 * ({@link Snapshot#hasValidIndex} cannot return true for it), never built, and never dropped: no DDL consumer may act
 * on an index that is not {@link Index#parsed()}. This NARROWS the RDR Schema sentence saying operator-made
 * {@code pci_} indexes follow the build and retire rules; the RDR's Schema paragraph carries the narrowed rule
 * (nexus-43ulx.23).
 *
 * <p><b>REINDEX CONCURRENTLY leftovers</b> (Sam, 2026-10-18, nexus-rn1yd; T2
 * {@code nexus_rdr/227-decision-4-amendment-reindex-leftovers}). One carve-out from the unparsed rule: an index named
 * {@code ^pci_[0-9a-f]{24}_cc(new|old)[0-9]*$} (PostgreSQL's REINDEX CONCURRENTLY names for a builder index; the
 * digits come from {@code ChooseRelationName} on a collision) that is {@code indisvalid} false and passes every OTHER
 * condition of the parsed rule (hnsw, parsing predicate, both leaf bounds parsed, {@code standard_conforming_strings}
 * on) is a <i>leftover</i>: {@link Index#leftover()} true, {@link Index#leftoverOf()} the collection its predicate
 * selects. It stays NOT parsed in the routing sense ({@link Index#parsed()} false, never routed to, never counted
 * against a cap, never taken as "has a pci_ index" for build eligibility) and is counted by
 * {@link Snapshot#reindexLeftoverCount()} INSTEAD of {@link Snapshot#unparsedCount()}, so no index is counted twice.
 * The reconciler drops it. A VALID {@code _ccnew} or {@code _ccold}, and every other unparsed index, keep decision 4:
 * never touched. The attribution is {@link #leftoverCollection}, beside {@link #attributedCollection}.
 *
 * <p><b>Deparse depends on a setting.</b> {@code pg_get_expr} writes string literals according to
 * {@code standard_conforming_strings} (backslashes are doubled when it is off). The read takes the setting in the same
 * statement, and when it is not {@code on} every index is unparsed, the safe direction.
 */
public final class PciCatalog {

    /** The prefix of every per-collection index name; the reader considers no other index. */
    public static final String PREFIX = "pci_";

    /** The longest the catalog statement may run: {@link SweepBounds#STATEMENT_TIMEOUT}, the sweep-statement bound. */
    public static final Duration DEFAULT_READ_BOUND = SweepBounds.STATEMENT_TIMEOUT;

    private static final int HASH_HEX_DIGITS = 24;

    /** {@code (collection = '<name>'::text)}, with {@code ''} standing for a quote inside the name. */
    private static final Pattern COLLECTION_PREDICATE =
        Pattern.compile("^\\(collection = '((?:[^']++|'')*+)'::text\\)$", Pattern.DOTALL);

    /**
     * {@code (collection_id = '<id>'::bigint)}: what PostgreSQL deparses for {@code WHERE collection_id = '<id>'} on a bigint
     * column (RDR-229 P3.4; {@code PciIdIndexIntegrationTest} pins it on a real server). An id is positive, so no sign.
     */
    private static final Pattern COLLECTION_ID_PREDICATE =
        Pattern.compile("^\\(collection_id = '([0-9]{1,19})'::bigint\\)$");

    /** The shape of every name {@link #indexName} makes; any other {@code pci_} name is not the builder's. */
    private static final Pattern BUILDER_NAME = Pattern.compile("^pci_[0-9a-f]{24}$");

    /** What REINDEX CONCURRENTLY makes of a builder name: {@code _ccnew} or {@code _ccold}, then a collision counter. */
    private static final Pattern LEFTOVER_NAME = Pattern.compile("^pci_[0-9a-f]{24}_cc(?:new|old)[0-9]*$");

    /** The only access method the router and the builder deal in. */
    private static final String ACCESS_METHOD = "hnsw";

    /** {@code FOR VALUES IN ('<value>')}, a single-value LIST bound. */
    private static final Pattern LIST_BOUND =
        Pattern.compile("^FOR VALUES IN \\('((?:[^']++|'')*+)'\\)$", Pattern.DOTALL);

    /**
     * The anchor's format version: {@code "nexus_pci":1}. It stays 1 when an optional field is added (an engine that
     * knows only the older fields reads the text and ignores the rest, so a rollback or a mixed-engine fleet does not
     * re-adopt); the fields an anchor has are what its readers test.
     */
    public static final int ANCHOR_VERSION = 1;
    /** {@link Anchor#origin()} of an index the builder built and anchored right after the build. */
    public static final String ORIGIN_BUILT = "built";
    /** {@link Anchor#origin()} of an index found with no parseable anchor and anchored by the pass that saw it. */
    public static final String ORIGIN_ADOPTED = "adopted";

    private static final ObjectMapper JSON = new ObjectMapper();

    /**
     * The index's anchor (RDR-230 Technical Design 1): what the builder knew when the index was built, kept as the
     * index's PostgreSQL comment,
     * {@code {"nexus_pci":1,"built_at":"<ISO-8601 Z>","rows":N,"bytes":N,"origin":"built","relfilenode":N}}. A comment
     * follows the index through {@code REINDEX CONCURRENTLY} and goes with it when it is dropped, but
     * {@code pg_class.relfilenode} changes with every rebuild, so an anchor whose relfilenode differs from the index's
     * now describes a build the engine did not do (nexus-4lnn1.1.10). {@code relfilenode} is optional: an anchor without
     * it stays valid, and the engine stamps it once.
     *
     * @param builtAt     when the index was built (adopted: when it was first seen), truncated to microseconds
     * @param rows        the collection's uncapped row count on its leaf at that moment
     * @param bytes       {@code pg_relation_size} of the index at that moment
     * @param origin      {@link #ORIGIN_BUILT} or {@link #ORIGIN_ADOPTED} (a parsed anchor may carry another word)
     * @param relfilenode {@code pg_class.relfilenode} of the index the anchor was written against, or {@code null} for
     *                    an anchor written before the field existed (and for one the engine has not yet stamped: the
     *                    writer sets it on the same session, right before the COMMENT)
     * @param redUnfixed  the optional {@code red_unfixed} field: a red recall canary that a REINDEX did not cure, or
     *                    {@code null}
     */
    public record Anchor(Instant builtAt, long rows, long bytes, String origin, Long relfilenode,
                         RedUnfixed redUnfixed) {
        public Anchor {
            Objects.requireNonNull(builtAt, "builtAt");
            Objects.requireNonNull(origin, "origin");
            if (rows < 0 || bytes < 0) {
                throw new IllegalArgumentException("rows and bytes must not be negative");
            }
            if (relfilenode != null && relfilenode < 0) {
                throw new IllegalArgumentException("relfilenode must not be negative");
            }
            builtAt = builtAt.truncatedTo(ChronoUnit.MICROS);
        }

        /** An anchor with no relfilenode yet (the writer stamps it). */
        public Anchor(Instant builtAt, long rows, long bytes, String origin) {
            this(builtAt, rows, bytes, origin, null, null);
        }

        public Anchor(Instant builtAt, long rows, long bytes, String origin, Long relfilenode) {
            this(builtAt, rows, bytes, origin, relfilenode, null);
        }

        /** This anchor against {@code relfilenode}; {@code red_unfixed} is carried forward, its own condition lifts it. */
        public Anchor withRelfilenode(long relfilenode) {
            return new Anchor(builtAt, rows, bytes, origin, relfilenode, redUnfixed);
        }

        /** This anchor with {@code red_unfixed} set to {@code red}. */
        public Anchor withRedUnfixed(RedUnfixed red) {
            return new Anchor(builtAt, rows, bytes, origin, relfilenode, red);
        }

        /** This anchor without {@code red_unfixed}. */
        public Anchor withoutRedUnfixed() {
            return new Anchor(builtAt, rows, bytes, origin, relfilenode, null);
        }

        /**
         * Whether the index was rebuilt since this anchor was written: it has a relfilenode and the index's differs. An
         * anchor without one, or an index whose relfilenode could not be read ({@code null}), is never rebuilt: no evidence.
         */
        public boolean rebuiltSince(Long currentRelfilenode) {
            return relfilenode != null && currentRelfilenode != null && !relfilenode.equals(currentRelfilenode);
        }

        /** Whether {@code other} says the same of the index (when, rows, bytes, origin), whatever its relfilenode. */
        public boolean sameFigures(Anchor other) {
            return builtAt.equals(other.builtAt) && rows == other.rows && bytes == other.bytes
                && origin.equals(other.origin);
        }

        /**
         * @throws IllegalStateException when the origin is neither {@link #ORIGIN_BUILT} nor {@link #ORIGIN_ADOPTED}
         */
        public void checkWritable() {
            if (!writable()) {
                throw new IllegalStateException("not an origin this engine writes: " + origin);
            }
        }

        /** Whether the origin is one this engine writes ({@link #ORIGIN_BUILT} or {@link #ORIGIN_ADOPTED}). */
        public boolean writable() {
            return ORIGIN_BUILT.equals(origin) || ORIGIN_ADOPTED.equals(origin);
        }

        /**
         * The comment text. It is built from numbers, a timestamp and one of two fixed words, so nothing in it needs
         * escaping. The optional {@code relfilenode} is last, so an anchor without one is the text this engine wrote
         * before the field existed (the engine always stamps before it writes).
         *
         * @throws IllegalStateException when the origin is neither {@link #ORIGIN_BUILT} nor {@link #ORIGIN_ADOPTED}
         */
        public String toJson() {
            checkWritable();
            String head = "{\"nexus_pci\":" + ANCHOR_VERSION
                + ",\"built_at\":\"" + builtAt + "\",\"rows\":" + rows + ",\"bytes\":" + bytes + ",\"origin\":\""
                + origin + "\"";
            String rf = relfilenode == null ? "" : ",\"relfilenode\":" + relfilenode;
            return head + rf + (redUnfixed == null ? "" : ",\"red_unfixed\":" + redUnfixed.toJson()) + "}";
        }
    }

    /**
     * A red recall canary that a REINDEX did not cure (RDR-230 Phase 2, nexus-4lnn1.1.10), kept in the anchor:
     * {@code "red_unfixed":{"at":"<ISO-8601 Z>","relfilenode":N,"rows":N,"queries":["<16 hex>",...]}}. The red-canary
     * trigger skips an index while the field holds ({@link PciReindexPolicy#redUnfixedHolds}): the index has the
     * relfilenode it names, every query that reads red now is among the queries it lists, and the collection has not
     * grown by 10% or more since. A rebuild, a red query it does not list or that growth lifts it, with no write; a red
     * set that only shrinks (the oldest red query left the window) does not.
     *
     * @param at          when the still-red re-measure was taken, truncated to microseconds
     * @param relfilenode the index's relfilenode at the red: the REINDEX's fresh storage
     * @param rows        the collection's row count at the red (the count the REINDEX's fresh anchor holds)
     * @param queries     {@link PciQueryRing#queryIds} of the queries that read red in the re-measure: sorted, distinct,
     *                    at most {@value #MAX_QUERIES} (the canary's window), never empty
     */
    public record RedUnfixed(Instant at, long relfilenode, long rows, List<String> queries) {
        /** The most queries a canary run has, so the most it can read red. */
        public static final int MAX_QUERIES = PciCanaryPolicy.MAX_QUERIES;   // the canary's window, one source

        public RedUnfixed {
            Objects.requireNonNull(at, "at");
            Objects.requireNonNull(queries, "queries");
            if (relfilenode < 0 || rows < 0 || queries.isEmpty() || queries.size() > MAX_QUERIES
                || queries.stream().anyMatch(q -> q == null || q.length() != PciQueryRing.QUERY_ID_HEX
                    || !q.chars().allMatch(c -> Character.digit(c, 16) >= 0 && !Character.isUpperCase(c)))) {
                throw new IllegalArgumentException("relfilenode and rows must not be negative, queries 1 to "
                    + MAX_QUERIES + " lower-case " + PciQueryRing.QUERY_ID_HEX + "-hex ids");
            }
            at = at.truncatedTo(ChronoUnit.MICROS);
            queries = List.copyOf(new java.util.TreeSet<>(queries));
        }

        /** The field's value as JSON: numbers, a timestamp and hex strings, so nothing needs escaping. */
        String toJson() {
            return "{\"at\":\"" + at + "\",\"relfilenode\":" + relfilenode + ",\"rows\":" + rows
                + ",\"queries\":[" + queries.stream().map(q -> "\"" + q + "\"")
                    .collect(java.util.stream.Collectors.joining(",")) + "]}";
        }
    }

    /**
     * The anchor in {@code comment}, or empty when there is none: a null or blank comment, text that is not a JSON
     * object, a {@code nexus_pci} other than {@value #ANCHOR_VERSION}, or a required field that is missing or of the
     * wrong type. Unknown fields are ignored, and so is an optional one of the wrong type (a {@code relfilenode} that is
     * not a non-negative integer reads as none). Never throws.
     */
    public static Optional<Anchor> parseAnchor(String comment) {
        if (comment == null || comment.isBlank()) {
            return Optional.empty();
        }
        try {
            JsonNode n = JSON.readTree(comment);
            if (n == null || !n.isObject()) {
                return Optional.empty();
            }
            JsonNode version = n.get("nexus_pci");
            JsonNode builtAt = n.get("built_at");
            JsonNode rows = n.get("rows");
            JsonNode bytes = n.get("bytes");
            JsonNode origin = n.get("origin");
            if (version == null || !version.isIntegralNumber() || !version.canConvertToInt()
                || version.asInt() != ANCHOR_VERSION
                || builtAt == null || !builtAt.isTextual()
                || rows == null || !rows.isIntegralNumber() || !rows.canConvertToLong() || rows.asLong() < 0
                || bytes == null || !bytes.isIntegralNumber() || !bytes.canConvertToLong() || bytes.asLong() < 0
                || origin == null || !origin.isTextual() || origin.asText().isBlank()) {
                return Optional.empty();
            }
            JsonNode rf = n.get("relfilenode");
            Long relfilenode = rf != null && rf.isIntegralNumber() && rf.canConvertToLong() && rf.asLong() >= 0
                ? rf.asLong() : null;
            return Optional.of(new Anchor(Instant.parse(builtAt.asText()), rows.asLong(), bytes.asLong(),
                origin.asText(), relfilenode, parseRedUnfixed(n.get("red_unfixed"))));
        } catch (JsonProcessingException | DateTimeParseException e) {
            return Optional.empty();
        }
    }

    /** The {@code red_unfixed} field, or {@code null} when it is absent or any part of it is of the wrong type. */
    private static RedUnfixed parseRedUnfixed(JsonNode n) {
        if (n == null || !n.isObject()) {
            return null;
        }
        JsonNode at = n.get("at");
        JsonNode rf = n.get("relfilenode");
        JsonNode rows = n.get("rows");
        JsonNode queries = n.get("queries");
        try {
            if (at == null || !at.isTextual() || rf == null || !rf.isIntegralNumber() || !rf.canConvertToLong()
                || rf.asLong() < 0 || rows == null || !rows.isIntegralNumber() || !rows.canConvertToLong()
                || rows.asLong() < 0 || queries == null || !queries.isArray()) {
                return null;
            }
            List<String> ids = new ArrayList<>();
            for (JsonNode q : queries) {
                if (!q.isTextual()) {
                    return null;
                }
                ids.add(q.asText());
            }
            return new RedUnfixed(Instant.parse(at.asText()), rf.asLong(), rows.asLong(), ids);
        } catch (DateTimeParseException | IllegalArgumentException e) {
            return null;
        }
    }

    /**
     * One {@code pci_} index on a leaf.
     *
     * @param name         the index name
     * @param valid        {@code pg_index.indisvalid}; false while a concurrent build runs and after one fails
     * @param collection   the collection its predicate selects, or {@code null} when the index is unparsed
     * @param leftoverOf   the collection its predicate selects when the index is a REINDEX CONCURRENTLY leftover
     *                     (see the class comment), else {@code null}; never set together with {@code collection}, and
     *                     never set on a valid index (the constructor refuses both)
     * @param comment      the index's PostgreSQL comment as the read saw it ({@code obj_description}), or {@code null}
     *                     when it has none; {@link #anchor()} parses it
     * @param leafLeftoverOf the name of the leaf's own HNSW index ({@link LeafIndex#name()}) this index is a REINDEX
     *                     CONCURRENTLY leftover of (RDR-230, nexus-4lnn1.1.10; see the class comment), else
     *                     {@code null}; never set together with {@code collection} or {@code leftoverOf}, and never
     *                     set on a valid index (the constructor refuses both)
     */
    public record Index(String name, boolean valid, String collection, String leftoverOf, String comment,
                        String leafLeftoverOf, Long collectionId, Long leftoverOfId) {
        public Index {
            if (collectionId != null && (collection != null || leftoverOf != null || leafLeftoverOf != null)) {
                throw new IllegalArgumentException("an id index is neither a name index nor a leftover: " + name);
            }
            if (leftoverOfId != null && (collection != null || leftoverOf != null || leafLeftoverOf != null || collectionId != null)) {
                throw new IllegalArgumentException("an id leftover is only an id leftover: " + name);
            }
            if (valid && leftoverOfId != null) {
                throw new IllegalArgumentException("a valid index is never a REINDEX CONCURRENTLY leftover: " + name);
            }
            if (collection != null && leftoverOf != null) {
                throw new IllegalArgumentException("an index is parsed or a leftover, not both: " + name);
            }
            if (valid && leftoverOf != null) {
                // A leftover is INVALID by definition (leftoverCollection refuses a valid index). Refusing the pair
                // here means no consumer, planner or executor, can ever be handed a valid index as a leftover.
                throw new IllegalArgumentException("a valid index is never a REINDEX CONCURRENTLY leftover: " + name);
            }
            if (leafLeftoverOf != null && (collection != null || leftoverOf != null)) {
                throw new IllegalArgumentException("a leaf leftover is neither parsed nor a pci_ leftover: " + name);
            }
            if (valid && leafLeftoverOf != null) {
                // The same invariant as above, for the leaf's own index: a leaf leftover is never valid.
                throw new IllegalArgumentException("a valid index is never a leaf REINDEX leftover: " + name);
            }
        }

        /** An id-predicate index, or none: not an id leftover. */
        public Index(String name, boolean valid, String collection, String leftoverOf, String comment,
                     String leafLeftoverOf, Long collectionId) {
            this(name, valid, collection, leftoverOf, comment, leafLeftoverOf, collectionId, null);
        }

        /** A leaf leftover or a name or unparsed index: no id. */
        public Index(String name, boolean valid, String collection, String leftoverOf, String comment,
                     String leafLeftoverOf) {
            this(name, valid, collection, leftoverOf, comment, leafLeftoverOf, null, null);
        }

        /** An index that is not a leaf leftover. */
        public Index(String name, boolean valid, String collection, String leftoverOf, String comment) {
            this(name, valid, collection, leftoverOf, comment, null, null);
        }

        /** An index with no comment. */
        public Index(String name, boolean valid, String collection, String leftoverOf) {
            this(name, valid, collection, leftoverOf, null, null, null);
        }

        /** An index that is parsed or plain unparsed, not a leftover, with no comment. */
        public Index(String name, boolean valid, String collection) {
            this(name, valid, collection, null, null, null, null);
        }

        /**
         * An id-predicate index (RDR-229 P3.4) on {@code collectionId}, read back from its predicate.
         * {@link #parsed()} is false for it: every consumer of the name rule keeps reading name indexes only.
         */
        public static Index byId(String name, boolean valid, long collectionId, String comment) {
            return new Index(name, valid, null, null, comment, null, collectionId);
        }

        /** Which predicate this index carries, as the read parsed it. */
        public PredicateKind kind() {
            return collection != null ? PredicateKind.NAME : collectionId != null ? PredicateKind.ID : PredicateKind.OTHER;
        }

        /** False for an unparsed index, an id index and a leftover: no name consumer may route to or build against it. */
        public boolean parsed() {
            return collection != null;
        }

        /** True for an index whose predicate is {@code collection_id = <id>} with the builder's name shape (RDR-229 P3.4). */
        public boolean idParsed() {
            return collectionId != null;
        }

        /**
         * True for a REINDEX CONCURRENTLY leftover of a per-collection index (the name predicate's, or, RDR-229 P3.4, the id predicate's:
         * {@link #leftoverOfId()}), the one kind of non-parsed index the reconciler drops.
         */
        public boolean leftover() {
            return leftoverOf != null || leftoverOfId != null;
        }

        /**
         * True for a REINDEX CONCURRENTLY leftover of a leaf's own HNSW index, the second kind of non-parsed index the
         * reconciler drops. It is never in {@link Leaf#indexes()}: it is in {@link Leaf#leafLeftovers()}.
         */
        public boolean leafLeftover() {
            return leafLeftoverOf != null;
        }

        /** The anchor in {@link #comment()}, or empty when there is none or it does not parse. */
        public Optional<Anchor> anchor() {
            return parseAnchor(comment);
        }
    }

    /** The predicate kind of a {@code pci_} index as the read parsed it (RDR-229 P3.4). */
    public enum PredicateKind {
        /** {@code WHERE collection = '<name>'}: the router's index today. */
        NAME,
        /** {@code WHERE collection_id = <id>}: built by P3.4, read by P3.5. */
        ID,
        /** Neither parsed: operator-made, a leftover or a hand-edited predicate. */
        OTHER
    }

    /**
     * A leaf's own HNSW index, as the read identifies it (RDR-230 Technical Design 3 and 4): access method
     * {@code hnsw}, no predicate, and attached to the parent's partitioned HNSW index (the index of the same column on
     * {@code nexus.chunks}, reached through the model partition's). Nothing else qualifies: not a {@code pci_} partial
     * index, not an operator's unpredicated HNSW index that is not attached, not any other access method.
     *
     * <p>{@code nexus.chunks} has one HNSW index per embedding column ({@code embedding_384}, {@code embedding_768},
     * {@code embedding_1024}), so a leaf has up to three. Only the one on the leaf's model's dimension holds its
     * rows; {@link Leaf#hnswIndex(int)} picks it.
     *
     * <p>The record carries no size and no anchor. {@code pg_relation_size} takes an {@code AccessShareLock} on the
     * index, and this read locks nothing (it runs on the shared sweep's pool, and a migration holding an
     * {@code AccessExclusiveLock} would stall every router refresh to the read bound), so the REINDEX phase reads the
     * size and the anchor comment of the indexes it judges, lazily, under a 5 s {@code lock_timeout}.
     *
     * @param name      the index name
     * @param valid     {@code pg_index.indisvalid}
     * @param dimension the {@code n} of the key column {@code embedding_<n>}, or 0 when the key column is not of that form
     */
    public record LeafIndex(String name, boolean valid, int dimension) {
        public LeafIndex {
            Objects.requireNonNull(name, "name");
            if (dimension < 0) {
                throw new IllegalArgumentException("dimension must not be negative");
            }
        }
    }

    /**
     * One tenant leaf of {@code nexus.chunks}.
     *
     * @param schema  the leaf's schema
     * @param name    the leaf's relation name
     * @param model   the embedding model of its model partition, or {@code null} when that bound did not parse
     * @param tenant  the leaf's tenant, or {@code null} when its own bound did not parse
     * @param indexes its {@code pci_} indexes, in name order; empty when it has none
     * @param hnswIndexes its own HNSW indexes ({@link LeafIndex}), in name order; empty when the leaf has none, or when
     *                    its model or tenant did not parse (a leaf the builder did not resolve has no identified index)
     * @param leafLeftovers the REINDEX CONCURRENTLY leftovers of {@code hnswIndexes}: invalid HNSW indexes with no
     *                    predicate named {@code <own index name>_cc(new|old)[0-9]*}, each an {@link Index} with
     *                    {@link Index#leafLeftover()} true, in name order; they are NOT in {@code indexes}
     */
    public record Leaf(String schema, String name, String model, String tenant, List<Index> indexes,
                       List<LeafIndex> hnswIndexes, List<Index> leafLeftovers) {
        public Leaf {
            indexes = List.copyOf(indexes);
            hnswIndexes = List.copyOf(hnswIndexes);
            leafLeftovers = List.copyOf(leafLeftovers);
        }

        /** A leaf with no identified own index and no leaf leftover. */
        public Leaf(String schema, String name, String model, String tenant, List<Index> indexes) {
            this(schema, name, model, tenant, indexes, List.of(), List.of());
        }

        /** Its id-predicate indexes (RDR-229 P3.4), in name order; they are in {@link #indexes()} too, with {@link Index#parsed()} false. */
        public List<Index> idIndexes() {
            return indexes.stream().filter(Index::idParsed).toList();
        }

        /** The leaf's own HNSW index on the embedding column of {@code dimension}, if the read identified one. */
        public Optional<LeafIndex> hnswIndex(int dimension) {
            return hnswIndexes.stream().filter(i -> i.dimension() == dimension).findFirst();
        }
    }

    record Key(String model, String tenant, String collection) { }

    /**
     * What one catalog read saw, immutable. It answers the router's question as a {@link PciIndexSet}.
     *
     * <p><b>Contract for callers:</b> an empty {@link #leaves()} means the read found no partition leaves of
     * {@code nexus.chunks}, which no installed schema produces. Treat it as a read failure, never as "there are no
     * indexes": acting on it (a builder deciding everything is missing, a drop pass deciding nothing is wanted)
     * would be acting on a read that saw nothing.
     */
    public static final class Snapshot implements PciIndexSet {
        private final List<Leaf> leaves;
        private final Map<Key, String> valid;
        private final Map<Key, Anchor> anchors;
        private final int validCount;
        private final int invalidCount;
        private final int unparsedCount;
        private final int reindexLeftoverCount;
        private final int leafLeftoverCount;
        private final Set<String> validIds;
        private final Set<String> invalidIds;

        Snapshot(List<Leaf> leaves) {
            this.leaves = List.copyOf(leaves);
            Map<Key, String> validKeys = new LinkedHashMap<>();
            Map<Key, Anchor> validAnchors = new LinkedHashMap<>();
            int validIndexes = 0;
            int invalidIndexes = 0;
            int unparsedIndexes = 0;
            int leftoverIndexes = 0;
            int leafLeftoverIndexes = 0;
            Set<String> validIdKeys = new java.util.TreeSet<>();
            Set<String> invalidIdKeys = new java.util.TreeSet<>();
            for (Leaf leaf : this.leaves) {
                leafLeftoverIndexes += leaf.leafLeftovers().size();
                for (Index index : leaf.indexes()) {
                    if (index.idParsed()) {
                        // Never in the router's maps or counts: no reader selects by id before P3.5.
                        (index.valid() ? validIdKeys : invalidIdKeys)
                            .add(idKey(leaf.model(), leaf.tenant(), index.collectionId()));
                    } else if (index.leftover()) {
                        leftoverIndexes++;
                        if (index.leftoverOfId() != null) {
                            // The leftover of an id index is an INVALID id-predicate index: gate 3's invalid count and id_index_invalid see it.
                            invalidIdKeys.add(idKey(leaf.model(), leaf.tenant(), index.leftoverOfId()));
                        }
                    } else if (!index.parsed()) {
                        unparsedIndexes++;
                    } else if (index.valid()) {
                        validIndexes++;
                        Key key = new Key(leaf.model(), leaf.tenant(), index.collection());
                        if (!validKeys.containsKey(key)) {
                            validKeys.put(key, index.name());
                            index.anchor().ifPresent(a -> validAnchors.put(key, a));
                        }
                    } else {
                        invalidIndexes++;
                    }
                }
            }
            this.valid = Collections.unmodifiableMap(validKeys);
            this.anchors = Collections.unmodifiableMap(validAnchors);
            this.validCount = validIndexes;
            this.invalidCount = invalidIndexes;
            this.unparsedCount = unparsedIndexes;
            this.reindexLeftoverCount = leftoverIndexes;
            this.leafLeftoverCount = leafLeftoverIndexes;
            this.validIds = Collections.unmodifiableSet(validIdKeys);
            this.invalidIds = Collections.unmodifiableSet(invalidIdKeys);
        }

        /** True when a valid id-predicate index exists for exactly this (model, tenant, collection id). */
        public boolean hasValidIdIndex(String model, String tenant, long collectionId) {
            return validIds.contains(idKey(model, tenant, collectionId));
        }

        /** {@link PciCatalog#idKey} of every valid id-predicate index: what gate 3 reads (RDR-229 P3.4). */
        public Set<String> validIdKeys() {
            return validIds;
        }

        /** {@link PciCatalog#idKey} of every INVALID id-predicate index: building now, or failed. */
        public Set<String> invalidIdKeys() {
            return invalidIds;
        }

        /** Id-predicate indexes with {@code indisvalid} true. */
        public int validIdCount() {
            return validIds.size();
        }

        /** Id-predicate indexes with {@code indisvalid} false. */
        public int invalidIdCount() {
            return invalidIds.size();
        }

        /**
         * Every tenant leaf of {@code nexus.chunks}, with or without indexes. Empty means a failed read; see the
         * class comment.
         */
        public List<Leaf> leaves() {
            return leaves;
        }

        /** True when a valid, parsed index exists for exactly this (model, tenant, collection). */
        @Override
        public boolean hasValidIndex(String model, String tenant, String collection) {
            return valid.containsKey(new Key(model, tenant, collection));
        }

        /** The index name this read saw for the key, with no history ({@link Instant#EPOCH}). */
        @Override
        public Optional<ValidIndex> validIndex(String model, String tenant, String collection) {
            String name = valid.get(new Key(model, tenant, collection));
            return name == null ? Optional.empty() : Optional.of(new ValidIndex(name, Instant.EPOCH));
        }

        /**
         * The anchor on the valid index this read saw for the key, or empty when there is no valid index or its
         * comment does not parse (the DDL half adopts it on its next pass).
         */
        @Override
        public Optional<Anchor> anchor(String model, String tenant, String collection) {
            return Optional.ofNullable(anchors.get(new Key(model, tenant, collection)));
        }

        /** The keys that have a valid parsed index, for the sweep's history. */
        Set<Key> validKeys() {
            return valid.keySet();
        }

        /**
         * One entry per valid parsed index, {@code leaf:collection:index}, sorted: the unit the sweep compares between
         * reads to log a change of the router's set.
         */
        java.util.SortedSet<String> validEntries() {
            java.util.SortedSet<String> entries = new java.util.TreeSet<>();
            for (Leaf leaf : leaves) {
                for (Index index : leaf.indexes()) {
                    if (index.parsed() && index.valid()) {
                        entries.add(leaf.name() + ":" + index.collection() + ":" + index.name());
                    }
                }
            }
            return entries;
        }

        /** Parsed indexes with {@code indisvalid} true. */
        public int validCount() {
            return validCount;
        }

        /** Parsed indexes with {@code indisvalid} false: building now, or failed. */
        public int invalidCount() {
            return invalidCount;
        }

        /**
         * {@code pci_} indexes that failed any part of the parsed rule, whatever their validity, EXCLUDING the
         * REINDEX CONCURRENTLY leftovers, which {@link #reindexLeftoverCount()} counts instead.
         */
        public int unparsedCount() {
            return unparsedCount;
        }

        /** Invalid {@code _ccnew}/{@code _ccold} leftovers of a builder index (nexus-rn1yd); not in any other count. */
        public int reindexLeftoverCount() {
            return reindexLeftoverCount;
        }

        /**
         * Invalid {@code _ccnew}/{@code _ccold} leftovers of a leaf's own HNSW index (RDR-230,
         * nexus-4lnn1.1.10); in no other count. Not yet on the status object.
         */
        public int leafLeftoverCount() {
            return leafLeftoverCount;
        }
    }

    private final DataSource dataSource;
    private final Duration readBound;

    /** @param dataSource the pool the engine's role connects through; the read borrows one connection per call */
    public PciCatalog(DataSource dataSource) {
        this(dataSource, DEFAULT_READ_BOUND);
    }

    /** As {@link #PciCatalog(DataSource)} with the statement bound named, which no production caller needs. */
    PciCatalog(DataSource dataSource, Duration readBound) {
        this.dataSource = Objects.requireNonNull(dataSource, "dataSource");
        this.readBound = Objects.requireNonNull(readBound, "readBound");
        if (readBound.toMillis() < 1) {
            throw new IllegalArgumentException("readBound must be at least 1 ms (0 would disable it), got " + readBound);
        }
    }

    /** The statement bound of {@link #read()}: how long a read may run before it fails. */
    public Duration readBound() {
        return readBound;
    }

    /**
     * The name of the per-collection index for a (model, tenant, collection): {@code pci_} plus the first 24 hex
     * digits of the SHA-256 of the three joined by a NUL byte.
     *
     * @throws IllegalArgumentException when an argument contains a NUL (the separator would be ambiguous;
     *                                  PostgreSQL text cannot hold one, so no real value does)
     */
    public static String indexName(String model, String tenant, String collection) {
        Objects.requireNonNull(model, "model");
        Objects.requireNonNull(tenant, "tenant");
        Objects.requireNonNull(collection, "collection");
        if (model.indexOf('\0') >= 0 || tenant.indexOf('\0') >= 0 || collection.indexOf('\0') >= 0) {
            throw new IllegalArgumentException("model, tenant and collection must not contain a NUL");
        }
        MessageDigest sha256;
        try {
            sha256 = MessageDigest.getInstance("SHA-256");
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 is required of every JRE", e);
        }
        sha256.update(model.getBytes(StandardCharsets.UTF_8));
        sha256.update((byte) 0);
        sha256.update(tenant.getBytes(StandardCharsets.UTF_8));
        sha256.update((byte) 0);
        sha256.update(collection.getBytes(StandardCharsets.UTF_8));
        return PREFIX + HexFormat.of().formatHex(sha256.digest()).substring(0, HASH_HEX_DIGITS);
    }

    /** The domain tag of {@link #indexNameById}: its hash input has a third separator, which no name's input can carry. */
    private static final String ID_DOMAIN_TAG = "collection_id";

    /**
     * The name of the per-collection index on the id predicate (RDR-229 P3.4): {@code pci_} plus the first 24 hex digits
     * of the SHA-256 of {@code model NUL tenant NUL "collection_id" NUL <decimal id>}. The shape is {@link #indexName}'s,
     * so {@link #isBuilderName} and the leftover pattern still match it. The input holds three NULs where a name hash's
     * holds two, and a name holds no NUL, so an id hash can never equal a name hash.
     *
     * @throws IllegalArgumentException when an argument contains a NUL or the id is not positive
     */
    public static String indexNameById(String model, String tenant, long collectionId) {
        Objects.requireNonNull(model, "model");
        Objects.requireNonNull(tenant, "tenant");
        if (model.indexOf('\0') >= 0 || tenant.indexOf('\0') >= 0) {
            throw new IllegalArgumentException("model and tenant must not contain a NUL");
        }
        if (collectionId <= 0) {
            throw new IllegalArgumentException("a collection id is positive, got " + collectionId);
        }
        MessageDigest sha256;
        try {
            sha256 = MessageDigest.getInstance("SHA-256");
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 is required of every JRE", e);
        }
        sha256.update(model.getBytes(StandardCharsets.UTF_8));
        sha256.update((byte) 0);
        sha256.update(tenant.getBytes(StandardCharsets.UTF_8));
        sha256.update((byte) 0);
        sha256.update(ID_DOMAIN_TAG.getBytes(StandardCharsets.UTF_8));
        sha256.update((byte) 0);
        sha256.update(Long.toString(collectionId).getBytes(StandardCharsets.UTF_8));
        return PREFIX + HexFormat.of().formatHex(sha256.digest()).substring(0, HASH_HEX_DIGITS);
    }

    /**
     * The key gate 3 compares: {@code model NUL tenant NUL id}. Keeps one tenant's collection from another's, and an id
     * index apart from a name index of the same collection (which has no key here).
     */
    public static String idKey(String model, String tenant, long collectionId) {
        return model + "\0" + tenant + "\0" + collectionId;
    }

    /**
     * Whether {@code name} has the shape {@link #indexName} makes ({@code ^pci_[0-9a-f]{24}$}). The builder checks
     * it before it renders DDL for a name, with the same pattern the reader uses to decide an index is parsed.
     */
    static boolean isBuilderName(String name) {
        return name != null && BUILDER_NAME.matcher(name).matches();
    }

    /** Whether {@code name} has the shape REINDEX CONCURRENTLY gives a builder index's leftover. */
    static boolean isLeftoverName(String name) {
        return name != null && LEFTOVER_NAME.matcher(name).matches();
    }

    /**
     * Whether {@code name} is what REINDEX CONCURRENTLY names the leftover of the leaf's own HNSW index {@code
     * ownIndexName}: that name, quoted (never a generic pattern), plus {@code _ccnew} or {@code _ccold} and a collision
     * counter. PostgreSQL clips the base name only when the result would pass 63 bytes; a leaf index name is
     * {@code chunks_<8 hex>_t_<16 hex>_embedding_<n>_idx}, at most 54 bytes, so no leaf index is ever clipped. A
     * {@code pci_} name never reaches this: {@link #isLeftoverName} decides those.
     */
    static boolean isLeafLeftoverName(String ownIndexName, String name) {
        return ownIndexName != null && name != null
            && name.matches(Pattern.quote(ownIndexName) + "_cc(?:new|old)[0-9]*");
    }

    /**
     * The collection an index selects, from its deparsed predicate ({@code pg_get_expr(indpred, indrelid)}), which
     * for the builder's indexes reads {@code (collection = '<name>'::text)}. A quote inside the name is doubled in
     * that text and undoubled here. Anything else, including a null predicate, is empty (unparsed).
     */
    public static Optional<String> parseCollection(String deparsedPredicate) {
        return unquote(COLLECTION_PREDICATE, deparsedPredicate);
    }

    /**
     * The collection id an index selects, from its deparsed predicate, which for the builder's id indexes reads
     * {@code (collection_id = '<id>'::bigint)}. Anything else, including a null predicate and an id past a long, is empty.
     */
    public static Optional<Long> parseCollectionId(String deparsedPredicate) {
        if (deparsedPredicate == null) {
            return Optional.empty();
        }
        Matcher m = COLLECTION_ID_PREDICATE.matcher(deparsedPredicate);
        if (!m.matches()) {
            return Optional.empty();
        }
        try {
            long id = Long.parseLong(m.group(1));
            return id > 0 ? Optional.of(id) : Optional.empty();
        } catch (NumberFormatException e) {
            return Optional.empty();
        }
    }

    /** The value of a single-value LIST partition bound ({@code FOR VALUES IN ('x')}); empty for any other bound. */
    static Optional<String> parseBoundValue(String deparsedBound) {
        return unquote(LIST_BOUND, deparsedBound);
    }

    /**
     * The one decision of the unparsed rule: the collection an index serves, or empty when the index is unparsed.
     * An index is parsed only when its name has the builder's shape, its access method is hnsw, its predicate parses,
     * both of its leaf's bounds parsed, and {@code standard_conforming_strings} was {@code on} for the read (otherwise
     * the deparsed text of a backslash is ambiguous).
     */
    static Optional<String> attributedCollection(String indexName, String accessMethod, String deparsedPredicate,
                                                 boolean leafBoundsParsed, String standardConformingStrings) {
        if (!"on".equals(standardConformingStrings) || !leafBoundsParsed || !ACCESS_METHOD.equals(accessMethod)
            || !isBuilderName(indexName)) {
            return Optional.empty();
        }
        return parseCollection(deparsedPredicate);
    }

    /**
     * {@link #attributedCollection} for the id predicate (RDR-229 P3.4): the collection id an index serves, or empty. The same
     * conditions hold: builder name shape, hnsw, a parsing predicate, both leaf bounds parsed, {@code standard_conforming_strings} on.
     */
    static Optional<Long> attributedCollectionId(String indexName, String accessMethod, String deparsedPredicate,
                                                 boolean leafBoundsParsed, String standardConformingStrings) {
        if (!"on".equals(standardConformingStrings) || !leafBoundsParsed || !ACCESS_METHOD.equals(accessMethod)
            || !isBuilderName(indexName)) {
            return Optional.empty();
        }
        return parseCollectionId(deparsedPredicate);
    }

    /**
     * The one decision of the leftover carve-out (nexus-rn1yd): the collection a REINDEX CONCURRENTLY leftover was
     * made from, or empty when the index is not one. It holds only for an INVALID index whose name is
     * {@code ^pci_[0-9a-f]{24}_cc(new|old)[0-9]*$} and that passes the same other conditions as
     * {@link #attributedCollection}: hnsw, a parsing predicate, both leaf bounds parsed and
     * {@code standard_conforming_strings} {@code on}. A valid index is never a leftover.
     */
    static Optional<String> leftoverCollection(String indexName, String accessMethod, String deparsedPredicate,
                                               boolean leafBoundsParsed, String standardConformingStrings,
                                               boolean valid) {
        if (valid || indexName == null || !isLeftoverName(indexName)
            || !"on".equals(standardConformingStrings) || !leafBoundsParsed || !ACCESS_METHOD.equals(accessMethod)) {
            return Optional.empty();
        }
        return parseCollection(deparsedPredicate);
    }

    /**
     * {@link #attributedCollectionId} for an index of the leaf ({@code model}, {@code tenant}): the id its predicate reads, provided the index's name
     * is {@link #indexNameById} of that (model, tenant, id). An operator's {@code pci_<hash>} index that happens to read an id predicate is not the
     * builder's, so it stays unparsed and is never planned for a drop. {@code indexName} may be a REINDEX CONCURRENTLY leftover's, whose base name
     * (before {@code _ccnew}/{@code _ccold} and the counter) is checked instead.
     */
    static Optional<Long> idOfBuilderIndex(String model, String tenant, String indexName, String accessMethod, String deparsedPredicate,
                                           String standardConformingStrings, boolean leftover, boolean valid) {
        if (model == null || tenant == null) {
            return Optional.empty();
        }
        Optional<Long> id = leftover
            ? leftoverCollectionId(indexName, accessMethod, deparsedPredicate, true, standardConformingStrings, valid)
            : attributedCollectionId(indexName, accessMethod, deparsedPredicate, true, standardConformingStrings);
        if (id.isEmpty()) {
            return Optional.empty();
        }
        String base = leftover ? indexName.substring(0, PREFIX.length() + HASH_HEX_DIGITS) : indexName;
        try {
            return base.equals(indexNameById(model, tenant, id.get())) ? id : Optional.empty();
        } catch (IllegalArgumentException e) {   // a NUL in the model or tenant
            return Optional.empty();
        }
    }

    /**
     * {@link #leftoverCollection} for the id predicate (RDR-229 P3.4, N2): the collection id a REINDEX CONCURRENTLY leftover of an id index was
     * made from, or empty. The same conditions: INVALID, a builder name plus {@code _ccnew}/{@code _ccold} and a counter, hnsw, a parsing
     * predicate, both leaf bounds parsed and {@code standard_conforming_strings} on.
     */
    static Optional<Long> leftoverCollectionId(String indexName, String accessMethod, String deparsedPredicate,
                                               boolean leafBoundsParsed, String standardConformingStrings, boolean valid) {
        if (valid || indexName == null || !isLeftoverName(indexName)
            || !"on".equals(standardConformingStrings) || !leafBoundsParsed || !ACCESS_METHOD.equals(accessMethod)) {
            return Optional.empty();
        }
        return parseCollectionId(deparsedPredicate);
    }

    private static Optional<String> unquote(Pattern shape, String text) {
        if (text == null) {
            return Optional.empty();
        }
        Matcher m = shape.matcher(text);
        return m.matches() ? Optional.of(m.group(1).replace("''", "'")) : Optional.empty();
    }

    /**
     * Read the catalog on a connection borrowed from the pool and returned before this method does, bounded as the
     * class comment says.
     *
     * @throws DataAccessException when the statement fails, hits {@link #readBound()} (SQLSTATE 57014) or the server
     *                             stays silent past the bound plus the network margin
     */
    public Snapshot read() {
        try (Connection conn = dataSource.getConnection()) {
            conn.setAutoCommit(false);
            try {
                DSLContext ctx = DSL.using(conn, SQLDialect.POSTGRES);
                SweepBounds.applyStatementTimeout(ctx, readBound);
                Snapshot snapshot = read(ctx);
                conn.commit();
                return snapshot;
            } catch (RuntimeException e) {
                rollbackQuietly(conn, e);
                throw e;
            }
        } catch (SQLException e) {
            throw new DataAccessException("PCI catalog read failed: " + e.getMessage(), e);
        }
    }

    private static void rollbackQuietly(Connection conn, RuntimeException cause) {
        try {
            conn.rollback();
        } catch (SQLException e) {
            cause.addSuppressed(e);
        }
    }

    /** Read the catalog through {@code ctx}: one statement, so one snapshot. */
    Snapshot read(DSLContext ctx) {
        Table<?> leaf = catalog("pg_class", "l");
        Table<?> leafSchema = catalog("pg_namespace", "ln");
        Table<?> toModel = catalog("pg_inherits", "i1");
        Table<?> model = catalog("pg_class", "m");
        Table<?> toParent = catalog("pg_inherits", "i2");
        Table<?> parent = catalog("pg_class", "p");
        Table<?> parentSchema = catalog("pg_namespace", "pn");

        Field<Object> leafOid = col("l", "oid");
        Field<Object> modelOid = col("m", "oid");
        Field<String> leafSchemaName = col("ln", "nspname", String.class);
        Field<String> leafName = col("l", "relname", String.class);
        Field<String> leafBound = bound("l");
        Field<String> modelBound = bound("m");

        // The pci_ indexes and the leaf's own HNSW index candidates, as a derived table so that a leaf with none still
        // comes back (LEFT JOIN below). A candidate is an unpredicated hnsw index that is attached to the parent's
        // partitioned HNSW index (the leaf's own index, see LeafIndex) or is invalid (a leftover is unattached, and is
        // matched to a leaf's own index by name in Java; an invalid unpredicated hnsw index is rare).
        Field<Object> indexTable = col("i", "indrelid");
        Field<String> indexName = col("ix", "relname", String.class);
        Field<Boolean> indexValid = col("i", "indisvalid", Boolean.class);
        Field<String> indexMethod = col("am", "amname", String.class);
        Field<String> indexPredicate = DSL.function(DSL.name("pg_catalog", "pg_get_expr"), SQLDataType.CLOB,
            col("i", "indpred"), indexTable);
        // The anchor (RDR-230) is the index's comment: obj_description reads pg_description and locks nothing.
        Field<String> indexComment = DSL.function(DSL.name("pg_catalog", "obj_description"), SQLDataType.CLOB,
            col("ix", "oid"), DSL.inline("pg_class"));
        Field<Boolean> noPredicate = DSL.field(col("i", "indpred").isNull());
        Field<String> indexKey = DSL.function(DSL.name("pg_catalog", "pg_get_indexdef"), SQLDataType.CLOB,
            col("ix", "oid"), DSL.inline(1), DSL.inline(false));
        Condition attached = attachedToChunksIndex();
        Table<?> pci = ctx.select(
                indexTable.as("indrelid"),
                indexName.as("index_name"),
                indexValid.as("index_valid"),
                indexPredicate.as("index_predicate"),
                indexMethod.as("index_method"),
                indexComment.as("index_comment"),
                noPredicate.as("index_unpredicated"),
                DSL.field(attached).as("index_attached"),
                indexKey.as("index_key"))
            .from(catalog("pg_index", "i"))
            .join(catalog("pg_class", "ix")).on(col("ix", "oid").eq(col("i", "indexrelid")))
            .join(catalog("pg_am", "am")).on(col("am", "oid").eq(col("ix", "relam")))
            .where(DSL.left(indexName, PREFIX.length()).eq(PREFIX)
                .or(indexMethod.eq(ACCESS_METHOD).and(col("i", "indpred").isNull())
                    .and(attached.or(indexValid.isFalse()))))
            .asTable("x");
        Field<Object> pciTable = pci.field("indrelid", Object.class);
        Field<String> pciName = pci.field("index_name", String.class);
        Field<Boolean> pciValid = pci.field("index_valid", Boolean.class);
        Field<String> pciPredicate = pci.field("index_predicate", String.class);
        Field<String> pciMethod = pci.field("index_method", String.class);
        Field<String> pciComment = pci.field("index_comment", String.class);
        Field<Boolean> pciUnpredicated = pci.field("index_unpredicated", Boolean.class);
        Field<Boolean> pciAttached = pci.field("index_attached", Boolean.class);
        Field<String> pciKey = pci.field("index_key", String.class);
        // Read in the statement that deparses the predicates, so the two are of one instant.
        Field<String> stringsSetting = DSL.function("current_setting", SQLDataType.VARCHAR,
            DSL.inline("standard_conforming_strings")).as("scs");

        // Every chunks parent the typed accessors name (all three dimensions share one table today).
        Set<List<String>> parents = new LinkedHashSet<>();
        for (DimTables.ChunkTable chunks : DimTables.CHUNKS.values()) {
            parents.add(List.of(chunks.table().getSchema().getName(), chunks.table().getName()));
        }
        List<Condition> any = new ArrayList<>();
        for (List<String> qualified : parents) {
            any.add(col("pn", "nspname", String.class).eq(qualified.get(0))
                .and(col("p", "relname", String.class).eq(qualified.get(1))));
        }
        Condition onParents = DSL.or(any);

        Result<? extends Record> rows = ctx
            .select(leafSchemaName, leafName, leafBound, modelBound, pciName, pciValid, pciPredicate, pciMethod,
                stringsSetting, pciComment, pciUnpredicated, pciAttached, pciKey)
            .from(leaf)
            .join(leafSchema).on(col("ln", "oid").eq(col("l", "relnamespace")))
            .join(toModel).on(col("i1", "inhrelid").eq(leafOid))
            .join(model).on(modelOid.eq(col("i1", "inhparent")))
            .join(toParent).on(col("i2", "inhrelid").eq(modelOid))
            .join(parent).on(col("p", "oid").eq(col("i2", "inhparent")))
            .join(parentSchema).on(col("pn", "oid").eq(col("p", "relnamespace")))
            .leftJoin(pci).on(pciTable.eq(leafOid))
            .where(onParents)
            .orderBy(leafSchemaName, leafName, pciName)
            .fetch();

        Map<List<String>, LeafBuilder> leaves = new LinkedHashMap<>();
        for (var row : rows) {
            LeafBuilder b = leaves.computeIfAbsent(List.of(row.get(leafSchemaName), row.get(leafName)),
                k -> new LeafBuilder(row.get(leafSchemaName), row.get(leafName),
                    parseBoundValue(row.get(modelBound)).orElse(null),
                    parseBoundValue(row.get(leafBound)).orElse(null)));
            if (row.get(pciName) != null) {
                b.add(row.get(pciName), row.get(pciValid), row.get(pciPredicate), row.get(pciMethod),
                    row.get(stringsSetting), row.get(pciComment), Boolean.TRUE.equals(row.get(pciUnpredicated)),
                    Boolean.TRUE.equals(row.get(pciAttached)), row.get(pciKey));
            }
        }
        List<Leaf> out = new ArrayList<>(leaves.size());
        for (LeafBuilder b : leaves.values()) {
            out.add(b.build());
        }
        return new Snapshot(out);
    }

    private static final class LeafBuilder {
        private final String schema;
        private final String name;
        private final String model;
        private final String tenant;
        private final List<Index> indexes = new ArrayList<>();
        /** Unpredicated hnsw candidates that are not pci_ indexes, sorted out in {@link #build()}. */
        private final List<Candidate> candidates = new ArrayList<>();

        private record Candidate(String name, boolean valid, boolean attached, String key) { }

        LeafBuilder(String schema, String name, String model, String tenant) {
            this.schema = schema;
            this.name = name;
            this.model = model;
            this.tenant = tenant;
        }

        void add(String indexName, Boolean valid, String predicate, String method, String stringsSetting,
                 String comment, boolean unpredicated, boolean attached, String key) {
            boolean isValid = Boolean.TRUE.equals(valid);
            if (!indexName.startsWith(PREFIX)) {
                // Not a pci_ index: only an unpredicated hnsw index is of interest, as the leaf's own or its leftover.
                if (unpredicated && ACCESS_METHOD.equals(method)) {
                    candidates.add(new Candidate(indexName, isValid, attached, key));
                }
                return;
            }
            // An index cannot be attributed to a (model, tenant) when either bound did not parse.
            String collection = attributedCollection(indexName, method, predicate,
                model != null && tenant != null, stringsSetting).orElse(null);
            Long collectionId = collection != null ? null
                : idOfBuilderIndex(model, tenant, indexName, method, predicate, stringsSetting, false, isValid).orElse(null);
            String leftoverOf = collection != null || collectionId != null ? null
                : leftoverCollection(indexName, method, predicate, model != null && tenant != null, stringsSetting,
                    isValid).orElse(null);
            Long leftoverOfId = collection != null || collectionId != null || leftoverOf != null ? null
                : idOfBuilderIndex(model, tenant, indexName, method, predicate, stringsSetting, true, isValid).orElse(null);
            indexes.add(new Index(indexName, isValid, collection, leftoverOf, comment, null, collectionId, leftoverOfId));
        }

        Leaf build() {
            List<LeafIndex> own = new ArrayList<>();
            List<Index> leftovers = new ArrayList<>();
            if (model != null && tenant != null) {
                // The leaf's own indexes first: attached and unpredicated hnsw (the read's condition). A leftover is
                // never attached, so an attached candidate is never read as one.
                for (Candidate c : candidates) {
                    if (c.attached()) {
                        own.add(new LeafIndex(c.name(), c.valid(), dimensionOf(c.key())));
                    }
                }
                for (Candidate c : candidates) {
                    if (c.attached() || c.valid()) {
                        continue;
                    }
                    own.stream().map(LeafIndex::name).filter(base -> isLeafLeftoverName(base, c.name())).findFirst()
                        .ifPresent(base -> leftovers.add(new Index(c.name(), false, null, null, null, base)));
                }
            }
            return new Leaf(schema, name, model, tenant, List.copyOf(indexes), List.copyOf(own),
                List.copyOf(leftovers));
        }
    }

    private static final Pattern EMBEDDING_COLUMN = Pattern.compile("^embedding_([0-9]{1,6})$");

    /** The {@code n} of {@code embedding_<n>}, or 0 for any other key column text (or none). */
    private static int dimensionOf(String keyColumn) {
        if (keyColumn == null) {
            return 0;
        }
        Matcher m = EMBEDDING_COLUMN.matcher(keyColumn);
        return m.matches() ? Integer.parseInt(m.group(1)) : 0;
    }

    /**
     * An index of {@code nexus.chunks}'s parents' own indexes, as a condition on the derived table's {@code i} and
     * {@code ix} aliases: the index's parent in {@code pg_inherits} is itself a child of an index on a chunks parent.
     * For this layout that is leaf index, then model partition index, then the parent's index; PostgreSQL attaches an
     * index only under an index of its own table's parent, so no further check of the middle is needed.
     */
    private static Condition attachedToChunksIndex() {
        Set<List<String>> parents = new LinkedHashSet<>();
        for (DimTables.ChunkTable chunks : DimTables.CHUNKS.values()) {
            parents.add(List.of(chunks.table().getSchema().getName(), chunks.table().getName()));
        }
        List<Condition> any = new ArrayList<>();
        for (List<String> qualified : parents) {
            any.add(col("atn", "nspname", String.class).eq(qualified.get(0))
                .and(col("att", "relname", String.class).eq(qualified.get(1))));
        }
        return DSL.exists(DSL.select(DSL.inline(1))
            .from(catalog("pg_inherits", "ah1"))
            .join(catalog("pg_inherits", "ah2")).on(col("ah2", "inhrelid").eq(col("ah1", "inhparent")))
            .join(catalog("pg_index", "at")).on(col("at", "indexrelid").eq(col("ah2", "inhparent")))
            .join(catalog("pg_class", "att")).on(col("att", "oid").eq(col("at", "indrelid")))
            .join(catalog("pg_namespace", "atn")).on(col("atn", "oid").eq(col("att", "relnamespace")))
            .where(col("ah1", "inhrelid").eq(col("i", "indexrelid")))
            .and(DSL.or(any)));
    }

    private static Table<?> catalog(String relation, String alias) {
        return DSL.table(DSL.name("pg_catalog", relation)).as(alias);
    }

    private static Field<Object> col(String alias, String column) {
        return col(alias, column, Object.class);
    }

    private static <T> Field<T> col(String alias, String column, Class<T> type) {
        return DSL.field(DSL.name(alias, column), type);
    }

    /** {@code pg_get_expr(<alias>.relpartbound, <alias>.oid)}: the bound as text, {@code FOR VALUES IN ('x')}. */
    private static Field<String> bound(String alias) {
        return DSL.function(DSL.name("pg_catalog", "pg_get_expr"), SQLDataType.CLOB,
            col(alias, "relpartbound"), col(alias, "oid"));
    }
}
