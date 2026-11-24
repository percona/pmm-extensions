# Stage 0: the machine, the workspace and the framework checkout

This stage takes a minute or two. It ends with one line to the person: the machine is ready, or what is missing and who can install it. It never holds up your questions.

Read the request against the stops first, as `SKILL.md` says. A request that meets a stop creates nothing here, except the framework checkout when you need one of its pages to decide.

## 1. Unfinished work

Look for `.app-work/*/state.md` in the working directory. When one exists, read it, tell the person in one line where the work stands, and go on from the stage it names. Skip what the file already answers.

## 2. Which kind of directory this is

- **A clone of `percona/pmm-extensions`:** the directory holds `app/extensions/apps/framework/`, and `git remote -v` names that repository. The clone is the framework checkout. Create no workspace. This is where a port and a repair of an app of the repository happen. Add the line `.app-work/` to the file `.git/info/exclude` of the clone, so that your notes never reach a pull request. Go on with step 4.
- **Anything else, and the job is a port or a repair of an app of the repository:** clone the repository into a new directory, work in that clone as above, and create no workspace.
- **Anything else:** the directory is, or becomes, an app workspace. Go on with step 3.

## 3. Make the workspace

Do each step only when its result is not there yet. Change nothing that exists, with the one exception in step 2.

1. Make the directory a Git repository: `git init`.
2. Copy the files of `assets/workspace/` of this skill into it: `app.yaml` under its own name, `gitignore` as `.gitignore`, and `instructions.md` as `AGENTS.md`. When the directory has a `.gitignore` already, add to it each line of `gitignore` that it lacks, and change no other line: your notes hold the passwords of the local PMM, and Git must never save them. Create `CLAUDE.md` with the one line `@AGENTS.md`. The two files tell any assistant that starts here later to follow this skill.
3. Get the framework checkout:

   ```bash
   git clone https://github.com/percona/pmm-extensions .framework
   ```

4. Copy the skill from that checkout to the two places where assistants read skills, so that the person can go on with another assistant. Copy to a place only when the skill is not there yet.

   ```bash
   mkdir -p .claude/skills .agents/skills
   cp -R .framework/.agents/skills/pmm-extensions-app .claude/skills/
   cp -R .framework/.agents/skills/pmm-extensions-app .agents/skills/
   ```

The workspace then looks like this:

```text
<workspace>/
  <app>/            the app's package, with the brief as README.md   (stage 4)
  tests/            the app's tests                                  (stage 4)
  app.yaml          who owns the app, and what it ran on
  .framework/       the framework checkout: pages, checks, the local PMM
  .app-work/        your notes: where the work stands
```

**The framework checkout is a tool, and it stays as it was cloned.** Run its commands. Read its pages. Change none of its tracked files, and build no app inside it: `git -C .framework status --short` prints nothing when you are done. `create.md` has one exception, and it puts the file back. `hand-over-core.md` has the other: an app that is proposed for PMM itself goes into a branch of this checkout.

## 4. Check the machine

Read "Prerequisites" of `docs/how-to/run-in-local-pmm.md` in the framework checkout, and check what it lists: Docker with Compose, the architecture, the free port, the disk space. Check Git too.

- When the port of the page is taken, choose another one as the page says, and use it in every later command.
- Set the passwords as the page says for a machine like this one. Write the port and the passwords into `.app-work/local-pmm.md`, so that every later stage uses the same ones.
- When Docker is missing or cannot start, the app can be built and checked and cannot be tried on a server. `when-the-answer-is-no.md`, "No database server to try it on", has the words. Go on.

Then start the local PMM, so that it is up when you need it. Follow "Start PMM with PMM Extensions" and "Add a database host" of the same page, for the database that the app is for. The first start takes minutes: do not wait for it before you ask your questions.

## 5. The virtualenv, later

The checks of stage 4 need the virtualenv of the framework checkout. Build it when stage 4 starts, not now: `docs/how-to/set-up-a-development-environment.md` has the steps and the Python versions that work.

When the virtualenv cannot be built on this machine, the app's checks cannot run here. Say so in one sentence, name the checks under "What was tried, and what was not" in the brief, and go on: the app is still built, loaded and tried on a server. Such an app stays private until the checks have run somewhere.
