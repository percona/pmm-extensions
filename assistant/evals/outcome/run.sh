#!/usr/bin/env bash
# Runs one outcome scenario of the app skill: a headless assistant session in a
# throwaway directory, with a scripted person who answers its questions.
#
# Usage: run.sh <scenario> <arm> <n> [model]
#   scenario  a directory under scenarios/: C1, P1, M1, M2, K1
#   arm       skill (copied into a workspace as the page for the person says) or pages (no skill)
#   n         the number of the run
#   model     the assistant's model (default: sonnet)
# Environment:
#   OUT_DIR         the run's record (default: runs/<scenario>-<arm>-<n> beside this file)
#   WORK_ROOT       where the throwaway directories are made (default: /var/tmp/app-skill-runs)
#   FROM_RUN        K1 only: the record of a passed C1 run, whose workspace is copied
#   PMM_PORT        the port of the run's local PMM (default: 8450 + n)
#   PROJECT_PREFIX  the start of the run's Compose project name (default: app-skill)
#   PERSON_MODEL    the scripted person's model (default: sonnet)
#   MAX_TURNS, MAX_MINUTES   the limits of one run (default: 300 and 90)
#   CACHE_DIR       a directory for downloaded Python packages that runs share (default: none)
#   SCENARIOS_DIR   a directory of scenarios other than scenarios/ beside this file
#   NO_IMAGE_BUILD  when set, the session finds no Podman and no buildah, so it cannot start
#                   the long build of the release image (default: unset)
#
# The session sees a stranger's machine: a home directory of its own, none of
# your Claude Code settings, a Git identity of its own, no GitHub sign-in, and
# github.com redirected to a copy of this checkout. So a session cannot push,
# open a pull request or read a key of yours. It is not a sandbox: the session
# runs without permission prompts, reaches the network, and uses your Docker.
# The copy is HEAD plus what is staged: `git add` a change before you test it.
# Grade the run afterwards: scripts/grade_app_skill_run.py <scenario> <OUT_DIR>.
# The run's directory under WORK_ROOT stays until you remove it.
set -o nounset
set -o pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source_tree="$(cd "$here/../../.." && pwd)"
scenario="$1" arm="$2" n="$3" model="${4:-sonnet}"
facts="${SCENARIOS_DIR:-$here/scenarios}/$scenario"
out="${OUT_DIR:-$here/runs/$scenario-$arm-$n}"
run="${WORK_ROOT:-/var/tmp/app-skill-runs}/$scenario-$arm-$n"
kind="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["kind"])' "$facts/scenario.json")" || exit 2
[[ $kind == workspace ]] && dir="$run/skill-ws-$scenario-$n" || dir="$run/skill-$scenario-$n"
project="${PROJECT_PREFIX:-app-skill}-${scenario,,}-$arm-$n" port="${PMM_PORT:-$((8450 + n))}"
rm -rf "$run" && mkdir -p "$out" "$run/person" "$run/gh" "$run/no-network" "$run/home" "$run/tmp" || exit 2
: > "$out/stream.jsonl"
rm -f "$out/dialogue.md" "$out/grade.json" "$out/meta.json"

cat > "$run/gitconfig" << EOF
[user]
    name = Dana (test)
    email = dana@example.invalid
[init]
    defaultBranch = main
[url "$run/framework"]
    insteadOf = https://github.com/percona/pmm-extensions.git
    insteadOf = https://github.com/percona/pmm-extensions
    insteadOf = git@github.com:percona/pmm-extensions.git
    insteadOf = git@github.com:percona/pmm-extensions
[url "$run/no-network/"]
    insteadOf = https://github.com/
    insteadOf = git@github.com:
    insteadOf = ssh://git@github.com/
EOF
strangers_machine=(env -i "HOME=$run/home" "TMPDIR=$run/tmp" "USER=${USER:-}" "LANG=${LANG:-C.UTF-8}" "TERM=${TERM:-dumb}"
    "PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" "XDG_RUNTIME_DIR=${XDG_RUNTIME_DIR:-}"
    "DOCKER_HOST=${DOCKER_HOST:-}" "CLAUDE_CONFIG_DIR=${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
    "GIT_CONFIG_GLOBAL=$run/gitconfig" "GH_CONFIG_DIR=$run/gh" GH_TOKEN= GITHUB_TOKEN=
    "COMPOSE_PROJECT_NAME=$project" "PMM_PORT=$port")
if [[ -n ${NO_IMAGE_BUILD:-} ]]; then # a machine without Podman: no session starts the long build of the release image
    mkdir -p "$run/bin"
    for tool in podman buildah; do
        printf '#!/bin/sh\necho "%s: not installed on this machine" >&2\nexit 127\n' "$tool" > "$run/bin/$tool" && chmod +x "$run/bin/$tool"
    done
    strangers_machine[7]="PATH=$run/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
fi
[[ -n ${CACHE_DIR:-} ]] && strangers_machine+=("POETRY_CACHE_DIR=$CACHE_DIR/poetry" "PIP_CACHE_DIR=$CACHE_DIR/pip" POETRY_VIRTUALENVS_IN_PROJECT=true)
as_dana() { "${strangers_machine[@]}" "$@"; }

# The framework that the session clones is this checkout's HEAD plus its index, and the
# scenario's seed, as one commit: a seeded defect must not be findable in the log.
mkdir "$run/framework" && git -C "$source_tree" archive HEAD | tar -x -C "$run/framework" || exit 2
as_dana git -C "$run/framework" init --quiet --initial-branch main || exit 2
git -C "$source_tree" diff --cached --binary HEAD | (cd "$run/framework" && git apply --allow-empty) || exit 2
if [[ -f $facts/seed.patch ]]; then (cd "$run/framework" && git apply "$facts/seed.patch") || exit 2; fi
# No session finds its own test: the scenarios, the scripted person and the grader stay out of the copy.
rm -rf "$run/framework/assistant/evals" "$run/framework/scripts/grade_app_skill_run.py" \
    "$run/framework/tests/scripts/test_grade_app_skill_run.py"
as_dana git -C "$run/framework" add --all
if [[ $arm == pages ]]; then # the arm without the skill gets the pages only: no skill, and no line that points to it
    (cd "$run/framework" && rm -rf assistant .agents .claude .claude-plugin docs/how-to/build-an-app-with-an-assistant.md &&
        git grep -lI -e pmm-extensions-app -e build-an-app-with-an-assistant | xargs -r sed -i -e '/pmm-extensions-app/d' -e '/build-an-app-with-an-assistant/d') || exit 2
    as_dana git -C "$run/framework" add --all
fi
as_dana git -C "$run/framework" commit --quiet --message "The state of the checkout under test" || exit 2

request="$(cat "$facts/request.md")"
if [[ $kind == workspace ]]; then
    if [[ -n ${FROM_RUN:-} ]]; then
        cp -r "$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["dir"])' "$FROM_RUN/meta.json")" "$dir" || exit 2
    else
        mkdir -p "$dir"
    fi
    if [[ $arm == pages ]]; then
        rm -rf "$dir/.framework" # the copy of an earlier run brings its own
        as_dana git clone --quiet "$run/framework" "$dir/.framework" || exit 2
        request+=$'\n\n'"The PMM Extensions repository is cloned in the directory .framework here. Its README lists the documentation."
    fi
else
    as_dana git clone --quiet "file://$run/framework" "$dir" || exit 2
    git -C "$dir" remote set-url origin https://github.com/percona/pmm-extensions.git
fi
# A clone holds the skill where the assistant reads it. A workspace gets a copy, as the page for
# the person says, also over the copy of an earlier run: the skill under test is this checkout's.
if [[ $arm == skill && $kind == workspace ]]; then
    rm -rf "$dir/.claude/skills/pmm-extensions-app"
    mkdir -p "$dir/.claude/skills" || exit 2
    cp -R "$run/framework/.agents/skills/pmm-extensions-app" "$dir/.claude/skills/" || exit 2
fi
git -C "$dir" rev-parse HEAD > "$run/start-commit" 2> /dev/null || true

stamp() { while IFS= read -r line; do printf '%s\t%s\n' "$(date +%s)" "$line"; done >> "$out/stream.jsonl"; }
this_turn() { # the field $1 of the result event among the stream's lines after line $2
    tail -n "+$(($2 + 1))" "$out/stream.jsonl" | cut -f 2- | python3 -c '
import json, sys
last = {}
for line in sys.stdin:
    try:
        event = json.loads(line)
    except ValueError:
        continue
    if isinstance(event, dict) and event.get("type") == "result":
        last = event
print(last.get(sys.argv[1], ""))' "$1"
}
write_meta() { # the run's record, with the reason $1 for its end
    SCENARIO="$scenario" ARM="$arm" N="$n" MODEL="$model" KIND="$kind" RUN_DIR="$dir" PROJECT="$project" PORT="$port" \
        START_COMMIT="$(cat "$run/start-commit")" FRAMEWORK_COMMIT="$framework_commit" STARTED="$started" STOP="$1" \
        TURNS="$turns" python3 -c '
import json, os, sys, time
value = os.environ.get
json.dump({"scenario": value("SCENARIO"), "arm": value("ARM"), "n": int(value("N")), "model": value("MODEL"),
           "kind": value("KIND"), "dir": value("RUN_DIR"), "project": value("PROJECT"), "port": int(value("PORT")),
           "start_commit": value("START_COMMIT"), "framework_commit": value("FRAMEWORK_COMMIT"),
           "started": int(value("STARTED")), "ended": int(time.time()), "stop": value("STOP"),
           "turns": int(value("TURNS"))}, open(sys.argv[1], "w"), indent=2)' "$out/meta.json"
}
framework_commit="$(git -C "$run/framework" rev-parse HEAD)"
session="$(uuidgen)" started="$(date +%s)" turns=0 stop="" resume=(--session-id "$session")
prompt="$request"
write_meta interrupted
while [[ -z $stop ]]; do
    remaining=$((${MAX_MINUTES:-90} * 60 - ($(date +%s) - started)))
    ((remaining > 0)) || { stop="time" && break; }
    lines="$(wc -l < "$out/stream.jsonl")"
    (cd "$dir" && printf '%s' "$prompt" | as_dana timeout "$remaining" claude -p "${resume[@]}" --model "$model" \
        --output-format stream-json --verbose --setting-sources project \
        --permission-mode bypassPermissions --disallowedTools AskUserQuestion 2> "$out/session.err" | stamp)
    resume=(--resume "$session")
    said="$(this_turn result "$lines")" used="$(this_turn num_turns "$lines")"
    turns=$((turns + ${used:-0}))
    [[ $prompt == "Not yet. Stop here." ]] && stop=publish-refused
    [[ -z $said ]] && stop="${stop:-session-ended-without-a-message}"
    ((turns >= ${MAX_TURNS:-300})) && stop="${stop:-turns}"
    [[ -n $stop ]] && break
    printf '\n## The assistant\n\n%s\n' "$said" >> "$out/dialogue.md"
    prompt="$(cd "$run/person" && printf '%s\n\n%s' "$(cat "$out/dialogue.md")" "Answer the assistant's last message as Dana." |
        as_dana claude -p --model "${PERSON_MODEL:-sonnet}" --system-prompt "$(cat "$here/person.md" "$facts/person-facts.md")" \
            --tools "" --setting-sources project --no-session-persistence 2>> "$out/session.err")"
    printf '\n## Dana\n\n%s\n' "$prompt" >> "$out/dialogue.md"
    python3 -c 'import json, sys; print(json.dumps({"type": "person", "text": sys.argv[1]}))' "$prompt" | stamp
    [[ $prompt == DONE ]] && stop=ended
    [[ -z $prompt ]] && stop=person-gave-no-answer
done
write_meta "$stop"
echo "$scenario-$arm-$n: stopped ($stop) after $turns turns and $((($(date +%s) - started) / 60)) minutes; record in $out"
