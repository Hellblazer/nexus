# rdr-close: one-time post-mortem migration

Loaded from Step 6 of the rdr-close skill when the legacy `knowledge__rdr_postmortem__<repo>` collection exists.

#### One-time migration (the legacy `knowledge__rdr_postmortem__<repo>` collection)

Rename that collection onto the subject BEFORE the first `store_put` to `{repo}-rdr-research`: `nx catalog rename-collection` refuses a target that exists, and a first `store_put` on a new subject mints a `c<id>` label, leaving no conformant name to rename onto. The target is the subject's four-segment name with the legacy collection's own model (a rename never re-embeds):

```bash
CUR="${CUR:?set CUR to the model this install writes knowledge in (read it as described below)}"
OLD="knowledge__rdr_postmortem__<repo>"
SUBJECT="$(printf '%s' "{repo}" | tr _ -)-rdr-research"   # the engine turns _ into - in a subject
# the subject must have no live collection yet, whatever its label (a minted one is c<id>): the reuse check in SKILL.md
[ -z "$(nx collection list | awk -v s="$SUBJECT" '$1=="NAME"{for(i=1;i<=NF;i++){if($i=="CONTENT_TYPE")t=i;if($i=="OWNER")o=i};next} /superseded->/{next} {split($1,p,"__")} (t&&o&&$t=="knowledge"&&$o==s)||(p[1]=="knowledge"&&p[2]==s){print $1}')" ] \
  || { echo "$SUBJECT already has a collection: skip" >&2; exit 1; }
# the legacy collection's model, by the MODEL header; a retired (superseded) row is not the collection
MODEL=$(nx collection list | awk -v o="$OLD" '$1=="NAME"{for(i=1;i<=NF;i++) if($i=="MODEL") c=i; next} /superseded->/{next} $1==o && c {print $c; exit}')
[ -n "$MODEL" ] || { echo "$OLD is not listed: nothing to migrate" >&2; exit 1; }
[ "$MODEL" != "-" ] || { echo "no registered model for $OLD: skip" >&2; exit 1; }
[ "$MODEL" = "$CUR" ] || { echo "MODEL is $MODEL, this install writes $CUR: skip" >&2; exit 1; }
nx catalog rename-collection "$OLD" "knowledge__${SUBJECT}__${MODEL}__v1" --yes
```

`nx collection list` has no `--json` form, so the filters read NAME (first word) and the CONTENT_TYPE, OWNER and MODEL headers, never a column position or a name split. `SUBJECT` hyphenates `{repo}` because the engine turns `_` into `-` in a subject; the legacy name keeps `<repo>` as it is. Afterwards the bare subject resolves to this collection for `store_put` and `search(corpus="knowledge__{repo}-rdr-research")`, but only when `MODEL` is the model this install writes knowledge in today, which is why the block stops unless `MODEL` equals `CUR`; otherwise a later `store_put` mints a second collection.

Set `CUR` to what this install writes knowledge in today:
- cloud mode: `voyage-context-3`;
- local mode: read `nx config get local.embed_model`. A value starting `voyage` means `voyage-context-3`; any other value means `bge-base-en-v15-768`. When it is unset, a Voyage key saved in `config.yml` (shown by `nx config list`) means `voyage-context-3`, and no saved key means `bge-base-en-v15-768`. A key that is only in the environment does not choose the model.

**Skip** (legacy post-mortems stay findable by their old collection and tags: `search(corpus="knowledge__rdr_postmortem__<repo>")`) when the subject already has a collection, when `MODEL` is not your current knowledge model, or when the post-mortems sit in an owner-id-shaped `knowledge__<owner-id>__...` collection that holds other knowledge too. Not `nx catalog migrate-fallback`: it re-points documents onto owner-id-shaped targets and does not move chunks.

Renamed chunks lack `category="rdr_postmortem"`; they stay findable by their `tags="rdr,post-mortem,..."`. To add the category, re-run `store_put` with it against the subject, overwriting by title.
