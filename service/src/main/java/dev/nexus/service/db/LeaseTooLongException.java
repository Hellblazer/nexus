// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

/**
 * RDR-205 typed error: {@code in}/{@code inp}'s explicit {@code lease_s}
 * exceeds the template's {@code take.max_lease_seconds}. A {@code lease_s}
 * within the cap but longer than the row's remaining TTL is CLAMPED to
 * {@code expires_at}, not refused — this error fires only above the
 * template's own ceiling.
 */
public final class LeaseTooLongException extends TupleException {

    private final long leaseSeconds;
    private final long maxLeaseSeconds;
    private final String template;

    public LeaseTooLongException(long leaseSeconds, long maxLeaseSeconds, String template) {
        super("LeaseTooLong", 400,
                "lease_s " + leaseSeconds + " exceeds template '" + template
                        + "' max_lease_seconds " + maxLeaseSeconds);
        this.leaseSeconds = leaseSeconds;
        this.maxLeaseSeconds = maxLeaseSeconds;
        this.template = template;
    }

    public long leaseSeconds() {
        return leaseSeconds;
    }

    public long maxLeaseSeconds() {
        return maxLeaseSeconds;
    }

    public String template() {
        return template;
    }
}
