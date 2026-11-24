# rdr-close: one-time post-mortem migration

Loaded from Step 6 of the rdr-close skill when the legacy `knowledge__rdr_postmortem__<repo>` collection exists.

#### One-time migration (the legacy `knowledge__rdr_postmortem__<repo>` collection)

Rename that collection onto the subject BEFORE the first `store_put` to `{repo}-rdr-research`: `nx catalog rename-collection` refuses a target that exists, and a first `store_put` on a new subject mints a `c<id>` label, leaving no conformant name to rename onto. The target is the subject's four-segment name with the legacy collection's own model (a rename never re-embeds):

```bash
OLD="knowledge__rdr_postmortem__<repo>"
# the subject must have no live collection yet, whatever its label (a minted one is c<id>): the reuse check above
[ -z "$(nx collection list | awk -v s="{repo}-rdr-research" '$1=="NAME"{for(i=1;i<=NF;i++){if($i=="CONTENT_TYPE")t=i;if($i=="OWNER")o=i};next} /superseded->/{next} {split($1,p,"__")} (t&&o&&$t=="knowledge"&&$o==s)||(p[1]=="knowledge"&&p[2]==s){print $1}')" ] \
  || { echo "{repo}-rdr-research already has a collection: skip" >&2; exit 1; }
# the legacy collection's model, by the MODEL header
MODEL=$(nx collection list | awk -v o="$OLD" '$1=="NAME"{for(i=1;i<=NF;i++) if($i=="MODEL") c=i; next} $1==o && c {print $c; exit}')
[ -n "$MODEL" ] || { echo "$OLD is not listed: nothing to migrate" >&2; exit 1; }
[ "$MODEL" != "-" ] || { echo "no registered model for $OLD: skip" >&2; exit 1; }
nx catalog rename-collection "$OLD" "knowledge__{repo}-rdr-research__${MODEL}__v1" --yes
```

`nx collection list` has no `--json` form, so the filters read NAME (first word) and the CONTENT_TYPE, OWNER and MODEL headers, never a column position or a name split. Afterwards the bare subject resolves to this collection for `store_put` and `search(corpus="knowledge__{repo}-rdr-research")`, but only when `MODEL` is the model this install writes knowledge in today; otherwise a later `store_put` mints a second collection.

To read the current knowledge model: in cloud mode it is `voyage-context-3`; in local mode `nx config get local.embed_model` names the choice (a Voyage choice writes `voyage-context-3`, the bundled one writes the `bge-base-en-v15-768` token), and the MODEL of your newest live knowledge collection in `nx collection list` is the same answer read from the registry. If it differs from `MODEL`, skip.

**Skip** (legacy post-mortems stay findable by their old collection and tags: `search(corpus="knowledge__rdr_postmortem__<repo>")`) when the subject already has a collection, when `MODEL` is not your current knowledge model, or when the post-mortems sit in an owner-id-shaped `knowledge__<owner-id>__...` collection that holds other knowledge too. Not `nx catalog migrate-fallback`: it re-points documents onto owner-id-shaped targets and does not move chunks.

Renamed chunks lack `category="rdr_postmortem"`; they stay findable by their `tags="rdr,post-mortem,..."`. To add the category, re-run `store_put` with it against the subject, overwriting by title.
