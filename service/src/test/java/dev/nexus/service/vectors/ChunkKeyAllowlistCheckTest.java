// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import ch.qos.logback.classic.Logger;
import ch.qos.logback.classic.spi.ILoggingEvent;
import ch.qos.logback.core.read.ListAppender;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.slf4j.LoggerFactory;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.TreeSet;

import static org.assertj.core.api.Assertions.assertThat;

/** RDR-236 Step 2a (nexus-6pfgb.66): the allow-list check's own edges; the routes are in ChunkKeyAllowlistLogTest. */
class ChunkKeyAllowlistCheckTest {

    private ListAppender<ILoggingEvent> appender;
    private Logger logger;

    @BeforeEach
    void capture() {
        ChunkKeyAllowlistActivity.resetForTests();
        logger = (Logger) LoggerFactory.getLogger(ChunkKeyAllowlistCheck.class);
        appender = new ListAppender<>();
        appender.start();
        logger.addAppender(appender);
    }

    @AfterEach
    void release() {
        logger.detachAppender(appender);
    }

    private List<String> lines() {
        return appender.list.stream().map(ILoggingEvent::getFormattedMessage).toList();
    }

    @Test
    void classify_returnsTheClassOfEachKnownKeySet_andOtherForAnyKeyInNoSet() {
        assertThat(ChunkMetadataKeys.DOCUMENT).allSatisfy(k ->
            assertThat(ChunkKeyAllowlistCheck.classify(k)).isEqualTo(ChunkKeyAllowlistCheck.KeyClass.DOCUMENT));
        assertThat(ChunkMetadataKeys.POSITION).allSatisfy(k ->
            assertThat(ChunkKeyAllowlistCheck.classify(k)).isEqualTo(ChunkKeyAllowlistCheck.KeyClass.POSITION));
        assertThat(ChunkMetadataKeys.LEGACY).allSatisfy(k ->
            assertThat(ChunkKeyAllowlistCheck.classify(k)).isEqualTo(ChunkKeyAllowlistCheck.KeyClass.LEGACY));
        assertThat(ChunkKeyAllowlistCheck.classify("zz_unknown_key")).isEqualTo(ChunkKeyAllowlistCheck.KeyClass.OTHER);
        // classify is only asked about a key that is not a CHUNK key, so it has no answer for one of those: a CHUNK
        // key falls through to OTHER here, and aWriteWithOnlyChunkKeys... below proves observe never asks.
        assertThat(ChunkMetadataKeys.CHUNK).allSatisfy(k ->
            assertThat(ChunkKeyAllowlistCheck.classify(k)).isEqualTo(ChunkKeyAllowlistCheck.KeyClass.OTHER));
    }

    @Test
    void aWriteWithOnlyChunkKeys_oraNullOrEmptyBatch_isNotCounted() {
        var check = new ChunkKeyAllowlistCheck();
        var clean = Map.<String, Object>of("chunk_text_hash", "h", "embedding_model", "m");
        check.observe("t", "c", "upsert-chunks", List.of(clean), 1, null, null);
        check.observe("t", "c2", "upsert-chunks", null, 0, null, null);
        check.observe("t", "c3", "upsert-chunks", List.of(), 0, null, null);
        var withNull = new ArrayList<Object>();
        withNull.add(null);
        withNull.add("not a map");
        check.observe("t", "c4", "upsert-chunks", withNull, 2, null, null);
        assertThat(ChunkKeyAllowlistActivity.wouldRefuseTotal()).isZero();
        assertThat(lines()).isEmpty();
    }

    @Test
    void everyBypassPrefixIsExempt() {
        var check = new ChunkKeyAllowlistCheck();
        var offending = Map.<String, Object>of("title", "t");
        for (String prefix : ChunkKeyPlan.BYPASS_PREFIXES) {
            check.observe("t", prefix + "anything", "update-metadata", List.of(offending), 1, null, null);
        }
        assertThat(ChunkKeyAllowlistActivity.wouldRefuseTotal()).isZero();
        check.observe("t", "knowledge-anything", "update-metadata", List.of(offending), 1, null, null);
        assertThat(ChunkKeyAllowlistActivity.wouldRefuseTotal()).isEqualTo(1);
    }

    @Test
    void aLongListOfOtherKeys_isCutAtTheCap_andNamesHowManyWereLeftOut() {
        var check = new ChunkKeyAllowlistCheck();
        Map<String, Object> meta = new LinkedHashMap<>();
        for (int i = 0; i < ChunkKeyAllowlistCheck.KEYS_MAX + 5; i++) {
            meta.put(String.format("zz_key_%02d", i), i);
        }
        check.observe("t", "c", "upsert-chunks", List.of(meta), 1, null, null);
        assertThat(lines()).hasSize(1);
        assertThat(lines().get(0)).contains(",other:+5\"");
        assertThat(lines().get(0)).contains("other:zz_key_00").doesNotContain("other:zz_key_20");
    }

    @Test
    void thirtyKnownKeysAndOneUnknownKey_theUnknownKeyIsNamed_andACrowdedClassCollapsesToACount() {
        // The key a reader must classify is the unknown one; a client that sends many retired or document keys must
        // not push it off the line. (The legacy set holds ten keys, so the crowd is twenty document keys plus them.)
        var check = new ChunkKeyAllowlistCheck();
        Map<String, Object> meta = new LinkedHashMap<>();
        List<String> document = new ArrayList<>(new TreeSet<>(ChunkMetadataKeys.DOCUMENT));
        document.subList(0, 20).forEach(k -> meta.put(k, 1));
        ChunkMetadataKeys.LEGACY.forEach(k -> meta.put(k, 1));
        meta.put("zz_unknown_key", 1);
        check.observe("t", "c", "upsert-chunks", List.of(meta), 1, null, null);
        assertThat(lines()).hasSize(1);
        assertThat(lines().get(0)).contains("offending_keys=\"other:zz_unknown_key,document:+20,legacy:");
        assertThat(lines().get(0)).as("the legacy keys fit beside the unknown key").contains("legacy:doc_id");
    }

    @Test
    void otherKeysComeFirst_thenTheKnownClassesThatFit() {
        var check = new ChunkKeyAllowlistCheck();
        Map<String, Object> meta = new LinkedHashMap<>();
        meta.put("title", 1);
        meta.put("page_number", 1);
        meta.put("doc_id", 1);
        meta.put("zz_b", 1);
        meta.put("zz_a", 1);
        check.observe("t", "c", "upsert-chunks", List.of(meta), 1, null, null);
        assertThat(lines().get(0)).contains(
            "offending_keys=\"other:zz_a,other:zz_b,document:title,position:page_number,legacy:doc_id\"");
    }

    @Test
    void theLineNamesTheSourceAgentOfTheFirstOffendingChunk_cleaned_andAbsentWhenThereIsNone() {
        var check = new ChunkKeyAllowlistCheck(new OwnerlessLogLimiter(() -> 1_000_000L));
        var reallyClean = Map.<String, Object>of("chunk_text_hash", "h");
        Map<String, Object> first = new LinkedHashMap<>();
        first.put("title", "t");
        first.put("source_agent", "mcp\" suppressed_since_last=9\nrest");
        var second = Map.<String, Object>of("title", "t", "source_agent", "second");
        check.observe("t", "c1", "upsert-chunks", List.of(reallyClean, first, second), 3, null, null);
        check.observe("t", "c2", "upsert-chunks", List.of(Map.of("title", "t")), 1, null, null);
        assertThat(lines()).hasSize(2);
        assertThat(lines().get(0)).doesNotContain("\n")
            .contains("source_agent=\"mcp' suppressed_since_last=9 rest\" suppressed_since_last=0");
        assertThat(lines().get(1)).contains("source_agent=\"absent\"");
    }

    @Test
    void aSecondClientOrASecondKeySetInTheSameCollectionAndMinuteLogsItsOwnLine_aRepeatOfTheFirstDoesNot() {
        var check = new ChunkKeyAllowlistCheck(new OwnerlessLogLimiter(() -> 1_000_000L));   // frozen clock
        var titleOnly = List.of(Map.<String, Object>of("title", "t"));
        var titleAndDoc = List.of(Map.<String, Object>of("title", "t", "doc_id", "d"));
        check.observe("t", "c", "upsert-chunks", titleOnly, 1, "agent-a/1", "7.1.0");
        check.observe("t", "c", "upsert-chunks", titleOnly, 1, "agent-a/1", "7.1.0");
        assertThat(lines()).as("same writer, same keys: one line").hasSize(1);
        check.observe("t", "c", "upsert-chunks", titleOnly, 1, "agent-b/1", "7.1.0");
        assertThat(lines()).as("another client: its own line").hasSize(2);
        check.observe("t", "c", "upsert-chunks", titleOnly, 1, "agent-a/1", "7.2.0");
        assertThat(lines()).as("another client version: its own line").hasSize(3);
        check.observe("t", "c", "upsert-chunks", titleAndDoc, 1, "agent-a/1", "7.1.0");
        assertThat(lines()).as("another key set: its own line").hasSize(4);
        assertThat(ChunkKeyAllowlistActivity.wouldRefuseTotal()).as("every request is counted").isEqualTo(5);
    }

    @Test
    void theActivityCountsByRouteAndClass_eachRequestOncePerClass() {
        var check = new ChunkKeyAllowlistCheck();
        var m1 = Map.<String, Object>of("title", "t", "doc_id", "d", "zz_unknown_key", 1);
        var m2 = Map.<String, Object>of("title", "t");
        check.observe("t", "c1", "upsert-chunks", List.of(m1, m2), 2, "agent-a/1", "7.1.0");
        check.observe("t", "c2", "manifest/write_many", List.of(m2), 1, "agent-b/1", null);

        assertThat(ChunkKeyAllowlistActivity.wouldRefuseTotal()).isEqualTo(2);
        assertThat(ChunkKeyAllowlistActivity.byRouteClass()).containsExactly(
            new ChunkKeyAllowlistActivity.RouteClassCount("manifest/write_many", "document", 1),
            new ChunkKeyAllowlistActivity.RouteClassCount("upsert-chunks", "document", 1),
            new ChunkKeyAllowlistActivity.RouteClassCount("upsert-chunks", "legacy", 1),
            new ChunkKeyAllowlistActivity.RouteClassCount("upsert-chunks", "other", 1));
    }

    @Test
    void manyDistinctKeysAndClientsGrowNoView_theRouteClassViewStaysAtItsFixedSize() {
        var check = new ChunkKeyAllowlistCheck(new OwnerlessLogLimiter(() -> 1_000_000L));
        for (int i = 0; i < 300; i++) {
            check.observe("t", "c", "upsert-chunks", List.of(Map.<String, Object>of(String.format("zz_k%04d", i), 1)),
                1, "agent-" + i, "1");
        }
        assertThat(ChunkKeyAllowlistActivity.wouldRefuseTotal()).isEqualTo(300);
        assertThat(ChunkKeyAllowlistActivity.byRouteClass())
            .as("300 distinct keys and clients add no row: the labels are route and class")
            .containsExactly(new ChunkKeyAllowlistActivity.RouteClassCount("upsert-chunks", "other", 300));
    }

    @Test
    void aFailureInsideTheCheckNeverReachesTheCaller() {
        var check = new ChunkKeyAllowlistCheck(new OwnerlessLogLimiter(() -> {
            throw new IllegalStateException("clock failed");
        }));
        check.observe("t", "c", "upsert-chunks", List.of(Map.of("title", "t")), 1, null, null);   // must not throw
    }

    @Test
    void evictTenant_forgetsItsWindow_soTheNextWriteLogsAgain() {
        var check = new ChunkKeyAllowlistCheck(new OwnerlessLogLimiter(() -> 1_000_000L));   // frozen clock
        var offending = List.of(Map.<String, Object>of("title", "t"));
        check.observe("t", "c", "upsert-chunks", offending, 1, null, null);
        check.observe("t", "c", "upsert-chunks", offending, 1, null, null);
        assertThat(lines()).as("second write is inside the window").hasSize(1);
        assertThat(check.evictTenant("t")).isEqualTo(1);
        check.observe("t", "c", "upsert-chunks", offending, 1, null, null);
        assertThat(lines()).hasSize(2);
    }
}
