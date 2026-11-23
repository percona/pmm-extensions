# Files for AI assistants

This directory holds what an AI assistant needs to build, port and maintain a PMM Extensions app with a person who knows databases and not Python. The page for that person is [Build an app with an assistant](../docs/how-to/build-an-app-with-an-assistant.md).

| Path | Holds |
|---|---|
| `evals/` | The skill's tests. |

The skill itself is in `.agents/skills/pmm-extensions-app/`, where assistants read skills: `SKILL.md`, the stage files under `references/`, and the files of a new app workspace under `assets/`. `.claude/skills/pmm-extensions-app` is a link to it, for Claude Code. `.agents/.claude-plugin/plugin.json` is a plugin manifest, and `.claude-plugin/marketplace.json` lists that plugin.

The skill states the order of the work, what to ask the person, and where to stop. It states no fact about the framework: each fact is on a page under `docs/`, and the skill names the page. When a page lacks a fact, change the page.

## Install the skill

An assistant that starts in a clone of this repository reads the skill from the clone. Nothing is installed.

For work in another directory, one command copies the skill to where an assistant reads it. Run it in that directory, with a clone of this repository as the source:

```bash
gh skill install <clone> pmm-extensions-app --from-local --allow-hidden-dirs --agent claude-code
```

Use `--agent cursor` for Cursor. The command needs `--allow-hidden-dirs` because the skill is in a directory whose name starts with a dot.

Claude Code can also install the skill as a plugin, from the marketplace file of a clone:

```bash
claude plugin marketplace add <clone> --scope project
claude plugin install pmm-extensions@percona-pmm-extensions --scope project
```

## Run the tests

The tests have three tiers. Change a file of the skill only after you saw the test for that change fail.

### Tier 0: no model

It checks the skill against the Agent Skills specification and installs it with `gh skill install`. A second module checks every path that a file here names.

```bash
pytest assistant/evals/static tests/docs/test_assistant_references.py -q
```

### Tier 1: what the assistant decides

Each directory under `evals/` with a `prompt.md` is one case: one request, and graders for the first answer. `claude plugin eval` runs each case with the skill and without it. It wants the cases inside the plugin that they test, so `evals/run-tier1.sh` first builds that plugin in `assistant/tier1-plugin/`, which Git ignores: the manifest, a copy of the skill and a copy of the cases. The script passes its arguments on. The cases need Claude Code 2.1.269 or later and a signed-in account. The runs use that account.

```bash
assistant/evals/run-tier1.sh --runs 3 --model sonnet --trust-plugin --no-publish \
  --scaffold --allow-tools Read,Glob,Grep,Skill,Bash
```

`--scaffold` runs each case's `place-files.sh`, which copies this repository's pages into the run's directory. Add `--case <name>` for one case.

### Tier 2: what the assistant delivers

`evals/outcome/run.sh` starts a headless Claude Code session in a throwaway directory, with a scripted person who answers its questions. `scripts/grade_app_skill_run.py` then grades what the session left: it runs the checks itself, and starts a local PMM to run one task of the app. A run needs Docker and what [Run PMM Extensions in a local PMM](../docs/how-to/run-in-local-pmm.md) lists. It can take 90 minutes.

```bash
assistant/evals/outcome/run.sh C1 skill 1
python scripts/grade_app_skill_run.py C1 assistant/evals/outcome/runs/C1-skill-1
```

Run the grader with the project's virtualenv active. The header of each file lists its arguments and variables.

| Scenario | The request | Works in |
|---|---|---|
| `C1` | A new app for a tool, kept private | An empty directory |
| `P1` | An app of this repository that does not ship | A clone |
| `M1` | An app that fails on a host, with the log | A clone with a seeded defect |
| `M2` | An app whose checks fail after a change to the framework | A clone with a seeded change |
| `K1` | An app that works, proposed for PMM | The workspace of a `C1` run, given in `FROM_RUN` |

A session cannot publish anything. It has no GitHub sign-in, and Git sends every `github.com` address to a directory on your machine.
