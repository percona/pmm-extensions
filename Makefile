# vim: ts=8:sw=8:ft=make:noai:noet

SHELL=env bash

PYTHON?=python3
RELEASE_VER?=HEAD
# Pin Poetry and pre-install the required plugin via pip. Without the plugin
# pre-installed, `poetry install` runs an auto-resolve to add it, which racks
# the bootstrap-fresh transitive deps (e.g. virtualenv) against the project's
# solver.min-release-age=7 and fails ("virtualenv 21.3.3 doesn't match any
# versions"). Pre-installing means Poetry sees the plugin in its env and skips
# the resolve entirely.
POETRY_VERSION?=2.4.0
ifdef VIRTUAL_ENV
    VENV=${VIRTUAL_ENV}
else
    VENV?=venv
endif
VENV_BIN?="${VENV}/bin"
ifdef POETRY
	START_PKGS=pip wheel
	VIRTUAL_ENV=$$("${POETRY}" env info --path)
	VENV=${VIRTUAL_ENV}
	VENV_BIN="${VENV}/bin"
else
	START_PKGS=pip wheel poetry==${POETRY_VERSION} poetry-plugin-export
	POETRY="${VENV_BIN}/poetry"
endif
PIP?="${VENV_BIN}/pip"
APPS=tasks inventory extensions
PYTEST_WORKERS?=auto
# xdist's default `load` hands each free worker the next single test, so a long
# test picked up late leaves the other workers idle at the tail. `worksteal`
# deals tests out in blocks and lets an idle worker steal from a busy one,
# which measured fastest of the four schedulers on this suite. Ignored when
# PYTEST_WORKERS=0 (the CI jobs that run single-process): -n 0 disables
# distribution regardless of --dist.
PYTEST_DIST?=worksteal
# COV=0 drops --cov=app (and the fail_under coverage gate) so a local run skips
# the coverage instrumentation tax; CI and coverage-main keep the default COV=1.
COV?=1
PYTEST_PATHS?=tests/
PYTEST_MARKERS?=

# WeasyPrint loads native libs (libgobject-2.0, libpango, libcairo) at import
# time. Homebrew installs them under /opt/homebrew/lib (Apple Silicon) or
# /usr/local/lib (Intel) — neither is on dyld's default search path. macOS SIP
# also strips DYLD_* from any env inherited by /usr/bin/make, so the export
# must happen inside each recipe shell. No-op on Linux/CI/Docker. (SEP-1125)
DARWIN_DYLD = if [ "$$(uname -s)" = "Darwin" ]; then \
		for d in /opt/homebrew/lib /usr/local/lib; do \
			if [ -d "$$d" ]; then \
				export DYLD_FALLBACK_LIBRARY_PATH="$$d:$${DYLD_FALLBACK_LIBRARY_PATH}"; \
				break; \
			fi; \
		done; \
	fi;

# `[ -d "${VENV}" ]` is what makes a caller-supplied VENV a *selector*. Without
# it the guard tests only the active shell, never the path the caller just
# named, so `make VENV=<existing env> <target>` runs `python -m venv` over that
# environment -- rewriting its pyvenv.cfg to whichever python3 is first on PATH
# while bin/python stays on the interpreter it was built with. Every tool in it
# then dies with ModuleNotFoundError: No module named 'encodings', one command
# later, in something unrelated.
venv: pyproject.toml poetry.lock
	@[ ! -z "${VIRTUAL_ENV}" ] || [ -d "${VENV}" ] || { \
		echo "venv: building ${VENV} with $$("${PYTHON}" -V) ($$(command -v "${PYTHON}"))"; \
		"${PYTHON}" -m venv "${VENV}"; \
	}
	@"${PIP}" install --no-cache ${START_PKGS};
	@source "${VENV_BIN}"/activate; "${POETRY}" install --all-extras --all-groups

build: venv app/
	@source "${VENV_BIN}"/activate; "${POETRY}" build --format wheel --output dist

pack:
ifndef BUNDLE
	@echo Exporting bundle
	@git archive --output=bundle.tgz --format=tar.gz "${RELEASE_VER}" app snippets
else
	@echo Copying custom bundle "${BUNDLE}"
	@cp -a "${BUNDLE}" bundle.tgz
endif

builder:
	@podman image exists "extensions:builder" && podman image rm "extensions:builder" || true
	@buildah build -f Containerfile.base --compress --force-rm --squash --no-cache --format oci --memory 100M --isolation rootless --tag "extensions:builder"

# The app-restricted PMM-embedded image is the only artifact PMM Extensions ships. Which
# apps survive is sidecar/settings.yaml's EXTENSIONS.APPS.
# docker format, not oci: OCI silently discards the HEALTHCHECK instruction
image: pack
	@podman image exists "extensions:${RELEASE_VER}" && podman image rm "extensions:${RELEASE_VER}" || true
	@buildah build -f sidecar/Containerfile.sidecar --compress --force-rm --squash --no-cache --format docker --memory 100M --isolation rootless --build-arg EXTENSIONS_RESTRICT_APPS=1 --tag "extensions:${RELEASE_VER}"

format: venv
	@"${VENV_BIN}"/ruff format .

ruff: venv
	@"${VENV_BIN}"/ruff check .
	@"${VENV_BIN}"/ruff format --check .

# Static type checking (Astral ty); CI runs it as the blocking `typecheck` job.
# Passes no paths, so `[tool.ty.src]` stays the single definition of the surface.
# `--python` is load-bearing: VIRTUAL_ENV is never exported, so ty would resolve
# imports against whichever interpreter is first on PATH.
# See docs/development/ty-policy.md under `Enforcement`.
typecheck: venv
	@"${VENV_BIN}"/ty check --python "${VENV}"

# Report the ty diagnostics a branch adds against BASE_SHA, which reaches the
# script through the recipe environment rather than being pasted into it.
# Advisory in CI only in the sense that `ci-success` does not depend on the
# `typecheck_diff` job: the job still fails and shows red when the branch adds a
# diagnostic in a changed non-test Python file. It forces every `warn` rule to
# `error` before diffing, so it reports what a severity-respecting local check does not.
# See docs/development/ty-policy.md under `Enforcement`.
typecheck-diff: venv
	@"${VENV_BIN}"/python -m scripts.check_ty_diff $(if $(PER_FILE),--per-file,)

lint: ruff

audit: bandit pip-audit

# python.org macOS builds ship without etc/openssl/cert.pem until you run
# "Install Certificates.command"; urllib then fails for hooks that fetch remotes.
run-pre-commit: venv
	@SSL_CERT_FILE=$$("${VENV_BIN}"/python -c 'import certifi; print(certifi.where())') \
		"${VENV_BIN}"/pre-commit run --all-files

# Local development only; production startup uses container/entrypoint paths.
dev-backend: venv
	@$(DARWIN_DYLD) "${VENV_BIN}"/python -m app.main $(if $(START_CELERY),--start-celery,)

# Local development only; production frontend startup stays outside Make.
dev-frontend:
	@cd frontend && pnpm dev

# One-time legacy data['_form'] backfill for framework-migrated task apps.
backfill-legacy-forms: venv
	@"${VENV_BIN}"/python -m app.extensions.apps.framework.form_backfill $(BACKFILL_ARGS)

# Read-only counterpart: reports the saved stamps a create form now rejects.
form-audit: venv
	@"${VENV_BIN}"/python -m app.extensions.apps.framework.form_audit $(AUDIT_ARGS)

pip-audit: venv
	@"${POETRY}" run pip-audit --verbose --progress-spinner=off \
		$$($(PYTHON) -c "import tomllib,pathlib;c=tomllib.loads(pathlib.Path('pyproject.toml').read_text());print(' '.join(f'--ignore-vuln {v}' for v in c.get('tool',{}).get('pip-audit',{}).get('ignore-vulnerabilities',[])))" 2>/dev/null)

bandit: venv
	@"${VENV_BIN}"/bandit -c pyproject.toml -r app

makemigrations: venv alembic.ini app/tasks/models.py app/inventory/models.py app/extensions/models.py
	@"${VENV_BIN}"/python scripts/sync_alembic_version_locations.py
	@for app in $(APPS); do \
		capitalized=$$(echo $$app | sed 's/^./\U&/'); \
		echo "Checking migrations for $$capitalized"; \
		"${VENV_BIN}"/alembic --name $$app check > alembic_check.log 2>&1 || { \
			if grep -q "Target database is not up to date" alembic_check.log; then \
				echo "Error: $$capitalized database is not up to date"; \
				echo "Error:   Run 'make migrate' to apply the migrations before making new migrations."; \
				rm alembic_check.log; \
				exit 1; \
			elif grep -q "New upgrade operations detected" alembic_check.log; then \
				echo "New upgrade operations detected for $$capitalized. Creating migration."; \
				read -p "Enter description for new $$capitalized Migration: " desc; \
				if [ "$$app" = "extensions" ]; then \
					extra_args="--head=extensions_main@head"; \
				else \
					extra_args=""; \
				fi; \
				"${VENV_BIN}"/alembic --name $$app revision --autogenerate $$extra_args -m "$$desc"; \
				rm alembic_check.log; \
				continue; \
			else \
				cat alembic_check.log; \
				rm alembic_check.log; \
				exit 1; \
			fi \
		}; \
		rm alembic_check.log; \
		echo "No new upgrade operations detected for $$capitalized"; \
	done

makemigrations-plugin: venv alembic.ini
ifndef PLUGIN
	$(error PLUGIN is required. Usage: make makemigrations-plugin PLUGIN=<plugin-name>)
endif
	@"${VENV_BIN}"/python scripts/sync_alembic_version_locations.py
	@read -p "Enter description for new $(PLUGIN) plugin migration: " desc; \
	"${VENV_BIN}"/alembic --name extensions revision --autogenerate --head=$(PLUGIN)@head -m "$$desc"

migrate: venv alembic.ini app/tasks/migrations/versions app/inventory/migrations/versions app/extensions/migrations/versions
	@"${VENV_BIN}"/python scripts/sync_alembic_version_locations.py
	@ret=0; \
	for app in $(APPS); do \
		"${VENV_BIN}"/alembic --name $$app upgrade heads || ret=1; \
	done; \
	if [ $$ret -ne 0 ]; then \
	  echo "Error: One or more Alembic upgrades failed."; \
	  exit $$ret; \
	fi
	@"${VENV_BIN}"/python -m app.core.celery.bootstrap --deadline-seconds 60

checkmigrations: migrate
	@"${VENV_BIN}"/python -m scripts.check_alembic_revision_tree
	@ret=0; \
	for app in $(APPS); do \
	  echo "Checking migrations for $$app"; \
	  "${VENV_BIN}"/alembic --name $$app check || ret=1; \
	done; \
	if [ $$ret -ne 0 ]; then \
	  echo "Error: One or more migration checks failed."; \
	  exit $$ret; \
	fi
	@echo "All migration checks passed."

mergemigrations: venv alembic.ini
	@"${VENV_BIN}"/python scripts/sync_alembic_version_locations.py
	@"${VENV_BIN}"/python -m scripts.merge_alembic_heads

test: venv
	@$(DARWIN_DYLD) "${VENV_BIN}"/pytest -v -r a -n ${PYTEST_WORKERS} --dist ${PYTEST_DIST} $(if $(filter 1,$(COV)),--cov=app,) $(if ${PYTEST_MARKERS},-m "${PYTEST_MARKERS}",) ${PYTEST_PATHS}

# Regenerate every derived API/form contract from the live app in one pass:
# the route GET /schema + OpenAPI snapshot goldens, the synthetic form-DSL
# goldens, the frontend OpenAPI spec, and the generated TS client. Run after
# changing an app form model, review the diff, then commit.
regen-specs: venv
	@$(DARWIN_DYLD) EXTENSIONS_UPDATE_SNAPSHOTS=1 "${VENV_BIN}"/pytest -q -p no:cacheprovider tests/app/extensions/test_schema_snapshot.py tests/app/extensions/test_openapi_snapshot.py tests/app/extensions/apps/framework/test_form_dsl_golden.py
	@$(DARWIN_DYLD) "${VENV_BIN}"/python scripts/dump_openapi.py
	@cd frontend && pnpm --filter @pmm-extensions/api codegen && pnpm --filter @pmm-extensions/api exec oxfmt --write src/generated

regen-pbm-payloads: venv
	@$(DARWIN_DYLD) "${VENV_BIN}"/python scripts/gen_pbm_payloads.py

regen-pbm-payloads-check: venv
	@$(DARWIN_DYLD) "${VENV_BIN}"/python scripts/gen_pbm_payloads.py --check

regen-xtrabackup-variants: venv
	@$(DARWIN_DYLD) "${VENV_BIN}"/python scripts/gen_xtrabackup_payload_variants.py

regen-xtrabackup-variants-check: venv
	@$(DARWIN_DYLD) "${VENV_BIN}"/python scripts/gen_xtrabackup_payload_variants.py --check

smoke-xtrabackup-variants: venv
	@$(DARWIN_DYLD) "${VENV_BIN}"/python scripts/smoke_xtrabackup_variants.py $(ARGS)

check-nomad-payload-size: venv
	@$(DARWIN_DYLD) "${VENV_BIN}"/python scripts/check_nomad_payload_size.py $(ARGS)

check-sidecar-purge: venv
	@$(DARWIN_DYLD) "${VENV_BIN}"/python scripts/check_sidecar_purge.py $(ARGS)

# A Fernet key is 32 random bytes in url-safe base64, so this needs neither the
# venv nor cryptography: an operator runs it on a fresh checkout to copy the one
# line it prints, and the venv bootstrap would both fail there and bury the key.
encryption-key:
	@$(PYTHON) -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())'

changelog-add:
ifndef TICKET
	$(error TICKET is required. Usage: make changelog-add TICKET=SEP-XXX SECTION=added MSG="description")
endif
ifndef SECTION
	$(error SECTION is required. Usage: make changelog-add TICKET=SEP-XXX SECTION=added MSG="description")
endif
ifndef MSG
	$(error MSG is required. Usage: make changelog-add TICKET=SEP-XXX SECTION=added MSG="description")
endif
# TICKET, SECTION and MSG are forwarded through the recipe shell environment as
# "$$VAR" — command-line variables are auto-exported, so the shell (not Make's
# textual expansion) supplies each value and embedded quotes, spaces and backticks
# stay literal (a literal '$' must be written as '$$' on the command line).
# Interpolating "$(MSG)" instead pastes the raw text inside an already-open quoted string, where a `"` closes it and a backtick
# command-substitutes; no caller-side quoting can prevent that.
	@$(PYTHON) scripts/changelog.py add --ticket "$$TICKET" --section "$$SECTION" --message "$$MSG" $(if $(FORCE),--force,)

changelog-check:
	@$(PYTHON) scripts/changelog.py check

changelog-list:
	@$(PYTHON) scripts/changelog.py list

# Bare `make startapp` (no NAME) drops into the interactive wizard. Each value is
# forwarded through the recipe shell environment as "$$VAR" — command-line
# variables are auto-exported, so the shell (not Make's textual expansion) supplies
# the value and embedded spaces/quotes stay intact; a literal `$` must be written
# `$$` on the command line. $(if ...) gates presence. Recognised variables: NAME
# TYPE DISPLAY_NAME ITEM_DISPLAY_NAME ITEM_DISPLAY_NAME_PLURAL DESCRIPTION GROUP
# SERVICE_TYPE NAV_ICON RUN_MODE COMMAND PAYLOAD SCRIPT NO_INPUT ENABLE
# DERIVE_UPDATE DERIVE_DELETE.
startapp:
	@$(DARWIN_DYLD) "${VENV_BIN}"/python app/extensions/apps/framework/scaffold.py \
		$(if $(NAME),--name "$$NAME") \
		$(if $(TYPE),--type "$$TYPE") \
		$(if $(DISPLAY_NAME),--display-name "$$DISPLAY_NAME") \
		$(if $(ITEM_DISPLAY_NAME),--item-display-name "$$ITEM_DISPLAY_NAME") \
		$(if $(ITEM_DISPLAY_NAME_PLURAL),--item-display-name-plural "$$ITEM_DISPLAY_NAME_PLURAL") \
		$(if $(DESCRIPTION),--description "$$DESCRIPTION") \
		$(if $(GROUP),--group "$$GROUP") \
		$(if $(SERVICE_TYPE),--service-type "$$SERVICE_TYPE") \
		$(if $(NAV_ICON),--nav-icon "$$NAV_ICON") \
		$(if $(RUN_MODE),--run-mode "$$RUN_MODE") \
		$(if $(COMMAND),--command "$$COMMAND") \
		$(if $(PAYLOAD),--payload "$$PAYLOAD") \
		$(if $(SCRIPT),--script "$$SCRIPT") \
		$(if $(NO_INPUT),--no-input) \
		$(if $(ENABLE),--enable) \
		$(if $(filter false 0 no,$(DERIVE_UPDATE)),--no-derive-update) \
		$(if $(filter false 0 no,$(DERIVE_DELETE)),--no-derive-delete)

startapp-check:
	@$(DARWIN_DYLD) "${VENV_BIN}"/python scripts/startapp_check.py

SIGN_FLAG := $(if $(SIGN_VIA_API),--sign-via-github-api,)
PUSH_IMAGE_DOCKER ?= true

release-prep:
ifndef VERSION
	$(error VERSION is required. Usage: make release-prep VERSION=X.Y.Z)
endif
	@$(PYTHON) scripts/release.py prep --version "$(VERSION)" $(SIGN_FLAG)

release-rc:
ifndef VERSION
	$(error VERSION is required. Usage: make release-rc VERSION=X.Y.Z RC=N)
endif
ifndef RC
	$(error RC is required. Usage: make release-rc VERSION=X.Y.Z RC=N)
endif
	@$(PYTHON) scripts/release.py rc --version "$(VERSION)" --rc "$(RC)" $(SIGN_FLAG)

release-stable:
ifndef VERSION
	$(error VERSION is required. Usage: make release-stable VERSION=X.Y.Z)
endif
	@$(PYTHON) scripts/release.py stable --version "$(VERSION)" $(SIGN_FLAG)

trigger-jenkins:
ifndef TAG
	$(error TAG is required. Usage: make trigger-jenkins TAG=vX.Y.Z [PUSH_IMAGE_DOCKER=false] [WEBHOOK_URL_ENV=... WEBHOOK_AUTH_ENV=...])
endif
	@set -euo pipefail; \
	tag='$(value TAG)'; \
	if [ -n "$${JENKINS_URL:-}" ] && [ -n "$${JENKINS_USER:-}" ] && [ -n "$${JENKINS_API_TOKEN:-}" ]; then \
		case "$${tag}" in \
			v*) jenkins_job="Release" ;; \
			*) jenkins_job="Build" ;; \
		esac; \
		echo "==> Triggering Jenkins $${jenkins_job} build for $${tag}..."; \
		if printf 'user = "%s:%s"\n' "$${JENKINS_USER}" "$${JENKINS_API_TOKEN}" \
			| curl -sSf -k --config - \
				-X POST "$${JENKINS_URL}/job/PMM-Extensions/job/$${jenkins_job}/buildWithParameters" \
				--data-urlencode "releaseTag=$${tag}" \
				--data-urlencode "pushImage=true" \
				--data-urlencode "pushImageDocker=$(PUSH_IMAGE_DOCKER)" 2>&1; then \
			echo "    Jenkins build triggered successfully."; \
			if [ -n "$(WEBHOOK_URL_ENV)" ] && [ -n "$(WEBHOOK_AUTH_ENV)" ]; then \
				$(PYTHON) scripts/post_jira_webhook.py \
					--url-env "$(WEBHOOK_URL_ENV)" \
					--auth-env "$(WEBHOOK_AUTH_ENV)" \
					--version-tag "$${tag}" || true; \
			fi; \
		else \
			echo "    Warning: Failed to trigger Jenkins build. Trigger it manually."; \
		fi; \
	else \
		echo "Note: JENKINS_URL/JENKINS_USER/JENKINS_API_TOKEN not all set, skipping Jenkins trigger."; \
	fi

# Jenkins Declarative validate. Usage: make lint-pipelines [FILE=build/x.pipeline]
lint-pipelines:
	@set -euo pipefail; \
	if [ -z "$${JENKINS_URL:-}" ] || [ -z "$${JENKINS_USER:-}" ] || [ -z "$${JENKINS_API_TOKEN:-}" ]; then \
		echo "Note: JENKINS_URL/JENKINS_USER/JENKINS_API_TOKEN not all set, skipping Declarative lint."; \
		exit 0; \
	fi; \
	if [ -n "$(FILE)" ]; then \
		files=("$(FILE)"); \
	else \
		files=(); \
		while IFS= read -r file; do files+=("$${file}"); done < <(git ls-files -- ':(glob)build/**/*.pipeline'); \
	fi; \
	if [ "$${#files[@]}" -eq 0 ]; then \
		echo "error: no build/**/*.pipeline files found"; \
		exit 1; \
	fi; \
	crumb="$$(printf 'user = "%s:%s"\n' "$${JENKINS_USER}" "$${JENKINS_API_TOKEN}" \
		| curl -sSf -k --config - \
			"$${JENKINS_URL}/crumbIssuer/api/xml?xpath=concat(//crumbRequestField,\":\",//crumb)")"; \
	failures=0; \
	for f in "$${files[@]}"; do \
		echo "==> Declarative-lint $${f}..."; \
		if ! resp="$$(printf 'user = "%s:%s"\n' "$${JENKINS_USER}" "$${JENKINS_API_TOKEN}" \
			| curl -sS -k --config - --fail-with-body \
				-H "$${crumb}" \
				-F "jenkinsfile=<$${f}" \
				"$${JENKINS_URL}/pipeline-model-converter/validate")"; then \
			echo "$${resp}"; \
			echo "error: request to Jenkins failed for $${f}"; \
			failures=$$((failures+1)); \
			continue; \
		fi; \
		echo "$${resp}"; \
		case "$${resp}" in \
			*"successfully validated"*) ;; \
			*) echo "error: Declarative validation failed for $${f}"; failures=$$((failures+1)) ;; \
		esac; \
	done; \
	if [ "$${failures}" -ne 0 ]; then exit 1; fi

.PHONY: venv build pack builder image format ruff typecheck typecheck-diff lint audit run-pre-commit dev-backend dev-frontend backfill-legacy-forms form-audit pip-audit bandit makemigrations makemigrations-plugin migrate checkmigrations mergemigrations test regen-specs regen-pbm-payloads regen-pbm-payloads-check regen-xtrabackup-variants regen-xtrabackup-variants-check smoke-xtrabackup-variants check-nomad-payload-size check-sidecar-purge release-prep release-rc release-stable trigger-jenkins lint-pipelines encryption-key changelog-add changelog-check changelog-list startapp startapp-check
