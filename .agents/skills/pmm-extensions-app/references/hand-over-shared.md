# Hand over: the person offers the app to others

The app stays the person's own. Other people get its code from a public repository, and build the image themselves.

## 1. The private hand-over first

Do steps 1 to 5 of `hand-over-private.md`. Its last step, with the last message, comes after this file. An app that is offered to others has passed every check of stage 4. When this machine could not run them, stop here: the app stays private until the checks have run somewhere.

## 2. Read what would become public

Everything in the workspace's repository becomes public, with its history. Before you ask, read it as a stranger would:

```bash
git ls-files
git log --oneline
git log -p
```

Look for the name of a customer, the name of a real server, an address inside a company, and a password or a key. Look in the brief, in the tests, in the request files and in the commit messages. Look in the earlier versions of every file too: `git log -p` shows them, and a line that you took out of a file is still in the commit before.

Take out of the files what you find, and say so. When a thing is in an earlier commit or in a commit message, the branch as it is must not be pushed. Make a branch with one commit that holds the files as they are now, read `git log -p` of that branch, and publish that branch in step 3.

## 3. Ask before anything leaves the machine

Other people need a public place on the internet to take the app from. Ask for it in these words:

> To offer the app, its files have to be on a public page on the internet, for example on GitHub. Do you have such a page for this app?
>
> 1. Yes. Tell me its address.
> 2. No. Create one for me.
> 3. Not now. Keep the app as it is. (Nothing is lost: you can offer it later.)

For the second answer, creating the page is publishing: it waits for the yes below. It also needs the GitHub CLI on this machine, signed in as the person. When that is missing, do not install it and do not sign in for the person. Say that you cannot create the page from here, that the person can create an empty public repository in a browser and give you its address, and that the app stays on this machine as it is. For the third, the hand-over ends as the private one does.

Then show what will be published: the address, the list of files, and the sentence that anyone on the internet can read them. Ask whether to publish. Without a yes, stop: the notes say what is ready.

After the yes:

1. Write the address into `repository` in `app.yaml`, and save a copy of the work.
2. Push the workspace's branch to that repository, or the branch with one commit when "Read what would become public" made one. When Git refuses the push, stop: say what Git answered, and that whoever manages that repository has to give this machine the right to push. Everything prepared stays on this machine.
3. Write the list entry to `.app-work/<app>/list-entry.yaml`: the fields of `app.yaml`, with `commit` set to the commit that you pushed. An entry names a commit, not a branch, because the entry describes what ran at that commit.

## 4. The list of apps

Look in the table of documentation in the framework checkout's `README.md` for a list of apps that are not part of PMM.

- **The table names one.** The entry goes there, by a pull request to `percona/pmm-extensions`. That is one more thing to publish: show the entry, and ask.
- **The table names none.** Percona keeps no list of such apps yet. Say so in one sentence. What the person can do today is send the repository's address to the people who should use the app. `CONTRIBUTING.md` says under "Where to ask" how to tell the maintainers of PMM Extensions about it.

## 5. Finish

Do the last step of `hand-over-private.md` now, with this last message in place of its own. It has four parts: where the app is public now; what a person who finds it has to do, which is the build of `FOR-THE-PMM-OWNER.md`; that nobody at Percona has reviewed the app; and that the entry names one commit, so a later change needs a new entry.
