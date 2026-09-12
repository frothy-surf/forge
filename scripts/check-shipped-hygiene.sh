#!/bin/bash
# Files that ship in the public forge image (or are embedded in its
# provenance) carry technical rationale only: no code origins, no incident
# history, no dates or job ids, no references to private documents or to
# the hosted service's internals. The file list is scripts/shipped-files.txt;
# the narrative behind those files lives in the private monorepo. This runs
# in both the private CI (before anything is mirrored) and the public
# reference repo's CI (before the image builds), unchanged in both.
#
#   scripts/check-shipped-hygiene.sh            # exit 1 on any hit
set -uo pipefail
cd "$(dirname "$0")/.."

FILES=()
while IFS= read -r line; do
  case "$line" in ''|'#'*) continue ;; esac
  FILES+=("$line")
done < scripts/shipped-files.txt

# One alternation, case-insensitive. Keep it about provenance and history,
# not vocabulary: a technical word that happens to be in here should be
# rewritten in the file, not removed from this list.
PATTERN='rasp|sfalmo|drjack|blipmap'
PATTERN+='|PLATFORM\.md|FEATURES-PLAN|COMMERCIAL\.md|DATA-API\.md|TODO\.md|workstream|\bws [0-9]'
PATTERN+='|railway|cloudflare|\bR2\b|nix/|admin/src|user_schedules|control[- ]plane|claim loop|\bfleet\b'
PATTERN+='|worker/(ingest|burnoff|bench|cam|sensor)|tj-?wrf|wxtofly|cams\.json|emulator'
PATTERN+='|\bjobs? [0-9]{3,}|20[0-9]{2}-[0-9]{2}-[0-9]{2}|seen in prod|user report|first hosted'
PATTERN+='|used to (be|come)|packaging bug|\bbug\b|regression|outage|incident|test-[a-z-]+\.py'

rc=0
for f in "${FILES[@]}"; do
  if hits=$(grep -nEi "$PATTERN" "$f"); then
    echo "== $f"; echo "$hits"; rc=1
  fi
done
[ $rc = 0 ] && echo "shipped files clean (${#FILES[@]} files)"
exit $rc
