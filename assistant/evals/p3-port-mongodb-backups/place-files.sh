#!/usr/bin/env bash
# Places this case's files into the run's working directory.
set -o errexit -o nounset -o pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bash "$here/../lib/place-repo.sh" clone
