#!/usr/bin/env bash
# The database, its archives and the dashboard page live on the `data`
# branch, not on main (2026-11-06). Committing a fresh 6 MB database to main
# every 15 minutes had grown the repo to 17 GB; `data` holds ONE commit --
# the latest snapshot -- replaced by every run, so it no longer grows.
# main's history (everything up to 2026-11-06) is untouched.
#
#   scripts/data_branch.sh load   fetch the latest snapshot into data/ + docs/
#   scripts/data_branch.sh save   replace the `data` branch with this snapshot
#                                 (+ the day's backup, see below)
#   scripts/data_branch.sh list   the backups on GitHub
#   scripts/data_branch.sh restore backup-YYYY-MM-DD
#                                 put that backup back as the `data` branch
#
# Backups: the first save of each UTC day also tags its snapshot
# backup-YYYY-MM-DD, so an older copy survives a bug that damages the data.
# Every daily backup is kept for 30 days; after that only the first backup
# of each month is kept, permanently.
#
# Locally, `load` is all you need before `python run.py ...`.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
BRANCH=data
MARK=.data_loaded_sha     # which snapshot this run started from (for save's lease)
KEEP_DAYS=30

days_before() {   # YYYY-MM-DD, N -> the date N days earlier (GNU or BSD date)
  date -u -d "$1 -$2 days" +%F 2>/dev/null || date -u -j -v-"$2"d -f %F "$1" +%F
}

backup() {
  commit=$1
  day=${BACKUP_DAY:-$(date -u +%F)}
  if git ls-remote --exit-code --tags origin "refs/tags/backup-$day" >/dev/null; then
    return 0                                   # today's backup already made
  fi
  git push -q origin "$commit:refs/tags/backup-$day"
  echo "Made daily backup backup-$day"
  # Past KEEP_DAYS, keep only the first backup of each month.
  cutoff=$(days_before "$day" "$KEEP_DAYS")
  seen_months=" "
  for tag in $(git ls-remote --tags origin 'refs/tags/backup-*' | awk '{print $2}' | sed 's#refs/tags/##' | sort); do
    d=${tag#backup-}
    month=${d:0:7}
    if [[ "$seen_months" != *" $month "* ]]; then
      seen_months="$seen_months$month "
      continue                                 # the month's first backup: kept for good
    fi
    if [[ "$d" < "$cutoff" ]]; then
      git push -q origin ":refs/tags/$tag" && echo "Dropped old daily backup $tag (monthly one kept)"
    fi
  done
}

load() {
  git fetch -q --depth=1 origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH"
  sha=$(git rev-parse "refs/remotes/origin/$BRANCH")
  rm -f data/archive/*.db.gz          # archives the snapshot no longer has (merged away)
  git archive "$sha" | tar -x
  echo "$sha" > "$MARK"
  echo "Loaded data snapshot ${sha:0:7}: $(git log -1 --format=%s "$sha")"
}

save() {
  [ -f "$MARK" ] || { echo "::error::no $MARK -- run '$0 load' first"; exit 1; }
  prev=$(cat "$MARK")
  files=(data/zoom.db.gz data/archive/*.db.gz docs/index.html)

  # Size guard: GitHub rejects any file over 100 MiB (the raw DB hit that in
  # Sept 2026 and every push failed for weeks). Fail loudly, warn early.
  for f in "${files[@]}"; do
    size=$(stat -c %s "$f" 2>/dev/null || stat -f %z "$f")
    mb=$((size / 1048576))
    if [ "$size" -ge $((90 * 1048576)) ]; then
      echo "::error file=$f::$f is ${mb} MiB -- GitHub rejects files over 100 MiB. Archive/split it (see db/archive.py)."
      exit 1
    elif [ "$size" -ge $((70 * 1048576)) ]; then
      echo "::warning file=$f::$f is ${mb} MiB, approaching GitHub's 100 MiB per-file limit."
    fi
  done

  # Build the snapshot commit in a throwaway index: main's own index and
  # history are never touched.
  index=$(mktemp -u)
  GIT_INDEX_FILE="$index" git add -f "${files[@]}"
  tree=$(GIT_INDEX_FILE="$index" git write-tree)
  rm -f "$index"
  if [ "$tree" = "$(git rev-parse "$prev^{tree}")" ]; then
    echo "No changes to save"
    return 0
  fi
  commit=$(git commit-tree "$tree" -m "Data snapshot: $(date -u +'%Y-%m-%d %H:%M UTC')")

  # Replace the branch -- but only if it is still the snapshot this run
  # loaded. If anything else replaced it meanwhile (a manual data fix), fail
  # rather than wipe that change; the next run starts from it.
  git push -q --force-with-lease="refs/heads/$BRANCH:$prev" origin "$commit:refs/heads/$BRANCH"
  echo "$commit" > "$MARK"
  echo "Saved data snapshot ${commit:0:7}"
  backup "$commit" || echo "::warning::Daily backup failed (the data itself was saved)."
}

list() {
  git ls-remote --tags origin 'refs/tags/backup-*' | awk '{print substr($1,1,7), $2}' | sed 's#refs/tags/##' | sort -k2
}

restore() {
  tag=${1:?usage: $0 restore backup-YYYY-MM-DD}
  git fetch -q --depth=1 origin "+refs/tags/$tag:refs/tags/$tag"
  cur=$(git ls-remote origin "refs/heads/$BRANCH" | cut -f1)
  git push -q --force-with-lease="refs/heads/$BRANCH:$cur" origin "refs/tags/$tag^{commit}:refs/heads/$BRANCH"
  echo "The data branch is now $tag ($(git rev-parse --short "refs/tags/$tag^{commit}")); it was ${cur:0:7}."
  echo "Note: data collected after $tag is not in it -- results re-collect, older odds may not."
}

case "${1:-}" in
  load) load ;;
  save) save ;;
  list) list ;;
  restore) restore "${2:-}" ;;
  *) echo "usage: $0 load|save|list|restore <backup-tag>"; exit 2 ;;
esac
