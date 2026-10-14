#!/usr/bin/env bash
# Prints the next release tag (e.g. v1.2.0) derived from the Conventional
# Commit subjects/bodies since the latest v* tag reachable from HEAD, or
# nothing if no commit warrants a release.
#   feat!/fix!/... or "BREAKING CHANGE" -> major
#   feat                                -> minor
#   fix / perf                          -> patch
#   anything else (chore, docs, ci, ...) -> no release
set -euo pipefail

last=$(git describe --tags --abbrev=0 --match 'v[0-9]*.[0-9]*.[0-9]*' 2>/dev/null || echo v0.0.0)
range="HEAD"
[ "$last" != v0.0.0 ] && range="$last..HEAD"

bump=none
while IFS= read -r -d $'\x1e' msg; do
  msg=${msg#$'\n'}  # git emits a newline after each separator
  subject=${msg%%$'\n'*}
  if [[ $subject =~ ^[a-z]+(\([^\)]*\))?!: ]] || grep -q '^BREAKING[ -]CHANGE:' <<<"$msg"; then
    bump="major"; break
  elif [[ $subject =~ ^feat(\([^\)]*\))?: ]]; then
    bump="minor"
  elif [[ $subject =~ ^(fix|perf)(\([^\)]*\))?: ]] && [ "$bump" = none ]; then
    bump="patch"
  fi
done < <(git log --format='%B%x1e' "$range")

[ "$bump" = none ] && exit 0

IFS=. read -r major minor patch <<<"${last#v}"
case $bump in
  major) echo "v$((major + 1)).0.0" ;;
  minor) echo "v$major.$((minor + 1)).0" ;;
  patch) echo "v$major.$minor.$((patch + 1))" ;;
esac
