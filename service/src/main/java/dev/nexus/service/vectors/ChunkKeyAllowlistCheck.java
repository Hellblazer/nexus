// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.Collection;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
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
 * <p><b>What the line and the counters name.</b> Step 2b reads the log to find who writes which key, so the line
 * names the writer. The line carries the route, tenant, collection, the offending keys (the {@code other} keys
 * first, then the known classes, see {@link #keyList}), the client's {@code User-Agent} and {@code
 * X-Nexus-Client-Version}, and the {@code source_agent} of the first offending chunk (the ownerless line names its
 * first unowned chunk the same way). The limiter key is route, tenant, collection, a signature of the offending key
 * set and the client, so a second client or a second key set in the same collection logs its own line in the same
 * minute; the limiter's bound ({@link OwnerlessLogLimiter#MAX_KEYS} keys plus one overflow bucket) is what keeps the
 * log finite. {@link ChunkKeyAllowlistActivity} keeps the total and the counts by route and class, and those are the
 * complete signal; the line is a sample. The status route carries no key name and no client text, because it has no
 * tenant scope and that text is one tenant's; the per-key and per-client detail is in this line only. Every
 * client-controlled text (collection, user agent, version, source agent, key names) is cleaned and cut by {@link
 * OwnerlessLogText}, so none can forge a field.
 *
 * <p><b>Seams: what the count is, and is not.</b> Read the numbers with these in mind.
 * <ol>
 *   <li>The check runs before the write, so a request is counted even when a later step refuses or fails it: the
 *       ownerless 422, an embed failure, an unregistered collection, a supersede refusal in the combined write. A
 *       request the client retries or re-sends is counted each time it arrives; these routes keep no replay ledger.
 *       The count is requests that offered a key, not writes that stored one.</li>
 *   <li>The append routes ({@code append}, {@code append_many}) observe every chunk in the body. The combined write
 *       later drops a chunk no manifest row references, and keeps the first of two chunks with one chash, so those
 *       routes can count an offending chunk the service never stores. Step 2c's refusal must not hang off this
 *       count alone on those two routes.</li>
 *   <li>Writers inside the engine are not seen: {@code ReembedRunner.moveDocsCombined} and the {@code gc} restore
 *       SQL copy stored metadata verbatim and never pass a handler. A PDF pipeline row staged before an upgrade is
 *       replayed through {@code upsert-chunks} with the metadata it was staged with, so it is seen late, when it is
 *       replayed, and counted as the replaying client.</li>
 *   <li>The test tree seeds legacy-keyed rows through these routes on purpose (dozens of test files): an engine
 *       booted by a test counts them, and only a production engine's counters mean anything.</li>
 * </ol>
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

        String wire() {
            return wire;
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
            Map<String, KeyClass> offending = new TreeMap<>();   // raw key -> class
            Map<?, ?> firstOffending = null;
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
                    if (firstOffending == null) {
                        firstOffending = meta;
                    }
                }
            }
            if (offending.isEmpty()) {
                return;
            }
            String ua = OwnerlessLogText.quoted(userAgent, FIELD_MAX);
            String version = OwnerlessLogText.quoted(clientVersion, FIELD_MAX);
            Map<String, KeyClass> cleaned = new TreeMap<>();     // cleaned key text -> class
            offending.forEach((k, c) -> cleaned.put(keyText(k), c));
            ChunkKeyAllowlistActivity.recordWouldRefuse(route, cleaned);
            // One line per route, tenant, collection, key set and client per window: a second writer or a second
            // key set in the same collection is not the first one's suppressed repeat. The delimiter is stripped
            // from client text so a tenant-keyed eviction (which matches |tenant|) reads only the tenant field.
            String limiterKey = route + "|" + tenant + "|" + collection + "|" + signature(offending.keySet())
                + "|" + ua.replace('|', '/') + "|" + version.replace('|', '/');
            long suppressed = limiter.tryAcquire(limiterKey);
            if (suppressed < 0) {
                return;
            }
            log.warn("event=chunk_metadata_key_would_refuse route={} tenant={} collection=\"{}\" offending_keys=\"{}\" "
                    + "offending_chunks={} requested={} user_agent=\"{}\" client_version=\"{}\" source_agent=\"{}\" "
                    + "suppressed_since_last={}",
                route, tenant, OwnerlessLogText.quoted(collection, 256), keyList(cleaned), offendingChunks,
                requested, ua, version, sourceAgent(firstOffending), suppressed);
        } catch (RuntimeException e) {
            log.debug("event=chunk_metadata_key_check_failed route={} error={}", route, e.toString());
        }
    }

    /** The first offending chunk's {@code source_agent}, cleaned like the other client fields; {@code absent} if none. */
    private static String sourceAgent(Map<?, ?> chunkMetadata) {
        Object v = chunkMetadata == null ? null : chunkMetadata.get("source_agent");
        return OwnerlessLogText.quoted(v instanceof String s ? s : null, FIELD_MAX);
    }

    /** A short stable digest of the offending key set, bounded however many keys offend. */
    private static String signature(Collection<String> sortedKeys) {
        try {
            MessageDigest sha = MessageDigest.getInstance("SHA-256");
            for (String k : sortedKeys) {
                sha.update(k.getBytes(StandardCharsets.UTF_8));
                sha.update((byte) '\n');
            }
            return HexFormat.of().formatHex(sha.digest(), 0, 8);
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);   // SHA-256 is mandatory on every JVM; caught by observe
        }
    }

    /**
     * {@code class:key} pairs, comma separated. The {@code other} keys, the ones a reader has to classify, come first
     * and in full up to {@link #KEYS_MAX}, then {@code other:+N} for the rest. A known class (document, position,
     * legacy) follows as its keys when they all fit in the slots {@code other} left, and otherwise as one {@code
     * class:+N} count, so a client that sends twenty retired keys and one unknown key still has the unknown key
     * named. Keys are already cleaned by {@link #keyText}.
     */
    static String keyList(Map<String, KeyClass> offending) {
        Map<KeyClass, TreeSet<String>> byClass = new java.util.EnumMap<>(KeyClass.class);
        for (var e : offending.entrySet()) {
            byClass.computeIfAbsent(e.getValue(), c -> new TreeSet<>()).add(e.getKey());
        }
        List<String> parts = new ArrayList<>();
        TreeSet<String> others = byClass.getOrDefault(KeyClass.OTHER, new TreeSet<>());
        int listed = 0;
        for (String k : others) {
            if (listed == KEYS_MAX) {
                break;
            }
            parts.add("other:" + k);
            listed++;
        }
        if (others.size() > listed) {
            parts.add("other:+" + (others.size() - listed));
        }
        for (KeyClass c : new KeyClass[] {KeyClass.DOCUMENT, KeyClass.POSITION, KeyClass.LEGACY}) {
            TreeSet<String> keys = byClass.get(c);
            if (keys == null) {
                continue;
            }
            if (listed + keys.size() <= KEYS_MAX) {
                keys.forEach(k -> parts.add(c.wire + ":" + k));
                listed += keys.size();
            } else {
                parts.add(c.wire + ":+" + keys.size());
            }
        }
        return String.join(",", parts);
    }

    /**
     * A key is client text: cleaned, cut, and stripped of the two delimiters of {@link #keyList}, of {@code =} and of
     * spaces, so it cannot start a {@code key=value} field of its own inside the quotes.
     */
    static String keyText(String key) {
        String text = OwnerlessLogText.plain(key, KEY_MAX)
            .replace(',', '.').replace(':', '.').replace('=', '~').replace(' ', '_')
            .replace('"', '\'').replace('\\', '/');
        return text.isEmpty() ? "empty" : text;
    }
}
