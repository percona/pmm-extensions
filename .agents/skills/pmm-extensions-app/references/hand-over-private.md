# Hand over: the person keeps the app

The person keeps the app for their own use, or for one customer. What you hand over is an image: the released PMM Extensions plus this app. Nothing leaves this machine.

Start only after the person said yes to the statement in `where-it-lives.md`.

## 1. Name the owner

Fill `maintainer` in `app.yaml` with the person's name. When you do not know it, take the name that Git has for this workspace, and say in your last message which name you wrote. `reviewed-by-percona` stays `false`.

## 2. Build the image

Follow "Build an image with the app" of `docs/how-to/run-an-app-outside-the-repository.md`, with the workspace as the directory that holds the app's package. The two build files stay in the workspace, so that the workspace can build its image again without you.

- `OWNER` is the `maintainer` of `app.yaml`.
- The tag is yours to choose. Put the PMM version and the app's name in it.
- When the build stops at the step that loads the apps, the app is wrong, not the build. Repair the app as `maintain.md` says.

## 3. Run it once from the image

Follow "Run the image" of the same page: the local PMM runs your image, with no mount, and one run ends with success. Then read the image's labels, as "Verify" of that page shows.

This run is the one that counts for the hand-over. Add it to "What was tried, and what was not" in the brief.

## 4. PMM's menu

Read `in-pmm-ui.md`, and tell the person what it finds for an app that they keep.

## 5. One page for whoever runs the PMM

Write `FOR-THE-PMM-OWNER.md` in the workspace. The reader runs a PMM, and did not see this work. One page, with these parts:

1. **What this is.** The image's name. It is the released PMM Extensions of one PMM version, plus one app. One sentence on what the app does.
2. **Who owns it.** The `maintainer`. Nobody at Percona has reviewed the app. With a problem on a PMM that runs this image, ask the owner first.
3. **What the app can reach.** The section "What the tool touches" of the brief, and "What a database server must have".
4. **How to run it.** The image takes the place of the image of the PMM Extensions container. The app is on as soon as the container starts.
5. **How to use the app.** The app has no page in PMM, unless `in-pmm-ui.md` found one. The command that runs it, with a request that works. This page is where the command is written: your messages to the person name the page, and show no command.
6. **How to upgrade.** The build command with the new PMM version, from "Upgrade" of the page, and that page's sentence about the copy of the database.

## 6. Finish

Save a copy of the work in the workspace. `git status --short` prints nothing, in the workspace and in the framework checkout. Write the stage into `.app-work/<app>/state.md`. Stop the local PMM as "Stop and remove" of `docs/how-to/run-in-local-pmm.md` says.

Your last message to the person has four parts: what exists now, and where; what was run, and what was not; that nothing was published; and what they do next, which is to give `FOR-THE-PMM-OWNER.md` to whoever runs the PMM.

The image is on this machine only. Putting it where another machine can pull it is publishing. Do it only when the person asks for it and names the place, and after a yes.
