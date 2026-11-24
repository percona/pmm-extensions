# Hand over: the person proposes the app for PMM itself

The app goes into `percona/pmm-extensions`, by a pull request that the maintainers review. It reaches users with a later PMM release. No statement about trust is needed: the review is the trust.

Nothing is published in this file before its last step.

## 1. A ticket number

Ask, in the words of `the-person.md`. With a number, use it as `CONTRIBUTING.md` says under "Branches". Without one, `when-the-answer-is-no.md` has the words: the person creates the ticket, or takes the way without a number that the same section of `CONTRIBUTING.md` describes. That way starts with something published, so it waits for step 7 like everything else. You never create a ticket. Prepare everything else, and leave open only what needs the number.

## 2. Where the work will go

Find out whether the person can push to the repository. When the GitHub CLI is on this machine and signed in, ask GitHub. This reads, and publishes nothing:

```bash
gh api repos/percona/pmm-extensions --jq .permissions.push
```

`true` means a branch in the repository. Anything else means a fork, as `CONTRIBUTING.md` describes. No answer means a fork too: the command is not there, it is not signed in, or it fails. Never install the GitHub CLI, and never sign in for the person. Tell the person in one sentence which one it is.

## 3. Put the app into a branch

For an app that was built in a workspace, the framework checkout now becomes the place of the work.

1. In the framework checkout, get the newest `main` and create the branch from it. `CONTRIBUTING.md` says how the branch is named.
2. Copy the app's package to `app/extensions/apps/<app>/` and its tests to `tests/app/extensions/apps/<app>/`. Copy, do not edit: the app that was tried is the app that is proposed.

   ```bash
   diff -r -x __pycache__ <workspace>/<app> app/extensions/apps/<app>
   ```

   The command prints nothing.

For an app that is already in a clone, as after a port, the branch exists. Go on.

## 4. What an app of the repository needs

1. Make the app one that ships: "Activate the app" and "Update the test that pins the list" of `docs/how-to/port-an-app.md`.
2. Create the app's snapshots and the generated files: "Accept a change or create the snapshots of a new app" of `docs/how-to/update-api-snapshots.md`.
3. Put the app under "What ships" on `docs/app-status.md`.
4. Add the changelog fragment: "Add the changelog fragment" of `docs/how-to/get-an-app-into-a-pmm-release.md`. It needs the ticket number. Without one, leave it open and say so.
5. Run every check, with the one command of `docs/how-to/test-an-app.md`, "Run every check for one app", in the form for an app of the repository. Every suite prints `passed`.
6. Save the work on the branch. `CONTRIBUTING.md` says how a saved change is named.

## 5. The image, and PMM's menu

1. Follow `docs/how-to/get-an-app-into-a-pmm-release.md` from "Build the image" to "Run the image in the local PMM". When the machine lacks what "Prerequisites" of that page lists, do not install it. Run the app from the branch instead, as "Run the code in your checkout" of `docs/how-to/run-in-local-pmm.md` shows, and write under "What was tried, and what was not" that the image was not built here. When that section's steps do not start PMM Extensions on this machine either, no run was made here: write that down in the same place.
2. Read `in-pmm-ui.md`.

## 6. The text of the pull request

Write it to `.app-work/<app>/pull-request.md`:

- **The title.** The ticket number, then what the app does.
- **What the app does.** Two or three sentences from the brief.
- **How it was tried.** Each run: the server, the PMM version, the status, and what the run proves. Then what was not tried, and why.
- **Decisions for a reviewer.** That section of the brief.

## 7. Ask, and only then publish

Show the person what would be published, in one message: where it goes, the branch's name, the list of changed files, and the text of the pull request. Ask whether to publish it.

- Without a yes, stop. Write into `.app-work/<app>/state.md` what is ready and what is open. Nothing has left the machine.
- After a yes, push the branch and open the pull request, as "Open the pull requests" of `docs/how-to/get-an-app-into-a-pmm-release.md` says. Give the person its address.

Publishing needs two things at this moment, and you install neither. The first is the right to push from this machine, to the repository or to the person's fork. When Git refuses the push, stop as `when-the-answer-is-no.md` says under "No ticket number, or no right to publish": say what Git answered and who can help. The branch and the text of the pull request stay on this machine. The second is a way to open the pull request. With the GitHub CLI signed in, open it yourself. Without it, push the branch, and give the person the address that GitHub prints for a new pull request and the text in `.app-work/<app>/pull-request.md` to paste there.

A pull request in a second repository is a second thing to publish. Show it and ask again.

After that, the app is in review. Each comment of a reviewer is a report: `maintain.md`.
