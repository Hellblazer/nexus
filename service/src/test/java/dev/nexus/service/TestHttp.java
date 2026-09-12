// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.time.Duration;

/**
 * HTTP client and request builders for engine tests, with timeouts that a
 * caller cannot forget to set.
 *
 * <p>nexus-9meyc. A {@code java.net.http} request with NO timeout sits on a
 * socket read forever. Three engine-suite runs wedged that way on 2026-09-07
 * and 2026-09-08: 28 minutes in {@code TaxonomyCentroidHandlerTest}, about an
 * hour and forty in {@code VectorsRepointFunctionsIntegrationTest}, and thirty
 * plus minutes in a full-suite pass. In every case the surefire fork sat at 0%
 * CPU holding the shared build lease, so it blocked not just its own run but
 * every other engine build on the box, until an operator jstack'd and killed
 * it. Each reran clean in seconds or a couple of minutes, so the cost was
 * entirely the hang, not the work.
 *
 * <p>A timeout does not fix whatever caused the read to park. It converts an
 * unbounded wedge into a test failure with a stack trace, which is the
 * difference between a defect someone can debug and a box someone has to
 * rescue. The server-side cause is still unestablished (case 1's fork had no
 * matching server thread; a dropped connection and a request the server never
 * reads are both consistent), and chasing it is easier once a hang reports
 * itself.
 *
 * <p>Two layers underneath this one, because a helper only protects the
 * callers that use it:
 * <ul>
 *   <li>{@code TestHttpTimeoutLintTest} ratchets the number of test files that
 *       still build their own timeout-less client or request, so the count can
 *       fall but never grow.</li>
 *   <li>{@code forkedProcessTimeoutSeconds} in {@code service/pom.xml} is the
 *       backstop that needs no cooperation at all. It also covers the third
 *       occurrence, which was NOT a request read: every class had reported and
 *       the fork simply never exited, so no per-request timeout could have
 *       caught it.</li>
 * </ul>
 */
public final class TestHttp {

    /**
     * Per-request read timeout. Generous relative to real engine work — the
     * slowest legitimate handler call in this suite is comfortably under a
     * second against a warm in-process service — and small relative to the
     * wedges it replaces, which ran to 28 and 100 minutes. The point is
     * bounding an unbounded wait, not measuring latency, so it is deliberately
     * far above anything that could flake on a loaded box.
     */
    public static final Duration REQUEST_TIMEOUT = Duration.ofSeconds(60);

    /** Connect timeout. A local in-process service either accepts promptly or is not there. */
    public static final Duration CONNECT_TIMEOUT = Duration.ofSeconds(10);

    private TestHttp() {
    }

    /** An {@link HttpClient} with a connect timeout set. */
    public static HttpClient client() {
        return HttpClient.newBuilder().connectTimeout(CONNECT_TIMEOUT).build();
    }

    /**
     * An {@link HttpRequest.Builder} for *uri* with {@link #REQUEST_TIMEOUT}
     * already applied. Callers add method, headers and body as usual; the
     * timeout is not theirs to remember.
     */
    public static HttpRequest.Builder request(String uri) {
        return HttpRequest.newBuilder(java.net.URI.create(uri)).timeout(REQUEST_TIMEOUT);
    }
}
