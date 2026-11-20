#!/usr/bin/env bash
# fb_tracker's database lives on its own `fb-data` branch (one snapshot commit,
# replaced on every save) -- never on main, and separate from the Zoom `data` branch.
#
#   fb_tracker/data_branch.sh load   fetch it into fb_tracker/data/fb.db (fresh start if none yet)
#   fb_tracker/data_branch.sh save   replace the `fb-data` branch with the current database
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
BRANCH=fb-data
DB=fb_tracker/data/fb.db
MARK=fb_tracker/data/.loaded_sha

load() {
  mkdir -p fb_tracker/data
  if git fetch -q --depth=1 origin "+refs/heads/$BRANCH:refs/remotes/origin/$BRANCH" 2>/dev/null; then
    sha=$(git rev-parse "refs/remotes/origin/$BRANCH")
    git show "$sha:$DB.gz" | gunzip > "$DB"
    echo "$sha" > "$MARK"
    echo "Loaded fb-data ${sha:0:7}: $(git log -1 --format=%s "$sha")"
  else
    : > "$MARK"
    echo "No fb-data branch yet: starting a fresh database"
  fi
}

save() {
  [ -f "$MARK" ] || { echo "::error::run '$0 load' first"; exit 1; }
  prev=$(cat "$MARK")
  gzip -kf "$DB"
  index=$(mktemp -u)
  GIT_INDEX_FILE="$index" git add -f "$DB.gz"
  tree=$(GIT_INDEX_FILE="$index" git write-tree)
  rm -f "$index"
  commit=$(git commit-tree "$tree" -m "fb_tracker snapshot: $(date -u +'%Y-%m-%d %H:%M UTC')")
  if [ -n "$prev" ]; then
    git push -q --force-with-lease="refs/heads/$BRANCH:$prev" origin "$commit:refs/heads/$BRANCH"
  else
    git push -q origin "$commit:refs/heads/$BRANCH"
  fi
  echo "$commit" > "$MARK"
  echo "Saved fb-data ${commit:0:7}"
}

case "${1:-}" in
  load) load ;;
  save) save ;;
  *) echo "usage: $0 load|save"; exit 2 ;;
esac
