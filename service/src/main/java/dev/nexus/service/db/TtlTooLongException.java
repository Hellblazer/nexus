// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * RDR-205 typed error: {@code out}'s explicit {@code ttl_seconds} exceeds
 * the template's {@code retention_seconds} ceiling — no row may outlive the
 * claim log's TTL, and the boot-time check (RDR-205 §Technical Design
 * "Registry") only covers every row when every row respects this cap.
 */
public final class TtlTooLongException extends TupleException {

    private final long ttlSeconds;
    private final long retentionSeconds;
    private final String template;

    public TtlTooLongException(long ttlSeconds, long retentionSeconds, String template) {
        super("TtlTooLong", 400,
                "ttl_seconds " + ttlSeconds + " exceeds template '" + template
                        + "' retention_seconds " + retentionSeconds);
        this.ttlSeconds = ttlSeconds;
        this.retentionSeconds = retentionSeconds;
        this.template = template;
    }

    public long ttlSeconds() {
        return ttlSeconds;
    }

    public long retentionSeconds() {
        return retentionSeconds;
    }

    public String template() {
        return template;
    }
}
