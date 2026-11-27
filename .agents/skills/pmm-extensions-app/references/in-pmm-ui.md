# The app in PMM's menu

Whether PMM shows an app depends on the PMM version, so check before you say anything about it. Never describe a page of PMM that nobody has seen.

## 1. Check what this PMM does

The local PMM runs the app. Ask the person to look, or look yourself when you can open a web page:

> Please open `https://127.0.0.1:<port>` in your browser, sign in as `admin` with the password `<password>`, and open **Apps** in the menu on the left. Is "<the app's name>" in the list?

The port and the password are in `.app-work/local-pmm.md`.

- **Yes.** PMM draws the app by itself. Ask the person to open it and to start one run from the form. That run is the check of this stage, and nothing below applies.
- **No.** Go on with the part below that fits where the app lives.

## 2. An app that the person keeps, or offers to others

The app has no page in PMM. Say it in one sentence, and say that the app is started with one command, which `FOR-THE-PMM-OWNER.md` holds with a request that works. `docs/how-to/run-an-app-outside-the-repository.md` says the same under "Run the image".

Do not change PMM's UI for such an app: the change would have to ship in PMM itself.

## 3. An app that is proposed for PMM itself

PMM's UI is in another repository, `percona/pmm`, and the app needs a change there. `docs/how-to/add-an-app-to-the-pmm-ui.md` has every step.

- Read "Prerequisites" of that page first. When the machine lacks what it lists, do not install it. Tell the person that the change to PMM's UI is still open, and write it into the text of the pull request.
- When the machine has it, make the edits of the page, build, serve the build in the local PMM, and ask the person the question of step 1. The answer is the check.
- The change is a pull request in `percona/pmm`: a second thing to publish, with its own yes.
