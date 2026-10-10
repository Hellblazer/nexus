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

import javax.sql.DataSource;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.sql.Connection;
import java.sql.SQLException;
import java.time.Duration;
import java.time.Instant;
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
 * {@code pci_} is <i>unparsed</i>: an operator-made index ({@code pci_foo}), a REINDEX CONCURRENTLY leftover
 * ({@code pci_<hash>_ccnew}), a non-hnsw index, a hand-edited predicate, or a PostgreSQL that deparses differently.
 * An unparsed index is listed with a {@code null} collection and counted by {@link Snapshot#unparsedCount()} (the
 * status object reports it), whatever its validity. It is never routed to ({@link Snapshot#hasValidIndex} cannot
 * return true for it), never built, and never dropped: no DDL consumer may act on an index that is not
 * {@link Index#parsed()}. This NARROWS the RDR Schema sentence saying operator-made {@code pci_} indexes follow the
 * build and retire rules; the RDR's Schema paragraph carries the narrowed rule (nexus-43ulx.23).
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

    /** The shape of every name {@link #indexName} makes; any other {@code pci_} name is not the builder's. */
    private static final Pattern BUILDER_NAME = Pattern.compile("^pci_[0-9a-f]{24}$");

    /** The only access method the router and the builder deal in. */
    private static final String ACCESS_METHOD = "hnsw";

    /** {@code FOR VALUES IN ('<value>')}, a single-value LIST bound. */
    private static final Pattern LIST_BOUND =
        Pattern.compile("^FOR VALUES IN \\('((?:[^']++|'')*+)'\\)$", Pattern.DOTALL);

    /**
     * One {@code pci_} index on a leaf.
     *
     * @param name       the index name
     * @param valid      {@code pg_index.indisvalid}; false while a concurrent build runs and after one fails
     * @param collection the collection its predicate selects, or {@code null} when the index is unparsed
     */
    public record Index(String name, boolean valid, String collection) {
        /** False for an unparsed index, which no consumer may route to, build or drop. */
        public boolean parsed() {
            return collection != null;
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
     */
    public record Leaf(String schema, String name, String model, String tenant, List<Index> indexes) { }

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
        private final int validCount;
        private final int invalidCount;
        private final int unparsedCount;

        Snapshot(List<Leaf> leaves) {
            this.leaves = List.copyOf(leaves);
            Map<Key, String> validKeys = new LinkedHashMap<>();
            int validIndexes = 0;
            int invalidIndexes = 0;
            int unparsedIndexes = 0;
            for (Leaf leaf : this.leaves) {
                for (Index index : leaf.indexes()) {
                    if (!index.parsed()) {
                        unparsedIndexes++;
                    } else if (index.valid()) {
                        validIndexes++;
                        validKeys.putIfAbsent(new Key(leaf.model(), leaf.tenant(), index.collection()), index.name());
                    } else {
                        invalidIndexes++;
                    }
                }
            }
            this.valid = Collections.unmodifiableMap(validKeys);
            this.validCount = validIndexes;
            this.invalidCount = invalidIndexes;
            this.unparsedCount = unparsedIndexes;
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

        /** {@code pci_} indexes that failed any part of the parsed rule, whatever their validity. */
        public int unparsedCount() {
            return unparsedCount;
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

    /**
     * Whether {@code name} has the shape {@link #indexName} makes ({@code ^pci_[0-9a-f]{24}$}). The builder checks
     * it before it renders DDL for a name, with the same pattern the reader uses to decide an index is parsed.
     */
    static boolean isBuilderName(String name) {
        return name != null && BUILDER_NAME.matcher(name).matches();
    }

    /**
     * The collection an index selects, from its deparsed predicate ({@code pg_get_expr(indpred, indrelid)}), which
     * for the builder's indexes reads {@code (collection = '<name>'::text)}. A quote inside the name is doubled in
     * that text and undoubled here. Anything else, including a null predicate, is empty (unparsed).
     */
    public static Optional<String> parseCollection(String deparsedPredicate) {
        return unquote(COLLECTION_PREDICATE, deparsedPredicate);
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

        // The pci_ indexes, as a derived table so that a leaf with none still comes back (LEFT JOIN below).
        Field<Object> indexTable = col("i", "indrelid");
        Field<String> indexName = col("ix", "relname", String.class);
        Field<Boolean> indexValid = col("i", "indisvalid", Boolean.class);
        Field<String> indexMethod = col("am", "amname", String.class);
        Field<String> indexPredicate = DSL.function(DSL.name("pg_catalog", "pg_get_expr"), SQLDataType.CLOB,
            col("i", "indpred"), indexTable);
        Table<?> pci = ctx.select(
                indexTable.as("indrelid"),
                indexName.as("index_name"),
                indexValid.as("index_valid"),
                indexPredicate.as("index_predicate"),
                indexMethod.as("index_method"))
            .from(catalog("pg_index", "i"))
            .join(catalog("pg_class", "ix")).on(col("ix", "oid").eq(col("i", "indexrelid")))
            .join(catalog("pg_am", "am")).on(col("am", "oid").eq(col("ix", "relam")))
            .where(DSL.left(indexName, PREFIX.length()).eq(PREFIX))
            .asTable("x");
        Field<Object> pciTable = pci.field("indrelid", Object.class);
        Field<String> pciName = pci.field("index_name", String.class);
        Field<Boolean> pciValid = pci.field("index_valid", Boolean.class);
        Field<String> pciPredicate = pci.field("index_predicate", String.class);
        Field<String> pciMethod = pci.field("index_method", String.class);
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
                stringsSetting)
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
                    row.get(stringsSetting));
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

        LeafBuilder(String schema, String name, String model, String tenant) {
            this.schema = schema;
            this.name = name;
            this.model = model;
            this.tenant = tenant;
        }

        void add(String indexName, Boolean valid, String predicate, String method, String stringsSetting) {
            // An index cannot be attributed to a (model, tenant) when either bound did not parse.
            String collection = attributedCollection(indexName, method, predicate,
                model != null && tenant != null, stringsSetting).orElse(null);
            indexes.add(new Index(indexName, Boolean.TRUE.equals(valid), collection));
        }

        Leaf build() {
            return new Leaf(schema, name, model, tenant, List.copyOf(indexes));
        }
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
