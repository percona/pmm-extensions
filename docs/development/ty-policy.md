# ty Type-Checking Policy

This document records three decisions about [`ty`](https://github.com/astral-sh/ty),
the static type checker `make typecheck` runs: **which trees are checked**,
**what severity each diagnostic rule carries**, and **where enforcement runs and
what it reads**. The first two are expressed in `pyproject.toml` —
`[tool.ty.src]` and `[tool.ty.rules]` — and this file is the rationale behind
them. The third is expressed in `.github/workflows/`, and the reasoning behind
it lives here: see *Enforcement* below.

`make typecheck` exits **0** on the current tree. Every rule at `error` reports
nothing, and the diagnostics that remain are all warning-severity. Enforcement
runs in CI, in the two layers recorded under *Enforcement*: a blocking
`typecheck` job over the whole tree, and an advisory `typecheck_diff` job over
what a branch adds. Neither target is part of `lint` or pre-commit.

**"Advisory" here means "not wired into `ci-success`", not "quiet".**
`typecheck_diff` fails its own step and shows a red check on the pull request
whenever it finds a new diagnostic in the branch's changed non-test Python files; what
`ci-success` job, whose `needs` list omits it, so it never gates the merge. Read
a red `typecheck_diff` as a report to act on rather than as a broken build — and
do not read a green pull request overall as evidence that it is clean.

Every measurement below was taken with **ty 0.0.49**, the version pinned in the
`typecheck` Poetry group, but the numbers fall into two classes that are read
differently.

**Historical snapshots**, taken on branch commit `3eede0dd3` — `dafd2df1` plus
the configuration this policy commits, so the counts depend on
`[tool.ty.rules]` and do not reproduce at `dafd2df1` alone. The 3926 figure, the
argument-list comparison, the two rule tables, and the sampling record are all
of this kind: a snapshot of that one tree at that one version, **not
maintained** against later commits. No decision turns on them — severity was
chosen from what a rule's diagnostics *say*, so a count that has since moved
dates the evidence without reopening the call. Read them as how each call was
reached, not as a figure anything checks.

**The maintained baseline and the enforcement evidence**, taken at
`a157c146115c`: the current **3,287 — 0 error, 3,287 warning** split and the
exit-status pair it comes from. These are current, and they are load-bearing —
the two-layer enforcement decision under *Enforcement* rests on the error count
being zero and on warnings not moving the exit status. *Changing this policy*
requires re-measuring the baseline whenever ty is upgraded or diagnostics are
cleared in bulk; it is the one figure kept current.

## The checked surface

`[tool.ty.src].include` names the four first-party trees that are checked:

| Tree | Modules | Decision | Reason |
|---|---|---|---|
| `app/` | 516 | in scope | The shipped application. |
| `tests/` | 494 | in scope | A wrong annotation is cheapest to ship in a test and least likely to be caught in review there. |
| `scripts/` | 15 | in scope | First-party release and CI tooling. |
| `sidecar/` | 5 | in scope | First-party PMM side-car code. |
| `**/migrations/**` | — | out of scope | Alembic-generated. |
| `frontend/**` | — | out of scope | Not Python. |

`include` is what makes the surface a decision rather than an accident. Without
it, a bare `ty check` also walks whatever untracked or personal directories
happen to sit in the working tree. **A new first-party tree is not checked until
it is added here**, deliberately.

## `[tool.ty.src]` is authoritative, and `make typecheck` passes no paths

`make typecheck` passes no *path* arguments, so the surface comes from
`[tool.ty.src]` alone. Its only argument is `--python`, which names the
environment imports resolve against and reads no files of its own; see
*Installing the pinned group in CI* below for why leaving it off is not an
option.

The reason is that this leaves exactly **one** definition of the surface. A path
argument in the Makefile and an `include` list in `pyproject.toml` are two places
that can drift apart, and the requirement this policy has to meet is that a bare
`ty check` and `make typecheck` read the same trees. With no argument list they
carry identical inputs by construction, so they cannot disagree — and a developer
running `ty` directly in an editor or shell gets the same surface the Makefile
does, without having to know what the recipe passes.

The trees are additive only when each is checked on its own. Measured one root
at a time, they sum to exactly what a bare `ty check` reports:

```
ty check app         ->   961
ty check tests       ->  2957
ty check scripts     ->     5
ty check sidecar     ->     3
                        ------
             summed      3926   == ty check (bare)

ty check app tests   ->  3916   != 961 + 2957
```

Passed together as an argument list they do not. `ty check app tests` reports
3916, two short of `app` plus `tests`, and the two it drops are real
`invalid-argument-type` diagnostics in `app/tasks/logs/log_reader.py` (lines 424
and 433) that `ty check app` on its own reports. Longer root lists diverge
further, in the same direction — see the caveat below.

So an argument list does not merely restate what `[tool.ty.src]` already says.
It silently loses genuine diagnostics, which makes it wrong rather than
redundant, and only the bare invocation accounts for all 3926.

### A caveat on measuring this

Run-to-run instability was observed while preparing this policy: in one
developer working tree, `ty check` given several directory roots reported about
208 fewer diagnostics on roughly a fifth of runs, with the missing diagnostics
concentrated in `tests/app/tasks/logs/`.

That behaviour **did not reproduce** in a `git archive` export of the same
commit — 24 consecutive runs there returned an identical count — so it is
recorded here as an environment-dependent observation, not as a property of ty
and not as part of the argument for this design. The cause was not identified;
neither the `env/` nor the `.claude/` symlink reproduced it when added to the
export.

Two things are worth taking from it. First, if you are comparing diagnostic
counts between invocations, run each more than once before trusting a
difference. Second, a bare `ty check` was stable in every environment tested
(the 3926 baseline below reproduced on every run in both the working tree and
the frozen export), which is a further reason to prefer it as the form this
project measures against.

## The recorded baseline

Under the committed configuration, at `3eede0dd3` (`dafd2df1` plus this policy):

```
make typecheck  ->  Found 3926 diagnostics   (358 error, 3568 warning), exit 2
ty check        ->  Found 3926 diagnostics, exit 1
```

The two agreed because they carried identical argument lists — none. They still
agree on the surface, though `make typecheck` now also passes `--python`, which
changes where imports resolve rather than which files are read; a bare
`ty check` reproduces these counts only from a shell whose active environment is
the project venv.

That figure predates both the artifact suppressions and SEP-1908's first-party
fixes. Under the tree as it now stands, measured at `a157c146115c` with the
pinned `ty 0.0.49`, the same command reports **3,287 — 0 error, 3,287 warning**,
and `make typecheck` exits **0**. The before/after split for the suppressions
alone, and what moved between them, are in *Neutralized dependency-typing
artifacts* below.

Re-measured at `b97ee985f`, the branch point the enforcement jobs were written
against: unchanged at **3,287**, exit **0** — the checked surface is
byte-identical across that span, since everything merged between the two touches
only `docs/`, `CONTRIBUTING.md` and a Makefile comment, none of it inside
`[tool.ty.src].include`. The tree the jobs actually ship on reads **3,284 — 0
error**, exit **0**, measured by the `typecheck` job itself — over a surface
that is not the same one. It carries two further merges, and this branch adds
`scripts/check_ty_diff.py` and `tests/scripts/test_check_ty_diff.py`, both
inside `[tool.ty.src].include`. Checked on their own at the pinned `ty 0.0.49`
those two report none, so the count moved with the merges rather than with them.

**That job's figure is the one to quote.** A local run of the same commit
reported 3,288: same ty, same tree, four diagnostics apart, because the two
environments do not hold identical packages. The job installs from the lock and
names its interpreter, so it is the reproducible measurement; a developer's
working environment is not, and the gap is small only by luck — the same axis
produced a 3,730 reading when the environment was wrong outright (see
*Installing the pinned group in CI* below). Read a count as a property of
(tree, ty version, environment), never of the tree alone.

From here the `typecheck` job re-measures the exit status on every PR, so the
figure that needs maintaining by hand is the count, not the status.

The error count reaching zero is what SEP-1908 was for; the warning fleet is
unchanged by design, because the nine rules at `warn` mix first-party defects
with dependency-typing artifacts and clearing them is separate work.

The two exit codes in that first pair differ because `make` reports its own
status: `ty` exits 1 when it finds an **error-severity** diagnostic — the 358 in
that run — and `make` turns any failed recipe into exit 2. Severity is what
drives the status, not the diagnostic count, which is why the larger 3,287 above
exits 0.

They are **not** guaranteed to be the same binary, and the parity claim carries a
precondition worth restating whenever it is re-checked: `make typecheck` runs
`"${VENV_BIN}"/ty`, and the Makefile resolves `VENV_BIN` three ways, in
precedence order — from `$(POETRY) env info --path` when `POETRY` is set, from
`VIRTUAL_ENV` when it is not, and otherwise from the repository-local
`venv/bin`. That last case is the default for a developer who sets neither
variable. A bare `ty check`, by contrast, runs whatever `ty` is first on
`PATH`. The
`ty = "0.0.49"` pin governs only the former. Before treating agreeing counts as
evidence, confirm `command -v ty` resolves to the same `${VENV_BIN}/ty` and that
`ty --version` reports the pinned version — otherwise the two numbers describe
two different programs.

## Enforcement

### The decision

Enforcement runs in **CI**, in two layers. Not pre-commit, and not local-only.

| Layer | Scope | Reads | Job | Gates the merge? |
|---|---|---|---|---|
| 1 | the whole tree | the exit status of `make typecheck` | hold error severity at zero | yes — `typecheck` is reached through `ci-success` |
| 2 | the changed non-test Python files | the diagnostics themselves | detect what the `warn` rules report | no — `typecheck_diff` is absent from `ci-success`'s `needs`, so it reports red without blocking |

Layer 2 reaches the `warn` rules by re-running `ty` with each of them forced to
`error` (`--error <rule>`, one per rule), then comparing the resulting set against
`BASE_SHA`. That forcing is why the same diagnostic prints as `warning` from any
tool that reads the severity table and as `error` in this job's output — so a
local check honouring the configured severities can be clean while layer 2 is red
on the same tree, and reproducing layer 2 means reproducing the forcing:

```bash
BASE_SHA="$(git merge-base origin/main HEAD)" make typecheck-diff
```

Run locally, it compares commits and checks non-test files only. Beside its
verdict it names the Python changes it left unexamined — uncommitted non-test
files, and changed files under `tests/` — without changing its exit status: a
zero exit is not a pass for either, and `make typecheck` is what covers
`tests/`.

**Both layers run the pinned binary, never whatever `ty` is first on `PATH`.**
Layer 1 gets that by invoking `make typecheck`, which runs `${VENV_BIN}/ty` — a
bare `ty check` does not, and the two are only the same program under the parity
precondition in *The recorded baseline* above. Layer 2, which cannot use the
target because the target takes no paths, resolves the binary itself; that is
constraint 5 below.

The two layers do different jobs and neither substitutes for the other. Layer 1
cannot reach a rule at `warn`; layer 2 cannot see a regression in a file the
branch did not touch. Both are wired: layer 1 as the `typecheck` job in
`.github/workflows/python.yaml`, layer 2 as the `typecheck_diff` job in
`.github/workflows/ci.yml`, which runs `scripts/check_ty_diff.py` through the
`typecheck-diff` Make target. *How the shipped gate is scoped* below records the
three questions this decision deliberately left open.

### Why scoping decides what enforcement catches

`ty` moves its exit status on error-severity diagnostics only; a run holding
nothing but warnings exits 0. Under the severity policy below every rule at
`error` reports nothing on this tree, so a whole-tree run keyed on exit status
passes. Measured at `a157c146115c`:

```
ty check                      ->  Found 3287 diagnostics, exit 0
ty check --error-on-warning   ->  Found 3287 diagnostics, exit 1
```

Same tree, same count, different exit code. That contrast is what establishes
that warning-severity diagnostics do not move the default exit status; the split
behind the total is **0 error, 3287 warning**.

Five defects are on record as caught by `ty` in this repository. Each was
reported by a rule this document holds at `warn`, and each surfaced through a
runner that read the diagnostics and filtered them to the lines a branch had
added rather than through an exit code. The list is the recorded set, not an
audit of every diagnostic ever acted on:

- **PR #1408** — `unresolved-attribute` on a `type[RetirableSQLModel]`
  annotation that did not carry the `.id` its callers read.
- **PR #1412** — a nullable dereference on a line the branch was already
  editing for a different type defect.
- **PR #1436** — `record_sync_health` annotated `instance: SyncHealthBase`, a
  base declaring only the four sync-health columns, while the body addressed the
  row by `instance.id`. Branch-added diagnostics fell from 65 to 3 once the
  annotation was corrected.
- **PR #1436** — `len()` applied to a nullable column, as
  `invalid-argument-type`.
- **SEP-1908's own branch** — a signature widened to a `Service` type carrying
  no `node_id` while the body read it, past a test that passed a same-named
  class from another module that does carry the attribute.

None of the five would have moved the exit status of a whole-tree run, so a gate
reading that status could not have failed on account of any of them. That is why
the scoping question is settled before the placement question, and why layer 2
is the layer that detects anything.

### Why layer 1 is kept anyway

Layer 1 would have caught **none of those five**. It is a ratchet, not a
detector. The property it defends is the one SEP-1908 bought — that no rule at
`error` reports anything — and nothing currently protects it. That set is
open-ended: `all = "error"` puts every rule not listed at `warn` or `ignore`
there too, so the regression layer 1 guards against includes rules that arrive
with a ty upgrade as well as rules that start firing after a code change.

Error-severity rules do fire on real code here. `[tool.ty.src].exclude` drops
`**/migrations/**`, and inside that tree
`app/extensions/migrations/versions/2024_10_07_1450-7f4dec8bc76a_create_sync_tables.py:40:25`
reports `error[possibly-missing-submodule]` when checked directly. The zero is a
property of the checked surface plus SEP-1908's work, not of there being nothing
left for those rules to find — which is why a *new* error-severity diagnostic
inside the surface fails layer 1 immediately.

So layer 1 is insurance on an invariant whose regrowth rate has never been
measured, and it is adopted **alongside** layer 2 rather than instead of it. On
its own it is the cheap option that reads as progress while catching nothing
this work exists to catch.

### How a scoped invocation re-establishes the surface

A path-scoped invocation does not read `[tool.ty.src]`, because the paths are
the query — see *Tools that invoke ty with explicit paths* below.
`--force-exclude` re-establishes it, and it restores **both** halves of that
setting, `exclude` and `include`:

| Invocation | Result |
|---|---|
| `ty check <a file under app/extensions/migrations/>` | `error[possibly-missing-submodule]`, exit 1 |
| the same path, `+ --force-exclude` | `WARN No python files found under the given path(s)`, exit 0 |
| a `.py` file outside every `include` root, `+ --force-exclude` | dropped; reported without the flag |

Layer 2 passes the flag. Without it, a branch touching a migration hands the
gate a tree the surface deliberately drops, and the error-severity diagnostic
above fails the gate on code no full check ever reads.

The flag is opt-in, not the default, which is what keeps the editor case below
working as it should; that section spells out which callers want the unscoped
behaviour.

### Severity without moving the baseline

`[tool.ty.rules]` keeps its meaning as the repository baseline. Layer 2 raises
severity **per invocation** instead, with `--error <RULE>`:

```
ty check --force-exclude app/api/deps.py                       ->  exit 0
ty check --force-exclude --error invalid-argument-type \
         --error unresolved-attribute app/api/deps.py           ->  exit 1
```

The set it promotes is **every rule this document holds at `warn`** — not the
subset of them with hits on the day the gate is written. Derive it from the
table at run time rather than transcribing it:

```bash
python3 -c "import tomllib,pathlib;r=tomllib.loads(pathlib.Path('pyproject.toml').read_text())['tool']['ty']['rules'];print(' '.join(f'--error {k}' for k,v in r.items() if v=='warn'))"
```

A hardcoded list, or a list drawn from what fires today, silently drops a rule
that is configured at `warn` but currently reports nothing — `unresolved-import`
is in exactly that position since SEP-1907 neutralized its artifacts. Such a
rule would then stay unenforced the moment it starts reporting again, which is
the failure `all = "error"` was set up to avoid, reintroduced one layer up.

### The constraints the gate obeys

Fixed by this decision before the gate existed, so that it would not rediscover
them. `scripts/check_ty_diff.py` obeys all six:

1. A working diff-scoped reference implementation existed **outside this
   repository** and was ported repo-side. It is personal tooling rather than
   a tracked artifact here, so it is described by behaviour rather than named by
   path. Four of the constraints below exist because that reference gets them
   wrong, so the port is not a transcription.
2. **Reuse the in-repo parser** rather than writing a fresh regex.
   `scripts/classify_ty_diagnostics.py` already ships `Diagnostic`,
   `DIAGNOSTIC_RE` and `parse_diagnostics()`, and the last of these reconciles
   the rows it parsed against ty's own `Found N diagnostics` trailer, raising
   `ReconciliationError` when the two disagree or the trailer is absent. A
   truncated or crashed run therefore cannot read as clean. The out-of-tree
   reference implementation has no such reconciliation and silently drops rows
   its regex does not match, which is the more dangerous behaviour in a
   blocking gate.
3. **Pass `--force-exclude`**, per the subsection above. The reference
   implementation does not.
4. **Derive the promoted rule set at run time**, per the subsection above.
5. **Resolve the pinned binary** rather than whatever `ty` is first on `PATH`.
   The parity precondition in *The recorded baseline* above says why. Layer 1
   gets this from `make typecheck`; layer 2 has to do it itself, because the
   target passes no paths and so cannot be the scoped invocation. The reference
   implementation invokes a bare `ty` and would silently measure whatever CI
   happens to have installed.
6. **Treat the batching of changed paths as load-bearing, not an optimisation.**
   *`[tool.ty.src]` is authoritative* above measures that paths passed together
   as one argument list do **not** report the union of what they report
   separately — `ty check app tests` returns 3916 where `app` and `tests` on
   their own sum to 3918, and the two it drops are real diagnostics. A runner
   that batches every changed file into one invocation inherits that, silently
   and in the direction that loses findings. The reference implementation
   batches.

Diff base, the batching policy itself, and the attribution rule were left open
here, as design for an artifact this decision did not ship. They are settled in
the next subsection.

### How the shipped gate is scoped

**Attribution is a baseline delta, not the lines a change adds.** The runner
checks the changed files twice — once at `HEAD`, once in a detached worktree at
the merge-base — and reports the multiset difference over `Diagnostic.fingerprint`.
Added-line attribution was measured and cannot work: reverting the
`record_sync_health` annotation at `app/inventory/crud.py:301` reports two
`unresolved-attribute` diagnostics at **:327 and :333**, so a rule keyed on the
added line attributes neither and stays green. That is the shape of the defect
class, not of the reconstruction — an annotation edit lands its consequences at
call sites. The delta also leaves `app/api/deps.py` alone: its pre-existing
`invalid-argument-type` at :296 is in both counters, so its surplus is zero.

**The diff base is `github.event.pull_request.base.sha`, reduced to a
merge-base.** A reusable workflow inherits the caller's event payload, and
`ci.yml` is reached only from `pull_request`. Taking the merge-base rather than
the branch tip keeps a base branch that has advanced since the branch point from
leaking other PRs' lines into the file list. Layer 2's checkout therefore needs
`fetch-depth: 0`; layer 1 does not.

**Batching stays, because under a baseline delta the loss cancels.** Constraint 6
is real and reproduces at whole-root granularity (`app tests` reports 3280 where
the two roots separately sum to 3282). It never reproduced at file granularity.
Both passes receive one file list and an identical batch composition, so a
diagnostic that batching drops is absent from both counters. The cancellation is
supported by measurement rather than proved, so the runner keeps a `--per-file`
flag: the documented response if a batch-suppression miss is ever observed.

**Layer 2 skips `tests/`, and is advisory.** Retro-run over all 37
Python-touching merges since `a10ce9dbd`, the gate as first specified — 9 warn
rules, whole surface — would have blocked 22 of them (59%). Excluding `tests/`
takes that to 24%; 279 of the 363 surplus diagnostics were in test files.
Narrowing the rule set on top moves the rate not at all, so all 9 rules stay
promoted and constraint 4 is preserved. The 24% is measured against authors who
were not trying to keep the gate green, so it is an upper bound of unknown
tightness — the wrong number to make a required check turn on. `typecheck_diff`
is therefore omitted from `ci-success`'s `needs`, which is the only required
check on `main`: it shows a red X on the PR and leaves the merge button enabled.
Promoting it is one line, and waits on a release cycle of observed surplus.

### The bound the overrides place on any gate's reach

`[[tool.ty.overrides]]` silences a rule across every expression in each listed
file. A first-party diagnostic newly written into one of those (file, rule)
pairs is never emitted, so **no** gate — layer 1 or layer 2 — can fail on it.
Scope is not the only bound on reach.

Layer 2's severity promotion does not reach past an override either. Measured on
`tests/app/core/alerts/test_config.py`, which the first override block holds at
`unresolved-attribute = "ignore"`, adding `--error unresolved-attribute` changes
neither the diagnostics reported nor the exit status — the per-file override
wins over the command-line severity. So the bound is a property of the
configuration rather than of how a gate is invoked, and no flag lifts it.

SEP-1950 owns narrowing the overrides; this decision records the bound rather
than leaving it to be discovered after a gate ships.

### Installing the pinned group in CI, and the upgrade cadence

`ty` lives in an optional Poetry group pinned exactly at `0.0.49`, because a
`0.0.x` beta carries breaking changes between any two versions.

**The target names its Python environment explicitly, and must.** ty resolves
imports against an environment it discovers, and its fallback is whichever
interpreter is first on `PATH`. The Makefile's `VIRTUAL_ENV` is a *make*
variable, never exported, so in any shell without the venv activated a bare
`ty check` resolves against the wrong `site-packages`. That is the CI case:
measured on the first run of this job, ty searched
`/opt/hostedtoolcache/Python/3.11.9/x64/.../site-packages` — the runner's
tool-cache Python — rather than the Poetry venv holding the project's
dependencies. `typecheck` therefore passes `--python "${VENV}"`, which pins the
environment without touching the surface: `[tool.ty.src]` still decides which
files are read.

The failure this prevents is quiet in the worst way. Nothing errors; the run
reports *more* diagnostics than a correct one — 3,730 against 3,288, reproduced
locally by taking the venv off `PATH` — and the surplus looks like findings.
Worse, every `ty: ignore` directive covering a third-party shape stops
suppressing anything and is reported as `unused-ignore-comment`, an
error-severity rule, so the job fails while appearing to demand the deletion of
189 suppressions that were doing their job. A reader who trusts the output
deletes them and makes the tree worse.

Two things generalise from it. First, **any tool that resolves imports must be
told which environment to resolve against** — the same reasoning that makes
layer 2 resolve the pinned binary rather than take one off `PATH`, applied to
the environment instead of the executable. Second, **a diagnostic count that
moves without the tree moving is an environment difference, not a finding**;
compare the `unresolved-import` count before believing a surplus.

For the same reason the job installs dependencies as its own step
(`poetry sync --no-root --all-extras --all-groups`) rather than leaning on the
`venv` prerequisite, matching what `test` and `test_postgres` already do. Prefer
that full form over `--with typecheck`, which installs *fewer* groups than
`make venv` intends: ty resolves imports against what is actually installed, so a
thinner environment changes what it reports.

The exact pin means ty's own behaviour cannot drift under the gate; what can
drift is the tree beneath it. The PMM Extensions team owns the upgrade cadence a blocking
gate creates, revisited each release cycle alongside the re-measure rule in
*Changing this policy* below.

### What was rejected, and the measurement that rejects it

An unstated default is not a decision, so every placement not chosen is recorded
with the measurement that rules it out.

| Rejected | Why |
|---|---|
| Pre-commit, whole tree | Tens of seconds on every commit: 25 s measured twice on an unloaded machine, 62–70 s on a loaded one. Like the diagnostic counts, wall clock here is environment-dependent — the order of magnitude is the argument, not the figure. |
| Pre-commit, path-scoped, promoted severity | Blocks on **pre-existing** diagnostics in a touched file, and the two highest-volume `warn` rules account for most of the 3,287 — so editing one line of an affected file would fail the commit. Only a comparison against a baseline fixes that — *How the shipped gate is scoped* above measures why attribution to added lines does not — and pre-commit's staged-file model supplies neither the base revision nor the second tree such a comparison reads. |
| Pre-commit, path-scoped, default severity | Strictly weaker than layer 1: the same rules over fewer files. |
| Whole tree, `--error-on-warning` | Fails on all 3,287 diagnostics today, as measured above. Reachable only after a cleanup that has not been chartered. |
| Local-only | The gap this work exists to close, restated as a decision. |

## Severity policy

### The stance for unlisted rules

`[tool.ty.rules]` opens with `all = "error"`. **A rule that is not listed is an
error.** A rule that starts firing after a code change, or that arrives with a ty
upgrade, therefore shows up loud and forces an explicit decision instead of
inheriting a default nobody chose.

`all = "error"` also switches on the rules ty ships disabled by default, so the
table lists those too — see [Rules ty disables by
default](#rules-ty-disables-by-default).

### How each severity was chosen

Severity was decided by what a rule's diagnostics *say*, read from the files they
land in — never by how many there are. Volume is evidence about cleanup cost, not
about whether a rule is worth keeping.

- **`error`** — the diagnostics are predominantly first-party annotation defects
  with a fix available in this repository, and fixing them improves correctness.
- **`warn`** — the diagnostics mix genuine defects with artifacts of how a
  dependency is typed (SQLModel, pydantic, FastAPI), so neither `error` nor
  `ignore` is honest while those stubs are unshimmed. A `warn` still prints, as
  `warning[...]`, and still exits 0 on its own; it does not reduce output volume.
  It marks a rule as *not yet enforceable*, not as *unimportant*.
- **`ignore`** — reserved for rules with no first-party action available, and for
  conventions this project has not adopted. Every `ignore` names its reason.
  `ignore` is strictly lossier than `warn`, so close calls go to `warn`.

Rules whose severity equals the `all` baseline are still listed explicitly, so
the table is a complete record of what has been deliberately softened.

### Rules at `warn`

Each mixes real defects with dependency-typing artifacts. Sample sizes and file
lists for the ones above 50 hits are in the next section.

| Rule | Hits | Why not `error` |
|---|---:|---|
| `invalid-argument-type` | 2116 | 198 hits are the SQLModel-vs-SQLAlchemy `AsyncSession` split, which has no first-party fix; 325 are genuine `int` vs `int \| None`. |
| `unresolved-attribute` | 863 | 382 are genuine Optional narrowing; the other 481 are test doubles patching private attributes, `object`-typed fixtures, and SQLAlchemy stub gaps. |
| `unknown-argument` | 200 | Dominated by pydantic-settings' `_env_file` / `_secrets_dir`, valid at runtime but absent from the generated `__init__`. |
| `invalid-assignment` | 187 | 83 of 187 are the pydantic `Field(...)` idiom, where ty sees `FieldInfo` assigned to the declared field type. |
| `missing-argument` | 113 | `Settings()` populates its required fields from the environment, which ty cannot see. |
| `call-non-callable` | 38 | Subscripted generics and `object`-typed fixtures, mixed with real calls through `None`. |
| `no-matching-overload` | 34 | Third-party overload sets: `AsyncSession.exec`, `select`, `create_model`. |
| `unresolved-import` | 11 | 8 of 11 are the framework golden-app templates, whose `app.extensions.apps.golden_*` modules are scaffolded at test time and do not exist statically. |
| `invalid-parameter-default` | 6 | 4 of 6 are the FastAPI `Query(...)` default idiom. |

### Rules at `error`

Predominantly first-party defects with a fix available here.

| Rule | Hits | What the diagnostics say |
|---|---:|---|
| `invalid-return-type` | 168 | Nullable returns, and generator fixtures annotated as the yielded type rather than as a generator. |
| `not-subscriptable` | 59 | 57 of 59 subscript a value ty knows may be `None`. |
| `invalid-type-form` | 39 | PMM Extensions' own dynamic `type[BaseUser]` / `type[BaseModel]` values used as annotations. |
| `unsupported-operator` | 22 | 16 of 22 have an un-narrowed `X \| None` operand; the rest widen an operand to `object`. |
| `invalid-method-override` | 19 | First-party overrides of first-party bases; these are Liskov violations. |
| `not-iterable` | 12 | Includes `async for` over a coroutine that was never awaited. |
| `possibly-unresolved-reference` | 11 | A name read on a path where it may never have been bound. |
| `call-top-callable` | 5 | `callable()` narrowing drops the signature; fixable by annotating the attribute. |
| `unbound-type-variable` | 4 | Type variables left unbound in the `app/core` generics. |
| `invalid-paramspec` | 4 | `*args: P.args` without the matching `**kwargs: P.kwargs`. |
| `invalid-context-manager` | 2 | An `__aexit__` signature that prevents `async with`. |
| `empty-body` | 2 | Implicit `None` return against a non-`None` annotation. |
| `possibly-missing-attribute` | 2 | Attribute read through a partially-narrowed union. |
| `redundant-cast` | 1 | A `cast` the inferred type already satisfies. |
| `possibly-missing-submodule` | 1 | Submodule referenced without being imported. |
| `mismatched-type-name` | 1 | `StrEnum` name does not match the variable it is assigned to. |
| `invalid-yield` | 1 | The yielded value is optional, the annotation is not. |
| `invalid-key` | 1 | Unknown key on a TypedDict. |
| `invalid-base` | 1 | Class base is not a valid type. |
| `invalid-attribute-access` | 1 | Assignment to a `ClassVar` through an instance. |
| `unsupported-dynamic-base` | 1 | Class base computed at runtime, so the hierarchy is unknown. |
| `deprecated` | 1 | Calls a symbol its own library marks deprecated. |

## Sampling record for the high-volume rules

Rules at 50 hits or fewer were read in full — 24 rules, 220 diagnostics. The
nine rules above 50 hits were sampled by first collapsing each diagnostic to its
message shape (backtick-quoted spans replaced), then reading a sample spread
across distinct files and across `app/` and `tests/`. The last two rows are the
default-off rules set to `ignore`; they are sampled on the same terms, because a
suppression needs the same evidence as a severity.

Those two counts partition the table: 24 read in full plus 9 sampled is the 33
entries `[tool.ty.rules]` carries. The 24 include the three default-off rules
kept at `error` (11 + 2 + 1 diagnostics), which are below the 50-hit threshold
like the rest.

| Rule | Hits | Shapes | Files | Sampled | Sampled from |
|---|---:|---:|---:|---:|---|
| `invalid-argument-type` | 2116 | 9 | 251 | 24 | `app/core/config.py`, `app/core/utils/fields.py`, `app/extensions/apps/dipper/payloads/pcs-collect-pmm-mysql.py`, `tests/app/tasks/logs/test_log_writer.py`, `tests/app/tasks/logs/test_log_eviction.py`, `tests/app/tasks/logs/test_log_reader.py`, `tests/app/tasks/test_crud.py`, `tests/app/tasks/test_celery.py`, `tests/app/tasks/execution/executors/nomad/test_models.py`, `tests/app/extensions/snippets/models/test_meta.py`, `tests/app/extensions/snippets/test_masking.py`, `tests/app/extensions/apps/mysql_backups/test_payload_snapshot.py`, `tests/app/extensions/apps/mysql_backups/variant_specs.py`, `tests/app/extensions/apps/framework/kit.py`, `tests/app/extensions/apps/framework/test_script_source.py`, `tests/app/extensions/apps/framework/test_conformance.py`, `tests/app/extensions/apps/framework/test_api.py`, `tests/app/extensions/apps/backup_mongo/test_models.py`, `tests/app/extensions/apps/alters/test_spec.py`, `tests/app/extensions/apps/report/test_service.py`, `tests/app/extensions/api/routes/test_settings.py`, `tests/app/extensions/api/routes/test_app_state.py`, `tests/app/extensions/test_config.py`, `tests/app/extensions/test_app_drain.py` |
| `unresolved-attribute` | 863 | 10 | 170 | 24 | `app/core/db/crud.py`, `app/extensions/apps/alters/spec.py`, `app/extensions/clients/pmm.py`, `app/tasks/celery.py`, `app/tasks/crud.py`, `app/tasks/execution/executors/nomad/models.py`, `tests/app/api/test_deps.py`, `tests/app/core/db/test_utils.py`, `tests/app/core/test_config.py`, `tests/app/extensions/api/test_router.py`, `tests/app/extensions/api/routes/test_settings.py`, `tests/app/extensions/apps/framework/test_registry.py`, `tests/app/extensions/apps/mysql_backups/test_xtrabackup_aes256_flags.py`, `tests/app/extensions/apps/mysql_backups/test_xtrabackup_aes256_encrypt.py`, `tests/app/extensions/apps/mysql_backups/test_xtrabackup_incremental_cycle.py`, `tests/app/extensions/db/test_seed.py`, `tests/app/extensions/snippets/test_schema.py`, `tests/app/extensions/snippets/models/test_meta.py`, `tests/app/extensions/sync/test_models.py`, `tests/app/extensions/test_override_callbacks.py`, `tests/app/extensions/test_periodic_tasks.py`, `tests/app/extensions/test_settings_override_integration.py`, `tests/app/tasks/logs/test_log_writer.py`, `tests/app/tasks/settings/test_routes.py` |
| `unknown-argument` | 200 | 2 | 25 | 22 | `app/core/config.py`, `app/extensions/apps/framework/form_dsl/derivation.py`, `app/extensions/apps/framework/responses.py`, `tests/app/api/routes/test_oauth.py`, `tests/app/core/alerts/providers/test_pagerduty.py`, `tests/app/core/alerts/test_models.py`, `tests/app/core/auth/test_config.py`, `tests/app/core/celery/test_config.py`, `tests/app/core/test_config.py`, `tests/app/core/test_models.py`, `tests/app/core/test_requests.py`, `tests/app/extensions/apps/alerts/test_api_routes.py`, `tests/app/extensions/apps/backup_pg/test_models.py`, `tests/app/extensions/apps/framework/test_rules.py`, `tests/app/extensions/apps/framework/test_schema.py`, `tests/app/extensions/apps/report/test_config.py`, `tests/app/extensions/snippets/models/test_meta.py`, `tests/app/extensions/snippets/test_schema.py`, `tests/app/extensions/sync/syncers/test_pmm.py`, `tests/app/extensions/test_config.py`, `tests/sidecar/test_embedded_settings.py`, `tests/sidecar/test_settings_env.py` |
| `invalid-assignment` | 187 | 5 | 71 | 22 | `app/core/alerts/config.py`, `app/core/config.py`, `app/core/utils/openapi.py`, `app/extensions/api/routes/dashboard.py`, `app/extensions/apps/alerts/config.py`, `app/extensions/apps/backup_mongo/restore/models.py`, `app/extensions/apps/checksums/models.py`, `app/extensions/apps/framework/registry.py`, `app/extensions/clients/pmm.py`, `app/extensions/config.py`, `app/extensions/snippets/config.py`, `app/extensions/sync/syncers/mysql/syncer.py`, `app/tasks/config.py`, `app/tasks/models.py`, `app/tasks/execution/executors/nomad/models.py`, `tests/app/core/settings_override/test_registry.py`, `tests/app/core/settings_override/test_registry_nested.py`, `tests/app/core/settings_override/api/test_registry_helpers.py`, `tests/app/extensions/apps/topology/test_topology_payload.py`, `tests/app/tasks/logs/test_log_reader.py`, `tests/app/tasks/test_routes.py`, `tests/app/tasks/execution/executors/nomad/test_models.py` |
| `invalid-return-type` | 168 | 1 | 87 | 22 | `app/core/auth/providers/casdoor/sdk.py`, `app/core/auth/providers/grafana/sdk.py`, `app/core/db/crud.py`, `app/core/db/utils.py`, `app/core/requests/registry.py`, `app/core/utils/fields.py`, `app/extensions/api/routes/periodic_tasks.py`, `app/extensions/apps/alters/pre_checks.py`, `app/extensions/apps/dipper/payloads/pcs-collect-pmm-mysql.py`, `app/extensions/apps/dipper/payloads/pcs-collect-pmm-valkey.py`, `app/extensions/apps/topology/api_routes.py`, `app/extensions/clients/pmm.py`, `app/extensions/crud.py`, `app/extensions/deps.py`, `app/extensions/sync/models.py`, `app/tasks/crud.py`, `tests/app/conftest.py`, `tests/app/core/db/test_list_query.py`, `tests/app/extensions/api/routes/test_connectivity_check.py`, `tests/app/extensions/apps/snippets/conftest.py`, `tests/app/extensions/test_settings_override_worker.py`, `tests/app/tasks/periodic/conftest.py` |
| `missing-argument` | 113 | 3 | 23 | 22 | `app/core/config.py`, `app/extensions/apps/framework/registry.py`, `tests/app/api/routes/test_oauth.py`, `tests/app/core/auth/test_models.py`, `tests/app/core/celery/test_config.py`, `tests/app/core/settings_override/test_policy.py`, `tests/app/core/test_config.py`, `tests/app/core/test_models.py`, `tests/app/extensions/apps/alters/test_schema.py`, `tests/app/extensions/apps/backup_pg/test_models.py`, `tests/app/extensions/apps/framework/test_api.py`, `tests/app/extensions/apps/framework/test_base.py`, `tests/app/extensions/apps/framework/test_registry.py`, `tests/app/extensions/apps/framework/test_schema.py`, `tests/app/extensions/apps/inventory/test_models.py`, `tests/app/extensions/bundle_upload/test_plan.py`, `tests/app/extensions/routes/test_artifacts.py`, `tests/app/extensions/sync/syncers/mysql/test_syncer.py`, `tests/app/extensions/sync/syncers/system_facts/test_syncer.py`, `tests/app/extensions/sync/syncers/test_pmm.py`, `tests/sidecar/test_embedded_settings.py`, `tests/sidecar/test_settings_env.py` |
| `not-subscriptable` | 59 | 1 | 23 | 22 | `app/core/auth/providers/casdoor/sdk.py`, `app/extensions/apps/backup_mongo/deps.py`, `app/extensions/apps/backup_mongo/restore/deps.py`, `app/extensions/apps/backup_pg/deps.py`, `app/extensions/apps/framework/responses.py`, `app/extensions/apps/framework/task_status.py`, `app/extensions/apps/tasks/api_routes.py`, `app/extensions/clients/pmm.py`, `app/extensions/deps.py`, `app/extensions/routes/stream_logs.py`, `app/extensions/sync/models.py`, `app/tasks/execution/executors/nomad/models.py`, `tests/app/core/auth/test_config.py`, `tests/app/core/test_log.py`, `tests/app/extensions/apps/archives/test_api.py`, `tests/app/extensions/apps/backup_pg/test_contract.py`, `tests/app/extensions/apps/checksums/test_route_args.py`, `tests/app/extensions/apps/framework/contract_suite.py`, `tests/app/extensions/apps/framework/kit.py`, `tests/app/extensions/snippets/test_schema.py`, `tests/app/tasks/test_deps.py`, `tests/app/tasks/execution/executors/nomad/test_models.py` |
| `missing-type-argument` | 559 | 3 | 121 | 20 | `app/core/db/crud.py`, `app/core/db/utils.py`, `app/core/settings_override/api/routes.py`, `app/core/settings_override/lifecycle.py`, `app/extensions/apps/framework/apps.py`, `tests/app/core/settings_override/test_lifecycle.py`, `tests/app/core/settings_override/test_worker.py`, `tests/app/extensions/apps/alters/test_api_routes.py`, `tests/app/extensions/apps/backup_mongo/test_api_routes.py`, `tests/app/extensions/apps/backup_mongo/restore/test_api_routes.py`, `tests/app/extensions/apps/framework/test_api.py`, `tests/app/extensions/apps/framework/test_apps.py`, `tests/app/extensions/apps/framework/test_script_source.py`, `tests/app/extensions/apps/shared/test_disk_script_source.py`, `tests/app/extensions/clients/test_pmm.py`, `tests/app/extensions/test_settings_override_integration.py`, `tests/app/extensions/test_settings_override_worker.py`, `tests/app/tasks/test_celery_settings_override.py`, `tests/app/tasks/test_settings_override_integration.py`, `tests/app/tasks/execution/executors/nomad/test_models.py` |
| `missing-override-decorator` | 241 | 1 | 54 | 20 | `app/core/alerts/providers/pagerduty.py`, `app/core/auth/providers/casdoor/models.py`, `app/core/auth/providers/grafana/models.py`, `app/core/config.py`, `app/core/db/crud.py`, `app/core/db/sql_types.py`, `app/core/utils/lazy.py`, `app/extensions/config.py`, `app/extensions/inventory.py`, `app/extensions/apps/framework/rules.py`, `app/extensions/sync/syncers/pmm.py`, `app/extensions/sync/syncers/mysql/syncer.py`, `app/extensions/sync/syncers/system_facts/syncer.py`, `app/tasks/execution/executors/celery/models.py`, `app/tasks/execution/executors/nomad/models.py`, `tests/app/core/alerts/test_models.py`, `tests/app/extensions/apps/mysql_backups/test_contract.py`, `tests/app/extensions/apps/mysql_backups/restore/test_contract.py`, `tests/app/extensions/sync/test_models.py`, `tests/app/tasks/execution/test_models.py` |

## `unresolved-attribute` stays reportable

`unresolved-attribute` is deliberately **not** set to `ignore`. It is the rule
behind most of the defects `ty` is recorded as having caught here — see
*Enforcement* above for the full list, which has grown since this section was
written, and which begins with the four hard errors on the single pull request
that first demonstrated the rule's value. 382 of its 863 hits are genuine
Optional narrowing of the `X | None` shape, which is exactly the class of latent
`AttributeError` worth keeping visible.

Its remaining hits are test doubles patching private attributes and gaps in
third-party stubs. Those are addressed by neutralizing the stub noise, not by
silencing the rule. If it ever turns out that no such mechanism can work, that is
a finding to raise against the parent epic — not a reason to ignore the rule
here.

## Neutralized dependency-typing artifacts

The section above promises that the stub noise mixed into the `warn` rules is
addressed by neutralizing it rather than by silencing the rule. This is that
work. **No severity changed here** — `[tool.ty.rules]` is untouched; what changed
is that 446 diagnostics which describe how a *dependency* is typed no longer
report, leaving a first-party remainder that can be sized and fixed.

Measured at `8ab18007e` with ty 0.0.49 via a bare `ty check`: 4,072 diagnostics
before (366 error, 3,706 warning), 3,626 after. Every one of the 446 removed is a
warning, and all 366 errors are untouched.

### The groups, and the discriminant each is classified on

`scripts/classify_ty_diagnostics.py` holds these as executable predicates; the
table is its `report` output, not a parallel record. The discriminant column is
the load-bearing one, because most groups share a rule with genuine defects —
under `unknown-argument`, the pydantic-settings `_secrets_dir` kwarg and a
first-party `PMM` kwarg differ only in the symbol the message names, and
`tests/app/extensions/test_config.py` holds both.

| Group | Rule(s) | Hits | via override | via comment |
|---|---|---:|---:|---:|
| `settings-subclass-attributes` | `unresolved-attribute` | 137 | 51 | 86 |
| `pydantic-fieldinfo` | `invalid-argument-type` / `invalid-assignment` | 87 | 29 | 58 |
| `env-populated-required-params` | `missing-argument` | 70 | 63 | 7 |
| `pydantic-settings-private-kwargs` | `unknown-argument` | 63 | 58 | 5 |
| `celery-app-attributes` | `unresolved-attribute` | 31 | 10 | 21 |
| `third-party-overload-sets` | `no-matching-overload` | 18 | 18 | 0 |
| `sa-type-typedecorator` | `invalid-argument-type` | 16 | 11 | 5 |
| `absent-modules` | `unresolved-import` | 9 | 9 | 0 |
| `subscripted-generics-called` | `call-non-callable` | 9 | 9 | 0 |
| `fastapi-query-default` | `invalid-parameter-default` | 4 | 4 | 0 |
| `pygments-textlexer` | `unresolved-import` | 2 | 2 | 0 |
| **Total** | | **446** | **264** | **182** |

One group is additionally confined to a set of paths. `Cannot resolve imported
module` reads identically for a golden-app module scaffolded at test time and for
a first-party import someone mistyped, so the message alone cannot establish the
verdict; `absent-modules` therefore matches only under
`tests/app/extensions/apps/framework/golden/` and at
`app/extensions/sync/syncers/system_facts/payload.py`, and a mistyped import anywhere
else stays a first-party defect. Every other group's message carries the evidence
on its own and matches at any path.

### Two mechanisms, chosen per (file, rule) pair

A `[[tool.ty.overrides]]` entry in `pyproject.toml` covers the 59 pairs whose
*every* hit of that rule in that file is an artifact — 264 hits. Each entry names
explicit file paths, never a directory wildcard, so it cannot silently widen as
files are added, and softens only the one rule it exists for. Overlapping entries
merge their rule tables, so a file legitimately appears in several.

The 27 pairs that mix take a per-site `# ty: ignore[rule]` comment instead — 182
of them across 27 files. A file-level mechanism cannot discriminate within a
file, and an override there would suppress the genuine defects alongside.

**The two mechanisms are never both applied to one (file, rule) pair.**
`unused-ignore-comment` is unlisted in `[tool.ty.rules]` and so inherits
`all = "error"`; an override makes any comment for the same rule in the same file
unused, which is a new error. The same property makes the suppressions
self-cleaning in the desirable direction: a comment that goes stale as the tree
drifts becomes a build error rather than lingering silently.

**The asymmetry runs the other way for overrides, and that is the cost of the
mechanism.** A comment suppresses one site; an override suppresses its rule
across the whole file, so a *future* first-party diagnostic of that rule written
into one of those files is never emitted, and `check` cannot recover it — the
gate reconciles against a baseline captured before the file changed, and a
diagnostic that was never emitted leaves no row to go missing. The pairs listed
were chosen because every hit in them was an artifact at `8ab18007e`, which is a
statement about that commit and not a property the entry maintains.

The recovery is to re-derive the split rather than to trust the entry. Stripping
every `[[tool.ty.overrides]]` block from `pyproject.toml` and re-running restores
the suppressed rows, and `report` re-partitions them:

The edit is in place and `git checkout` is the restore, so an interrupted run
leaves nothing to reconstruct by hand:

```bash
python3 - <<'EOF'
import pathlib
p = pathlib.Path("pyproject.toml")
out, skip = [], False
for line in p.read_text().splitlines(keepends=True):
    if line.startswith("[[tool.ty.overrides]]"):
        skip = True
        continue
    if skip and line.startswith("[") and not line.startswith("[tool.ty.overrides.rules]"):
        skip = False
    if not skip:
        out.append(line)
p.write_text("".join(out))
EOF
ty check --output-format concise > /tmp/ty-unsuppressed.txt
git checkout -- pyproject.toml
python3 scripts/classify_ty_diagnostics.py report --from /tmp/ty-unsuppressed.txt
```

Any pair the report now shows as *mixed* has acquired a first-party diagnostic
since the entry was written, and belongs on per-site comments instead. Run this
before widening an existing entry's `include` list, and when a listed file grows
substantially.

Two mechanical constraints govern where a comment may sit:

- **The comment must be on the exact line ty reports**, which is often a
  continuation line inside a multi-line call. On the opening line or the closing
  paren it produces `unused-ignore-comment` *and* leaves the diagnostic firing.
- **`ruff format` relocates a comment that pushes its line past the line
  length** — it explodes the call and re-attaches the comment to the closing
  paren, which is the failing placement above. So placement has to be re-derived
  from ty's output *after* formatting, not before; the tree here is at that
  fixpoint.

### What was *not* neutralized, and why

The sqlmodel-vs-sqlalchemy `AsyncSession` mismatch (201 hits of
`invalid-argument-type`, expecting `sqlmodel.ext.asyncio.session.AsyncSession`
and finding the `sqlalchemy` one) is **first-party, not an artifact**. The two
classes are not parallel declarations by two libraries: the sqlmodel class is a
*subclass* of the sqlalchemy one, adding `exec`, and `app/core/db/crud.py`
imports the subclass and calls `session.exec(...)`. A value typed as the
supertype genuinely cannot satisfy a parameter requiring the subtype, so ty is
right and every hit arises where a test file or helper annotates its own
`session` parameter with the sqlalchemy import. Those hits stay reportable and
belong to the first-party remainder.

Recorded while probing that group, and left for the remainder: `app/core/db/utils.py`
declares `-> async_sessionmaker` while returning the *synchronous*
`sessionmaker` from `sqlalchemy.orm`. Parameterizing the return type surfaces a
latent `invalid-return-type` in the same function. The bare annotation is
repeated in four wrappers — `app/inventory/db.py`, `app/extensions/db/engine.py`,
`app/tasks/db/engine.py`, `app/core/celery/db.py`. Fixing the factory and all
four does **not** clear the group (measured: 201 hits before, 202 after), because
the test files' own parameter annotations drive it.

There are no retained exceptions: the script's `RETAINED` list is empty, and
`check` prints it on every run so it cannot grow unnoticed. One site needed the
expression split so that an artifact and a first-party diagnostic of the same
rule stopped sharing a line — `_override_nomad` in
`tests/app/tasks/execution/test_nomad_lifecycle.py`.

### The remainder, as a baseline

Per-rule residual under the committed configuration, measured at `8ab18007e`.
Only the nine `warn` rules had artifacts; the `error` rules are listed together
because none did. This residual is the baseline the follow-up work on the
first-party typing defects is sized and tracked against — dated evidence for
that commit, like the per-rule tables above, not a figure to keep current.

| Rule | Before | Artifacts | Residual |
|---|---:|---:|---:|
| `invalid-argument-type` | 2212 | 16 | 2196 |
| `unresolved-attribute` | 885 | 168 | 717 |
| `unknown-argument` | 205 | 63 | 142 |
| `invalid-assignment` | 194 | 87 | 107 |
| `missing-argument` | 119 | 70 | 49 |
| `call-non-callable` | 38 | 9 | 29 |
| `no-matching-overload` | 36 | 18 | 18 |
| `unresolved-import` | 11 | 11 | 0 |
| `invalid-parameter-default` | 6 | 4 | 2 |
| every rule at `error` | 366 | 0 | 366 |
| **Total** | **4072** | **446** | **3626** |

### Reproducing the split

The fingerprint manifest is a build artifact, not a committed file, so the claim
above is checkable rather than asserted:

```bash
# Capture the base run first: the classifier does not exist at the merge base.
git switch --detach $(git merge-base HEAD origin/main)
ty check --output-format concise > /tmp/ty-base.txt
git switch -
python3 scripts/classify_ty_diagnostics.py baseline --from /tmp/ty-base.txt \
    --out /tmp/ty-baseline.json
python3 scripts/classify_ty_diagnostics.py check --baseline /tmp/ty-baseline.json
```

`check` takes the multiset difference over `(path, rule, message)` fingerprints
and fails unless **every** diagnostic that stopped reporting is one the
classification marks as an artifact. That is what a count comparison cannot
establish: a suppression that hides one artifact *and* one first-party
diagnostic, while some unrelated new diagnostic appears, reconciles to the
expected total. The fingerprint is exactly what `classify` reads, so two
diagnostics sharing one always share a verdict; folding the line and column away
costs the check no precision and lets it survive the reformatting the comments
provoke, which would otherwise report every diagnostic below an edited line as
newly suppressed.

`report` prints the same tables from a live run and names any line holding both
an artifact and a first-party diagnostic of the same rule. It does not flag a
group with no hits: on a neutralized tree every group reaches zero by design.
`check` names them instead, against the baseline — the only run in which a group
matching nothing means its predicate has gone stale rather than done its job. That
list is advisory and does not move `check`'s exit status: a group matching nothing
is either drift, in which case the diagnostics it used to claim are unclassified
and suppressing them fails the reconciliation on its own, or an artifact class a
dependency upgrade retired, which leaves a run that lost no first-party diagnostic
and so has nothing to fail. Naming the group points at the override or comments to
remove; failing on it would red a clean run.

## Rules ty disables by default

Because `all = "error"` switches on every rule ty knows, including those it ships
disabled, the table also has to take a position on five rules that were not
reported before this policy existed. Together they add 814 diagnostics.

Three are latent-bug detectors and are kept at `error`:
`possibly-unresolved-reference` (11), `possibly-missing-attribute` (2), and
`unsupported-dynamic-base` (1).

Two are set to `ignore`:

- **`missing-type-argument`** (559) flags a generic used without type arguments.
  Bare generics are widespread and intended here; parameterizing all 559 sites is
  its own decision, not a consequence of choosing a severity for the rules that
  were already firing.
- **`missing-override-decorator`** (241) requires PEP 698's `@override` on every
  overriding method. That is a code convention this project has not adopted, and
  the rule detects its absence rather than any defect.

Neither was reported before this table existed, so ignoring them removes no
signal that was previously available. Both remain easy to reconsider: deleting
the entry restores it to the `all = "error"` baseline.

## Tools that invoke ty with explicit paths

`[tool.ty.src]` governs the **repository baseline** — what a full check covers.
It does not scope a tool that passes explicit file paths, because those paths are
the query. Editors, LSP integrations, and any diff-scoped wrapper that checks
only the files a change touches therefore read `[tool.ty.rules]` for severities
but not `[tool.ty.src]` for scope.

That asymmetry is inherent to a path-scoped query, and for an editor or LSP
integration it is the correct behaviour: a query about the file in front of you
should answer about that file, whether or not `include` covers it. So the
asymmetry is not an inconsistency to be "fixed" by making every such tool read
`include`.

A tool that *does* want the surface back can have it. `ty check --force-exclude`
enforces the exclusions for paths given on the command line, and it honours the
`include` half as well — a path outside every `include` root is dropped, not just
one inside `exclude`. Because the flag is opt-in, the editor case above keeps
the behaviour it needs. A gate is the case that wants it on; *Enforcement* above
records why, and what fails without it.

The asymmetry has one further consequence worth knowing, which no flag removes:
because a rule set to `ignore` disappears from ty's output entirely, it also
disappears from any such tool's report — which is why close calls in this table
go to `warn` rather than `ignore`.

## Changing this policy

- **Moving a rule's severity** — update its row here, and leave a trailing
  comment on its entry only where the reason is not already carried by the rule
  name and the severity. A move from `warn` to `error` should be able to point
  at the artifacts that stopped being artifacts.
- **Adding a tree** — add it to `[tool.ty.src].include` and to the surface table
  above. Nothing checks a tree that is not listed.
- **Upgrading ty** — new rules arrive at `error` through the `all` baseline, and
  the baseline count will move. Re-measure with a bare `ty check` and update the
  recorded baseline. Only that one figure is re-measured: the per-rule hit
  counts and the sampling record stay as they are, dated evidence for calls
  already made rather than values to keep current.
- **Clearing diagnostics in bulk** — the same re-measure applies, for the same
  reason and with the same limit. A change that drives a rule's count to zero,
  or that narrows a first-party signature enough to reveal diagnostics standing
  behind it, moves the recorded baseline; update that figure and nothing else.
  Expect the total to rise before it falls: correcting an annotation ty was
  giving up on exposes what it was hiding, so measure iteratively rather than
  projecting a burn-down.
- **Adding or removing a suppression** — never by hand. Add the shape to
  `GROUPS` in `scripts/classify_ty_diagnostics.py` with the discriminant it
  classifies on, then let `report` say which (file, rule) pairs take an override
  and which take per-site comments. `check` against a baseline from the merge
  base is what establishes that no first-party diagnostic went with it.
- **Do not add path arguments to the `typecheck` target.** See the stability
  measurements above.
