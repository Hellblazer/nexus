// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.QuarantineOrigin;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Objects;
import java.util.Set;
import java.util.TreeSet;
import java.util.regex.Pattern;

/**
 * What a retire-keys sweep or constraint request asks the engine to enforce (RDR-236 Phase 4 Step 3, nexus-6pfgb.43):
 * the allowed metadata keys, the collection-name prefixes the allow-list does not apply to, and the quarantine
 * implication.
 *
 * <p><b>Why the request carries them.</b> The Python key table ({@code chunk-metadata-keys.toml}) is the one place the
 * CHUNK and ROW classes are written down, and the engine may not name the ROW keys: {@code
 * EngineOwnedPredicateIntegrationTest} forbids a main source naming the reaper's quarantine keys, which is why {@link
 * ChunkMetadataKeys} carries no ROW set. The sweep's allow-list and the CHECK's array are therefore rendered by the
 * client from that table and sent in the request, and the engine does not take them on trust.
 *
 * <p><b>What the engine pins.</b> The sweep strips every key a plan does not allow and the CHECK refuses the same set,
 * so a plan that is wrong in either direction deletes data or exempts it for good. The engine therefore holds the exact
 * answer, without naming a ROW key:
 * <ul>
 *   <li>the CHUNK keys are present, and no DOCUMENT, POSITION or LEGACY key is (the sets {@link ChunkMetadataKeys}
 *       carries);</li>
 *   <li>the ROW keys (the allowed keys that are not CHUNK keys) hash to {@link ChunkMetadataKeys#ROW_KEYS_SHA256},
 *       which {@code scripts/gen_chunk_metadata_keys_java.py} renders from the table: a dropped ROW key (the sweep
 *       would strip it from every row) and an unknown key (the CHECK would admit it) both change the digest;</li>
 *   <li>the bypass prefixes are exactly {@link QuarantineOrigin#PREFIX} and {@link #TAXONOMY_PREFIX}: a request that
 *       omits the taxonomy prefix would make the sweep strip taxonomy rows (irreversible), and an extra prefix would
 *       exempt a collection from the CHECK for good;</li>
 *   <li>the quarantine implication is present and its pair hashes to {@link
 *       ChunkMetadataKeys#QUARANTINE_IMPLIES_SHA256}.</li>
 * </ul>
 * A stale or malicious client sends a plan the engine refuses with the item named.
 *
 * <p>The taxonomy prefix is built from the collection-name grammar ({@code <content_type>} then the double-underscore
 * separator), not written as a literal: {@code CollectionParseGateTest} counts the string literals that hold the
 * separator and only lets them go down. {@link QuarantineOrigin#PREFIX} is the one constant the engine's other guards
 * share.
 *
 * @param allowed          CHUNK and ROW keys, in sorted order (the DDL array is rendered in this order)
 * @param rowKeys          {@code allowed} minus the CHUNK keys: the engine-owned keys, never named in this source
 * @param bypassPrefixes   collection-name prefixes whose rows the allow-list leaves alone, quarantine first
 * @param quarantineImplies {@code [by, at]}: the key {@code by} may be present only when {@code at} is
 */
public record ChunkKeyPlan(List<String> allowed, List<String> rowKeys, List<String> bypassPrefixes,
                           List<String> quarantineImplies) {

    /** The collection-name separator (the RDR-103 grammar): two underscores. Built, not written, so no literal holds it. */
    static final String COLLECTION_SEPARATOR = "_".repeat(2);

    /** The taxonomy collections' prefix: the content type, then the separator. */
    public static final String TAXONOMY_PREFIX = "taxonomy" + COLLECTION_SEPARATOR;

    /** The exact bypass set, quarantine first (the order the DDL lists them in). */
    public static final List<String> BYPASS_PREFIXES = List.of(QuarantineOrigin.PREFIX, TAXONOMY_PREFIX);

    /** A metadata key as the table writes it: lower-case words joined by underscores, or the dotted frecency key. */
    private static final Pattern KEY_SHAPE = Pattern.compile("^[a-z][a-z0-9_.]{0,63}$");

    /** A bypass prefix: a lower-case word that ends in the collection separator ({@code -} or the double underscore). */
    private static final Pattern PREFIX_SHAPE =
        Pattern.compile("^[a-z][a-z0-9]{0,31}(-|" + COLLECTION_SEPARATOR + ")$");

    static final int MAX_KEYS = 32;
    static final int MAX_PREFIXES = 4;

    public ChunkKeyPlan {
        allowed = List.copyOf(allowed);
        rowKeys = List.copyOf(rowKeys);
        bypassPrefixes = List.copyOf(bypassPrefixes);
        quarantineImplies = List.copyOf(quarantineImplies);
    }

    /**
     * A digest of the whole plan: the allow-list, the ROW keys, the bypass prefixes and the implication, each list
     * framed by its length so a key moved between lists changes it. Two requests carry the same plan exactly when this
     * is equal; a sweep job's start fingerprint is built over it, so only a start with the same plan is handed a running job (nexus-v75nw).
     */
    public String fingerprint() {
        StringBuilder b = new StringBuilder();
        for (List<String> l : List.of(allowed, rowKeys, bypassPrefixes, quarantineImplies)) {
            b.append(l.size()).append(':').append(String.join("\n", l)).append('\u0000');
        }
        return digest(List.of(b.toString()));
    }

    /** SHA-256 (hex) of {@code keys} joined by a newline, in the order given: what the generator renders. */
    static String digest(List<String> keys) {
        try {
            byte[] sum = MessageDigest.getInstance("SHA-256")
                .digest(String.join("\n", keys).getBytes(StandardCharsets.UTF_8));
            return HexFormat.of().formatHex(sum);
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 is a required JDK algorithm", e);
        }
    }

    /**
     * Validate and build a plan.
     *
     * @param quarantineImplies exactly two keys {@code [by, at]}, both allowed row keys, hashing to the pinned digest
     * @throws IllegalArgumentException naming what is wrong; the handler answers 400
     */
    public static ChunkKeyPlan of(List<String> allowedKeys, List<String> bypassPrefixes,
                                  List<String> quarantineImplies) {
        Objects.requireNonNull(allowedKeys, "allowed_keys");
        Objects.requireNonNull(bypassPrefixes, "bypass_prefixes");
        if (allowedKeys.isEmpty() || allowedKeys.size() > MAX_KEYS) {
            throw new IllegalArgumentException("allowed_keys must hold 1 to " + MAX_KEYS + " keys, got "
                + allowedKeys.size());
        }
        Set<String> keys = new TreeSet<>();
        for (String k : allowedKeys) {
            if (k == null || !KEY_SHAPE.matcher(k).matches()) {
                throw new IllegalArgumentException("allowed_keys holds a malformed key: " + k);
            }
            if (!keys.add(k)) {
                throw new IllegalArgumentException("allowed_keys names " + k + " twice");
            }
        }
        for (String chunkKey : ChunkMetadataKeys.CHUNK) {
            if (!keys.contains(chunkKey)) {
                throw new IllegalArgumentException("allowed_keys must keep the CHUNK key " + chunkKey);
            }
        }
        for (Set<String> retired : List.of(ChunkMetadataKeys.DOCUMENT, ChunkMetadataKeys.POSITION,
                ChunkMetadataKeys.LEGACY)) {
            for (String k : keys) {
                if (retired.contains(k)) {
                    throw new IllegalArgumentException("allowed_keys names " + k
                        + ", a DOCUMENT, POSITION or LEGACY key: its home is not the chunk row");
                }
            }
        }
        List<String> row = new ArrayList<>();
        for (String k : keys) {
            if (!ChunkMetadataKeys.CHUNK.contains(k)) {
                row.add(k);
            }
        }
        // The engine-owned keys are the exact set the table names: a dropped one would be stripped from every row, an
        // unknown one admitted by the CHECK. The set is compared by digest, so no source here names a ROW key.
        if (!digest(row).equals(ChunkMetadataKeys.ROW_KEYS_SHA256)) {
            throw new IllegalArgumentException("allowed_keys does not carry the engine-owned (ROW) keys of this engine's "
                + "key table: " + row.size() + " non-CHUNK keys sent, digest " + digest(row)
                + " is not the pinned " + ChunkMetadataKeys.ROW_KEYS_SHA256
                + " (a ROW key is missing, or a key the table does not know is named)");
        }

        if (bypassPrefixes.isEmpty() || bypassPrefixes.size() > MAX_PREFIXES) {
            throw new IllegalArgumentException("bypass_prefixes must hold 1 to " + MAX_PREFIXES + " prefixes, got "
                + bypassPrefixes.size());
        }
        Set<String> sent = new LinkedHashSet<>();
        for (String p : bypassPrefixes) {
            if (p == null || !PREFIX_SHAPE.matcher(p).matches()) {
                throw new IllegalArgumentException("bypass_prefixes holds a malformed prefix: " + p);
            }
            if (!sent.add(p)) {
                throw new IllegalArgumentException("bypass_prefixes names " + p + " twice");
            }
        }
        for (String required : BYPASS_PREFIXES) {
            if (!sent.contains(required)) {
                throw new IllegalArgumentException("bypass_prefixes must name " + required
                    + ": the bypass set is exactly " + BYPASS_PREFIXES);
            }
        }
        for (String p : sent) {
            if (!BYPASS_PREFIXES.contains(p)) {
                throw new IllegalArgumentException("bypass_prefixes names " + p
                    + ", which is not a bypass prefix: the bypass set is exactly " + BYPASS_PREFIXES
                    + " (an extra prefix would exempt a collection from the allow-list for good)");
            }
        }

        List<String> implies = quarantineImplies == null ? List.of() : List.copyOf(quarantineImplies);
        if (implies.size() != 2 || implies.get(0).equals(implies.get(1))) {
            throw new IllegalArgumentException("quarantine_implies is required: two distinct keys [by, at]");
        }
        for (String k : implies) {
            if (!row.contains(k)) {
                throw new IllegalArgumentException("quarantine_implies names " + k
                    + ", which is not an engine-owned row key of allowed_keys");
            }
        }
        if (!digest(implies).equals(ChunkMetadataKeys.QUARANTINE_IMPLIES_SHA256)) {
            throw new IllegalArgumentException("quarantine_implies is not the pair this engine's key table names, in "
                + "the order [by, at] (digest " + digest(implies) + ")");
        }
        return new ChunkKeyPlan(new ArrayList<>(keys), row, new ArrayList<>(BYPASS_PREFIXES), implies);
    }
}
