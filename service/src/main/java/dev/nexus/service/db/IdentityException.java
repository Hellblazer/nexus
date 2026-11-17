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
    /** Key, value, key, value... (an array, so the exception holds no collection field). */
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
