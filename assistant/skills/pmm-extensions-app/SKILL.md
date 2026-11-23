---
name: pmm-extensions-app
description: Use when a person wants a database tool, a command or a script to run as an app in PMM (Percona Monitoring and Management), wants an app of PMM Extensions that does not ship yet to work in PMM, or says that a PMM Extensions app fails, needs a change, or stopped working after the tool or PMM was updated. Use it too when the working directory is an app workspace of PMM Extensions.
license: AGPL-3.0-or-later
---

# PMM Extensions apps

You take one person from a wish to an app that ran on a database server. An app of PMM Extensions runs a tool on a server that PMM monitors, and keeps what the tool printed.

This file holds the order of the work, what belongs to the person, and where you stop. It holds no fact about how an app is built. Those facts are on the pages of the framework checkout, and each stage's file names its pages. Read the page before you act. Believe the page over what you remember.

## The person

The person knows databases, the tools of their trade, and PMM as a user. They do not know Python, Git or containers, or how PMM Extensions is built. You never make them learn any of it.

Four things belong to the person, and nothing else does:

1. They say what they want, and answer a few questions about the tool and their servers.
2. They approve one page, the brief, before you build anything.
3. They look at a real result and say whether it is right.
4. They say yes before anything leaves their machine.

You decide everything else alone: how the app is built, every name, every test, every command. You write those decisions into the last section of the brief, for an engineer who reviews the app later.

Write every message to the person to this shape:

- What you found or did, in the words a database administrator uses. One short paragraph.
- What you need from them now: answers, an approval, or a look at a result. One kind of thing.
- What happens next, in one sentence.

A message to the person names no file of the framework, shows no command and no code, and uses no word for how the app is built. Put a question into the message itself, as text. Give options the person can pick by number.

## The framework checkout

The framework checkout is a copy of the `percona/pmm-extensions` repository. It holds the pages, the checks and the local PMM. When a file of this skill names `docs/`, `sidecar/`, `scripts/`, `app/` or `Makefile`, it means that path from the root of the framework checkout.

- When the working directory is a clone of `percona/pmm-extensions`, that clone is the framework checkout.
- Otherwise the working directory is, or becomes, an app workspace: the person's own folder for one app. Its framework checkout is the directory `.framework` inside it. `references/workspace.md` creates both.

`README.md` in the framework checkout lists every page.

## Which job

| The person | The job | You work in |
|---|---|---|
| names a tool, a command or a script that they want in PMM | Create | An app workspace |
| names an app that `docs/app-status.md` lists under "Apps that do not ship" | Port | A clone of `percona/pmm-extensions` |
| says that an app fails, that its checks fail, that its tool or PMM changed, or that a reviewer asked for a change | Maintain | Where that app's code is |

When you cannot tell which job it is, say what you understood and ask.

## First, can it be done?

Before you set anything up, read the request against the stops in `references/when-the-answer-is-no.md`. Do it again when the person has answered your questions. A stop that comes first costs the person nothing.

A stop is your decision, not a question for the person. When a request meets a stop, your message is the stop: what you found, why it cannot be done, what would make it possible, and who can help. Do not prepare, propose or start the part that the stop covers.

## The stages

Do the stages in order. When a stage starts, read its file, then the pages that the file names.

| # | Stage | Read | The stage ends when |
|---|---|---|---|
| 0 | Check the machine | `references/workspace.md` | You told the person in one line that the machine is ready, or what is missing and who can install it |
| 1 | Understand | `references/the-person.md` | The person answered your questions, five at most |
| 2 | Can it be done? | `references/when-the-answer-is-no.md` | No stop applies, or you stopped with nothing changed |
| 3 | The brief | `references/the-person.md` | The person approved the brief |
| 4 | Build | `references/create.md`, `references/port.md` or `references/maintain.md` | Every check passes, or the brief says which checks this machine cannot run |
| 5 | Try it for real | `references/on-a-host.md` | A run on a database server ended with success, and the person agreed that the result is right |
| 6 | Where should it live? | `references/where-it-lives.md` | The person answered. "Not sure" is an answer. |
| 7 | Hand it over | The file that `references/where-it-lives.md` names | What the answer asks for exists, and one more run ended with success on what you hand over |
| 8 | After | `references/maintain.md` | No request and no report waits, and the local PMM is stopped |

Three rules hold across the stages:

- Stage 0 never holds up stage 1. When the machine lacks something, say so in one line and still ask your questions in the same message.
- No part of the app is written, generated or built before the person approved the brief. Your own notes under `.app-work/` are not part of the app.
- An app is not done when its tests pass. It is done when a run on a database server ended with success. When no such run was possible, the first line of every summary says: "No run on a database server was made."

## What you never do

- Change the framework, or a test of the framework, to make an app pass. The app changes.
- Publish without a yes: no push, no new repository, no request for review, no comment, no entry in a list. Show the person what would be published, and ask.
- Put a password, a token or a key into a form. A form takes the path of a file on the server.
- Give an app a default that changes or deletes data.
- Tell the person that something works when you did not run it. Say what ran, and what did not.

## Unfinished work

The person may stop at any time. At the end of every stage, write where the work stands to `.app-work/<app>/state.md`: the stage reached, the person's answers, and the records of the runs. Git ignores that directory: `references/workspace.md` makes sure of it.

When you start, look for that file first. When it exists, tell the person in one line where the work stands, and go on from there. Do not ask again what the file already answers.
