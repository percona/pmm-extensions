# Port: an app of the repository that does not ship

A port makes an app that is already in the framework checkout run inside PMM. You work in a clone of `percona/pmm-extensions`, not in an app workspace. The app goes to PMM itself, so you do not ask where it should live: after stage 5 you go to `hand-over-core.md`.

That the app's code and its tests exist proves nothing about PMM. An app works in PMM when PMM Extensions serves it there and one run ended with success on a database server. Until you have seen both, do not tell the person that the app works, and do not describe a page of PMM that you have not seen.

## Before the brief

1. Read the app's entry on `docs/app-status.md`, and the table under "How the cost is judged". For a cost of M or L, for "after a decision" and for "Not a candidate", stop: `when-the-answer-is-no.md` has the words.
2. Make the three checks of `docs/how-to/port-an-app.md`, "Check what the app depends on": its pages, its tables, its tool. The tool must be on a test server of the local PMM, which `docs/how-to/run-in-local-pmm.md` lists under "Add a database host".
3. Read what the app's form asks and what the app sends to the tool. Set it beside what the person asked for. When the two differ, the brief says so, and the person chooses: the app as it is now, or a change as a second step. The second step is the maintain job, after the port.

A port keeps the app's form and its defaults as they are. You offer no change to them in your questions, and you make none while you port. What the tool writes by itself, such as a table of its own results, goes into "What the tool touches". When a default of the app changes or deletes the data of the server, that is a stop: `when-the-answer-is-no.md`.

## The brief

The app has no `README.md`. Write it from the app's code and from its entry on the status page, with the sections of `the-person.md`. "What a database server must have" and "What the tool touches" come from the tool's own help text and from the app's command, not from memory.

## Build

Follow `docs/how-to/port-an-app.md` in its own order: "Activate the app", "Update the test that pins the list", "Run the tests". Then run every check for the app with the one command of `docs/how-to/test-an-app.md`, "Run every check for one app". The virtualenv that the checks need is built as `docs/how-to/set-up-a-development-environment.md` says.

- Change what makes the app run in PMM, and nothing else. A port does not improve the app.
- Keep the work on a branch of its own, and save it there after the checks pass. `CONTRIBUTING.md` says how a branch and a saved change are named.

## Try it for real

Follow `on-a-host.md`, with the clone's code, as `docs/how-to/port-an-app.md` does under "Run the app in a local PMM".

- Write the request for the run from the form that the running app reports, not from the app's code: `on-a-host.md` names the command.
- Expect the first run to fail. `docs/app-status.md` says why under "How the cost is judged". That failure is the work of a port, not an obstacle to it: read the run's output, find the cause, and repair the app as `maintain.md` says, with a test first.
- Write into "What was tried, and what was not" what the run proves, and what it cannot prove on this test server.
