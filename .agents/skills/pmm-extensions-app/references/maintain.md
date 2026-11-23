# Maintain: an app that exists and needs a change

An app needs care after it works: a user reports a failure, the checks fail after an update, the tool changes, PMM is upgraded, a reviewer asks for a change. You work where the app is: in its workspace, or in a clone of `percona/pmm-extensions` for an app of the repository.

## Before you change anything

1. Read the app's `README.md`. An app of the repository that has none has an entry on `docs/app-status.md`.
2. Run every check for the app, with the one command of `docs/how-to/test-an-app.md`, "Run every check for one app". Write down which suites pass now. A suite that fails before your change is a finding, not your fault and not yours to hide.
3. Ask the person only for what you cannot find: what the user did, what they saw, and the output of the failed run.

## Find where the fault is

A failure has one of three homes. Decide which one before you write a fix, and say it to the person in one sentence.

| The fault is in | You see | What you do |
|---|---|---|
| The database server | The same request works on the test server of the local PMM. The server of the report lacks the tool, the login file, a directory or a right. | Change nothing. Say what the server needs. Add it to "What a database server must have" when the brief did not say it. |
| The app | The failure happens again on the local PMM, with the values of the report. | The repair below. |
| The framework | The failing code is under `app/extensions/apps/framework/`, or a check of the framework fails for every app. | Change nothing in the framework. Write the report for its maintainers to `.app-work/<app>/framework-report.md`: what fails, the command that shows it, and what you expected. Tell the person. Sending it is publishing, and needs a yes. |

## The repair

The order is fixed. Each step proves the one before it.

1. **See it fail.** Make the failure happen yourself: the run on the local PMM with the values of the report (`on-a-host.md`), or the failing check.
2. **Write a test that fails for the same reason.** It goes with the app's tests. Run it, and read that it fails with the error of the report, not with a mistake of the test.
3. **Change the app.** The smallest change that makes the test pass. Not the framework, and not a test of the framework.
4. **Run every check** for the app again.
5. **Run the app on a server** again, as in step 1. The repair is done when that run ends with success. Tests that pass are not the proof.
6. Add the run to "What was tried, and what was not" in the brief.

When you cannot make the failure happen, say so. Do not change code for a failure that you have not seen.

When this machine cannot build the virtualenv, steps 2 and 4 cannot run here. Say so in one sentence. Steps 1 and 5 still run: the local PMM needs no virtualenv. A repair with no run on a server is not done, and you do not ask the person to try it on a server of theirs in your place.

## What starts the work

| The person says | Start with |
|---|---|
| "A user reported that it fails." | The output of the failed run. Then "Find where the fault is". |
| "The checks fail since the last update." | The failing suite's log, and what changed in the framework since the app last passed: `CHANGELOG.md`, and the history of `app/extensions/apps/framework/`. The app follows the framework. Never the other way round. |
| "The tool has a new version." | The tool's help text on the server, beside the options that the app sends. Change the form and the command. An app of the repository also needs its snapshots renewed (`docs/how-to/update-api-snapshots.md`) and a changelog fragment (`CONTRIBUTING.md`). |
| "PMM was upgraded." | For an app that the person keeps: "Upgrade" of `docs/how-to/run-an-app-outside-the-repository.md`. When the build stops at the step that loads the apps, that message is the report: go to the repair. Then run the app once from the new image, add the PMM version to `ran-on` in `app.yaml`, and renew `FOR-THE-PMM-OWNER.md`. |
| "The reviewer asked for changes." | Read every comment. For each one: make the change with the repair above, or tell the person in plain words why not. An answer to the reviewer and a push are publishing: show them, and ask. |

## When it is done

Save the work first, and do not ask about it: how the work is saved is yours to decide. In a clone, save it on a branch of its own, as `CONTRIBUTING.md` says.

Then tell the person, in the words of their own work: what was wrong, what you changed, which run proves it, and what you did not try. "I saved the change on this machine" is all that the person hears about how it is kept. The message names no file and no command, and asks nothing about how the change is stored, named or described.

One thing is the person's: whether the change leaves the machine. A change to an app that is already handed over needs the hand-over again, with the file that `where-it-lives.md` names for where the app lives. For an app of the repository that is `hand-over-core.md`, which prepares everything and asks once, before it publishes.
