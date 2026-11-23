# Copilot Instructions

This file gives GitHub Copilot the project context it needs when working on PMM Extensions. Both Copilot code review and the Copilot cloud agent read it, so it carries only guidance that applies whether Copilot is reviewing a diff or writing one. Per-area rules live under `.github/instructions/`; how to word a review lives in `.github/instructions/code-review.instructions.md`, which the cloud agent is excluded from.

## Project shape

PMM Extensions is a FastAPI application with three mounted sub-applications, each with its own database and Alembic migration chain. One Celery app (`app/celery.py`) and one beat process serve all three; each service contributes its schedules through its own seed module.

- `app/inventory/` — nodes, services, schemas, tables; mounted at `/api/inventory`.
- `app/tasks/` — task execution, the Nomad and Celery executors, periodic tasks; mounted at `/api/tasks`.
- `app/extensions/` — the PMM Extensions API gateway, OAuth, apps; mounted at `/`. Inside PMM it serves the API only: the pages that a user sees are part of PMM's UI, in the `percona/pmm` repository (`docs/explanation/architecture.md`, "Where the UI lives").

A PMM Extensions app is a package under `app/extensions/apps/<name>/` that exports one app object from `app.py`. The framework derives the app's routes, its form and its validation from that object and from the form model in `models.py` (`docs/explanation/app-framework.md`). `Annotated[..., Depends(...)]` aliases live in `deps.py`.

To build, port or fix an app with a person who does not write Python, follow the skill in `assistant/skills/pmm-extensions-app/SKILL.md`.

The UI is **API-first + React** — see `api-first.instructions.md` for rules that apply when a PR touches the API gateway, app schemas, or `frontend/packages/`. The server-rendered Jinja2 layer has been removed; the only Jinja left renders the report PDF.

## Repo-wide rules (apply to every PR)

- **PR title** — with a Jira ticket, `SEP-XXX: <description>` (or `PMM-XXXXX: ` for PMM-tracked work): key, colon, space, non-empty description. **Internal-only work needs no ticket** — a change with no product-facing effect: dependency and security bumps, version bumps, release branches, CI, dev tooling, feature-build pins, docs and review instructions. Its title takes a conventional-commit prefix instead (`fix(deps):`, `chore(release):`, `docs(copilot):`). Flag a ticketless title with no prefix, and a ticketless PR whose diff changes product behaviour — that needs a ticket, not a prefix. Bot-authored PRs are held to the same rule.
- **HTTP status codes**: `fastapi.status` constants (`status.HTTP_404_NOT_FOUND`) — never raw integers — in route decorators, exception raises, test assertions, `JSONResponse`.
- **Prefer named project exceptions over raw `fastapi.HTTPException`.** Direct `fastapi.HTTPException` is a tech-debt signal — check `app/core/exceptions.py` and `app/core/auth/exceptions.py` first (`HTTPNotFoundException`, `HTTPConflictException`, `HTTPForbiddenException`, `HTTPGoneException`, `HTTPBadRequestException`, `HTTPUnprocessableEntityException`, and for upstream failures `HTTPInternalServerErrorException` / `HTTPBadGatewayException` / `HTTPServiceUnavailableException` already exist). This matters especially when the same `fastapi.HTTPException(status_code=…, detail=…)` shape appears in 2+ places with the same custom params — that repetition should become a named exception class.
- **Changelog fragments** — user-facing PRs add a file under `changelog.d/` via `make changelog-add TICKET=SEP-XXX SECTION=<added|changed|fixed|security|breaking|config> MSG="…"`. Skip for purely internal changes (CI, refactors, tooling, docs with no user-visible effect), for same-release-cycle fixes (regression from a sibling ticket sharing the unreleased `Fix Version`, linked in Jira via `is caused by`), and for framework-spine-uniform surface (a verb/param/field a shared framework like `TaskExecutionApp` ships once and every migrating app inherits identically — documented once when the framework ships it; add a fragment only for the app-specific delta). Don't expect edits to `[Unreleased]` in `CHANGELOG.md` — that section is assembled at release time.
- **No emojis** in code, comments, docstrings, or commit messages unless the file already uses them.
- **No new files** unless required by the change. Prefer editing existing modules.
- **Dependency edits**: when `pyproject.toml` changes a dependency declaration (`tool.poetry.dependencies`, `tool.poetry.group.*.dependencies`), `poetry.lock` must be updated together via `poetry add` / `poetry remove` / `poetry update`. Non-dependency edits to `pyproject.toml` (ruff config, tool sections) don't require a lockfile update.
