// SPDX-License-Identifier: AGPL-3.0-or-later
// Copyright (c) 2026 Hal Hildebrand. All rights reserved.
package dev.nexus.service.board;

import com.fasterxml.jackson.databind.ObjectMapper;
import dev.nexus.service.NexusService;
import dev.nexus.service.db.TupleRepository;
import dev.nexus.service.http.GroupTestEnv;
import dev.nexus.service.http.IdentityTestEnv;
import dev.nexus.service.tuples.TemplateRegistry;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.UUID;

/**
 * The real-PostgreSQL fixture of the owner stamper tests: the group and identity fixture of RDR-252 Step 1 and
 * RDR-253, a {@link TupleRepository} over the same database, and an {@link OwnerStamper} on the fixture's clock.
 * {@link #done} does what {@code TupleHandler} does for a {@code --done} write: the row is written, then the hook runs.
 */
final class StamperEnv implements AutoCloseable {

    static final ObjectMapper MAPPER = new ObjectMapper();
    static final String BASE = "nexus-develop";

    final GroupTestEnv env;
    final TupleRepository tuples;
    final OwnerStamper stamper;

    StamperEnv() throws Exception {
        env = GroupTestEnv.start();
        TemplateRegistry registry = TemplateRegistry.loadAtBoot(null, null, NexusService.SWEEP_INTERVAL_HOURS * 3600L);
        tuples = TupleRepository.fromEnv(env.ids.scope, registry);
        stamper = new OwnerStamper(env.ids.scope, tuples, env.groups, env.ids.repo, env.ids.clock);
    }

    /** A group {@code push-train} led by {@code leader} that serves {@code BASE}; the others join as members. */
    void form(String tenant, String leader, String... members) {
        formEnding(tenant, leader, null, members);
    }

    void formEnding(String tenant, String leader, String ends, String... members) {
        Map<String, Object> body = GroupTestEnv.body("push-train", leader, List.of(BASE));
        if (ends != null) {
            body.put("ends", ends);
        }
        int status = env.form(tenant, leader, body).status();
        if (status != 200) {
            throw new IllegalStateException("form: " + status);
        }
        for (String m : members) {
            if (env.join(tenant, m, "push-train", "member").status() != 200) {
                throw new IllegalStateException("join " + m);
            }
        }
    }

    /** A push record, stamped with the session {@code stamp} (null: unstamped). */
    void push(String tenant, String base, String sha, String prev, String stamp) {
        String topic = base + "--push";
        tuples.out(tenant, "board/ci/" + topic, Map.of("topic", topic),
            Map.of("from", stamp == null ? "unknown" : stamp, "kind", "push", "sha", sha),
            "{\"base\":\"" + prev + "\",\"n\":1}", UUID.randomUUID().toString(), null, stamp);
    }

    /** A {@code --done} write and the hook after it, as the handler runs them. */
    void done(String tenant, String base, String sha, String outcome) {
        String topic = base + "--done";
        Map<String, String> dims = new LinkedHashMap<>();
        dims.put("from", "github");
        dims.put("kind", "sha");
        dims.put("sha", sha);
        dims.put("outcome", outcome);
        String body = "{\"state\":\"concluded\",\"sha\":\"" + sha + "\"}";
        tuples.out(tenant, "board/ci/" + topic, Map.of("topic", topic), dims, body, UUID.randomUUID().toString(), null, null);
        stamper.afterOut(tenant, "board/ci/" + topic, dims, body);
    }

    /** A {@code --done} write that carries {@code dims.branch_outcome} too, and the hook after it. */
    void doneBranch(String tenant, String base, String sha, String outcome, String branchOutcome) {
        String topic = base + "--done";
        Map<String, String> dims = new LinkedHashMap<>();
        dims.put("from", "github");
        dims.put("kind", "sha");
        dims.put("sha", sha);
        dims.put("outcome", outcome);
        dims.put("branch_outcome", branchOutcome);
        String body = "{\"state\":\"concluded\",\"sha\":\"" + sha + "\"}";
        tuples.out(tenant, "board/ci/" + topic, Map.of("topic", topic), dims, body, UUID.randomUUID().toString(), null, null);
        stamper.afterOut(tenant, "board/ci/" + topic, dims, body);
    }

    /** A {@code --done} write with a body of the caller's, no {@code branch_outcome}, and the hook after it. */
    void doneBody(String tenant, String base, String sha, String outcome, String body) {
        String topic = base + "--done";
        Map<String, String> dims = new LinkedHashMap<>();
        dims.put("from", "github");
        dims.put("kind", "sha");
        dims.put("sha", sha);
        dims.put("outcome", outcome);
        tuples.out(tenant, "board/ci/" + topic, Map.of("topic", topic), dims, body, UUID.randomUUID().toString(), null, null);
        stamper.afterOut(tenant, "board/ci/" + topic, dims, body);
    }

    /** The write only, for a test that runs the hook itself. */
    void doneRowOnly(String tenant, String base, String sha, String outcome) {
        String topic = base + "--done";
        tuples.out(tenant, "board/ci/" + topic, Map.of("topic", topic),
            Map.of("from", "github", "kind", "sha", "sha", sha, "outcome", outcome),
            "{\"state\":\"concluded\",\"sha\":\"" + sha + "\"}", UUID.randomUUID().toString(), null, null);
    }

    /** An ack row, stamped with the session of the acknowledging identity. */
    void ack(String tenant, String base, String sha, String stamp) {
        String topic = base + "--owner";
        tuples.out(tenant, "board/ci/" + topic, Map.of("topic", topic),
            Map.of("from", stamp, "kind", "ack", "sha", sha), "{}", UUID.randomUUID().toString(), null, stamp);
    }

    /** A {@code disclaim} row on the owner subspace, stamped with the session of the disclaiming identity, and the hook after it. */
    void disclaim(String tenant, String base, String sha, String stamp) {
        String topic = base + "--owner";
        Map<String, String> dims = new LinkedHashMap<>();
        dims.put("from", stamp);
        dims.put("kind", "disclaim");
        dims.put("sha", sha);
        tuples.out(tenant, "board/ci/" + topic, Map.of("topic", topic), dims, "{}", UUID.randomUUID().toString(), null, stamp);
        stamper.afterOut(tenant, "board/ci/" + topic, dims, "{}");
    }

    /**
     * An ack row as the engine stores it after stamping: {@code from} an identity id, {@code stamp} the sender session,
     * or null for a row with no session (a raw client, or a hand-typed {@code from}).
     */
    void ackFrom(String tenant, String base, String sha, String from, String stamp) {
        String topic = base + "--owner";
        tuples.out(tenant, "board/ci/" + topic, Map.of("topic", topic),
            Map.of("from", from, "kind", "ack", "sha", sha), "{}", UUID.randomUUID().toString(), null, stamp);
    }

    /** The owner, clear and nudge rows of a topic, oldest first (the sessions' ack and disclaim rows are left out). */
    List<Row> ownerRows(String tenant, String base) {
        return tuples.rdp(tenant, "board/ci/" + base + "--owner", Map.of(), 200, null).stream()
            .filter(r -> !"ack".equals(r.dims().get("kind")) && !"disclaim".equals(r.dims().get("kind")))
            .map(r -> new Row(r.dims().get("kind"), r.dims().get("sha"), r.dims().get("from"), parse(r.body()))).toList();
    }

    record Row(String kind, String sha, String from, Map<String, Object> body) {
        Object get(String key) {
            return body.get(key);
        }
    }

    private static Map<String, Object> parse(String body) {
        return OwnerDecision.parse(body);
    }

    String tenant() {
        return IdentityTestEnv.freshTenant();
    }

    @Override
    public void close() throws Exception {
        env.close();
    }
}
