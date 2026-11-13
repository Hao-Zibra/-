#!/usr/bin/env bash
# Keeps scan history on its own branch, so the code branch's history stays clean.
#
#   data-branch.sh checkout <dir>   check the data branch out into <dir> (empty if the branch is new)
#   data-branch.sh save <dir>       commit whatever changed in <dir> and push it
#
# Run from inside a clone whose "origin" remote can be pushed to.
set -euo pipefail

usage="usage: data-branch.sh checkout|save <dir>"
branch="${DATA_BRANCH:-farewatch-data}"
command="${1:?$usage}"
dir="${2:?$usage}"

case "$command" in
  checkout)
    # ls-remote exits 2 when the branch does not exist yet; anything else non-zero is a real failure.
    status=0
    git ls-remote --exit-code --heads origin "$branch" >/dev/null || status=$?
    if [ "$status" -eq 0 ]; then
      git fetch --quiet --depth 1 origin "refs/heads/$branch"
      git worktree add --quiet -B "$branch" "$dir" FETCH_HEAD
    elif [ "$status" -eq 2 ]; then
      git worktree add --quiet --detach "$dir"
      git -C "$dir" checkout --quiet --orphan "$branch"
      git -C "$dir" rm -rf --quiet .
    else
      echo "data-branch.sh: cannot list branches on origin (git exit $status)" >&2
      exit "$status"
    fi
    ;;
  save)
    git -C "$dir" add --all
    if git -C "$dir" diff --cached --quiet; then
      echo "data branch: nothing new to save"
      exit 0
    fi
    git -C "$dir" \
      -c user.name="github-actions[bot]" \
      -c user.email="41898282+github-actions[bot]@users.noreply.github.com" \
      commit --quiet --message "scan $(date -u +%Y-%m-%dT%H:%MZ)"
    git -C "$dir" push --quiet origin "HEAD:refs/heads/$branch"
    ;;
  *)
    echo "$usage" >&2
    exit 2
    ;;
esac
