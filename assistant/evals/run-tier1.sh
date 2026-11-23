#!/usr/bin/env bash
# Runs the tier 1 cases with `claude plugin eval`.
#
# `claude plugin eval` wants the cases inside the plugin that they test, and the
# skill is tracked in one place, .agents/skills/. So this script builds the plugin
# first, in a directory that Git ignores: the manifest, a copy of the skill, and a
# copy of the cases.
#
# Usage: run-tier1.sh [--build-only] [arguments of `claude plugin eval` ...]
#   --build-only   build the plugin, print its directory, and run nothing
# Results go to assistant/evals/results/<time>/ unless --output-dir says otherwise.
set -o errexit -o nounset -o pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
build="$root/assistant/tier1-plugin"

rm -rf "$build"
mkdir -p "$build/evals"
cp -r "$root/.agents/.claude-plugin" "$root/.agents/skills" "$build/"
for entry in "$here"/*/; do
    name="$(basename "$entry")"
    case "$name" in
        results | outcome | static) continue ;;
    esac
    cp -r "$entry" "$build/evals/$name"
done

if [[ ${1:-} == --build-only ]]; then
    echo "$build"
    exit 0
fi
cd "$root"
exec claude plugin eval "$build" --output-dir "$here/results/$(date +%Y%m%d-%H%M%S)" "$@"
