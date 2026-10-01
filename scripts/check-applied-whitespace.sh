#!/usr/bin/env bash
# Check actual patched source, independently of patch-artefact whitespace rules.
set -euo pipefail
src=${1:?usage: check-applied-whitespace.sh <source-repository>}
rules=blank-at-eol,blank-at-eof,space-before-tab
git -C "$src" -c core.whitespace="$rules" diff --cached --check
git -C "$src" -c core.whitespace="$rules" diff --check
