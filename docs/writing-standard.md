# Writing standard

This page sets the rules for every document in this repository. The rules apply to people and to AI assistants.

## Readers

Decide which reader a page is for before you write it.

| Reader | Knows | Wants to |
|---|---|---|
| App developer | Python. Not this codebase, and not how PMM works inside. | Build or port an app, see it work inside PMM, and contribute it. |
| Maintainer | PMM, or this codebase, but not both. | Build, run and debug the sidecar next to PMM. |
| Operator | Databases, and PMM as a user. Not Python. | Run, test or migrate PMM Extensions. |
| Decision maker | The product. Not the code. | Judge what an app costs to ship. |

Many readers use English as a second language.

## Sources, in order

Follow these sources. When two disagree, the earlier one wins.

1. The rules on this page.
2. The [PMM documentation guidelines](https://github.com/percona/pmm/blob/main/documentation/AGENTS.md): the sections "Voice and tone", "Page structure" and "Word choices". The MkDocs syntax in that file does not apply here.
3. The [Google developer documentation style guide](https://developers.google.com/style).

## Rules

1. **One page, one job.** Each page is one of the four types in the next section. Don't mix them.
2. **Run it before you write it.** Run every command and every step exactly as the page gives it. Then show what the reader sees when it worked.
3. **Sentences.** Aim for 15 to 20 words. Split a sentence that runs over 25 words. Write a step as one instruction of 20 words or fewer.
4. **Tone.** State facts. Leave out words that praise, such as "easy", "simple" and "powerful". Leave out humor and idioms.
5. **Subject.** Address the reader as "you". Write "PMM Extensions", never "we".
6. **Terms.** Use one term for one thing, from the list on this page. Define a term the reader may not know where it first appears.
7. **Say it once.** State each fact on one page. Link to that page from the others.
8. **Describe what works today.** Mark a plan as a plan, in one sentence, with no date.
9. **Nothing internal.** Leave out internal hostnames, customer names, people's names and accounts of internal discussions.
10. **Format.** Use sentence case for headings and US English spelling. Put code, commands and paths in backticks, and names from the UI in bold. Write one paragraph per line, with no hard wrap.

## Page types

| Type | The reader wants to | Rules |
|---|---|---|
| Tutorial | Learn by doing | One path, with no choices. Every step shows a result. It works every time from a clean checkout. Link to explanations; don't include them. |
| How-to | Reach a goal | The title is an instruction: "Run PMM Extensions in a local PMM". Sections, in order: Prerequisites, the steps, Verify, Next steps. |
| Reference | Look something up | It describes and does not instruct. Every entry has the same structure. |
| Explanation | Understand why | Prose, with no steps. The title is a noun phrase. |

`README.md` and `CONTRIBUTING.md` are entry points. They say what PMM Extensions is and where to go next, and they link to pages of the four types.

## Terms

| Use | Not |
|---|---|
| PMM Extensions | Extensions on its own, SEP |
| the `extensions` service, in code formatting | the Extensions service |
| app | plugin |
| sidecar | side-car |
| standalone SEP | Any other name for v0.13.1 and earlier. Use it only where a page covers migration. |
| enables you to | lets you, allows you to |

## Before you open a pull request

Check each page that you changed:

1. Read the page as its reader. Confirm that the reader can act on it with nothing else.
2. Run every command on the page from a clean checkout.
3. Split each sentence that runs over 25 words.
4. Follow every link.
