# Taxonomy projection tuning

Operator guide for `nx taxonomy project`, `nx taxonomy hubs`, and
`nx taxonomy audit` — how the projection quality signals work, how to
calibrate thresholds for a new corpus, and how to diagnose bad results.

Scope: RDR-077 Phase 4a onwards. Related RDRs: RDR-070 (HDBSCAN topic
discovery), RDR-075 (cross-collection projection routing), RDR-076
(idempotent upgrade mechanism), RDR-077 (projection quality additions).

## Similarity semantics

`topic_assignments.similarity` stores **raw cosine similarity** between
the source chunk embedding and the target topic centroid, always. No
ICF weighting, no rescaling, no post-processing. The value is what
ChromaDB returned (`1.0 - distance`) at write time.

This is deliberate. A single stored value can be re-interpreted by any
number of query-time ranking strategies — ICF-weighted, time-decayed,
operator-boosted. Mutating the stored value at write time would either
require re-projection whenever the weighting policy changes, or leave
stale adjusted scores in the table. Raw cosine is the one invariant
every strategy can derive its own view from.

When you see similarity columns in `topic_assignments`, that is the
raw cosine. ICF appears only in **filter decisions** and **ranking
order**, never in persisted values.

## Why ICF

Some topics — generic-label clusters like "class", "import",
"exception" — pick up projection hits from nearly every source
collection. They are the taxonomy equivalent of English stopwords:
common, low-information, and they swamp useful signal in top-K
ranking.

Inverse Collection Frequency borrows the idea from TF-IDF:

    ICF(topic) = log2(N_effective / DF(topic))

where

    N_effective = COUNT(DISTINCT source_collection)
                  from projection rows with non-NULL source_collection.

    DF(topic)   = number of distinct source_collections that have
                  assigned any chunk to this topic via projection.

**Base choice.** `log2` is a TF-IDF convention. It keeps ICF values in a
human-readable range: ICF=1 means the topic appears in half the
collections, ICF=3 means it appears in an eighth. The ratio of two ICF
values has the same meaning under any log base; only the absolute scale
differs.

**Disabled when N_effective < 2.** ICF cannot discriminate across a
single-collection corpus — there is no "rare vs common" comparison. The
`compute_icf_map` call returns `{}` and the caller falls back to raw
cosine ranking. Documented in RDR-077 PQ-1.

**DF = N_effective → ICF = 0.** A topic that appears in every
collection is a hub by definition. `log2(1) = 0` — the topic's
adjusted score is zero regardless of cosine. This is intentional
suppression, not a bug.

**Legacy rows excluded.** Pre-RDR-077 projection rows have NULL
`source_collection` and do not participate in either numerator or
denominator. The ICF map reflects post-migration writes only until
those rows are superseded by re-projection (which the prefer-higher
UPSERT handles transparently).

## Per-corpus-type default thresholds

`--threshold` on `nx taxonomy project` resolves in this order:

1. Explicit `--threshold F` — always wins.
2. Per-prefix default — applied when `--threshold` is omitted.
3. Hard fallback — `0.70` for unknown prefixes.

| Source prefix | Default | Rationale |
|---------------|---------|-----------|
| `code__*` | 0.70 | Syntax tokens (class, def, return, import) inflate raw cosine; a high bar keeps projections meaningful. |
| `knowledge__*` | 0.50 | Dense prose with semantic embeddings yields lower raw cosine even for highly related content. |
| `docs__*` | 0.55 | Mixed prose + fenced code; splits the difference between the above. |
| `rdr__*` | 0.55 | Same composition as docs. |
| unknown | 0.70 | Safer under-match bias — prefer false negatives over false positives for an unfamiliar corpus. |

Source: `nexus.corpus.default_projection_threshold`.

## Calibrating a new corpus type

If you find yourself adding a new collection prefix that does not fit
the table above — or the defaults produce obvious misses or hubs —
follow this loop. No configuration surface exists yet for overriding
these in `.nexus.yml` (RDR-077 PQ-2); calibration informs a future RDR
or a hardcoded edit.

1. **Discover the baseline.** Run projection with no threshold filter
   (`--threshold 0.1`) against the new corpus and at least two
   well-understood peer corpora. Use `--persist` so the data is
   available for audit.

        nx taxonomy project <new_collection> --threshold 0.1 --persist

2. **Audit the distribution.** `nx taxonomy audit --collection
   <new_collection>` reports p10 / p50 / p90 similarity plus the
   receiving hub list. The p50 is a rough centerpoint; the p10-p90
   spread tells you how wide the cosine distribution is in that
   embedding space.

3. **Pick the inflection.** Start with p75 as a first-pass threshold.
   Re-project with that value. Examine:

    - **Too few matches** (novel chunks dominate): lower by 0.05.
    - **Too many hub matches** (generic labels like "class",
      "declaration"): enable `--use-icf` before lowering.
    - **Ranking feels right**: stop.

4. **Validate against hubs.** `nx taxonomy hubs --min-collections N
   --max-icf 0.5` surfaces topics that span many source collections
   with low ICF. If a useful, specific topic shows up here, your
   threshold is too low; raise it or turn ICF on. If generic patterns
   dominate and match expectation, ICF is doing its job.

5. **Record the decision.** Until RDR-077 PQ-2 ships a config surface,
   either hardcode the new default in `nexus.corpus.default_projection_threshold`
   with a rationale comment, or document the recommended override for
   operators invoking `nx taxonomy project <new_prefix> --threshold X`.

## Upsert semantics on re-projection

Projection rows use a prefer-higher UPSERT (RDR-077 RF-5 / Phase 2):

    ON CONFLICT(doc_id, topic_id) DO UPDATE SET
        similarity  = MAX(COALESCE(stored, -1.0), incoming),
        assigned_at = <refresh if incoming wins>,
        source_collection = <refresh if incoming wins>,
        assigned_by = 'projection'

Rules in effect:

- **Prefer higher.** Repeated projection with a lower similarity never
  overwrites a higher one. Centroid drift is absorbed by the next
  re-discover or an explicit re-projection with a higher-quality
  match.
- **Source refresh only when incoming wins.** If the new match is
  stronger, `assigned_at` and `source_collection` update to reflect
  that match's origin. Ties do not refresh.
- **Legacy NULL-safe.** `COALESCE(stored_similarity, -1.0)` ensures any
  pre-RDR-077 row (NULL similarity) is promoted on the first
  projection write, since any real cosine exceeds `-1.0`.
- **HDBSCAN, centroid, and manual rows keep `INSERT OR IGNORE`
  idempotency.** Only projection rows use UPSERT. This keeps
  clustering-time writes cheap and avoids silently replacing a
  hand-curated label via a model re-run.

## Staleness and the `--warn-stale` flag

Cross-collection projection rows capture a point-in-time view of the
centroid space. Re-discovering topics in a source collection without
re-projecting leaves stale similarity values behind.

`nx taxonomy hubs --warn-stale` flags hubs whose source collections'
`taxonomy_meta.last_discover_at` post-dates the hub's most recent
`assigned_at`. A true positive there signals: discover ran, projection
did not. Run `nx taxonomy project --backfill` or a targeted
`nx taxonomy project <src>` to refresh.

See RDR-077 RF-9 and the Phase 5 bead notes for implementation.

## Troubleshooting

**All projections report ICF=0 or empty map.** `N_effective < 2` —
only one distinct `source_collection` has projection rows. Either the
corpus really is single-source, or the write path did not populate
`source_collection` for these rows. Check
`SELECT DISTINCT source_collection FROM topic_assignments WHERE
assigned_by = 'projection'`.

**Hubs dominate the ranking.** Enable `--use-icf`. If hubs persist
after that, look at `nx taxonomy hubs --explain` for which generic
label tokens are firing. Consider raising the threshold for that
corpus type.

**Ranking looks identical with and without `--use-icf`.** Check
`compute_icf_map()` directly — if every topic's ICF is near 1.0, the
corpus has no dominant hubs. ICF correctly cannot discriminate; the
topics are roughly evenly distributed.

**Projection writes nothing despite threshold=0.1.** Verify source
collection has docs (`nx collection info <src>`) and target
collections have centroids (`nx taxonomy list --collection <target>`).
An empty source yields empty `chunk_assignments`.

**Legacy rows appear with NULL similarity.** Pre-RDR-077 projections.
They remain visible in `topic_assignments` until re-projected. The
COALESCE guard promotes them on the next matching write.

## See also

- `docs/architecture.md` — module map, taxonomy section.
- `docs/rdr/rdr-075-*.md` — cross-collection projection routing baseline.
- `docs/rdr/rdr-076-idempotent-upgrade-mechanism.md` — migration pattern.
- `docs/rdr/rdr-077-projection-quality-similarity-icf.md` — design record.
