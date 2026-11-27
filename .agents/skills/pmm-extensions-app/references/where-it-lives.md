# Where should it live?

Ask this once, after the person agreed that the result of stage 5 is right. Do not ask it earlier: the work up to here is the same for every answer, and a person who does not know yet must not have to know.

When the person already said where the app goes, at the start or later, say what you understood and do not ask.

## The question

Ask it in these words:

> The app works on the test server. What do you want to do with it now?
>
> 1. Keep it for my own use, or for one customer. (Recommended if you are not sure. You can still choose 2 or 3 later, and nothing is lost.)
> 2. Offer it to other PMM users, from a public place of your own.
> 3. Propose it for PMM itself, so that it ships with a later PMM version.

"I am not sure", "I do not know" and "you decide" all mean the first answer. Take it, say in one sentence that the other two stay open and that nothing is lost, and go on to the statement below. Do not ask the question a second time, and do not put another piece of work in front of it.

| The answer | Go on with |
|---|---|
| 1, or not sure | `hand-over-private.md` |
| 2 | `hand-over-shared.md` |
| 3 | `hand-over-core.md` |

## Before anything is built for the first two answers

The hand-over for answer 1 or 2 makes a copy of PMM Extensions that holds the app, and that copy can leave this machine. Before you build it, the person reads what the app can reach, and says yes.

Say this, in these words:

> This app runs inside PMM Extensions with PMM's administrator rights. Its command runs on the database servers that PMM monitors, as the user that runs PMM's tasks there. Nobody at Percona has reviewed it. You are its owner: you decide where it runs, and you fix it when it breaks. Run it on a PMM only when the person responsible for that PMM has agreed.

Then ask one thing: whether the person agrees and wants you to go on.

- Build nothing before the yes.
- Ask nothing else in that message. A name, a version, a place to keep the copy and every other detail are yours to decide.
- When the person says no, stop. The app stays in its folder as it is, and the notes say where the work stands.

An app that is proposed for PMM itself needs no such statement: the maintainers review it before it ships.
