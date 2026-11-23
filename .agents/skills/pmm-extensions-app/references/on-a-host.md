# Stage 5: try it for real

Tests do not run a tool on a server. This stage runs the app once on a database server of the local PMM, and the person judges the result. It is the stage where the person's knowledge counts most.

## 1. Make PMM Extensions serve the app

| The app is | Follow |
|---|---|
| in an app workspace | "Try the app by a mount" of `docs/how-to/run-an-app-outside-the-repository.md` |
| in a clone of `percona/pmm-extensions` | "Run the code in your checkout" of `docs/how-to/run-in-local-pmm.md` |

Use the port and the passwords that stage 0 wrote into `.app-work/local-pmm.md`. After each change to the app's code, restart the container as the page says, or the old code keeps running.

## 2. Give the tool something to find

A run that finds nothing proves little. Look at what the sample data of the test server gives the tool. When it gives nothing, create something small for the tool to find, in the sample database only, and write into the brief what you created.

A tool that changes or deletes data runs against that sample data only, and never with a value that points anywhere else.

## 3. Run it once

1. Read the fields of the form as the running app reports them, and write the request from them: "Run a task with one command" of `docs/how-to/run-in-local-pmm.md` has both commands. Do not write the request from the app's code.
2. Run the command. Keep its whole output in `.app-work/<app>/`.
3. Read the exit status and the record. Success is exit status 0, the status `success`, and a database server as the place where the tool ran.

## 4. When the first run fails

A first run on a real server often fails where every test passed. Read what the tool printed, not only the status.

| What you see | What it usually means |
|---|---|
| The tool cannot sign in | The tool does not find its login. The run is made by another user than the one whose file you had in mind. Find which file that user reads on the server. |
| "Permission denied" on a path, or "No such file" | A default path of the app exists for another user, or on your machine only. |
| The tool's own message about an unknown option | The app sends an option that this version of the tool does not have. Compare with the help text on the server. |
| The tool waits, and the run ends at the timeout | The tool wants a person, or tries to reach the internet. Send the option that stops it. |
| The command's exit status is 2 | The API refused the request, and the message is the API's own. The table of exit statuses on the page of step 3 says what each one means. When the app is not served, go back to step 1. |
| Another answer of the local PMM | "Troubleshooting" of `docs/how-to/run-in-local-pmm.md` |

Repair the app, not the server. A change on the test server that a real server would also need is a requirement: write it under "What a database server must have". Then run every check again, and run the app again. After the same failure three times, stop: `when-the-answer-is-no.md`.

## 5. The person judges

Show the person what the tool printed, as the tool printed it, and ask:

> This is what the app got from the test server. Is this what you expect from the tool?

When the person says no, find out what they expected, change the app, and run it again.

## 6. Write it down

Fill "What was tried, and what was not" in the brief:

- The run: the status, the server, the version of PMM, the date.
- What the run proves, in one or two plain sentences.
- What it does not prove on this test server. For example: the test server has no replica, so the run cannot show a difference between two servers.
- For an app in a workspace, which PMM Extensions the app met. The checks ran against the framework checkout: write the commit that `git -C .framework rev-parse --short HEAD` prints. The run on the server used the released PMM Extensions of the local PMM: write its version. Then say that a part of the app which this one run did not reach was not tried on the released version.

Add the PMM version to `ran-on` in `app.yaml`. Save a copy of the work, and write the stage into `.app-work/<app>/state.md`. Leave the local PMM running: stage 7 uses it. When all work is done, stop it as "Stop and remove" of the local PMM page says.
