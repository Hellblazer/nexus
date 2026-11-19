// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.db;

import java.util.Map;

/**
 * What a caller tells the engine about itself when it binds (RDR-253 "Binding"): the session id, the process
 * (host, pid, {@code started_at} in milliseconds, {@code proc_start} as a UTC instant to the second), the pane,
 * the client version, the lease, the session it came from, and a {@code holder_gone} verdict naming the
 * process it found dead. The engine records it and compares it; the check that a holder is gone is the
 * client's, the engine cannot read a laptop's files.
 */
public record IdentityEvidence(String sessionId, String host, Integer pid, Long startedAt, String procStart,
                               String pane, String clientVersion, String lease, String priorSessionId,
                               Process holderGone) {

    /** A process as the binding log names it. {@code procStart} may be null (a client that could not read it). */
    public record Process(String host, Integer pid, Long startedAt, String procStart) {

        public Process {
            bound(host, "host");
            bound(procStart, "proc_start");
        }

        /** Two processes are the same only when all four fields agree (null equals null). */
        public boolean sameAs(Process other) {
            return other != null && java.util.Objects.equals(host, other.host) && java.util.Objects.equals(pid, other.pid)
                && java.util.Objects.equals(startedAt, other.startedAt)
                && java.util.Objects.equals(procStart, other.procStart);
        }

        public Map<String, Object> toMap() {
            var m = new java.util.LinkedHashMap<String, Object>();
            m.put("host", host);
            m.put("pid", pid);
            m.put("started_at", startedAt);
            m.put("proc_start", procStart);
            return m;
        }
    }

    public static final String LEASED = "leased";
    public static final String UNLEASED = "unleased";

    public IdentityEvidence {
        bound(sessionId, "session_id");
        bound(host, "host");
        bound(procStart, "proc_start");
        bound(pane, "pane");
        bound(clientVersion, "client_version");
        bound(priorSessionId, "prior_session_id");
        if (lease == null) {
            lease = LEASED;
        }
        if (!LEASED.equals(lease) && !UNLEASED.equals(lease)) {
            throw new IllegalArgumentException("'lease' must be leased or unleased");
        }
    }

    /** Free text a caller writes is bounded (256 characters): a data credential may send these. */
    private static void bound(String value, String field) {
        if (value != null && value.length() > IdentityRepository.MAX_TEXT) {
            throw new IllegalArgumentException("'" + field + "' is longer than " + IdentityRepository.MAX_TEXT + " characters");
        }
    }

    /** The empty evidence of a call that carries none (a refresh, an end). */
    public static IdentityEvidence none() {
        return new IdentityEvidence(null, null, null, null, null, null, null, null, null, null);
    }

    /** The process this evidence describes. */
    public Process process() {
        return new Process(host, pid, startedAt, procStart);
    }

    boolean hasProcess() {
        return host != null && pid != null && startedAt != null;
    }
}
