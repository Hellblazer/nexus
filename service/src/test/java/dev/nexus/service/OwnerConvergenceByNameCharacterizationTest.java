// SPDX-License-Identifier: AGPL-3.0-or-later
package dev.nexus.service;

import dev.nexus.service.db.CatalogMoves;
import dev.nexus.service.db.CatalogRepository;
import dev.nexus.service.db.TenantScope;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.sql.Connection;
import java.util.LinkedHashMap;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

/**
 * Characterization tests (GH #1571, GH #1605): what the engine does today when a repo
 * owner is registered again from a path other than the one it was first registered
 * with. Every test here pins CURRENT behaviour as research input for an RDR on
 * identity across a path move. A failing test here means behaviour changed: that is a
 * design decision for the RDR, not a regression to be reverted.
 *
 * <p>Mechanism under test: {@code CatalogRepository.upsertOwner} resolves a prefix-less
 * payload by {@code (name, owner_type)} first and {@code repo_hash} second
 * ({@code resolveOwnerAddress}), converges on the row it finds, and the DO UPDATE arm
 * then overwrites {@code repo_hash} and {@code repo_root} whenever the payload carries
 * them. Document {@code source_uri} is derived from {@code repo_root} once, at
 * registration, and never rewritten by an owner upsert.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class OwnerConvergenceByNameCharacterizationTest {

    private static final String SVC_ROLE = "svc_owner_conv_char";
    private static final String SVC_PASS = "svc_owner_conv_char_pass";

    private static final String H1 = "aaaa1111";
    private static final String H2 = "bbbb2222";

    PostgreSQLContainer<?> pg;
    com.zaxxer.hikari.HikariDataSource svcDs;
    CatalogRepository repo;

    @BeforeAll
    void startAll() throws Exception {
        pg = PgContainerHelper.start();
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.applyProductSchema(su);
        }
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.bootstrapServiceRole(su, SVC_ROLE, SVC_PASS);
        }
        var config = new com.zaxxer.hikari.HikariConfig();
        config.setJdbcUrl(pg.getJdbcUrl());
        config.setUsername(SVC_ROLE);
        config.setPassword(SVC_PASS);
        config.setMaximumPoolSize(4);
        config.setAutoCommit(true);
        svcDs = new com.zaxxer.hikari.HikariDataSource(config);
        repo = new CatalogRepository(new TenantScope(svcDs));
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    /** The payload the Python client sends for register_owner(name, "repo", repo_hash=, repo_root=): no prefix. */
    private static Map<String, Object> ownerPayload(String name, String hash, String root) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("name", name);
        m.put("owner_type", "repo");
        m.put("repo_hash", hash);
        m.put("repo_root", root);
        return m;
    }

    private static Map<String, Object> doc(String title, String relPath) {
        return Map.of("title", title, "content_type", "code", "corpus", "code",
                      "file_path", relPath, "physical_collection", "code__x__v1");
    }

    /** The one owner row with this name; asserts there is exactly one. */
    private Map<String, Object> soleOwnerNamed(String tenant, String name) {
        var rows = repo.ownersByName(tenant, name);
        assertThat(rows).hasSize(1);
        return rows.get(0);
    }

    private String sourceUri(String tenant, String tumbler) {
        return (String) repo.getDocument(tenant, tumbler).get("source_uri");
    }

    /**
     * characterization: pins current behaviour for RDR moved-path identity; a change here
     * is a design decision, not a regression.
     *
     * <p>Pins: an owner registered for name N at /a/N (hash H1) with documents, then
     * registered again as name N at /b/N (hash H2), resolves by (name, owner_type) to the
     * SAME owner tumbler (1.1), no second owner is minted, repo_hash and repo_root are
     * overwritten to H2 and /b/N, the old hash H1 no longer resolves to any owner, and the
     * existing documents keep their source_uri under /a/N (stale). A document re-registered
     * under the same relative file_path misses on source_uri (now derived from /b/N) but hits
     * on the owner-scoped file_path leg, so it returns the OLD tumbler, created=false, and
     * its source_uri stays /a/N.
     */
    @Test
    void sameBasenameMove_convergesOnOldOwner_overwritesHashAndRoot_leavesDocumentSourceUriStale() {
        final String t = "owner-conv-move";
        repo.upsertOwner(t, ownerPayload("N", H1, "/a/N"));
        var before = soleOwnerNamed(t, "N");
        assertThat(before.get("tumbler_prefix")).isEqualTo("1.1");
        assertThat(before.get("repo_hash")).isEqualTo(H1);
        assertThat(before.get("repo_root")).isEqualTo("/a/N");

        String d1 = repo.registerDocument(t, "1.1", doc("x", "docs/x.md"));
        String d2 = repo.registerDocument(t, "1.1", doc("y", "docs/y.md"));
        assertThat(d1).isEqualTo("1.1.1");
        assertThat(d2).isEqualTo("1.1.2");
        assertThat(sourceUri(t, d1)).isEqualTo("file:///a/N/docs/x.md");
        assertThat(sourceUri(t, d2)).isEqualTo("file:///a/N/docs/y.md");

        // The move: same name, new root, new hash, no prefix.
        repo.upsertOwner(t, ownerPayload("N", H2, "/b/N"));

        var after = soleOwnerNamed(t, "N");                 // still exactly one owner named N
        assertThat(repo.listOwners(t)).hasSize(1);
        assertThat(after.get("tumbler_prefix")).isEqualTo("1.1");
        assertThat(after.get("repo_hash")).isEqualTo(H2);
        assertThat(after.get("repo_root")).isEqualTo("/b/N");
        assertThat(repo.ownerByRepoHash(t, H1)).isNull();   // the old hash resolves to nothing
        assertThat(repo.ownerByRepoHash(t, H2)).isNotNull();
        assertThat(repo.ownerByRepoHash(t, H2).get("tumbler_prefix")).isEqualTo("1.1");

        // Documents are untouched: source_uri still names the old root.
        assertThat(sourceUri(t, d1)).isEqualTo("file:///a/N/docs/x.md");
        assertThat(sourceUri(t, d2)).isEqualTo("file:///a/N/docs/y.md");

        // Re-registering the same relative path from the new root: source_uri leg misses,
        // owner-scoped file_path leg hits the old row.
        var again = repo.registerDocumentWithOutcome(t, "1.1", doc("x", "docs/x.md"));
        assertThat(again.tumbler()).isEqualTo(d1);
        assertThat(again.created()).isFalse();
        assertThat(sourceUri(t, d1)).isEqualTo("file:///a/N/docs/x.md");

        // A path the old root never had is derived from the NEW root, under the same owner.
        String d3 = repo.registerDocument(t, "1.1", doc("z", "docs/z.md"));
        assertThat(d3).isEqualTo("1.1.3");
        assertThat(sourceUri(t, d3)).isEqualTo("file:///b/N/docs/z.md");
    }

    /**
     * characterization: pins current behaviour for RDR moved-path identity; a change here
     * is a design decision, not a regression.
     *
     * <p>Pins the takeover: two live clones /a/N (H1) and /b/N (H2) of the same basename
     * share one owner. Registering the second clone converges on the first's owner (1.1)
     * and overwrites its repo_hash and repo_root to H2 and /b/N, although /a/N is still
     * present; no error and no second owner. The first clone's hash then resolves to
     * nothing. The second clone's registration of a relative path the first already
     * holds is swallowed by the file_path leg (old tumbler, created=false, source_uri
     * still /a/N), while a path only the second has becomes a new document under the same
     * owner with a /b/N source_uri. Registering the first clone again flips the owner
     * back to H1 and /a/N: the two clones alternate ownership with every registration.
     */
    @Test
    void sameBasenameSecondClone_takesOverFirstClonesOwner_andTheTwoAlternate() {
        final String t = "owner-conv-clone";
        repo.upsertOwner(t, ownerPayload("N", H1, "/a/N"));
        String a1 = repo.registerDocument(t, "1.1", doc("x", "docs/x.md"));
        String a2 = repo.registerDocument(t, "1.1", doc("only-a", "docs/only-a.md"));
        assertThat(sourceUri(t, a1)).isEqualTo("file:///a/N/docs/x.md");
        assertThat(sourceUri(t, a2)).isEqualTo("file:///a/N/docs/only-a.md");

        // Second clone registers; the first is still live on disk (the engine cannot know).
        repo.upsertOwner(t, ownerPayload("N", H2, "/b/N"));

        assertThat(repo.listOwners(t)).hasSize(1);
        var taken = soleOwnerNamed(t, "N");
        assertThat(taken.get("tumbler_prefix")).isEqualTo("1.1");
        assertThat(taken.get("repo_hash")).isEqualTo(H2);
        assertThat(taken.get("repo_root")).isEqualTo("/b/N");
        assertThat(repo.ownerByRepoHash(t, H1)).isNull();

        // Clone B indexes: a path clone A already holds returns A's document, not a new one.
        var shared = repo.registerDocumentWithOutcome(t, "1.1", doc("x", "docs/x.md"));
        assertThat(shared.tumbler()).isEqualTo(a1);
        assertThat(shared.created()).isFalse();
        assertThat(sourceUri(t, a1)).isEqualTo("file:///a/N/docs/x.md");
        // A path only clone B has is a new document with a /b/N source_uri, same owner.
        String b1 = repo.registerDocument(t, "1.1", doc("only-b", "docs/only-b.md"));
        assertThat(b1).isEqualTo("1.1.3");
        assertThat(sourceUri(t, b1)).isEqualTo("file:///b/N/docs/only-b.md");

        // Clone A registers again: converges on the same owner and takes it back.
        repo.upsertOwner(t, ownerPayload("N", H1, "/a/N"));
        assertThat(repo.listOwners(t)).hasSize(1);
        var back = soleOwnerNamed(t, "N");
        assertThat(back.get("tumbler_prefix")).isEqualTo("1.1");
        assertThat(back.get("repo_hash")).isEqualTo(H1);
        assertThat(back.get("repo_root")).isEqualTo("/a/N");
        assertThat(repo.ownerByRepoHash(t, H2)).isNull();
        // Clone B's document keeps its /b/N source_uri, now under an owner rooted at /a/N.
        assertThat(sourceUri(t, b1)).isEqualTo("file:///b/N/docs/only-b.md");
    }

    /**
     * characterization: pins current behaviour for RDR moved-path identity; a change here
     * is a design decision, not a regression.
     *
     * <p>Pins: a move that changes the basename, /a/N (name N, H1) to /b/M (name M, H2),
     * matches neither (name, owner_type) nor repo_hash, so a NEW owner (1.2) is minted
     * with name M, H2 and /b/M. The old owner 1.1 is left exactly as it was (name N, H1,
     * /a/N, documents with /a/N source_uri) and is not deactivated or linked to the new
     * one. The same relative path registered under the new owner is a separate document
     * (1.2.1) with a /b/M source_uri.
     */
    @Test
    void differentBasenameMove_mintsNewOwner_andLeavesOldOwnerAndDocumentsUntouched() {
        final String t = "owner-conv-rename";
        repo.upsertOwner(t, ownerPayload("N", H1, "/a/N"));
        String d1 = repo.registerDocument(t, "1.1", doc("x", "docs/x.md"));
        assertThat(sourceUri(t, d1)).isEqualTo("file:///a/N/docs/x.md");

        repo.upsertOwner(t, ownerPayload("M", H2, "/b/M"));

        assertThat(repo.listOwners(t)).hasSize(2);
        var oldOwner = soleOwnerNamed(t, "N");
        assertThat(oldOwner.get("tumbler_prefix")).isEqualTo("1.1");
        assertThat(oldOwner.get("repo_hash")).isEqualTo(H1);
        assertThat(oldOwner.get("repo_root")).isEqualTo("/a/N");
        assertThat(oldOwner.get("deactivated_at")).isNull();
        var newOwner = soleOwnerNamed(t, "M");
        assertThat(newOwner.get("tumbler_prefix")).isEqualTo("1.2");
        assertThat(newOwner.get("repo_hash")).isEqualTo(H2);
        assertThat(newOwner.get("repo_root")).isEqualTo("/b/M");
        assertThat(repo.ownerByRepoHash(t, H1).get("tumbler_prefix")).isEqualTo("1.1");
        assertThat(repo.ownerByRepoHash(t, H2).get("tumbler_prefix")).isEqualTo("1.2");

        // Old documents stay on the old owner, source_uri unchanged.
        assertThat(sourceUri(t, d1)).isEqualTo("file:///a/N/docs/x.md");
        // The same relative path under the new owner is a fresh document, not a move.
        var moved = repo.registerDocumentWithOutcome(t, "1.2", doc("x", "docs/x.md"));
        assertThat(moved.created()).isTrue();
        assertThat(moved.tumbler()).isEqualTo("1.2.1");
        assertThat(sourceUri(t, moved.tumbler())).isEqualTo("file:///b/M/docs/x.md");
        assertThat(repo.getDocument(t, d1)).isNotNull();
    }

    /**
     * RDR-254 (254-P1-S4): the renamed-move case above, then the move command. The state is the one the
     * characterization pins (old owner 1.1 as N at /a/N with H1; new owner 1.2 as M at /b/M with H2, each with the file
     * docs/x.md). {@code owners/move} of 1.1 to /b/M with H2 folds 1.2 into 1.1: one live owner, the OLD tumbler at the
     * NEW path, the second owner deactivated with no repo hash. The cases above keep their assertions: they pin the
     * engine's matching, which the move does not change.
     */
    @Test
    void differentBasenameMove_thenTheMoveCommand_foldsTheNewOwnerIntoTheOldOne() throws Exception {
        final String t = "owner-conv-rename-move";
        repo.upsertOwner(t, ownerPayload("N", H1, "/a/N"));
        String d1 = repo.registerDocument(t, "1.1", doc("x", "docs/x.md"));
        repo.upsertOwner(t, ownerPayload("M", H2, "/b/M"));
        String d2 = repo.registerDocument(t, "1.2", doc("x", "docs/x.md"));
        assertThat(d2).isEqualTo("1.2.1");

        var report = new CatalogMoves(repo).moveOwner(t, "1.1", "/b/M", H2, false).toMap();

        assertThat(report).containsEntry("owners_folded", 1).containsEntry("absorbed", 1).containsEntry("repointed", 1);
        var live = repo.listOwners(t);
        assertThat(live).as("one live owner").hasSize(1);
        assertThat(live.get(0)).containsEntry("tumbler_prefix", "1.1").containsEntry("name", "N")
            .containsEntry("repo_root", "/b/M").containsEntry("repo_hash", H2);
        assertThat(sourceUri(t, d1)).as("the old tumbler at the new path").isEqualTo("file:///b/M/docs/x.md");
        assertThat(repo.getDocument(t, d2)).as("the absorbed row is trashed").isNull();
        var all = repo.listOwners(t, true);
        assertThat(all).hasSize(2);
        var folded = all.stream().filter(o -> "1.2".equals(o.get("tumbler_prefix"))).findFirst().orElseThrow();
        assertThat(folded.get("deactivated_at")).isNotNull();
        assertThat((String) folded.get("repo_hash")).isNullOrEmpty();
        assertThat(repo.ownerByRepoHash(t, H2).get("tumbler_prefix")).isEqualTo("1.1");
    }
}
