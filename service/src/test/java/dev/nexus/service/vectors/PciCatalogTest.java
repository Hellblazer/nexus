// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

/** RDR-227 Step 2 (nexus-43ulx.11): the pure half of {@link PciCatalog}. No database. */
class PciCatalogTest {

    @Test
    void indexName_is28Characters_andStableForFixedInputs() {
        String name = PciCatalog.indexName("voyage-code-3", "t1", "code__x__v1");

        assertThat(name).hasSize(28).startsWith("pci_").matches("pci_[0-9a-f]{24}");
        // sha256("voyage-code-3" NUL "t1" NUL "code__x__v1"), first 24 hex digits, computed outside this code.
        assertThat(name).isEqualTo("pci_7c53d438cd40ad5fa827b64e");
        assertThat(PciCatalog.indexName("voyage-code-3", "t1", "code__x__v1")).isEqualTo(name);
    }

    @Test
    void indexName_changesWithEachInput_andTheSeparatorKeepsFieldsApart() {
        String base = PciCatalog.indexName("m", "t", "c");

        assertThat(PciCatalog.indexName("m2", "t", "c")).isNotEqualTo(base);
        assertThat(PciCatalog.indexName("m", "t2", "c")).isNotEqualTo(base);
        assertThat(PciCatalog.indexName("m", "t", "c2")).isNotEqualTo(base);
        // Without a separator these two would hash the same bytes.
        assertThat(PciCatalog.indexName("ab", "c", "d")).isNotEqualTo(PciCatalog.indexName("a", "bc", "d"));
        assertThat(PciCatalog.indexName("a", "bc", "d")).isNotEqualTo(PciCatalog.indexName("a", "b", "cd"));
    }

    @Test
    void indexName_refusesNulAndNull() {
        assertThatThrownBy(() -> PciCatalog.indexName("m\0x", "t", "c")).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> PciCatalog.indexName("m", "t\0x", "c")).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> PciCatalog.indexName("m", "t", "c\0x")).isInstanceOf(IllegalArgumentException.class);
        assertThatThrownBy(() -> PciCatalog.indexName(null, "t", "c")).isInstanceOf(NullPointerException.class);
    }

    @Test
    void parseCollection_readsThePredicateAndUndoublesQuotes() {
        assertThat(PciCatalog.parseCollection("(collection = 'code__1-1__v1'::text)")).contains("code__1-1__v1");
        assertThat(PciCatalog.parseCollection("(collection = 'it''s'::text)")).contains("it's");
        assertThat(PciCatalog.parseCollection("(collection = '''a'''''::text)")).contains("'a''");
        assertThat(PciCatalog.parseCollection("(collection = ''::text)")).contains("");
        assertThat(PciCatalog.parseCollection("(collection = 'a\\b'::text)")).contains("a\\b");
    }

    @Test
    void parseCollection_everythingElseIsUnparsed() {
        assertThat(PciCatalog.parseCollection(null)).isEmpty();
        assertThat(PciCatalog.parseCollection("")).isEmpty();
        assertThat(PciCatalog.parseCollection("((collection = 'a'::text) AND (tenant_id = 't'::text))")).isEmpty();
        assertThat(PciCatalog.parseCollection("((collection = 'a'::text) OR (collection = 'b'::text))")).isEmpty();
        assertThat(PciCatalog.parseCollection("(collection <> 'a'::text)")).isEmpty();
        assertThat(PciCatalog.parseCollection("(tenant_id = 'a'::text)")).isEmpty();
        assertThat(PciCatalog.parseCollection("(collection = ANY ('{a,b}'::text[]))")).isEmpty();
        assertThat(PciCatalog.parseCollection("(collection = 'a'::text) ")).isEmpty();
        assertThat(PciCatalog.parseCollection("collection = 'a'::text")).isEmpty();
        // An unbalanced quote: the run 'a'b' is not a quoted literal.
        assertThat(PciCatalog.parseCollection("(collection = 'a'b'::text)")).isEmpty();
        assertThat(PciCatalog.parseCollection("(collection = 'a'::varchar)")).isEmpty();
    }

    @Test
    void parseBoundValue_readsASingleValueListBound() {
        assertThat(PciCatalog.parseBoundValue("FOR VALUES IN ('minilm-l6-v2-384')")).contains("minilm-l6-v2-384");
        assertThat(PciCatalog.parseBoundValue("FOR VALUES IN ('o''brien')")).contains("o'brien");
        assertThat(PciCatalog.parseBoundValue("DEFAULT")).isEmpty();
        assertThat(PciCatalog.parseBoundValue("FOR VALUES IN ('a', 'b')")).isEmpty();
        assertThat(PciCatalog.parseBoundValue("FOR VALUES FROM (1) TO (2)")).isEmpty();
        assertThat(PciCatalog.parseBoundValue(null)).isEmpty();
    }

    private static final String GOOD_NAME = "pci_0123456789abcdef01234567";
    private static final String GOOD_PREDICATE = "(collection = 'c'::text)";

    @Test
    void attributedCollection_parsesOnlyWhenEveryConditionHolds() {
        assertThat(PciCatalog.attributedCollection(GOOD_NAME, "hnsw", GOOD_PREDICATE, true, "on")).contains("c");
        // Each condition alone breaks it.
        assertThat(PciCatalog.attributedCollection("pci_foo", "hnsw", GOOD_PREDICATE, true, "on")).isEmpty();
        assertThat(PciCatalog.attributedCollection(GOOD_NAME + "_ccnew", "hnsw", GOOD_PREDICATE, true, "on")).isEmpty();
        assertThat(PciCatalog.attributedCollection("pci_0123456789ABCDEF01234567", "hnsw", GOOD_PREDICATE, true, "on"))
            .as("upper case hex is not the builder's name").isEmpty();
        assertThat(PciCatalog.attributedCollection("pci_0123456789abcdef0123456", "hnsw", GOOD_PREDICATE, true, "on"))
            .as("23 digits").isEmpty();
        assertThat(PciCatalog.attributedCollection(null, "hnsw", GOOD_PREDICATE, true, "on")).isEmpty();
        assertThat(PciCatalog.attributedCollection(GOOD_NAME, "btree", GOOD_PREDICATE, true, "on")).isEmpty();
        assertThat(PciCatalog.attributedCollection(GOOD_NAME, "ivfflat", GOOD_PREDICATE, true, "on")).isEmpty();
        assertThat(PciCatalog.attributedCollection(GOOD_NAME, null, GOOD_PREDICATE, true, "on")).isEmpty();
        assertThat(PciCatalog.attributedCollection(GOOD_NAME, "hnsw", "(tenant_id = 'c'::text)", true, "on")).isEmpty();
        assertThat(PciCatalog.attributedCollection(GOOD_NAME, "hnsw", null, true, "on")).isEmpty();
        assertThat(PciCatalog.attributedCollection(GOOD_NAME, "hnsw", GOOD_PREDICATE, false, "on")).isEmpty();
    }

    @Test
    void attributedCollection_standardConformingStringsNotOn_isUnparsed() {
        assertThat(PciCatalog.attributedCollection(GOOD_NAME, "hnsw", GOOD_PREDICATE, true, "off")).isEmpty();
        assertThat(PciCatalog.attributedCollection(GOOD_NAME, "hnsw", GOOD_PREDICATE, true, "")).isEmpty();
        assertThat(PciCatalog.attributedCollection(GOOD_NAME, "hnsw", GOOD_PREDICATE, true, null)).isEmpty();
    }

    @Test
    void snapshot_ofNoLeaves_isEmptyAndCountsNothing() {
        PciCatalog.Snapshot snap = new PciCatalog.Snapshot(java.util.List.of());

        assertThat(snap.leaves()).isEmpty();
        assertThat(snap.validCount()).isZero();
        assertThat(snap.unparsedCount()).isZero();
        assertThat(snap.hasValidIndex("m", "t", "c")).isFalse();
    }
}
