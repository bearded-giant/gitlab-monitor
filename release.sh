#!/bin/bash
set -euo pipefail
V=${1:?usage: ./release.sh <version>}
V="${V#v}"
sed -i '' "s/^version = \".*\"/version = \"$V\"/" pyproject.toml
git add -A
git diff --cached --quiet || git commit -m "bump to v$V"
git tag "v$V" && git push origin main "v$V"
