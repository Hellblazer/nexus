// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;

import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * nexus-f3yqd, route R2: the wire envelope of {@code POST /v1/catalog/manifest/fill} and {@code GET
 * /v1/catalog/manifest/fill/{job_id}}: what each request shape answers (202, 400, 404, 405), the exact keys of the
 * start and the status, the bounds of {@code batch_rows}, and the {@code /version} flag. The fill itself is
 * {@code ManifestFillJobTest}; its concurrency and lifecycle are {@code ManifestFillConcurrencyTest}.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class ManifestFillRoutesTest extends ManifestFillTestBase {

    private static final String FILL = "/v1/catalog/manifest/fill";

    private HttpResponse<String> postFill(String body) throws Exception {
        return post(TOKEN, TENANT, FILL, body);
    }

    private HttpResponse<String> send(String method, String path) throws Exception {
        var req = TestHttp.request("http://127.0.0.1:" + service.getPort() + path)
            .header("Authorization", "Bearer " + TOKEN).header("X-Nexus-Tenant", TENANT)
            .method(method, HttpRequest.BodyPublishers.noBody()).build();
        return http.send(req, HttpResponse.BodyHandlers.ofString());
    }

    @Test
    void theStartAnswers202WithTheJobIdAndCreated_andTheStatusHasTheDocumentedKeys() throws Exception {
        var resp = postFill("{\"kind\":\"spans\"}");
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(202);
        Map<String, Object> body = mapper.readValue(resp.body(), MAP_T);
        assertThat(body.keySet()).containsExactly("job_id", "created");
        assertThat(body).containsEntry("created", true);

        var status = awaitEnd((String) body.get("job_id"));

        assertThat(status.keySet()).containsExactly("job_id", "kind", "dry_run", "state", "stage", "elapsed_ms",
            "done_rows", "total_rows", "filled", "cursor", "counts", "safe_to_stop", "on_stop");
        assertThat(status).containsEntry("job_id", body.get("job_id")).containsEntry("kind", "spans")
            .containsEntry("dry_run", false).containsEntry("state", "done").containsEntry("stage", "done")
            .containsEntry("done_rows", 0).containsEntry("total_rows", 0).containsEntry("filled", 0)
            .containsEntry("safe_to_stop", true);
        assertThat(status.get("cursor")).isNull();
        assertThat(counts(status)).containsOnly(Map.entry("fillable", 0), Map.entry("edge", 0), Map.entry("skipped_changed", 0));
    }

    @Test
    void aBadKindDryRunOrBodyIs400() throws Exception {
        for (String body : List.of("{}", "{\"kind\":\"bogus\"}", "{\"kind\":3}", "{\"kind\":\"Labels\"}", "{\"kind\":null}",
                "{\"kind\":\"labels\",\"dry_run\":\"yes\"}", "{\"kind\":\"labels\",\"dry_run\":1}", "not json", "[]")) {
            var resp = postFill(body);
            assertThat(resp.statusCode()).as(body + " -> " + resp.body()).isEqualTo(400);
            assertThat(mapper.readValue(resp.body(), MAP_T)).containsKey("error");
        }
    }

    @Test
    void batchRowsIsBounded_oneToFiveThousand() throws Exception {
        for (String rows : List.of("0", "-1", "5001", "1000000", "\"10\"", "1.5", "true")) {
            var resp = postFill("{\"kind\":\"labels\",\"batch_rows\":" + rows + "}");
            assertThat(resp.statusCode()).as("batch_rows " + rows + " -> " + resp.body()).isEqualTo(400);
        }
        for (int rows : List.of(1, 5000)) {
            var resp = postFill("{\"kind\":\"labels\",\"dry_run\":true,\"batch_rows\":" + rows + "}");
            assertThat(resp.statusCode()).as("batch_rows " + rows + " -> " + resp.body()).isEqualTo(202);
            awaitEnd((String) mapper.readValue(resp.body(), MAP_T).get("job_id"));
        }
        // absent takes the default of 1000
        var absent = postFill("{\"kind\":\"labels\",\"dry_run\":true}");
        assertThat(absent.statusCode()).isEqualTo(202);
        awaitEnd((String) mapper.readValue(absent.body(), MAP_T).get("job_id"));
    }

    @Test
    void onlyPostStartsAndOnlyGetPolls_405() throws Exception {
        for (String method : List.of("GET", "PUT", "DELETE")) {
            assertThat(send(method, FILL).statusCode()).as(method + " " + FILL).isEqualTo(405);
        }
        String id = (String) start("labels", true, 1000).get("job_id");
        awaitEnd(id);
        for (String method : List.of("POST", "PUT", "DELETE")) {
            assertThat(send(method, FILL + "/" + id).statusCode()).as(method + " poll").isEqualTo(405);
        }
    }

    @Test
    void anUnknownEmptyOrNestedJobIdIs404() throws Exception {
        for (String id : List.of("00000000-0000-0000-0000-000000000000", "nope", "", "a/b", "../x")) {
            var resp = get(TOKEN, TENANT, FILL + "/" + id);
            assertThat(resp.statusCode()).as("poll '" + id + "' -> " + resp.body()).isEqualTo(404);
            assertThat(mapper.readValue(resp.body(), MAP_T)).containsKey("error");
        }
    }

    @Test
    void versionAdvertisesTheFillJob() throws Exception {
        var resp = get(TOKEN, TENANT, "/version");
        assertThat(resp.statusCode()).as(resp.body()).isEqualTo(200);
        assertThat(mapper.readValue(resp.body(), MAP_T)).containsEntry("catalog_manifest_fill_job_supported", true)
            .containsEntry("catalog_manifest_fillable_supported", true);
    }
}
