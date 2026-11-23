# When the answer is no

A stop protects the person from an app that should not exist, or from work that you cannot finish. Check the request against this file before you set anything up, and again when the person has answered your questions.

At a stop, your message to the person has four parts, in plain words:

1. What you found.
2. Why it cannot be done.
3. What would make it possible.
4. Who can help.

Then offer what you can do, when the stop leaves something. Leave nothing half-made: no generated file, no changed list, no running container.

A stop is not a question. Do not ask the person to choose between ways to build what the stop forbids. Do not describe how it could be built anyway.

## The tool needs a person, or never ends

**You see it when:** the tool draws a screen and waits for keys, opens a prompt, or runs until someone stops it. `htop`, a database shell and an editor are examples. An app runs a tool once, with no one there, and keeps what the tool printed.

**Say:** the tool is made for a person at a keyboard, so it cannot be an app.

**It becomes possible when:** the tool has a way to print once and end, or another tool gives the same facts that way. Offer one, or ask what the person wants to see.

**Who can help:** nobody needs to. This is a property of the tool.

A tool that only needs an option to end, such as a count of rows, is not a stop. Send that option always, and say so in the brief.

## A password in the form

**You see it when:** the person asks for a field that takes a password, a token or a key, or the tool takes its secret as an option.

**Say:** an app's form never takes a password. What the form holds is kept with the run and sent to the database server.

**It becomes possible when:** the tool reads its login from a file on the database server. The form then takes the path of that file at most. `docs/how-to/run-in-local-pmm.md`, "Use your own database host", states the rule.

**Who can help:** whoever manages the database server can create the file.

## A default that changes data

**You see it when:** the person wants the app to drop, delete, repair or rewrite something as soon as it runs, with nothing chosen in the form.

**Say:** the app's default is to report, because a run that changes a database must be someone's choice each time.

**It becomes possible when:** the changing way is a choice in the form that is off by default. The brief says what it changes. Stage 5 runs it against sample data only. When the tool itself cannot make the change, the app only reports.

**Who can help:** nobody needs to.

## The app needs a table, a new library or a change to the framework

**You see it when:** the person wants the app to keep its own records between runs, or the tool cannot be expressed by a form and a command. The limits of a form that are known are in `docs/reference/form-dsl.md`. For a limit that is not there, try it in the app's own test and believe the framework's error.

**Say:** an app made this way has no table of its own, and it cannot change PMM Extensions itself. The history of every run is already kept, and the person can read it.

**It becomes possible when:** the PMM team changes the framework. `docs/explanation/ownership.md` says who owns what. Write the request as a short text that the person can send, and show it.

**Who can help:** the maintainers of PMM Extensions, through an issue in `percona/pmm-extensions`.

Offer the app without that part.

## The app needs pages of its own

**You see it when:** the person wants a graph, a chart or a page that is not a form, a list of runs and the result of one run. For an app of the repository, `docs/app-status.md` says "Written by hand" or gives the cost M or L.

**Say:** PMM draws an app's pages from its form. Pages written by hand have to be written again inside PMM's own UI, which is work for a frontend engineer in another project.

**It becomes possible when:** that engineer does the work in `percona/pmm`.

**Who can help:** the PMM team.

Do not activate such an app, and do not prepare the part that you could do. An app with no pages helps nobody.

## Someone has to decide first

**You see it when:** `docs/app-status.md` says "after a decision" or "Not a candidate" for the app, or the app does what a PMM feature already does.

**Say:** the app overlaps something PMM already has, and nobody has decided whether PMM wants both.

**It becomes possible when:** the person tells you that the decision was made, and by whom.

**Who can help:** the PMM team decides.

## No database server to try it on

**You see it when:** the app is for a database that the local PMM has no server for. `docs/how-to/run-in-local-pmm.md`, "Add a database host", lists the servers it has. The same holds when the machine cannot run the local PMM at all: its "Prerequisites" say what the machine needs, and where a PostgreSQL server does not start.

**Say it with your first questions:** the app can be built and checked, and it cannot be tried on a real server here. The work then ends at the checks.

**It becomes possible when:** the person has a server for it. The same page, "Use your own database host", says what such a server needs.

**Who can help:** whoever can give the person a test server.

Go on with the person's agreement. From then on, the first line of every summary is: "No run on a database server was made." The work ends after stage 4, with the app built, checked and saved. Stages 5 to 7 wait for a server: write that into `.app-work/<app>/state.md`.

## The same failure three times

**You see it when:** one step failed three times, each time after a change of yours.

**Say:** what ran, what failed, and that you stop here.

**It becomes possible when:** an engineer reads what you wrote down: the commands, the output and what you tried.

**Who can help:** the maintainers of PMM Extensions.

## No ticket number, or no right to publish

**You see it when:** the app is proposed for PMM itself, and the person has no ticket number or cannot publish to Percona's repositories.

**Say:** everything is prepared, and what is missing. `hand-over-core.md` prepares all of it, and publishes nothing.

**It becomes possible when:** the person creates the ticket in the PMM project, or asks for the right to publish. A copy of the repository under the person's own account always works.

**Who can help:** the person's team lead, or the PMM team.
