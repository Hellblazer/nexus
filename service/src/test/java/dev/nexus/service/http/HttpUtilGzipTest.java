// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.http;

import org.junit.jupiter.api.Test;

import java.io.ByteArrayInputStream;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.util.zip.GZIPInputStream;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-tjyzn: {@link HttpUtil#send} compresses a response of at least {@link HttpUtil#GZIP_MIN_BYTES} when
 * the request's {@code Accept-Encoding} accepts gzip, and sends the identity body otherwise.
 */
class HttpUtilGzipTest {

    private static String json(int bytes) {
        // Exactly `bytes` ASCII characters: the 8 of the JSON frame, then filler.
        StringBuilder sb = new StringBuilder("{\"v\":\"");
        while (sb.length() < bytes - 2) sb.append("abc012 ".charAt(sb.length() % 7));
        return sb.append("\"}").toString();
    }

    private static CapturingExchange exchange(String acceptEncoding) {
        var ex = new CapturingExchange("GET", URI.create("/v1/x"), "");
        if (acceptEncoding != null) ex.requestHeaders.add("Accept-Encoding", acceptEncoding);
        return ex;
    }

    private static String gunzip(byte[] body) throws Exception {
        try (var in = new GZIPInputStream(new ByteArrayInputStream(body))) {
            return new String(in.readAllBytes(), StandardCharsets.UTF_8);
        }
    }

    @Test
    void withAcceptEncodingGzip_aLargeBodyIsCompressedAndDecodesToTheSameJson() throws Exception {
        String body = json(50_000);
        var ex = exchange("gzip, deflate, br");
        HttpUtil.send(ex, 200, body);
        assertThat(ex.status).isEqualTo(200);
        assertThat(ex.responseHeaders.getFirst("Content-Encoding")).isEqualTo("gzip");
        assertThat(ex.responseHeaders.getFirst("Vary")).isEqualTo("Accept-Encoding");
        assertThat(ex.responseHeaders.getFirst("Content-Type")).startsWith("application/json");
        assertThat(ex.bodyBytes().length).isLessThan(body.length());
        assertThat(gunzip(ex.bodyBytes())).isEqualTo(body);
    }

    @Test
    void withoutTheHeader_theBodyIsTheIdentityBytes() throws Exception {
        String body = json(50_000);
        var ex = exchange(null);
        HttpUtil.send(ex, 200, body);
        assertThat(ex.responseHeaders.containsKey("Content-Encoding")).isFalse();
        assertThat(ex.bodyString()).isEqualTo(body);
    }

    @Test
    void aSmallBodyIsNeverCompressed_evenWhenGzipIsAccepted() throws Exception {
        String body = json(HttpUtil.GZIP_MIN_BYTES - 1);
        var ex = exchange("gzip");
        HttpUtil.send(ex, 200, body);
        assertThat(ex.responseHeaders.containsKey("Content-Encoding")).isFalse();
        assertThat(ex.responseHeaders.containsKey("Vary")).isFalse();
        assertThat(ex.bodyString()).isEqualTo(body);
    }

    @Test
    void theThresholdItselfIsCompressed() throws Exception {
        String body = json(HttpUtil.GZIP_MIN_BYTES);
        var ex = exchange("gzip");
        HttpUtil.send(ex, 200, body);
        assertThat(body.length()).isGreaterThanOrEqualTo(HttpUtil.GZIP_MIN_BYTES);
        assertThat(ex.responseHeaders.getFirst("Content-Encoding")).isEqualTo("gzip");
        assertThat(gunzip(ex.bodyBytes())).isEqualTo(body);
    }

    @Test
    void qualityZeroAndOtherCodingsDoNotAcceptGzip() throws Exception {
        String body = json(5_000);
        for (String header : new String[] {"gzip;q=0", "gzip; q=0.0", "br", "identity", "deflate", ""}) {
            var ex = exchange(header);
            HttpUtil.send(ex, 200, body);
            assertThat(ex.responseHeaders.containsKey("Content-Encoding")).as("Accept-Encoding: '%s'", header)
                .isFalse();
            assertThat(ex.bodyString()).isEqualTo(body);
        }
    }

    @Test
    void gzipWithAQualityAndTheWildcardAreAccepted() throws Exception {
        String body = json(5_000);
        for (String header : new String[] {"gzip;q=0.5", "br, gzip;q=1.0", "*", "GZIP"}) {
            var ex = exchange(header);
            HttpUtil.send(ex, 200, body);
            assertThat(ex.responseHeaders.getFirst("Content-Encoding")).as("Accept-Encoding: '%s'", header)
                .isEqualTo("gzip");
            assertThat(gunzip(ex.bodyBytes())).isEqualTo(body);
        }
    }

    @Test
    void anErrorResponseOfSizeIsCompressedToo() throws Exception {
        String body = json(4_000);
        var ex = exchange("gzip");
        HttpUtil.send(ex, 422, body);
        assertThat(ex.status).isEqualTo(422);
        assertThat(gunzip(ex.bodyBytes())).isEqualTo(body);
    }
}
