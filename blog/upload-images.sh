#!/usr/bin/env bash
# Upload local PNGs to tensegrity.blog wp-content/uploads/2026/04/
# Skips files that are byte-identical to remote.
# Idempotent — safe to re-run.

set -euo pipefail

SSH_HOST="ssh.wp.com"
SSH_USER="tensegritydotblog.wordpress.com"
SSH_KEY="$HOME/.ssh/id_ed25519"
REMOTE_DIR="htdocs/wp-content/uploads/2026/04"

LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$LOCAL_DIR"

SSH_OPTS=(-o BatchMode=yes -o IdentitiesOnly=yes -o ConnectTimeout=60 -i "$SSH_KEY")
SSH_TARGET="${SSH_USER}@${SSH_HOST}"

echo "── Step 1: fetch remote md5 manifest from $REMOTE_DIR"
REMOTE_MANIFEST=$(mktemp)
trap 'rm -f "$REMOTE_MANIFEST"' EXIT

ssh "${SSH_OPTS[@]}" "$SSH_TARGET" \
  "cd $REMOTE_DIR && md5sum *.png 2>/dev/null || true" > "$REMOTE_MANIFEST"
echo "  remote manifest: $(wc -l < "$REMOTE_MANIFEST") files"
echo

echo "── Step 2: compare each local PNG to remote"
TO_UPLOAD=()
SKIPPED=()
NEW_REMOTE=()

for f in *.png; do
  [ -f "$f" ] || continue
  LOCAL_MD5=$(md5 -q "$f")
  REMOTE_LINE=$(grep -E "  $f\$" "$REMOTE_MANIFEST" || true)
  if [ -z "$REMOTE_LINE" ]; then
    NEW_REMOTE+=("$f")
    TO_UPLOAD+=("$f")
    echo "  + $f  (NEW on remote)"
  else
    REMOTE_MD5=$(echo "$REMOTE_LINE" | awk '{print $1}')
    if [ "$LOCAL_MD5" = "$REMOTE_MD5" ]; then
      SKIPPED+=("$f")
      echo "  = $f  (identical, skip)"
    else
      TO_UPLOAD+=("$f")
      echo "  ~ $f  (differs, will overwrite)"
    fi
  fi
done

echo
echo "── Summary"
echo "  to upload: ${#TO_UPLOAD[@]}"
echo "  identical: ${#SKIPPED[@]}"
echo "  new:       ${#NEW_REMOTE[@]}"

if [ "${#TO_UPLOAD[@]}" -eq 0 ]; then
  echo
  echo "Nothing to do."
  exit 0
fi

echo
read -p "Proceed with upload? [y/N] " yn
case "$yn" in
  [Yy]*) ;;
  *) echo "Aborted."; exit 1 ;;
esac

echo
echo "── Step 3: scp uploads"
for f in "${TO_UPLOAD[@]}"; do
  echo "  uploading $f ..."
  scp "${SSH_OPTS[@]}" "$f" "${SSH_TARGET}:${REMOTE_DIR}/$f"
done

echo
echo "── Step 4: verify"
ssh "${SSH_OPTS[@]}" "$SSH_TARGET" \
  "cd $REMOTE_DIR && md5sum $(printf '%s ' "${TO_UPLOAD[@]}")" \
  | while IFS= read -r line; do
      remote_md5=$(echo "$line" | awk '{print $1}')
      remote_file=$(echo "$line" | awk '{print $2}')
      local_md5=$(md5 -q "$remote_file")
      if [ "$remote_md5" = "$local_md5" ]; then
        echo "  ✓ $remote_file"
      else
        echo "  ✗ $remote_file  (MISMATCH local=$local_md5 remote=$remote_md5)"
      fi
    done

echo
echo "Done."
