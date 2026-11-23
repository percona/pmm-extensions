# The person: questions and the brief

## Questions

Ask the person only what you cannot find out and what they can answer from their own work.

1. **Find out first.** Read the tool's own help text and the person's words, and say what you concluded from them. A question whose answer is in the help text is not a question for the person.
2. **Ask two to four questions, in one message.** Five is the limit before the brief. Number them.
3. **Give each question options.** Take two to four options from what the tool really does. Mark one as recommended, and say why in a few words.
4. **Ask about the tool and the servers, never about the app's construction.** The person does not choose a kind of app, a way to run the command, a type of field, a name of a file, a group in a menu or an icon. You choose, and you write the choice into "Decisions for a reviewer".

What a question can be about: what the app should report or do, which of the tool's options the person uses, what a good result looks like, what their database servers have on them.

### The first message

When no stop applies, your first message to the person has these four parts, in this order:

1. **The machine**, in one line: ready, or what is missing and who can install it.
2. **What you concluded** from the request and from the tool's help text. Say what the app will always do without being asked, such as an option that makes the tool end.
3. **Where the app will be tried.** Name the kind of test server that the app will run on. When there is none for this database, or the machine cannot start one, say that the app can be built and checked but not tried on a real server here, and that the work then ends at the checks.
4. **Your questions.**

### Two questions that you always settle

You may conclude the answer from the request and say so. You may not leave it open.

> On your database servers, where does the tool get its login from?
>
> 1. From a file on the server that the tool reads by itself, such as `/root/.my.cnf`. (Recommended: the app then holds no secret.)
> 2. From another file. Tell me where it is.
> 3. I do not know yet.

> Does this tool change or delete anything on the server?
>
> 1. No. It only reads.
> 2. Yes. Tell me what.

When the tool changes or deletes something, the brief says what. The app's default is the way that changes nothing, and stage 5 runs the other way against sample data only.

### One question for later

Ask this one at the hand-over, and only when the app is proposed for PMM itself:

> Do you have a ticket number for this work? It looks like `PMM-12345`.

## The brief

The brief is one page that says what the app will be. Until the person approves it, keep it in `.app-work/<app>/brief.md`. After the approval it becomes the file `README.md` in the app's own directory, so it travels with the app. A reviewer reads it first, and whoever repairs the app later starts from it.

Write it in the person's words, with these sections in this order:

```markdown
# <What the person calls the app>

## What the app does
One or two sentences. What it runs, on which kind of database, and what the person gets.

## What the form asks
One line for each thing the person fills in, with its default.

## What the result looks like
A few lines of the tool's real output, or a description of it.

## What a database server must have
The tool and its version. Where the tool gets its login from.

## What the tool touches
What it reads. What it writes or changes, if anything. Whether it reaches the internet.

## What was tried, and what was not
Empty until stage 5. Then: the record of each run, and in plain words what the run proves and what it does not.

## Where the work stops
Each stop of `when-the-answer-is-no.md` that applies to this app, said in advance.

## Decisions for a reviewer
What you decided alone, for an engineer: how the app is built, its names, which options it sends and why.
```

Show the person every section except the last one, in your message, and ask:

> Is this the app you want? Approve it, or tell me what to change.

Build nothing before the person approves. When the person changes something, change the brief and show it again.

For an app that already exists and has no `README.md`, write the brief from the app's code and from its entry on `docs/app-status.md`. Set what the person asked for beside what the app does today. When the two differ, say so in "What the app does", and let the person choose: the app as it is, or the change as a second step.
