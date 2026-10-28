// SPDX-License-Identifier: AGPL-3.0-or-later
package dev.nexus.service.db;

import static org.assertj.core.api.Assertions.assertThat;

import dev.nexus.service.PgContainerHelper;
import org.jooq.SQLDialect;
import org.jooq.impl.DSL;
import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestInstance;
import org.testcontainers.containers.PostgreSQLContainer;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.sql.Connection;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.TreeSet;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;

import static dev.nexus.service.jooq.nexus.Tables.CATALOG_DOCUMENTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_ASPECTS;
import static dev.nexus.service.jooq.nexus.Tables.DOCUMENT_HIGHLIGHTS;

/**
 * RDR-231 (nexus-bivz3.2): every route that takes a caller's note {@code source_uri} stores and
 * matches it in the {@code nx-note://} spelling, and no route is added that touches the column
 * without being listed here.
 *
 * <p><b>Static half.</b> Every reference to the {@code source_uri} column in
 * {@code CatalogRepository}, {@code AspectRepository} and the {@code vectors} package, by the jOOQ
 * {@code SOURCE_URI} symbol or by a string literal naming the column ({@code "source_uri"},
 * {@code "EXCLUDED.source_uri"}, a {@code ..._source_uri} count key), lies inside a method (or a
 * field declaration) that is LISTED (a route that canonicalizes a caller's URI) or EXEMPT in a
 * class that carries its reason. The allowlist is consumed in full: an entry that no longer
 * matches is itself a failure, so it cannot go stale. A LISTED method must also call a
 * canonicalizer in its body, so a listed route that loses its {@code NoteUri.canonical} turns
 * red. {@code RawSqlGateTest.blank} neutralizes comments and strings, so the symbol pass reads
 * the blanked text and the key pass reads the original text at positions whose blanked character
 * is still a quote (a string outside every comment).
 *
 * <p><b>Behavioural half.</b> Each LISTED route is driven on a real database with
 * {@code chroma://<c>/<t>} (stores or matches {@code nx-note://<c>/<t>}) and with
 * {@code nxexp://<T>/chroma://<c>/<t>} (stores or matches {@code nxexp://<T>/nx-note://<c>/<t>}),
 * and file-routed residue {@code chroma://rdr__...} keeps its spelling.
 *
 * <p>The move beads (nexus-ql5m5, the collection update, the re-embed move) add source_uri
 * references to methods named under MOVE; a new move method that touches the column is added to
 * the MOVE list by the bead that adds it.
 */
@TestInstance(TestInstance.Lifecycle.PER_CLASS)
class NoteUriRouteGateTest {

    // ───────────────────────────── allowlist ─────────────────────────────

    /** Why a method may touch the column without being a canonicalizing route. */
    enum Cls {
        /** A route that takes a caller's URI and canonicalizes it before it reads or writes. */
        LISTED,
        /** mergeDocuments: moves a STORED URI between two rows; no caller URI enters. */
        MERGE,
        /**
         * The move paths (rename, re-home, collection update, re-embed move). They rewrite stored URIs
         * by collection and are pinned by their own beads (nexus-ql5m5 and the collection-update and
         * re-embed-move beads), with {@code NoteUri.moveTo}. Other sessions edit these methods in
         * parallel and add source_uri references to them, so a MOVE entry stays live while the method is
         * declared, whether or not it names the column today.
         */
        MOVE,
        /** The vectors routes return the stored URI (include_source_uri) and take none from the caller. */
        VECTORS,
        /** Reads or renders a stored value, or declares a column constant; no caller URI enters. */
        PASS_THROUGH
    }

    record Entry(String file, String name, Cls cls, boolean declaredOnly, String why) {}

    private static Entry listed(String file, String name, String why) {
        return new Entry(file, name, Cls.LISTED, false, why);
    }

    private static Entry exempt(String file, String name, Cls cls, String why) {
        return new Entry(file, name, cls, false, why);
    }

    private static final String CAT = "CatalogRepository.java";
    private static final String ASP = "AspectRepository.java";
    private static final String PGV = "PgVectorRepository.java";
    private static final String MLS = "ManifestLabelStage.java";

    static final List<Entry> ENTRIES = List.of(
        // ── LISTED: CatalogRepository ─────────────────────────────────────
        listed(CAT, "upsertDocument",
            "POST /v1/catalog/register: guardDocumentIdentity and the insert take sourceUriOf(d)"),
        new Entry(CAT, "deriveSourceUri", Cls.LISTED, true,
            "doc/register and doc/register_many: the one place a request's URI becomes the stored one, so the "
                + "idempotency lookup, the insert and the race-loser lookup agree; it names no column, so it is "
                + "pinned by declaration and by its body calling NoteUri.canonical"),
        listed(CAT, "registerDocumentWithOutcome",
            "doc/register: lookup, insert and race-loser lookup use the deriveSourceUri result"),
        listed(CAT, "registerDocumentManyWithOutcome",
            "doc/register_many: the pre-batch lookup, the insert and the race-loser lookup use the deriveSourceUri result"),
        listed(CAT, "buildUpdateDocumentQuery",
            "update and update_many: a source_uri in the field map is canonicalized before the SET"),
        listed(CAT, "documentsBySourceUri",
            "list?source_uri= and resolve?source_uri=: canonicalizes inside the repo method, not in the handler"),
        listed(CAT, "importDocumentsBatch", "import/document batch: identity guard and insert take sourceUriOf(d)"),
        listed(CAT, "doImportDocument", "import/document single: identity guard and insert take sourceUriOf(d)"),
        listed(CAT, "sourceUriOf", "the one helper the document routes above share: canonical form of a row map's source_uri"),
        // ── LISTED: AspectRepository ──────────────────────────────────────
        listed(ASP, "upsertAspect", "aspects/upsert: the stored source_uri is the canonical form"),
        listed(ASP, "importAspectsBatch", "aspects/import batch: the stored source_uri is the canonical form"),
        listed(ASP, "doImportAspect", "aspects/import single: the stored source_uri is the canonical form"),
        listed(ASP, "upsertHighlight", "highlights/upsert: the stored source_uri is the canonical form"),
        listed(ASP, "importHighlightsBatch", "highlights/import batch: the stored source_uri is the canonical form"),
        listed(ASP, "doImportHighlight", "highlights/import single: the stored source_uri is the canonical form"),
        listed(ASP, "getHighlightBySourceUri", "highlights/get_by_source_uri: matches on the canonical form"),
        listed(ASP, "filterBySourceUris", "aspects/operator-query filter: the source_uris list is canonicalized once"),
        listed(ASP, "groupByField", "aspects/operator-query groupby: the source_uris list is canonicalized once"),
        listed(ASP, "confidenceAggregate", "aspects/operator-query aggregate: the source_uris list is canonicalized once"),
        // ── MERGE ─────────────────────────────────────────────────────────
        exempt(CAT, "mergeDocuments", Cls.MERGE,
            "moves a stored URI from the duplicate row to the canonical row (the dup is blanked first so "
                + "the live-uri unique index holds); both values are already stored, no caller URI enters"),
        // ── MOVE ──────────────────────────────────────────────────────────
        exempt(CAT, "renameCollectionTxn", Cls.MOVE,
            "rewrites stored note URIs naming X (either spelling) to name Y on catalog_documents, "
                + "document_aspects and document_highlights; its spelling-aware rewrite is pinned by nexus-ql5m5"),
        exempt(CAT, "rehomeCollection", Cls.MOVE, "re-home: files a source collection's rows under a target; moves rows, takes no caller URI"),
        exempt(CAT, "moveScopedTable", Cls.MOVE, "re-home's per-table row mover, called by rehomeCollection"),
        exempt(CAT, "movableAtSource", Cls.MOVE, "re-home's per-table movability predicate, shared by moveScopedTable and rehomeStatus"),
        exempt(CAT, "rehomeStatus", Cls.MOVE, "re-home's remaining/stuck count read, polled after a re-home"),
        exempt(CAT, "completeMove", Cls.MOVE, "the re-embed move of one document and its satellites into the sibling collection (nexus-bivz3.4)"),
        exempt(CAT, "failureDetail", Cls.MOVE, "renders the per-document move failure; named so a source_uri mention added there is tolerated"),
        exempt(CAT, "moveUriCollisions", Cls.MOVE, "the one move pre-check (re-embed, rename, re-home): which moving documents' rewritten URIs a live document holds (nexus-bivz3.4)"),
        exempt(CAT, "requireCollectionUrisFree", Cls.MOVE, "the rename and re-home pre-check on stored URIs, over the shared moveUriCollisions; takes no caller URI (nexus-ql5m5)"),
        exempt(CAT, "noteUriColumn", Cls.MOVE, "maps a collection-scoped table to its source_uri column for the re-home rewrite (nexus-ql5m5)"),
        exempt(CAT, "moveTarget", Cls.MOVE, "picks the collection update's move: a field map setting physical_collection without a non-null source_uri (nexus-bivz3.3)"),
        exempt(CAT, "refuseCollectionMoveCollisions", Cls.MOVE, "the collection update's pre-check on stored URIs; a caller's own source_uri is canonicalized in buildUpdateDocumentQuery (nexus-bivz3.3)"),
        // ── VECTORS ───────────────────────────────────────────────────────
        exempt(PGV, "sourceUrisByChash", Cls.VECTORS, "include_source_uri: reads the STORED catalog_documents.source_uri for a chunk"),
        exempt(PGV, "enrichSearchRows", Cls.VECTORS, "include_source_uri: attaches the stored URI to each search row"),
        exempt(PGV, "enrichGetEnvelope", Cls.VECTORS, "include_source_uri: attaches the stored URIs to a get envelope"),
        exempt(MLS, "occurrences", Cls.VECTORS, "include_source_uri: reads each owning document's STORED source_uri"),
        exempt(MLS, "expanded", Cls.VECTORS, "re-points a row at the owning document's STORED source_uri; takes none from the caller"),
        // ── PASS_THROUGH: CatalogRepository ───────────────────────────────
        exempt(CAT, "EX_DOC_URI", Cls.PASS_THROUGH, "field: the EXCLUDED.source_uri column constant the upsert arms SET from"),
        exempt(CAT, "DOCUMENTS_LIVE_SOURCE_URI", Cls.PASS_THROUGH, "field: the name of the live-source_uri unique index, not a value"),
        exempt(CAT, "UPDATABLE_DOC_COLUMNS", Cls.PASS_THROUGH, "field: the column allowlist for update; the value is canonicalized in buildUpdateDocumentQuery"),
        exempt(CAT, "liveTumblerForSourceUri", Cls.PASS_THROUGH, "identity guard lookup: receives the already-canonical URI from the listed callers (guardDocumentIdentity)"),
        exempt(CAT, "documentFields", Cls.PASS_THROUGH, "column list of a document read; returns the stored value"),
        exempt(CAT, "docRowFromRecord", Cls.PASS_THROUGH, "renders a stored row; RDR-231 forbids response rewriting"),
        // ── PASS_THROUGH: AspectRepository ────────────────────────────────
        exempt(ASP, "EX_SOURCE_URI", Cls.PASS_THROUGH, "field: EXCLUDED.source_uri for the aspect upsert arm"),
        exempt(ASP, "EX_SOURCE_URI_COALESCE", Cls.PASS_THROUGH, "field: EXCLUDED.source_uri coalesced for the aspect import arm"),
        exempt(ASP, "EX_HL_SOURCE_URI", Cls.PASS_THROUGH, "field: EXCLUDED.source_uri for the highlight arms"),
        exempt(ASP, "getAspect", Cls.PASS_THROUGH, "read by (collection, source_path); returns the stored URI"),
        exempt(ASP, "getAspectByDocId", Cls.PASS_THROUGH, "read by doc id; returns the stored URI"),
        exempt(ASP, "listByCollection", Cls.PASS_THROUGH, "read by collection; returns the stored URI"),
        exempt(ASP, "listByExtractorVersion", Cls.PASS_THROUGH, "read by extractor version; returns the stored URI"),
        exempt(ASP, "getHighlight", Cls.PASS_THROUGH, "read by doc id; returns the stored URI"),
        exempt(ASP, "listHighlights", Cls.PASS_THROUGH, "read paged list; returns the stored URI"),
        exempt(ASP, "recordToMap", Cls.PASS_THROUGH, "renders a stored aspect row; RDR-231 forbids response rewriting"),
        exempt(ASP, "highlightToMap", Cls.PASS_THROUGH, "renders a stored highlight row; RDR-231 forbids response rewriting")
    );

    // ───────────────────────────── scanner ─────────────────────────────

    /** A method (or constructor) declaration: its name, the declaration start, body '{' and closing '}'. */
    record Region(String name, int start, int body, int end) {}

    /** One reference to the column, attributed to the outermost method region or field declaration holding it. */
    record Ref(int pos, int line, String kind, String owner) {}

    private static final Set<String> NOT_A_METHOD = Set.of(
        "if", "for", "while", "switch", "catch", "synchronized", "try", "return", "super", "this",
        "else", "new", "throw", "assert", "case", "do");
    private static final Set<String> NOT_A_DECLARATION_BEFORE = Set.of(
        "new", "record", "return", "throw", "else", "case", "assert", "instanceof", "enum", "interface");

    private static final Pattern CALL_LIKE = Pattern.compile("(?<![\\w$.])(\\w+)\\s*\\(");
    private static final Pattern SYMBOL = Pattern.compile("\\bSOURCE_URI\\b");
    /** A literal that names the column: source_uri, source_uris, EXCLUDED.source_uri, document_aspects_source_uri. */
    private static final Pattern COLUMN_LITERAL = Pattern.compile("\"([\\w.]*source_uris?)\"");
    private static final Pattern FIELD_NAME = Pattern.compile("(\\w+)\\s*=");

    /** Tokens that mean a body canonicalizes a caller's URI. Searched after the opening brace. */
    private static final List<String> CANONICALIZERS =
        List.of("NoteUri.canonical(", "NoteUri::canonical", "sourceUriOf(", "deriveSourceUri(");

    static List<Region> regions(String blanked) {
        List<Region> out = new ArrayList<>();
        Matcher m = CALL_LIKE.matcher(blanked);
        while (m.find()) {
            String name = m.group(1);
            if (NOT_A_METHOD.contains(name)) continue;
            int k = m.start(1) - 1;
            while (k >= 0 && Character.isWhitespace(blanked.charAt(k))) k--;
            if (k >= 0 && blanked.charAt(k) == '@') continue;          // annotation with arguments
            int e = k;
            while (k >= 0 && Character.isJavaIdentifierPart(blanked.charAt(k))) k--;
            if (e > k && NOT_A_DECLARATION_BEFORE.contains(blanked.substring(k + 1, e + 1))) continue;
            int open = blanked.indexOf('(', m.start(1));
            int depth = 0;
            int i = open;
            while (i < blanked.length()) {
                char c = blanked.charAt(i);
                if (c == '(') depth++;
                else if (c == ')' && --depth == 0) break;
                i++;
            }
            if (i >= blanked.length()) continue;
            int j = i + 1;
            while (j < blanked.length() && (Character.isWhitespace(blanked.charAt(j))
                    || Character.isJavaIdentifierPart(blanked.charAt(j))
                    || ".,".indexOf(blanked.charAt(j)) >= 0)) {
                j++;
            }
            if (j >= blanked.length() || blanked.charAt(j) != '{') continue;
            int bd = 0;
            int q = j;
            while (q < blanked.length()) {
                char c = blanked.charAt(q);
                if (c == '{') bd++;
                else if (c == '}' && --bd == 0) break;
                q++;
            }
            if (q >= blanked.length()) continue;
            out.add(new Region(name, m.start(1), j, q));
        }
        return out;
    }

    private static int lineOf(String text, int pos) {
        int line = 1;
        for (int i = 0; i < pos && i < text.length(); i++) if (text.charAt(i) == '\n') line++;
        return line;
    }

    /** The outermost region holding pos, or null. */
    private static Region outermost(List<Region> regions, int pos) {
        Region best = null;
        for (Region r : regions) {
            if (r.start() <= pos && pos <= r.end() && (best == null || r.start() < best.start())) best = r;
        }
        return best;
    }

    /** The declared name of a class-level statement holding pos ({@code NAME = ...}), or a line marker. */
    private static String fieldOwner(String blanked, int pos) {
        int s = pos;
        while (s > 0 && ";{}".indexOf(blanked.charAt(s - 1)) < 0) s--;
        Matcher m = FIELD_NAME.matcher(blanked.substring(s, pos));
        return m.find() ? m.group(1) : "<unattributed line " + lineOf(blanked, pos) + ">";
    }

    static List<Ref> refs(String src) {
        String blanked = RawSqlGateTest.blank(src);
        List<Region> regions = regions(blanked);
        List<int[]> hits = new ArrayList<>();
        Matcher sym = SYMBOL.matcher(blanked);
        while (sym.find()) hits.add(new int[] {sym.start(), 0});
        Matcher lit = COLUMN_LITERAL.matcher(src);
        while (lit.find()) {
            // A string outside every comment: blank() keeps the opening quote, spaces a comment.
            if (blanked.charAt(lit.start()) == '"') hits.add(new int[] {lit.start(), 1});
        }
        hits.sort((a, b) -> Integer.compare(a[0], b[0]));
        List<Ref> out = new ArrayList<>();
        for (int[] h : hits) {
            Region r = outermost(regions, h[0]);
            out.add(new Ref(h[0], lineOf(src, h[0]), h[1] == 0 ? "SOURCE_URI" : "\"source_uri\"-key",
                r != null ? r.name() : fieldOwner(blanked, h[0])));
        }
        return out;
    }

    /** What one file's check found. */
    record Findings(List<String> unlisted, List<String> dead, List<String> uncanonical,
                    int regionCount, int refCount, Set<String> referencing, Set<String> declared) {}

    static Findings check(String file, String src, List<Entry> all) {
        String blanked = RawSqlGateTest.blank(src);
        List<Region> regions = regions(blanked);
        List<Ref> refs = refs(src);
        Map<String, Entry> mine = new LinkedHashMap<>();
        for (Entry e : all) if (e.file().equals(file)) mine.put(e.name(), e);
        Set<String> referencing = new TreeSet<>();
        Set<String> declared = new TreeSet<>();
        for (Region r : regions) declared.add(r.name());
        List<String> unlisted = new ArrayList<>();
        for (Ref r : refs) {
            referencing.add(r.owner());
            if (!mine.containsKey(r.owner())) {
                unlisted.add(file + ":" + r.line() + " " + r.kind() + " in `" + r.owner()
                    + "` is neither a listed route nor exempt");
            }
        }
        List<String> dead = new ArrayList<>();
        List<String> uncanonical = new ArrayList<>();
        for (Entry e : mine.values()) {
            boolean live = switch (e.cls()) {
                case MOVE -> declared.contains(e.name());
                default -> e.declaredOnly() ? declared.contains(e.name()) : referencing.contains(e.name());
            };
            if (!live) {
                dead.add(file + " `" + e.name() + "` (" + e.cls() + ") "
                    + (e.cls() == Cls.MOVE || e.declaredOnly() ? "is not declared" : "no longer references the column")
                    + ": remove or fix the entry");
            }
            if (e.cls() == Cls.LISTED) {
                for (Region r : regions) {
                    if (!r.name().equals(e.name())) continue;
                    boolean holdsRef = refs.stream().anyMatch(x -> r.start() <= x.pos() && x.pos() <= r.end());
                    if (!e.declaredOnly() && !holdsRef) continue;
                    String body = blanked.substring(r.body(), r.end());
                    if (CANONICALIZERS.stream().noneMatch(body::contains)) {
                        uncanonical.add(file + ":" + lineOf(src, r.start()) + " listed `" + e.name()
                            + "` never canonicalizes a caller URI (none of " + CANONICALIZERS + ")");
                    }
                }
            }
        }
        return new Findings(unlisted, dead, uncanonical, regions.size(), refs.size(), referencing, declared);
    }

    // ───────────────────────────── the sweep ─────────────────────────────

    private static final Path MAIN = Path.of("src", "main", "java", "dev", "nexus", "service");

    private static Map<String, Path> targetFiles() throws IOException {
        Map<String, Path> files = new LinkedHashMap<>();
        files.put(CAT, MAIN.resolve("db").resolve(CAT));
        files.put(ASP, MAIN.resolve("db").resolve(ASP));
        try (Stream<Path> s = Files.walk(MAIN.resolve("vectors"))) {
            s.filter(p -> p.toString().endsWith(".java")).sorted()
                .forEach(p -> files.put(p.getFileName().toString(), p));
        }
        return files;
    }

    @Test
    void everySourceUriReferenceIsInAListedOrExemptMethod() throws IOException {
        List<String> unlisted = new ArrayList<>();
        List<String> dead = new ArrayList<>();
        List<String> uncanonical = new ArrayList<>();
        Map<String, Findings> perFile = new TreeMap<>();
        int regionTotal = 0;
        int refTotal = 0;
        Set<String> referencingAll = new TreeSet<>();
        for (Map.Entry<String, Path> f : targetFiles().entrySet()) {
            Findings fd = check(f.getKey(), Files.readString(f.getValue()), ENTRIES);
            perFile.put(f.getKey(), fd);
            unlisted.addAll(fd.unlisted());
            dead.addAll(fd.dead());
            uncanonical.addAll(fd.uncanonical());
            regionTotal += fd.regionCount();
            refTotal += fd.refCount();
            for (String n : fd.referencing()) referencingAll.add(f.getKey() + "#" + n);
        }
        // Entries naming a file the sweep did not scan are dead too.
        Set<String> scanned = perFile.keySet();
        for (Entry e : ENTRIES) {
            if (!scanned.contains(e.file())) dead.add("entry `" + e.name() + "` names unscanned file " + e.file());
        }

        long listed = ENTRIES.stream().filter(e -> e.cls() == Cls.LISTED).count();
        Map<Cls, Long> exempt = new LinkedHashMap<>();
        for (Cls c : Cls.values()) {
            if (c != Cls.LISTED) exempt.put(c, ENTRIES.stream().filter(e -> e.cls() == c).count());
        }
        System.out.println("NoteUriRouteGate sweep: files=" + perFile.size()
            + " method-regions-examined=" + regionTotal
            + " source_uri-references=" + refTotal
            + " per-file-references=" + perFile.entrySet().stream()
                .filter(x -> x.getValue().refCount() > 0).map(x -> x.getKey() + ":" + x.getValue().refCount()).toList()
            + " methods-with-references=" + referencingAll.size()
            + " listed=" + listed
            + " exempted=" + exempt.values().stream().mapToLong(Long::longValue).sum() + " " + exempt);

        assertThat(unlisted).as("source_uri references outside every listed or exempt method").isEmpty();
        assertThat(dead).as("allowlist entries that match nothing").isEmpty();
        assertThat(uncanonical).as("listed routes that never canonicalize").isEmpty();

        // Non-vacuity: a sweep that saw nothing is a failure, not a pass.
        assertThat(perFile.keySet()).as("vectors files scanned").contains(PGV, MLS);
        assertThat(perFile.get(CAT).refCount()).as("CatalogRepository references seen").isGreaterThanOrEqualTo(45);
        assertThat(perFile.get(ASP).refCount()).as("AspectRepository references seen").isGreaterThanOrEqualTo(33);
        assertThat(perFile.get(PGV).refCount()).as("PgVectorRepository references seen").isGreaterThanOrEqualTo(3);
        assertThat(regionTotal).as("method regions examined").isGreaterThanOrEqualTo(1000);
        assertThat(listed).as("listed routes").isGreaterThanOrEqualTo(19);
        assertThat(ENTRIES.stream().filter(e -> e.cls() != Cls.LISTED && e.why().isBlank())).as("exempt entries without a reason").isEmpty();
        assertThat(ENTRIES.stream().filter(e -> e.why().isBlank())).as("entries without a reason").isEmpty();
    }

    // ───────────────────────────── meta-tests ─────────────────────────────

    private static final String SYNTH_HEAD =
        "class Synth {\n"
        + "    private static final Field<String> EX_URI = DSL.field(\"EXCLUDED.source_uri\", String.class);\n";

    private static String synth(String... members) {
        return SYNTH_HEAD + String.join("\n", members) + "\n}\n";
    }

    private static final String LISTED_OK =
        "    void goodRoute(Map<String,Object> d) {\n"
        + "        ctx.insertInto(T).set(T.SOURCE_URI, NoteUri.canonical(s(d, \"source_uri\"))).execute();\n"
        + "    }\n";

    private static List<Entry> synthEntries(Entry... extra) {
        List<Entry> l = new ArrayList<>();
        l.add(exempt("Synth.java", "EX_URI", Cls.PASS_THROUGH, "declares the EXCLUDED column"));
        l.add(listed("Synth.java", "goodRoute", "synthetic route"));
        l.addAll(List.of(extra));
        return l;
    }

    @Test
    void aMethodNotInTheAllowlistIsCaught() {
        String src = synth(LISTED_OK,
            "    List<String> dummyUnlisted(DSLContext ctx) {\n"
            + "        return ctx.select(T.SOURCE_URI).fetch(T.SOURCE_URI);\n"
            + "    }\n");
        Findings f = check("Synth.java", src, synthEntries());
        assertThat(f.unlisted()).hasSize(2).allMatch(s -> s.contains("`dummyUnlisted`"));
        assertThat(f.dead()).isEmpty();
        assertThat(f.uncanonical()).isEmpty();
    }

    @Test
    void theStringKeyAloneIsCaughtAndACommentOrMessageIsNot() {
        String src = synth(LISTED_OK,
            "    // source_uri in a comment, SOURCE_URI too\n"
            + "    void quiet(String uri) {\n"
            + "        log.info(\"event=x source_uri={}\", uri);\n"
            + "        throw new IllegalStateException(\"cross-project source_uri rejected\");\n"
            + "    }\n"
            + "    void viaKey(Map<String,Object> m) {\n"
            + "        m.put(\"source_uri\", \"x\");\n"
            + "    }\n");
        Findings f = check("Synth.java", src, synthEntries());
        assertThat(f.unlisted()).hasSize(1);
        assertThat(f.unlisted().get(0)).contains("`viaKey`").contains("\"source_uri\"-key");
    }

    @Test
    void aDeadAllowlistEntryIsCaught() {
        String src = synth(LISTED_OK, "    void noUriHere() { int x = 1; }\n");
        Findings f = check("Synth.java", src, synthEntries(
            exempt("Synth.java", "noUriHere", Cls.PASS_THROUGH, "claims a reference it does not have"),
            exempt("Synth.java", "gone", Cls.PASS_THROUGH, "method that was deleted")));
        assertThat(f.dead()).hasSize(2);
        assertThat(f.dead().get(0)).contains("noUriHere").contains("no longer references");
        assertThat(f.dead().get(1)).contains("gone");
    }

    @Test
    void aMoveEntryStaysLiveWhileDeclaredWithOrWithoutAReference() {
        String src = synth(LISTED_OK, "    void moveIt() { int x = 1; }\n");
        Findings live = check("Synth.java", src, synthEntries(exempt("Synth.java", "moveIt", Cls.MOVE, "mover")));
        assertThat(live.dead()).isEmpty();
        Findings gone = check("Synth.java", src, synthEntries(exempt("Synth.java", "moveGone", Cls.MOVE, "mover")));
        assertThat(gone.dead()).hasSize(1).allMatch(s -> s.contains("moveGone") && s.contains("not declared"));
    }

    @Test
    void aListedRouteThatDoesNotCanonicalizeIsCaught() {
        String src = synth(
            "    void goodRoute(Map<String,Object> d) {\n"
            + "        ctx.insertInto(T).set(T.SOURCE_URI, s(d, \"source_uri\")).execute();\n"
            + "    }\n");
        Findings f = check("Synth.java", src, synthEntries());
        assertThat(f.uncanonical()).hasSize(1).allMatch(s -> s.contains("`goodRoute`"));
        // The declaration's own name is not a canonicalizer: deriveSourceUri( inside deriveSourceUri's signature.
        String selfName = synth("    String deriveSourceUri(String u) {\n        return u;\n    }\n");
        Findings g = check("Synth.java", selfName, List.of(
            exempt("Synth.java", "EX_URI", Cls.PASS_THROUGH, "column constant"),
            new Entry("Synth.java", "deriveSourceUri", Cls.LISTED, true, "declared-only route")));
        assertThat(g.uncanonical()).hasSize(1);
    }

    @Test
    void overloadsLambdasAnonymousClassesAndRecordsAreAttributedToTheEnclosingMethod() {
        String src = synth(
            "    record Pair(String a, String b) {}\n"
            + "    static String over(String a) { return over(a, \"\"); }\n"
            + "    static String over(String a, String b) {\n"
            + "        Runnable r = new Runnable() { public void run() { use(T.SOURCE_URI); } };\n"
            + "        list.forEach(x -> { if (x != null) { use(T.SOURCE_URI); } });\n"
            + "        return a;\n"
            + "    }\n");
        List<Ref> refs = refs(src);
        assertThat(refs).filteredOn(r -> r.owner().equals("over")).hasSize(2);
        assertThat(refs).filteredOn(r -> r.owner().equals("EX_URI")).hasSize(1);
        assertThat(refs).hasSize(3);
    }

    // ───────────────────────────── behavioural half ─────────────────────────────

    private static final String TENANT = "noteuri-route-tenant";
    private static final String SVC_ROLE = "svc_noteuri_route";
    private static final String SVC_PASS = "svc_noteuri_route_pass";

    private PostgreSQLContainer<?> pg;
    private com.zaxxer.hikari.HikariDataSource svcDs;
    private CatalogRepository catalog;
    private AspectRepository aspects;
    private int seq;

    private synchronized void substrate() throws Exception {
        if (pg != null) return;
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
        TenantScope scope = new TenantScope(svcDs);
        catalog = new CatalogRepository(scope);
        aspects = new AspectRepository(scope);
        catalog.upsertOwner(TENANT, Map.of(
            "tumbler_prefix", "9", "name", "noteuri-owner", "owner_type", "curator",
            "repo_hash", "", "description", "", "repo_root", "",
            "head_hash", "", "next_seq", 0L));
    }

    @AfterAll
    void stopAll() {
        if (svcDs != null) svcDs.close();
        if (pg != null) pg.stop();
    }

    /** The spellings each route is driven with: input, then the stored/matched form. */
    private record Spelling(String input, String stored) {}

    private static List<Spelling> spellings(String tag) {
        return List.of(
            new Spelling("chroma://coll-" + tag + "/title-" + tag, "nx-note://coll-" + tag + "/title-" + tag),
            new Spelling("nxexp://T" + tag + "/chroma://coll-" + tag + "/title-" + tag,
                "nxexp://T" + tag + "/nx-note://coll-" + tag + "/title-" + tag),
            // Already canonical: unchanged (idempotent).
            new Spelling("nx-note://coll-" + tag + "/already-" + tag, "nx-note://coll-" + tag + "/already-" + tag),
            // File-routed residue: never a note, keeps its spelling.
            new Spelling("chroma://rdr__1-1__voyage-context-3__v1/docs/" + tag + ".md",
                "chroma://rdr__1-1__voyage-context-3__v1/docs/" + tag + ".md"),
            new Spelling("file:///repo/" + tag + ".md", "file:///repo/" + tag + ".md"));
    }

    private String nextTag() {
        return "s" + (++seq);
    }

    private List<String> liveUrisAt(String... tumblers) throws Exception {
        List<String> out = new ArrayList<>();
        try (Connection su = pg.createConnection("")) {
            for (String t : tumblers) {
                out.add(DSL.using(su, SQLDialect.POSTGRES).select(CATALOG_DOCUMENTS.SOURCE_URI).from(CATALOG_DOCUMENTS)
                    .where(CATALOG_DOCUMENTS.TENANT_ID.eq(TENANT).and(CATALOG_DOCUMENTS.TUMBLER.eq(t)))
                    .fetchOne(CATALOG_DOCUMENTS.SOURCE_URI));
            }
        }
        return out;
    }

    private String storedAspectUriAt(String docId) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).select(DOCUMENT_ASPECTS.SOURCE_URI).from(DOCUMENT_ASPECTS)
                .where(DOCUMENT_ASPECTS.TENANT_ID.eq(TENANT).and(DOCUMENT_ASPECTS.DOC_ID.eq(docId)))
                .fetchOne(DOCUMENT_ASPECTS.SOURCE_URI);
        }
    }

    private String storedHighlightUriAt(String docId) throws Exception {
        try (Connection su = pg.createConnection("")) {
            return DSL.using(su, SQLDialect.POSTGRES).select(DOCUMENT_HIGHLIGHTS.SOURCE_URI).from(DOCUMENT_HIGHLIGHTS)
                .where(DOCUMENT_HIGHLIGHTS.TENANT_ID.eq(TENANT).and(DOCUMENT_HIGHLIGHTS.DOC_ID.eq(docId)))
                .fetchOne(DOCUMENT_HIGHLIGHTS.SOURCE_URI);
        }
    }

    private void seedCollectionAndDoc(String collection, String docId) throws Exception {
        try (Connection su = pg.createConnection("")) {
            PgContainerHelper.insertCollection(DSL.using(su, SQLDialect.POSTGRES), TENANT, collection);
            DSL.using(su, SQLDialect.POSTGRES).insertInto(CATALOG_DOCUMENTS)
                .set(CATALOG_DOCUMENTS.TENANT_ID, TENANT)
                .set(CATALOG_DOCUMENTS.TUMBLER, docId)
                .set(CATALOG_DOCUMENTS.TITLE, "fixture " + docId)
                .onConflict(CATALOG_DOCUMENTS.TENANT_ID, CATALOG_DOCUMENTS.TUMBLER).doNothing()
                .execute();
        }
    }

    // ── catalog routes ──

    private Map<String, Object> docRow(String tumbler, String uri) {
        Map<String, Object> d = new LinkedHashMap<>();
        d.put("tumbler", tumbler);
        d.put("title", "doc " + tumbler);
        d.put("content_type", "knowledge");
        d.put("corpus", "knowledge");
        d.put("source_uri", uri);
        return d;
    }

    /** list?source_uri= and resolve?source_uri= both end in documentsBySourceUri. */
    private void assertMatchedBy(Spelling sp, String tumbler) {
        for (String probe : List.of(sp.input(), sp.stored())) {
            var hits = catalog.documentsBySourceUri(TENANT, probe, 0, 0);
            assertThat(hits).as("documentsBySourceUri(%s)", probe).hasSize(1);
            assertThat(hits.get(0).get("tumbler")).isEqualTo(tumbler);
            assertThat(hits.get(0).get("source_uri")).as("response is the stored value, not rewritten").isEqualTo(sp.stored());
        }
    }

    @Test
    void registerStoresAndMatchesTheNoteSpelling() throws Exception {
        substrate();
        for (Spelling sp : spellings(nextTag())) {
            String t = "9.reg." + Math.abs(sp.hashCode());
            catalog.upsertDocument(TENANT, docRow(t, sp.input()));
            assertThat(liveUrisAt(t)).containsExactly(sp.stored());
            assertMatchedBy(sp, t);
        }
    }

    @Test
    void docRegisterStoresMatchesAndIsIdempotentAcrossSpellings() throws Exception {
        substrate();
        for (Spelling sp : spellings(nextTag())) {
            String t = catalog.registerDocument(TENANT, "9", Map.of("title", "r", "source_uri", sp.input()));
            assertThat(liveUrisAt(t)).containsExactly(sp.stored());
            // Same document again, in the stored spelling: the idempotency lookup sees the stored form.
            assertThat(catalog.registerDocument(TENANT, "9", Map.of("title", "r", "source_uri", sp.stored()))).isEqualTo(t);
            assertThat(catalog.registerDocument(TENANT, "9", Map.of("title", "r", "source_uri", sp.input()))).isEqualTo(t);
            assertMatchedBy(sp, t);
        }
    }

    @Test
    void docRegisterManyStoresMatchesAndIsIdempotentAcrossSpellings() throws Exception {
        substrate();
        List<Spelling> sps = spellings(nextTag());
        List<Map<String, Object>> docs = new ArrayList<>();
        for (Spelling sp : sps) docs.add(Map.of("title", "m", "source_uri", sp.input()));
        List<String> tumblers = catalog.registerDocumentMany(TENANT, "9", docs);
        for (int i = 0; i < sps.size(); i++) {
            assertThat(liveUrisAt(tumblers.get(i))).containsExactly(sps.get(i).stored());
            assertMatchedBy(sps.get(i), tumblers.get(i));
        }
        List<Map<String, Object>> again = new ArrayList<>();
        for (Spelling sp : sps) again.add(Map.of("title", "m", "source_uri", sp.stored()));
        assertThat(catalog.registerDocumentMany(TENANT, "9", again)).containsExactlyElementsOf(tumblers);
    }

    @Test
    void updateAndUpdateManyStoreTheNoteSpelling() throws Exception {
        substrate();
        for (Spelling sp : spellings(nextTag())) {
            String one = "9.upd1." + Math.abs(sp.hashCode());
            String many = "9.upd2." + Math.abs(sp.hashCode());
            catalog.upsertDocument(TENANT, docRow(one, "file:///seed/" + one));
            catalog.upsertDocument(TENANT, docRow(many, "file:///seed/" + many));
            assertThat(catalog.updateDocument(TENANT, one, Map.of("source_uri", sp.input()))).isEqualTo(1);
            assertThat(liveUrisAt(one)).containsExactly(sp.stored());
            // A distinct URI per entry keeps the live-uri unique index quiet.
            assertThat(catalog.updateDocumentsMany(TENANT, List.of(Map.of("tumbler", many, "source_uri", sp.input() + "/many"))))
                .containsExactly(1);
            assertThat(liveUrisAt(many)).containsExactly(sp.stored() + "/many");
        }
    }

    @Test
    void importDocumentAndBatchStoreTheNoteSpelling() throws Exception {
        substrate();
        for (Spelling sp : spellings(nextTag())) {
            String t = "9.imp1." + Math.abs(sp.hashCode());
            catalog.importDocument(TENANT, docRow(t, sp.input()));
            assertThat(liveUrisAt(t)).containsExactly(sp.stored());
            assertMatchedBy(sp, t);
        }
        List<Spelling> batch = spellings(nextTag());
        List<Map<String, Object>> rows = new ArrayList<>();
        for (Spelling sp : batch) rows.add(docRow("9.imp2." + Math.abs(sp.hashCode()), sp.input()));
        assertThat(catalog.importDocumentsBatch(TENANT, rows)).isEqualTo(batch.size());
        for (Spelling sp : batch) {
            assertThat(liveUrisAt("9.imp2." + Math.abs(sp.hashCode()))).containsExactly(sp.stored());
            assertMatchedBy(sp, "9.imp2." + Math.abs(sp.hashCode()));
        }
    }

    // ── aspect and highlight routes ──

    private Map<String, Object> aspectBody(String coll, String docId, String uri) {
        Map<String, Object> b = new LinkedHashMap<>();
        b.put("collection", coll);
        b.put("source_path", "p-" + docId);
        b.put("source_uri", uri);
        b.put("proposed_method", "Method " + docId);
        b.put("confidence", 0.9);
        b.put("extracted_at", "2026-06-01T10:00:00.000000Z");
        b.put("model_version", "v1");
        b.put("extractor_name", "scholarly");
        b.put("doc_id", docId);
        return b;
    }

    private Map<String, Object> highlightBody(String coll, String docId, String uri) {
        Map<String, Object> b = new LinkedHashMap<>();
        b.put("doc_id", docId);
        b.put("source_uri", uri);
        b.put("collection", coll);
        b.put("highlights_md", "h " + docId);
        b.put("mentions_md", "m " + docId);
        b.put("ingested_at", "2026-06-01T10:00:00.000000Z");
        return b;
    }

    @Test
    void aspectUpsertAndImportStoreTheNoteSpelling() throws Exception {
        substrate();
        for (Spelling sp : spellings(nextTag())) {
            String coll = "coll-asp-" + Math.abs(sp.hashCode());
            String d1 = "9.asp1." + Math.abs(sp.hashCode());
            seedCollectionAndDoc(coll, d1);
            aspects.upsertAspect(TENANT, aspectBody(coll, d1, sp.input()));
            assertThat(storedAspectUriAt(d1)).as("aspects/upsert").isEqualTo(sp.stored());
            assertThat(aspects.getAspectByDocId(TENANT, d1).orElseThrow().get("source_uri")).isEqualTo(sp.stored());

            String d2 = "9.asp2." + Math.abs(sp.hashCode());
            seedCollectionAndDoc(coll, d2);
            aspects.importAspect(TENANT, aspectBody(coll, d2, sp.input()));
            assertThat(storedAspectUriAt(d2)).as("aspects/import single").isEqualTo(sp.stored());

            String d3 = "9.asp3." + Math.abs(sp.hashCode());
            seedCollectionAndDoc(coll, d3);
            assertThat(aspects.importAspectsBatch(TENANT, List.of(aspectBody(coll, d3, sp.input())))).isEqualTo(1);
            assertThat(storedAspectUriAt(d3)).as("aspects/import batch").isEqualTo(sp.stored());
        }
    }

    @Test
    void highlightUpsertImportAndGetByUriUseTheNoteSpelling() throws Exception {
        substrate();
        for (Spelling sp : spellings(nextTag())) {
            String coll = "coll-hl-" + Math.abs(sp.hashCode());
            String d1 = "9.hl1." + Math.abs(sp.hashCode());
            seedCollectionAndDoc(coll, d1);
            assertThat(aspects.upsertHighlight(TENANT, highlightBody(coll, d1, sp.input()))).isTrue();
            assertThat(storedHighlightUriAt(d1)).as("highlights/upsert").isEqualTo(sp.stored());
            for (String probe : List.of(sp.input(), sp.stored())) {
                var got = aspects.getHighlightBySourceUri(TENANT, probe);
                assertThat(got).as("get_by_source_uri(%s)", probe).isPresent();
                assertThat(got.get().get("source_uri")).isEqualTo(sp.stored());
            }

            String d2 = "9.hl2." + Math.abs(sp.hashCode());
            seedCollectionAndDoc(coll, d2);
            aspects.importHighlight(TENANT, highlightBody(coll, d2, sp.input() + "/second"));
            assertThat(storedHighlightUriAt(d2)).as("highlights/import single").isEqualTo(sp.stored() + "/second");

            String d3 = "9.hl3." + Math.abs(sp.hashCode());
            seedCollectionAndDoc(coll, d3);
            assertThat(aspects.importHighlightsBatch(TENANT, List.of(highlightBody(coll, d3, sp.input() + "/third")))).isEqualTo(1);
            assertThat(storedHighlightUriAt(d3)).as("highlights/import batch").isEqualTo(sp.stored() + "/third");
        }
    }

    @Test
    void operatorQueryMatchesTheNoteSpelling() throws Exception {
        substrate();
        for (Spelling sp : spellings(nextTag())) {
            String coll = "coll-opq-" + Math.abs(sp.hashCode());
            String d = "9.opq." + Math.abs(sp.hashCode());
            seedCollectionAndDoc(coll, d);
            aspects.upsertAspect(TENANT, aspectBody(coll, d, sp.input()));
            // Probe in the input spelling, in the stored spelling, and in both at once (one canonical, distinct list).
            for (List<String> probe : List.of(List.of(sp.input()), List.of(sp.stored()), List.of(sp.input(), sp.stored()))) {
                assertThat(aspects.filterBySourceUris(TENANT, probe, "proposed_method", "%Method%"))
                    .as("filter %s", probe).containsExactly(sp.stored());
                assertThat(aspects.groupByField(TENANT, probe, "proposed_method"))
                    .as("groupby %s", probe).containsOnlyKeys(sp.stored());
                assertThat(aspects.confidenceAggregate(TENANT, probe, "avg_confidence"))
                    .as("aggregate %s", probe).isEqualTo(0.9);
            }
        }
    }

    @Test
    void theSpellingTableCoversBothDirectionsAndTheResidue() {
        List<Spelling> sps = spellings("x");
        assertThat(sps).anyMatch(s -> s.input().startsWith("chroma://coll-") && s.stored().startsWith("nx-note://coll-"));
        assertThat(sps).anyMatch(s -> s.input().startsWith("nxexp://") && s.stored().contains("/nx-note://coll-"));
        assertThat(sps).anyMatch(s -> s.input().startsWith("chroma://rdr__") && s.input().equals(s.stored()));
    }
}
