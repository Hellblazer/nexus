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
     * Per-request read timeout: a bound on a HANG, not on performance.
     *
     * <p>Sized deliberately far above any legitimate call. The slowest real
     * handler in this suite is comfortably under a second against a warm
     * in-process service, and the wedges this replaces ran to 28 and 100
     * minutes, so anywhere between one and ten minutes converts the same
     * unbounded wait into the same bounded failure. Given that, the number
     * should be chosen to minimise FALSE positives rather than to catch a hang
     * quickly — nothing is gained by failing at 60s instead of 300s, and a box
     * running sixteen workers beside a gate can make an in-process round trip
     * take far longer than a quiet one.
     *
     * <p>That matters because of what a false positive looks like here: a red
     * naming an HTTP timeout, which reads as a product defect and is actually
     * contention. Three probes in this repo's Python suite fired exactly that
     * way on 2026-09-12, each on a wall-clock number, each pointing away from
     * the real cause (nexus-61vos). A wall-clock bound on a shared box is
     * load-sensitive by construction; the only safe use is one where the
     * threshold is far outside the legitimate range, which is why this is not
     * tuned tight.
     *
     * <p>If it ever DOES fire, read it as "this call did not return in five
     * minutes" and suspect contention before suspecting the handler.
     */
    public static final Duration REQUEST_TIMEOUT = Duration.ofSeconds(300);

    /**
     * Connect timeout. Same reasoning as {@link #REQUEST_TIMEOUT}: a local
     * in-process service either accepts or is not there, so the legitimate
     * value is milliseconds and anything above a few seconds is already
     * diagnostic. Set generously anyway, because the cost of being wrong in
     * the tight direction is a false red blamed on the engine.
     */
    public static final Duration CONNECT_TIMEOUT = Duration.ofSeconds(60);

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
