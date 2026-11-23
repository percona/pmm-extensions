# Build an app with an assistant

This page shows how to get an app for PMM with an AI coding assistant. It is for a person who knows a database tool and does not write Python. You say what you need. The assistant asks a few questions, builds the app, and runs it on a test database server on your machine. You judge the result.

An app runs a tool on the database servers that PMM monitors, and keeps what the tool printed. The assistant works from a skill in this repository, `pmm-extensions-app`, which tells it the order of the work, what to ask you, and where to stop.

An app runs inside PMM Extensions and has the same reach: see the second paragraph of [Run an app outside the repository](run-an-app-outside-the-repository.md). The assistant tells you what your app can reach before it builds anything that could leave your machine.

## Prerequisites

- An AI coding assistant that reads skills in the Agent Skills format, signed in. [What was run](#what-was-run) lists what was tested.
- Git.
- The GitHub CLI with the `skill` command. `gh skill --help` prints its usage when you have it.
- Docker with Compose, and what else [Run PMM Extensions in a local PMM](run-in-local-pmm.md#prerequisites) lists. The assistant runs your app on a test PMM there.

When your machine lacks one of these, ask the person who administers it. The assistant tells you what is missing, and does not install it.

## Start a new app

Use this start when you want a tool, a command or a script of yours to run from PMM.

1. Create an empty directory for the app, and change to it:

   ```bash
   mkdir duplicate-indexes
   cd duplicate-indexes
   ```

2. Get this repository into the directory `.framework`. The assistant reads its pages and uses its tools:

   ```bash
   git clone https://github.com/percona/pmm-extensions .framework
   ```

3. Install the skill for your assistant. For Claude Code:

   ```bash
   gh skill install .framework pmm-extensions-app --from-local --agent claude-code
   ```

   For Cursor, give `--agent cursor`. `gh skill install --help` lists the other assistants.

4. Start the assistant in the directory, and say what you need in your own words. For example:

   ```text
   I want to run pt-duplicate-key-checker on a MySQL server from PMM.
   ```

## Start in a clone of this repository

Use this start for an app that this repository already holds. That is an app that [App status](../app-status.md) lists under "Apps that do not ship", or an app that needs a repair.

1. Clone the repository, and change to the clone.

2. For Claude Code, install the skill from the clone:

   ```bash
   gh skill install . pmm-extensions-app --from-local --agent claude-code
   ```

   Cursor and the other assistants that read `.agents/skills/` find the skill in the clone. They need no command.

3. Start the assistant in the clone, and say what you need.

## What you are asked

The assistant asks you for four things, and decides everything else:

| When | What you do |
|---|---|
| At the start | You answer up to five questions about the tool and your servers. Each has options and one recommendation. "I don't know" is an answer. |
| Before anything is built | You read one page, the brief, and approve it or change it. It says what the app does, what its form asks, what a database server must have, and what the tool touches. |
| After the first run | You look at what the tool printed on the test server, and say whether it is what you expect. |
| Before anything leaves your machine | You say yes or no. Without a yes, nothing is published. |

When the assistant uses a word that you do not know, say so. It has to explain in the words of your work.

## Where the app can live

After the first run that you agree with, the assistant asks where the app should live. You can keep it for your own use or for one customer. You then get an image that holds PMM Extensions and your app, and one page for the person who runs the PMM. You can offer it to other PMM users, from a public repository of your own. You can propose it for PMM itself, as a pull request to this repository that its maintainers review.

"I am not sure" means the first. You can choose another one later, and nothing is lost.

An app that you keep or offer has no page in PMM's UI today. You run it with one command, which the assistant gives you.

## What the assistant does not do

The assistant stops, and says why, when a request needs one of these:

- A tool that needs a person at a terminal, or that never ends.
- A password, a token or a key in the app's form. A form takes the path of a file on the server.
- A default that changes or deletes data.
- A table of the app's own, a new library, or a change to the framework.
- Pages of the app's own in PMM's UI.
- A decision that belongs to the PMM team.

It also does not create a ticket for you. It does not tell you that an app works when no run on a database server was made.

## Pick the work up later

You can stop at any time. The assistant writes where the work stands into the directory `.app-work/` after each stage.

1. Start the assistant in the same directory.
2. Say that you want to go on.

The assistant tells you in one line where the work stands, and continues from there.

## What was run

| Assistant | Model | What was run |
|---|---|---|
| Claude Code | Claude Sonnet | A new app, a port and a repair, each from the request to a run on a database server |
| Claude Code | Claude Opus | A new app and a port, each from the request to a run on a database server |
| Cursor | | Not run. The skill uses no feature of one assistant, and the installer has a target for Cursor. |

## Verify

The assistant shows you the output of your tool from a database server of the test PMM, and the run has the status `success`. For an app that you keep, your directory also holds `FOR-THE-PMM-OWNER.md`.

## Next steps

- [Run an app outside the repository](run-an-app-outside-the-repository.md), for what the assistant does with an app that you keep
- [App status](../app-status.md), for the apps of this repository that do not ship yet
- [Ownership of the framework and the apps](../explanation/ownership.md)

When something on this route was hard, open an issue on GitHub and say what it was.
