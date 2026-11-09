// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.util.Collection;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.TreeSet;

/**
 * RDR-236 Phase 4 Step 2a (nexus-6pfgb.66): the engine's chunk-metadata allow-list, in LOG MODE. A client write
 * that names a metadata key outside {@link ChunkMetadataKeys#CHUNK} is counted ({@link ChunkKeyAllowlistActivity})
 * and logged as one rate-limited {@code chunk_metadata_key_would_refuse} line, and then proceeds exactly as before:
 * nothing here refuses, rewrites or drops a key. Step 2b reads the log over a stated window; Step 2c turns the same
 * check into the 422 {@code chunk_metadata_key_refused}.
 *
 * <p><b>Which keys.</b> The client may write the CHUNK keys only. Every other key is an offender, and its class
 * names where it belongs: {@code document} (the catalog document), {@code position} (the manifest row), {@code
 * legacy} (retired, swept), or {@code other}. The engine-owned row keys are deliberately not a class: this source
 * may not name them ({@code EngineOwnedPredicateIntegrationTest}), and a client that sends one is {@code other}.
 *
 * <p><b>Which collections.</b> A collection named with one of {@link ChunkKeyPlan#BYPASS_PREFIXES} is skipped, the
 * same exemption the database CHECK carries. The prefixes come from that constant and are not written here.
 *
 * <p><b>Which routes.</b> The handlers call {@link #observe} before the write on every client route that writes
 * chunk metadata: {@code upsert-chunks} and {@code update-metadata} (vector handler), and {@code write_many},
 * {@code append} and {@code append_many} when they carry {@code chunks} (catalog handler, through the combined write
 * service). {@code delete_keys} and {@code metadata_delete_keys} are not inspected: they only remove keys.
 *
 * <p><b>Never fails a write.</b> The check is observation. It catches every RuntimeException and logs it at debug.
 *
 * <p>The log limiter and the line's client fields are the ownerless-write check's ({@link OwnerlessLogLimiter},
 * {@link OwnerlessLogText}): one line per route, tenant and collection per minute, with the request's {@code
 * User-Agent} and {@code X-Nexus-Client-Version}, each cleaned so a tenant-controlled value cannot forge a field.
 * The offending key names are client text too, and are cleaned and cut the same way.
 */
public final class ChunkKeyAllowlistCheck {

    private static final Logger log = LoggerFactory.getLogger(ChunkKeyAllowlistCheck.class);

    /** Longest user-agent, client-version and key text the line carries (the ownerless line's bound). */
    static final int FIELD_MAX = 120;
    /** Longest single offending key the line carries. */
    static final int KEY_MAX = 64;
    /** Most offending keys the line names; the count of the rest follows. */
    static final int KEYS_MAX = 16;

    /** Where an offending key belongs. */
    enum KeyClass {
        DOCUMENT("document"), POSITION("position"), LEGACY("legacy"), OTHER("other");

        private final String wire;

        KeyClass(String wire) {
            this.wire = wire;
        }
    }

    private final OwnerlessLogLimiter limiter;

    public ChunkKeyAllowlistCheck() {
        this(OwnerlessLogLimiter.system());
    }

    ChunkKeyAllowlistCheck(OwnerlessLogLimiter limiter) {
        this.limiter = limiter;
    }

    /** Drops this check's log windows for {@code tenant} (the test-only tenant reset route); returns how many. */
    public int evictTenant(String tenant) {
        return limiter.evictTenant(tenant);
    }

    /** True when {@code collection} carries a bypass prefix: its rows are outside the allow-list. */
    static boolean isBypass(String collection) {
        if (collection == null) {
            return false;
        }
        for (String prefix : ChunkKeyPlan.BYPASS_PREFIXES) {
            if (collection.startsWith(prefix)) {
                return true;
            }
        }
        return false;
    }

    /** The class of a key that is not a CHUNK key. */
    static KeyClass classify(String key) {
        if (ChunkMetadataKeys.DOCUMENT.contains(key)) {
            return KeyClass.DOCUMENT;
        }
        if (ChunkMetadataKeys.POSITION.contains(key)) {
            return KeyClass.POSITION;
        }
        if (ChunkMetadataKeys.LEGACY.contains(key)) {
            return KeyClass.LEGACY;
        }
        return KeyClass.OTHER;
    }

    /**
     * Observe one chunk-write request's metadata objects. A null or non-map element is skipped.
     *
     * @param metadatas     the incoming metadata object of each chunk of the request
     * @param requested     how many chunks the request carries (the line names it beside the offending count)
     * @param userAgent     the request's {@code User-Agent}; may be null
     * @param clientVersion the request's {@code X-Nexus-Client-Version}; null or blank reads as {@code absent}
     */
    public void observe(String tenant, String collection, String route, Collection<?> metadatas, int requested,
                        String userAgent, String clientVersion) {
        try {
            if (metadatas == null || metadatas.isEmpty() || isBypass(collection)) {
                return;
            }
            Map<String, KeyClass> offending = new TreeMap<>();
            int offendingChunks = 0;
            for (Object m : metadatas) {
                if (!(m instanceof Map<?, ?> meta)) {
                    continue;
                }
                boolean offends = false;
                for (Object k : meta.keySet()) {
                    if (k instanceof String key && !ChunkMetadataKeys.CHUNK.contains(key)) {
                        offending.put(key, classify(key));
                        offends = true;
                    }
                }
                if (offends) {
                    offendingChunks++;
                }
            }
            if (offending.isEmpty()) {
                return;
            }
            ChunkKeyAllowlistActivity.recordWouldRefuse();
            long suppressed = limiter.tryAcquire(route + "|" + tenant + "|" + collection);
            if (suppressed < 0) {
                return;
            }
            log.warn("event=chunk_metadata_key_would_refuse route={} tenant={} collection=\"{}\" offending_keys=\"{}\" "
                    + "offending_chunks={} requested={} user_agent=\"{}\" client_version=\"{}\" suppressed_since_last={}",
                route, tenant, OwnerlessLogText.quoted(collection, 256), keyList(offending), offendingChunks,
                requested, OwnerlessLogText.quoted(userAgent, FIELD_MAX),
                OwnerlessLogText.quoted(clientVersion, FIELD_MAX), suppressed);
        } catch (RuntimeException e) {
            log.debug("event=chunk_metadata_key_check_failed route={} error={}", route, e.toString());
        }
    }

    /**
     * {@code class:key} pairs, comma separated, grouped by class then sorted, at most {@link #KEYS_MAX} of them and a
     * {@code +N} for the rest. A key is client text: cleaned, cut, and stripped of the two delimiters of this list,
     * of {@code =} and of spaces, so it cannot start a {@code key=value} field of its own inside the quotes.
     */
    private static String keyList(Map<String, KeyClass> offending) {
        Set<String> parts = new LinkedHashSet<>();
        for (KeyClass c : KeyClass.values()) {
            Set<String> sorted = new TreeSet<>();
            for (var e : offending.entrySet()) {
                if (e.getValue() == c) {
                    sorted.add(c.wire + ":" + keyText(e.getKey()));
                }
            }
            parts.addAll(sorted);
        }
        List<String> all = List.copyOf(parts);
        if (all.size() <= KEYS_MAX) {
            return String.join(",", all);
        }
        return String.join(",", all.subList(0, KEYS_MAX)) + ",+" + (all.size() - KEYS_MAX);
    }

    private static String keyText(String key) {
        String text = OwnerlessLogText.plain(key, KEY_MAX)
            .replace(',', '.').replace(':', '.').replace('=', '~').replace(' ', '_')
            .replace('"', '\'').replace('\\', '/');
        return text.isEmpty() ? "empty" : text;
    }
}
