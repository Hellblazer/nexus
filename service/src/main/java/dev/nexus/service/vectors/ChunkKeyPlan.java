// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.vectors;

import dev.nexus.service.db.QuarantineOrigin;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Objects;
import java.util.Set;
import java.util.TreeSet;
import java.util.regex.Pattern;

/**
 * What a retire-keys sweep or constraint request asks the engine to enforce (RDR-236 Phase 4 Step 3, nexus-6pfgb.43):
 * the allowed metadata keys, the collection-name prefixes the allow-list does not apply to, and the optional
 * quarantine implication.
 *
 * <p><b>Why the request carries them.</b> The Python key table ({@code chunk-metadata-keys.toml}) is the one place the
 * CHUNK and ROW classes are written down, and the engine may not name the ROW keys: {@code
 * EngineOwnedPredicateIntegrationTest} forbids a main source naming the reaper's quarantine keys, which is why {@link
 * ChunkMetadataKeys} carries no ROW set. The sweep's allow-list and the CHECK's array are therefore rendered by the
 * client from that table and sent in the request, and the engine does not take them on trust: {@link #of} refuses a list
 * that drops a CHUNK key, names a DOCUMENT, POSITION or LEGACY key, or has a key shape no table row has. A stale client
 * sends a list the engine can still recognise as wrong.
 *
 * <p><b>The bypass prefixes</b> are likewise the client's (the table's {@code bypass_prefixes}); the engine requires
 * the quarantine prefix ({@link QuarantineOrigin#PREFIX}, the one constant its other guards share) and a shape that
 * ends in a separator, so no prefix is a string literal in this file (CollectionParseGateTest counts those down).
 *
 * @param allowed          CHUNK and ROW keys, in sorted order (the DDL array is rendered in this order)
 * @param rowKeys          {@code allowed} minus the CHUNK keys: the engine-owned keys, never named in this source
 * @param bypassPrefixes   collection-name prefixes whose rows the allow-list leaves alone, quarantine first
 * @param quarantineImplies {@code [by, at]}: the key {@code by} may be present only when {@code at} is; empty when the
 *                         request names none
 */
public record ChunkKeyPlan(List<String> allowed, List<String> rowKeys, List<String> bypassPrefixes,
                           List<String> quarantineImplies) {

    /** A metadata key as the table writes it: lower-case words joined by underscores, or the dotted frecency key. */
    private static final Pattern KEY_SHAPE = Pattern.compile("^[a-z][a-z0-9_.]{0,63}$");

    /** A bypass prefix: a lower-case word that ends in the collection separator ({@code -} or the double underscore). */
    private static final Pattern PREFIX_SHAPE =
        Pattern.compile("^[a-z][a-z0-9]{0,31}(-|" + "_".repeat(2) + ")$");

    static final int MAX_KEYS = 32;
    static final int MAX_PREFIXES = 4;

    public ChunkKeyPlan {
        allowed = List.copyOf(allowed);
        rowKeys = List.copyOf(rowKeys);
        bypassPrefixes = List.copyOf(bypassPrefixes);
        quarantineImplies = List.copyOf(quarantineImplies);
    }

    /**
     * Validate and build a plan.
     *
     * @param quarantineImplies null or empty for none, else exactly two keys {@code [by, at]}, both allowed row keys
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

        if (bypassPrefixes.isEmpty() || bypassPrefixes.size() > MAX_PREFIXES) {
            throw new IllegalArgumentException("bypass_prefixes must hold 1 to " + MAX_PREFIXES + " prefixes, got "
                + bypassPrefixes.size());
        }
        Set<String> prefixes = new LinkedHashSet<>();
        prefixes.add(QuarantineOrigin.PREFIX);
        boolean sawQuarantine = false;
        for (String p : bypassPrefixes) {
            if (p == null || !PREFIX_SHAPE.matcher(p).matches()) {
                throw new IllegalArgumentException("bypass_prefixes holds a malformed prefix: " + p);
            }
            sawQuarantine |= QuarantineOrigin.PREFIX.equals(p);
            prefixes.add(p);
        }
        if (!sawQuarantine) {
            throw new IllegalArgumentException("bypass_prefixes must name the quarantine prefix "
                + QuarantineOrigin.PREFIX);
        }

        List<String> implies = quarantineImplies == null ? List.of() : List.copyOf(quarantineImplies);
        if (!implies.isEmpty()) {
            if (implies.size() != 2 || implies.get(0).equals(implies.get(1))) {
                throw new IllegalArgumentException("quarantine_implies must be two distinct keys [by, at]");
            }
            for (String k : implies) {
                if (!row.contains(k)) {
                    throw new IllegalArgumentException("quarantine_implies names " + k
                        + ", which is not an engine-owned row key of allowed_keys");
                }
            }
        }
        return new ChunkKeyPlan(new ArrayList<>(keys), row, new ArrayList<>(prefixes), implies);
    }
}
