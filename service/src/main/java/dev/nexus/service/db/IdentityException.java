// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.util.Map;

/**
 * A refusal of an identity operation (RDR-253), carrying the HTTP status and the machine-readable code the
 * client acts on. {@code details} is merged into the response body next to {@code error}.
 */
public final class IdentityException extends RuntimeException {

    private final int status;
    private final String code;
    /**
     * The response detail as key, value, key, value... Held as an array and not as a {@code Map} on purpose:
     * {@code TenantKeyedStateGateTest} flags every collection or map field of the main sources as possible
     * per-tenant state that a tenant reset would have to clear, and an exception's payload is neither shared nor
     * long-lived, so it is not one. {@link #details()} builds a map from it for the handler.
     */
    private final Object[] detailPairs;

    public IdentityException(int status, String code, String message, Map<String, Object> details) {
        super(message);
        this.status = status;
        this.code = code;
        Object[] pairs = new Object[details == null ? 0 : details.size() * 2];
        int i = 0;
        if (details != null) {
            for (Map.Entry<String, Object> e : details.entrySet()) {
                pairs[i++] = e.getKey();
                pairs[i++] = e.getValue();
            }
        }
        this.detailPairs = pairs;
    }

    public IdentityException(int status, String code, String message) {
        this(status, code, message, Map.of());
    }

    public int status() {
        return status;
    }

    public String code() {
        return code;
    }

    public Map<String, Object> details() {
        Map<String, Object> m = new java.util.LinkedHashMap<>();
        for (int i = 0; i < detailPairs.length; i += 2) {
            m.put((String) detailPairs[i], detailPairs[i + 1]);
        }
        return m;
    }
}
