# Create: a new app, in its workspace

The person approved the brief. You now build what the brief says, in the app workspace. The app's package is `<workspace>/<app>/`, and its tests are `<workspace>/tests/`. Nothing of the app stays in the framework checkout.

## 1. The tool's real options

Before you write the form, read the tool's own help text on the test server of the local PMM, where the tool is installed: `docker compose ... exec <host> <tool> --help`, with the host that `docs/how-to/run-in-local-pmm.md` names under "Add a database host". Send only options that this text holds. When the brief names an option that the tool does not have, correct the brief and tell the person.

When no test server has the tool, use the tool's published documentation, and say in the brief that the options were not checked against the tool.

## 2. Generate the app, and move it out

Follow "Create the package of a new app" of `docs/how-to/run-an-app-outside-the-repository.md`, with the workspace as the directory of your own. Give the scaffolder every value, as `docs/reference/scaffolder.md` lists them. Never let it ask.

That section changes one file of the framework checkout and puts it back. Check it: `git -C .framework status --short` prints nothing.

The kind of app is your decision:

- A tool that is on the server, called with options: the `task` flavor with `run-command`. This is the usual case.
- A script that the person brings: read "Run modes" of the scaffolder page for the size that a script may have, and for where a longer one belongs.

## 3. The form and the command

Write the form from the brief, with `docs/reference/form-dsl.md` and the section "Describe the form" of `docs/tutorials/first-app.md`.

- Every option that the app sends is in the tool's help text.
- A field is safe by its default. The brief's default is the form's default.
- A list of fixed values is a type, not only a list to pick from: the form page says why under "`Choices` and `Option`".
- The command reaches the server that the person picks in the form. "Run modes" of the scaffolder page says where the builder finds it.
- Send what makes the tool end by itself and stay on the server: a count, a timeout, an option that keeps it from calling out. Say so in the brief.
- The tool's login comes from the file that the brief names, never from the form.

Copy the approved brief from `.app-work/<app>/brief.md` to `<app>/README.md` now, and add its last section, "Decisions for a reviewer".

## 4. Tests

`docs/how-to/test-an-app.md` says what the generated test covers and what it cannot know. Add what is yours:

- One test for each rule of the form.
- One test that builds the command for a filled form and compares it with the exact command line that you expect. This is the test that a later repair starts from.

## 5. Run every check

Build the virtualenv of the framework checkout as `workspace.md` says. Then run the one command of `docs/how-to/test-an-app.md`, "Run every check for one app", in the form for an app that is not in the repository.

Every suite must print `passed`. When one fails, read its log and change the app. Never change a file of the framework checkout to make a suite pass.

## 6. Save the work

Fill what you know in `app.yaml`: `key`, `name`, `does`, `database` and `needs-on-host`.

Save a copy of the work in the workspace:

```bash
git add --all
git commit --message "<what the app is, in one line>"
```

`git status --short` prints nothing afterwards, in the workspace and in the framework checkout. Write the stage into `.app-work/<app>/state.md`.
