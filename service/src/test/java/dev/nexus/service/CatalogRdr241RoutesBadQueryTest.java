// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;

import java.sql.Connection;
import java.util.List;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-uwggq.2.3 to .5 (engine review r1): a query string the three Phase 2 routes cannot read is a 400, never a 500. A
 * NUL byte in a parameter (%00) cannot be stored in PostgreSQL text and used to surface as an internal error from the
 * statement; a malformed percent escape made the decoder throw. Every parameter of every route is covered, key and value,
 * each beside a control request that answers 200 so the 400 is the route's answer to the byte and not to the request.
 */
class CatalogRdr241RoutesBadQueryTest extends Rdr241RouteTestBase {

    private static final String SVC_ROLE = "svc_rdr241_badquery_test";
    private static final String SVC_PASS = "svc_rdr241_badquery_test_pass";

    @Override String svcRole() { return SVC_ROLE; }
    @Override String svcPass() { return SVC_PASS; }
    @Override void bootstrapRole(Connection su) throws Exception {
        PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
    }

    private static final String AUTHOR = "/v1/catalog/docs/by-author";
    private static final String META = "/v1/catalog/docs/by-meta";
    private static final String LOCATORS = "/v1/catalog/sensitive/locators";

    @Test
    void aNulByteInAnyParameterIsA400_andTheControlsAnswer200() throws Exception {
        insert(Row.of("5.1.1").author("Needle").flagged().meta("{\"k\":\"v\",\"m\":3}"));
        for (String ok : List.of(AUTHOR + "?author=needle&limit=5&offset=0", META + "?meta.k=v&meta_min.m=1&meta_default.k=v&content_type=prose&sensitive=true&limit=5&offset=0",
            LOCATORS + "?limit=5")) {
            assertThat(status(ok)).as("control " + ok).isEqualTo(200);
        }
        for (String bad : List.of(
            AUTHOR + "?author=need%00le", AUTHOR + "?author=needle&limit=5%00", AUTHOR + "?author=needle&offset=0%00", AUTHOR + "?author=%00",
            AUTHOR + "?au%00thor=needle&author=needle",
            META + "?meta.k=v%00", META + "?meta.k%00=v", META + "?meta.%00=v", META + "?meta_min.m=1%00", META + "?meta_min.m%00=1",
            META + "?meta.k=v&meta_default.k=v%00", META + "?meta.k=v&meta_default.k%00=v", META + "?meta.k=v&content_type=prose%00",
            META + "?meta.k=v&sensitive=true%00", META + "?meta.k=v&limit=1%00", META + "?meta.k=v&offset=0%00",
            LOCATORS + "?limit=5%00", LOCATORS + "?limit=%00", LOCATORS + "?other=%00")) {
            assertThat(status(bad)).as("NUL in " + bad).isEqualTo(400);
        }
    }

    /** The request line is written by hand: java.net.http refuses to build a URI with a malformed escape. */
    private int rawStatus(String requestTarget) throws Exception {
        try (var socket = new java.net.Socket("127.0.0.1", service.getPort())) {
            socket.setSoTimeout(10_000);
            String req = "GET " + requestTarget + " HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer " + TOKEN
                + "\r\nX-Nexus-Tenant: " + TENANT + "\r\nConnection: close\r\n\r\n";
            socket.getOutputStream().write(req.getBytes(java.nio.charset.StandardCharsets.US_ASCII));
            socket.getOutputStream().flush();
            var in = new java.io.BufferedReader(new java.io.InputStreamReader(socket.getInputStream(), java.nio.charset.StandardCharsets.US_ASCII));
            String statusLine = in.readLine();
            assertThat(statusLine).startsWith("HTTP/1.1 ");
            return Integer.parseInt(statusLine.split(" ")[1]);
        }
    }

    @Test
    void aMalformedPercentEscapeIsA400_notA500() throws Exception {
        assertThat(rawStatus(AUTHOR + "?author=needle")).as("control").isEqualTo(200);
        for (String bad : List.of(AUTHOR + "?author=%zz", AUTHOR + "?author=100%", META + "?meta.k=%4", META + "?meta.%g1=v", LOCATORS + "?limit=%")) {
            assertThat(rawStatus(bad)).as(bad).isEqualTo(400);
        }
    }
}
