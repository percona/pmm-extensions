---
applyTo: "**/*.py"
---

# Python — Duplication & Existing-Pattern Reuse

The most common LLM-review failure is approving correct-but-reinvented code. Before accepting any new helper, constant, decorator, or pattern, check whether the codebase already has it.

## DUP-1 — Duplicated literals

Same string or numeric literal appearing in 2+ files in the diff (or 1 in the diff plus 1+ in `main`) MUST be a named constant.

- **Important** if the literal appears in 3+ files, if changing it would require touching every occurrence, or if it encodes a convention (port, key, format string).
- **Minor** if it appears in 2 files and is unlikely to change.
- Flag even **single-file app-identity literals** — app slug, app-owned meta-key, app `settings.yaml` key, URL fragment routed to the app — they encode a cross-app contract. Extract to a module-level constant.
- Don't flag test fixture values or Python builtins.
- **Field-name sets that mirror a model** — any tuple/list/set of, or dict keyed by, a model's field-name string literals, at **any scope** (module-level constants and sets built inside a function body alike): `RENDERED_FIELDS = ("os_version", "config")`, a dict keyed by `Capabilities` fields. This duplicates the model, the single source of truth. Derive from `Model.model_fields` or hang a `ClassVar` on the model. Caveat: prefer a named allow-set over a blanket `frozenset(Model.model_fields) - {excluded}`, which silently absorbs every future field the model gains. **The highest-yield tell is a *partially* derived set** — some members read off the model (`ClassVar`s, `Field(exclude=True)`) and one sits beside them as a bare literal. The derived neighbours are what make the literal look vetted, and a rename desyncs only that one. When most of a set is derived, ask why the remainder isn't.

Concrete cases: `3306`/`5432` ports → `DEFAULT_MYSQL_PORT`/`DEFAULT_POSTGRESQL_PORT` in `app/inventory/constants.py`. Any hardcoded service-type string (`"mysql"`, `"postgresql"`, `"valkey"`, …) → `ServiceTypeEnum.<member>` (it's a `StrEnum` — the member *is* the string).

**Ask the ownership question before choosing where the constant lives.** "Should we share a constant?" and "which layer owns this value?" are different questions, and answering the first doesn't discharge the second — the second decides whether a shared constant is even the right artifact. Fire this only when the literal is drawn from **another component's vocabulary**: an executor task or step name, a queue name, a wire-protocol field, a third-party enum value. There the fix is that component exposing the value, not `app/` sharing a string. The strongest tell is that the owning abstraction **already exposes generic siblings and simply has no name for this one** — `BaseExecutor` abstracts `stream_logs` / `stream_file` / `list_files` / `get_hosts` but has none for the step carrying a run's own output, so `_MAIN_LOG_STEP = "run-script"` pinned inside a PMM Extensions app is a Nomad task name in the wrong layer (correct only while every dispatch happens to be Nomad-seeded; a `CeleryExecutor` records under a different source and the filter silently matches zero records). Ordinary literals PMM Extensions itself owns — app slugs, meta keys, its own thresholds and paths — skip this question and go straight to the extraction rules above.

## DUP-2 — Hand-rolled where a helper exists

| Hand-rolled | Use instead |
|---|---|
| Module-level dict + `time.monotonic()` TTL | `@alru_cache(ttl=N)` / `@ttl_cache(ttl=N)` / `@lru_cache` |
| Raw `SELECT … FROM information_schema.columns` | `sqlalchemy.inspect(conn).get_columns(table)` |
| Dialect branches for idempotent INSERT | `app/core/db/utils.py::idempotent_insert()` |
| Dialect branches for JSON path extraction | `app/core/db/utils.py::func_json_extract()` |
| `SQLField(sa_column=Column(ForeignKey(...)))` | `SQLField(foreign_key=…, ondelete=…)` |
| Hardcoded `"MYSQL"`/`"POSTGRESQL"` | `ServiceTypeEnum.<member>.value` |
| New string/dict/date helper | Check `app/core/utils/` first |
| Raw `session.execute(select(...))` | `BaseSQLModelManager` methods |
| `session.exec(...)` inside a Manager classmethod body | `await cls._exec(session, query)` |
| `Manager.first(...)` then `if obj is None: raise HTTPNotFoundException(...)` | `Manager.get_or_404(session, **f)` |
| `Manager.first(...)` → `if existing: update() else: create()` | `Manager.get_or_create(session, data, filter_include={...})` |
| Inline `datetime.now(UTC)` / `datetime.utcnow()` timestamp | `utc_now()` from `app/core/utils/date_time` (microseconds zeroed) |
| `fastapi.HTTPException` | `HTTPNotFoundException` / `HTTPConflictException` / etc. |
| Inline `RemoteAPI` client | `Annotated[RemoteAPI, Depends(get_*_api)]` in `deps.py` |
| Manual JWT/auth | `CurrentUser = Annotated[BaseUser, IsAuthenticatedDep]` from `app/api/deps.py` — or `dependencies=[IsAuthenticatedDep]` when the handler needs no user object |
| `len(v) > 0` `field_validator` | `NonEmptyStr` |
| `.strip().lower()` `field_validator` | `Annotated[str, StringConstraints(strip_whitespace=True, to_lower=True)]` or `LowercaseStr` |
| `field_validator` doing a string-*shape* check (split on a separator, reject empty halves, reject stray whitespace) | `Annotated[str, StringConstraints(pattern=...)]` field type |
| `.nulls_last()` on an `ORDER BY` term | `app/core/db/utils.py::NullsLastOrdering(column, *, descending=False)` — one shared cache-keyed construct; pass the bare column plus `descending=`, never a pre-`desc()`-ed expression (which would render `<expr> DESC ASC NULLS LAST`) |
| A version prefix or envelope hand-rolled around ciphertext (`f"v1:{encrypt(s)}"`, `s.startswith("enc:")`) | `app/core/encryption.py::mark_ciphertext()` / `marked_ciphertext()` — the marker's character set is load-bearing (outside base64 so `is_encrypted` cannot also claim the value, RFC 3986 unreserved so it survives a URL userinfo segment), and the accessor pairs the prefix with a payload check so a plaintext that merely starts with it is not misread as ciphertext |

**Rule of thumb.** If a new decorator or class has 15+ lines of state management (timestamps, eviction, key hashing, TTL math), ask "why isn't this `@alru_cache` or `@ttl_cache`?" Flag as **Important**.

## DUP-3 — Idiom inconsistency

Compare each new pattern against the dominant form in sibling files in the same package. Non-dominant forms get **Minor** (or **Important** if divergence creates real maintenance burden). Carve-out: if siblings in the same subtree all use the non-dominant form, the local convention wins — but matching the local form never launders a rule break; the specific rules win over local consistency.

**Check the file under edit first, not only its siblings.** A sibling-file grep never reads the file the diff is editing, yet that file is the strongest evidence of the local idiom and a divergence inside it is the one the next reader hits first. Before adding a construct to an existing file, grep **that file** for how it already writes the same construct and match the majority form; a split inside one file is internal inconsistency, not two valid styles.

## DUP-4 — Sibling-route-body duplication

Before accepting a non-trivial block of new code, grep the package's route handlers for the same logic shape — DUP-2 misses this, since route bodies aren't in the helper catalog.

- **Same-package** — extract the duplicated logic into a `Depends()` or helper.
- **Cross-app** — if a route in app A processes a domain object owned by app B, prefer a classmethod on B's response model (`SnippetResponse.from_snippet(snippet)`) so consumers import and call it.

Don't flag trivial duplication or new code that legitimately needs a different shape.

## DUP-5 — Repeated call shape or boilerplate block

- Same function call (name + overlapping kwargs) appearing ≥3 times in one file → extract a thin one-liner wrapper that names the intent (e.g. `hot_field(default)` for repeated `field_with_metadata(default, metadata={...})`).
- A verbatim multi-line setup/teardown block (≥3 lines, `try`/`finally`/`async with`) duplicated ≥2 times across sibling callers → extract a shared context manager or helper.

The wrapper must name the *intent*, not just collapse syntax — a 3-line helper saving 1 line per call with no clear name isn't worth it.

## DUP-6 — Reuse stops at the leading underscore

Before flagging "reuse the existing symbol," check for a `_` prefix. A `_`-prefixed module member is not public — importing it across module boundaries couples the importer to an internal the owner can rename or delete without notice. Fix: define a trivial literal locally, or promote the symbol to public (drop `_`, add to `__all__`) in the owning module and import that. Applies to tests too (they're `SLF001`-exempt, so nothing mechanical catches it).

**Carve-out — a white-box test of the symbol itself.** DUP-6 governs importing a private symbol to **reuse it as a dependency for other code**. It does not govern a `test_*.py` importing a private name **from the very module it is the test for**, where the symbol under import *is* the subject under test — `tests/app/core/utils/test_openapi.py` importing `_strip_core_ref_ids` from `app.core.utils.openapi` is testing that helper, not reusing it. There the coupling is the point, and promoting the symbol to public purely so a test may import it is the anti-pattern. The carve-out is narrow: it requires the `tests/<pkg>/…/test_<mod>.py` ↔ `<pkg>/…/<mod>.py` correspondence. A test importing a private name from an *unrelated* module is an ordinary DUP-6 violation.

## DUP-9 — Accretion: a new check must integrate with the block it joins

An edit that adds a check to a block already checking the same subject is responsible for **integrating** with what's there — collapsing redundant traversals and deleting clauses the addition makes dead — not only for the correctness of the lines it added. Each check reads as a self-contained addition, so the block grows one `if any(...)` at a time and no single diff looks wrong; the redundancy is visible only to someone reading the block whole. The rule is author-agnostic: it binds the original author adding the second check to their own block exactly as much as a later pass adding the third.

```python
# Bad — three walks over self.sortable, and `not key` is dead
# ("".strip() is falsy, so `not key.strip()` already rejects the empty key)
if any(not key or not key.strip() for key in self.sortable): ...
if any(key.startswith("-") for key in self.sortable): ...
if any(not hasattr(column, "asc") for column in self.sortable.values()): ...

# Good — one pass, no dead clause
for key, column in self.sortable.items():
    ...
```

Two exceptions, in both of which the passes stay separate *and* say why in the code: **pinned error precedence** (collapsing changes which error a multiply-invalid input reports first, and a test pins it) and **deliberate short-circuit ordering** (a cheap pass placed ahead of an expensive one). Neither is available by default — absent a pinning test or a stated cost reason, a second traversal of the same collection is the defect.

## DUP-10 — One contract, one site

Three shapes of the same defect: a predicate or contract stated in the owner *and* restated at a caller, so both must now be edited together and the copy is the one a reader misses.

- **A caller pre-filtering on the owner's own constant.** When a helper owns the question "which members qualify?" — a `shared_field_names`, a `visible_columns`, any function whose contract *is* the selection — a caller that pre-filters on the same constant before handing data over has copied that contract. The tell: **the caller imports a constant whose docstring lives beside the owning function.** Fold the filter into the owner and drop the import. Exempt: a caller filtering on a *different* question (a permission check, a pagination window, a request-scoped subset). The test is whether the two predicates would ever be edited apart — if changing one without the other is always a bug, they are one predicate in two places.
- **A validator and the parser it guards disagreeing on the value space.** When one function assigns special meaning to a character, prefix or suffix inside a string value space — a leading `-` for descending order, a `!` for negation, a `__` path separator — every validator over that space must apply the **same transformation before checking**, and must reject literal values colliding with the reservation. Both failure modes usually ship together: **over-accept** (a value whose literal form collides passes validation, then becomes unreachable because the parser strips the sigil before lookup — dead config that raises nothing) and **over-reject** (a value the parser *would* resolve is refused at construction, because the validator compared the raw form against an allowlist the parser only consults post-strip). The tell: **a `strip` / `removeprefix` / `split` appears in the resolving function but not in the validating one.**
- **An eligibility predicate applied on the presentation path but not the resolution path.** Where a component exposes a predicate classifying members as usable or unusable — `getOptionDisabled`, `isEnabled`, an allowlist test — every path that *resolves* a member must apply it: a label match, an id lookup, a default-selection fallback, a deserialized persisted value. Over-accept is the common one: a member the predicate excludes is committed through the path that never consults it. Grep the predicate's name; every site that selects a member either applies it or is deliberately exempt, and the exemptions (an admin override, an audit view rendering historical values) are **stated**, not implied by omission.

## DUP-11 — Sibling fields sharing a role share a guard

Where one construction introduces several fields sharing a type or role, the construction-time guard applies to the **whole family** — validate the role, not an enumerated list of field names. An enumeration drops members: the checks get written field by field, the field the design discussion focused on gets the real predicate, and a sibling holding the same kind of value gets a weaker one (or a bare presence check) that nothing later re-examines. The tell is **asymmetric strength across fields of one type**: two fields validated as column expressions and a third checked only for `not None`; two validated as callables and a third accepted raw.

**The family can span constructions.** A *new primitive introduced as a sibling of an existing one* — a second marker, field type, or validator explicitly modelled on a predecessor — joins that predecessor's family and owes the same contract. Diff the **admitted value space** of the new one against the old, in both directions, and justify any divergence. The trap is that the sibling's contract is often de-facto, expressed only in how every declaration site writes it with no explicit guard to read, so authoring a narrower guard for the newcomer leaves no diff to inspect and nothing to contradict. Grep the sibling's *declaration sites*, not just its validator — if it has none, the declaration sites **are** the contract.

Carve-outs: a narrow annotation *is* the guard (a member typed `ColumnElement` rather than `Any` needs no runtime check); a primitive opening a genuinely new axis has no sibling to diff against. The rule binds on **join**, not only at co-introduction — a field joining an existing guarded family in a later change owes the family's guard too.

## DUP-12 — A deletion leaves dead consumers behind

The mirror of dead code: a deletion that leaves its consumers standing, and the survivors are systematically the ones no tool can see. An import-graph sweep (`grep` for the symbol, ruff `F401`, the type checker) finds consumers that *import*; it finds nothing in surfaces naming the deleted thing as a **string**, and that's where the residue collects:

- **Tooling config** — `pyproject.toml` tool sections (`also_copy`, `omit`, `known-first-party`), `Makefile` variables, `.pre-commit-config.yaml` args.
- **Scaffolder templates** — `app/extensions/apps/framework/templates/**`. `.tmpl` files are not imported, linted, or type-checked, so a stale symbol survives every gate and surfaces only when someone next runs `make startapp`, where it reads as a broken new app rather than a stale template.
- **The labeler surface** — `.github/labeler.yml`'s hand-written top block (`language:python`, `language:shell`, `language:typescript`, `frontend`, `builtin-snippets`, `snippets-core`, `svc:tasks`, `svc:inventory`) and the path constants `scripts/sync_pr_labels.py` hardcodes outside it (`GENERATED_PREFIXES`, `GENERATED_EXACT`, `QA_NOT_REQUIRED_GLOBS`, `TEST_FILE_PREFIXES`, `FRONTEND_TEST_FILE_RE`), any of which can be keyed on a directory that no longer exists. The generated block between the `BEGIN/END generated app labels` markers is exempt — `scripts/sync_labeler_apps.py --check` runs on pre-commit and asserts every app glob and `E2E_ALIASES` entry against disk — so a deletion inside `app/extensions/apps/` is caught there and a deletion anywhere else is not.
- **Docs and operator-facing prose.**

**Test imports keep dead production symbols alive to every linter.** A module-level symbol whose only remaining callers are its own tests is dead production code, but `F401` sees a live import, the suite stays green, and coverage reports the symbol as exercised. Run the orphan sweep **ignoring test-only references** — a symbol referenced solely from `tests/` is a deletion candidate. (When such a symbol produced user-visible output, confirm the consumer is genuinely gone first; if a UI still surfaces it, that's a parity gap, not dead code.)

**Carving a file out of a deleted tree is a relocation decision, not an exemption.** "Delete the tree except this one file" leaves the tree alive holding one file, and with it every setting, loader, build argument and CI filter that existed to serve the tree. Say where the survivor goes.

## DUP-7 — Conformance checks derive their expectation from the verified source

When a diff adds a consistency/conformance check, the expected value must be *derived from* the production function or constant being verified (called, imported, computed) — never re-typed as an independent copy. A re-typed expectation drifts from the code silently and defeats the check.

## DUP-8 — Unit label agrees with the factor and the documented contract

When a value is shown to a human with a unit — an error message, log line, validation failure, or UI string — the label, the conversion factor that produces the number, and the unit in the field's docstring / schema description must all agree. Binary-vs-decimal (KB/KiB, MB/MiB) and seconds-vs-milliseconds mismatches are the common, user-visible drift. Flag when the three diverge.

## Pydantic — declarative over imperative

- Prefer custom field types and `StringConstraints` over `field_validator`.
- Prefer `@computed_field` over externally-set derived fields.
- Use `BaseModel` for containers that accept >1 input shape and must normalise (a `model_validator(mode="before")`) or have derived fields. Plain value bags stay as `NamedTuple`/`@dataclass`.
- When adding a `@dataclass` into a module with existing dataclasses, match the dominant flags (`slots=True`, `frozen=True`, `kw_only=True`).
