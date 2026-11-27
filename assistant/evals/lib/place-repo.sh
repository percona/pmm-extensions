#!/usr/bin/env bash
# Places a copy of this repository's pages, tools and apps into a tier 1 run.
# The copy leaves out the skill and assistant/, so that the arm without the
# skill has the pages only.
# Usage: place-repo.sh workspace   -> the copy is ./.framework, as in an app workspace
#        place-repo.sh clone       -> the copy is the working directory, as in a clone
set -o errexit -o nounset -o pipefail
root="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
case "${1:-}" in
    workspace) target=.framework ;;
    clone) target=. ;;
    *)
        echo "usage: place-repo.sh workspace|clone" >&2
        exit 2
        ;;
esac
mkdir -p "$target"
# Only files in Git's index: a contributor's own untracked files stay out of the run.
git -C "$root" ls-files -z --cached -- README.md CONTRIBUTING.md \
    Makefile settings.yaml docs sidecar scripts app/extensions tests/app/extensions/apps |
    tar --directory="$root" --null --files-from=- --create --file=- |
    tar --extract --file=- --directory="$target"
git -C "$target" init --quiet
git -C "$target" remote add origin https://github.com/percona/pmm-extensions.git
