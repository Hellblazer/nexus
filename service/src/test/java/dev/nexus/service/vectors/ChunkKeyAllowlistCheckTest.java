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
    void everyClassOfKeyIsNamed_andTheChunkKeysAreNeverOffenders() {
        for (String k : ChunkMetadataKeys.CHUNK) {
            assertThat(ChunkKeyAllowlistCheck.classify(k)).as(k).isEqualTo(ChunkKeyAllowlistCheck.KeyClass.OTHER);
        }
        assertThat(ChunkMetadataKeys.DOCUMENT).allSatisfy(k ->
            assertThat(ChunkKeyAllowlistCheck.classify(k)).isEqualTo(ChunkKeyAllowlistCheck.KeyClass.DOCUMENT));
        assertThat(ChunkMetadataKeys.POSITION).allSatisfy(k ->
            assertThat(ChunkKeyAllowlistCheck.classify(k)).isEqualTo(ChunkKeyAllowlistCheck.KeyClass.POSITION));
        assertThat(ChunkMetadataKeys.LEGACY).allSatisfy(k ->
            assertThat(ChunkKeyAllowlistCheck.classify(k)).isEqualTo(ChunkKeyAllowlistCheck.KeyClass.LEGACY));
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
    void aLongKeyList_isCutAtTheCap_andNamesHowManyWereLeftOut() {
        var check = new ChunkKeyAllowlistCheck();
        Map<String, Object> meta = new LinkedHashMap<>();
        for (int i = 0; i < ChunkKeyAllowlistCheck.KEYS_MAX + 5; i++) {
            meta.put(String.format("zz_key_%02d", i), i);
        }
        check.observe("t", "c", "upsert-chunks", List.of(meta), 1, null, null);
        assertThat(lines()).hasSize(1);
        assertThat(lines().get(0)).contains(",+5\"");
        assertThat(lines().get(0)).contains("other:zz_key_00").doesNotContain("other:zz_key_20");
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
