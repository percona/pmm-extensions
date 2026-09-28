#!/usr/bin/env python3
# Copyright (C) 2026 Percona LLC
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""Release PMM Extensions — prep a release, cut an RC, or promote to stable.

Owns the preconditions, version-bump, tagging, GitHub release, and Jenkins
trigger for ``make release-prep``, ``make release-rc`` and
``make release-stable``. All three Makefile targets are thin shims that
forward ``VERSION`` (and ``RC`` for ``release-rc``) to this script.

Subcommands:

- ``prep``: Day-27 prep step. Creates ``release/vX.Y.Z`` from ``main``
  HEAD, dispatches the Jira ``version-create`` automation webhook to lock
  the ``fixVersion=sep-next`` scope, opens the ``main`` dev-version-bump
  PR, smoke-builds the wheel, and triggers an internal-registry-only
  Jenkins build for the team-wide internal QA day before rc1 publishes
  to Docker Hub. No version bump, no tag.
- ``rc``: cut release candidate ``vX.Y.ZrcN``. ``RC=1`` runs from
  ``main`` (legacy fresh-from-main path) or, if a prior ``prep`` already
  pushed ``release/vX.Y.Z``, from that existing branch (after-prep
  path) — idempotent against the scope-lock side effects already done
  by prep. ``RC>1`` runs from the existing release branch.
- ``stable``: promote ``release/vX.Y.Z`` to stable ``vX.Y.Z`` and
  back-merge into ``main``. The Jira ``version-released`` automation
  webhook is dispatched from the ``trigger-jenkins`` Makefile rule, gated
  on the same ``JENKINS_*`` env vars that gate the build trigger itself.

All webhook dispatch is best-effort: ``scripts/post_jira_webhook.py`` exits
non-zero on failure with a redacted warning on stderr, and the "Next steps"
output always carries the manual reminder. The Jenkins trigger is best-effort
too, but explicitly: every flow passes ``JENKINS_OPTIONAL=1``, so unset
``JENKINS_*`` credentials or a failed trigger print a ``WARNING`` on stderr
instead of aborting a flow that has already pushed its release branch
(``prep``) or its tag (``rc``, ``stable``). A bare
``make trigger-jenkins`` fails on either.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

PYPROJECT = Path("pyproject.toml")
APP_INIT = Path("app/__init__.py")
WEBHOOK_CREATE_URL_ENV = "JIRA_VERSION_CREATE_WEBHOOK_URL"
WEBHOOK_CREATE_AUTH_ENV = "JIRA_VERSION_CREATE_WEBHOOK_SECRET"
WEBHOOK_RELEASE_URL_ENV = "JIRA_VERSION_RELEASE_WEBHOOK_URL"
WEBHOOK_RELEASE_AUTH_ENV = "JIRA_VERSION_RELEASE_WEBHOOK_SECRET"
POST_JIRA_WEBHOOK_SCRIPT = Path(__file__).resolve().parent / "post_jira_webhook.py"

_VERSION_LINE_RE: re.Pattern[str] = re.compile(r'^version = ".*"$', re.MULTILINE)
_DUNDER_VERSION_LINE_RE: re.Pattern[str] = re.compile(
    r'^__version__ = ".*"$',
    re.MULTILINE,
)

# ``git ls-remote --exit-code`` returns 2 when the ref is missing; any other
# non-zero exit indicates a network/auth error worth surfacing.
_LS_REMOTE_NOT_FOUND_EXIT_CODE = 2


def _run(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Invoke a subprocess with sensible defaults.

    :param cmd: The command and its arguments.
    :type cmd: list[str]
    :param check: Raise ``CalledProcessError`` on a non-zero exit when ``True``.
    :type check: bool
    :param capture: Capture stdout/stderr instead of inheriting them.
    :type capture: bool
    :return: The completed process.
    :rtype: subprocess.CompletedProcess[str]
    """
    return subprocess.run(
        cmd,
        check=check,
        text=True,
        capture_output=capture,
    )


def _current_branch() -> str:
    """Return the current git branch name.

    :return: The branch name.
    :rtype: str
    """
    result = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], capture=True)
    return result.stdout.strip()


def _working_tree_clean() -> bool:
    """Return ``True`` when ``git status --porcelain`` is empty.

    :return: ``True`` if the working tree has no uncommitted changes.
    :rtype: bool
    """
    result = _run(["git", "status", "--porcelain"], capture=True)
    return not result.stdout.strip()


def _local_branch_exists(branch: str) -> bool:
    """Return ``True`` when a local branch exists.

    :param branch: The branch name to check.
    :type branch: str
    :return: ``True`` if the local branch exists.
    :rtype: bool
    """
    result = _run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        check=False,
    )
    return result.returncode == 0


def _remote_branch_exists(branch: str) -> bool:
    """Return ``True`` when ``branch`` exists on ``origin``.

    Uses ``git ls-remote --exit-code`` which exits ``0`` when the ref is
    found, ``2`` when it is missing, and ``1`` (or higher) for network /
    auth errors. The missing case is the happy path here; a network error
    is surfaced as ``CalledProcessError`` so the operator sees the real
    failure rather than silently falling through to a fresh-from-main
    code path that would later fail in a more confusing way.

    :param branch: The branch name to check on ``origin``.
    :type branch: str
    :return: ``True`` if ``origin/{branch}`` exists.
    :rtype: bool
    :raises subprocess.CalledProcessError: If ``git ls-remote`` exits with
        any code other than ``0`` (found) or ``2`` (missing).
    """
    result = _run(
        ["git", "ls-remote", "--exit-code", "origin", f"refs/heads/{branch}"],
        check=False,
        capture=True,
    )
    if result.returncode == 0:
        return True
    if result.returncode == _LS_REMOTE_NOT_FOUND_EXIT_CODE:
        return False
    raise subprocess.CalledProcessError(
        result.returncode,
        ["git", "ls-remote", "--exit-code", "origin", f"refs/heads/{branch}"],
        output=result.stdout,
        stderr=result.stderr,
    )


def _gh_available() -> bool:
    """Return ``True`` when the ``gh`` CLI is on ``PATH``.

    :return: ``True`` if ``gh`` is available.
    :rtype: bool
    """
    return shutil.which("gh") is not None


def _gh_api(
    method: str,
    path: str,
    *,
    payload: dict | None = None,
) -> dict:
    """Call ``gh api`` and return the parsed JSON response.

    Uses the ``gh`` CLI's built-in auth (``GH_TOKEN`` / ``GITHUB_TOKEN`` env
    or ``gh auth login``) and the ``{owner}``/``{repo}`` placeholders in
    ``path`` are substituted by ``gh`` from the current repository context,
    so callers pass them literally (e.g. ``repos/{owner}/{repo}/git/refs``).

    :param method: HTTP method (``GET``, ``POST``, ``PATCH``).
    :type method: str
    :param path: API path, with ``{owner}``/``{repo}`` placeholders.
    :type path: str
    :param payload: Optional JSON-serializable body (sent on stdin with
        ``--input -``).
    :type payload: dict | None
    :return: Parsed JSON response body, or ``{}`` when the response is empty.
    :rtype: dict
    """
    cmd = ["gh", "api", "--method", method, path]
    input_text = None
    if payload is not None:
        cmd.extend(["--input", "-"])
        input_text = json.dumps(payload)
    result = subprocess.run(
        cmd,
        check=True,
        text=True,
        capture_output=True,
        input=input_text,
    )
    return json.loads(result.stdout) if result.stdout.strip() else {}


def _api_branch_head_sha(branch: str) -> str:
    """Return the head commit SHA for ``branch`` on the remote.

    :param branch: Branch name.
    :type branch: str
    :return: Head commit SHA.
    :rtype: str
    """
    ref = _gh_api("GET", f"repos/{{owner}}/{{repo}}/git/ref/heads/{branch}")
    return ref["object"]["sha"]


def _api_push_signed_commit(
    branch: str,
    *,
    base_sha: str,
    message: str,
    files: dict[str, str],
    create_branch: bool,
) -> str:
    """Create a signed commit via the git-data API and publish it on ``branch``.

    Commits produced via ``POST /repos/{o}/{r}/git/commits`` are auto-signed
    by GitHub's ``web-flow`` GPG key, so a tag ref that subsequently points at
    the returned SHA satisfies the ``required_signatures`` rule on the
    ``refs/tags/v*`` ruleset without requiring any GPG key management in the
    runner.

    :param branch: Target branch name.
    :type branch: str
    :param base_sha: Parent commit SHA for the new commit.
    :type base_sha: str
    :param message: Commit message.
    :type message: str
    :param files: Mapping of repository path → full new file content.
    :type files: dict[str, str]
    :param create_branch: When ``True``, create the branch ref at the new
        commit; otherwise update an existing branch ref.
    :type create_branch: bool
    :return: The new commit SHA.
    :rtype: str
    """
    base_commit = _gh_api(
        "GET",
        f"repos/{{owner}}/{{repo}}/git/commits/{base_sha}",
    )
    tree_entries = [
        {"path": path, "mode": "100644", "type": "blob", "content": content}
        for path, content in files.items()
    ]
    new_tree = _gh_api(
        "POST",
        "repos/{owner}/{repo}/git/trees",
        payload={"base_tree": base_commit["tree"]["sha"], "tree": tree_entries},
    )
    new_commit = _gh_api(
        "POST",
        "repos/{owner}/{repo}/git/commits",
        payload={
            "message": message,
            "tree": new_tree["sha"],
            "parents": [base_sha],
        },
    )
    new_sha = new_commit["sha"]
    if create_branch:
        _gh_api(
            "POST",
            "repos/{owner}/{repo}/git/refs",
            payload={"ref": f"refs/heads/{branch}", "sha": new_sha},
        )
    else:
        _gh_api(
            "PATCH",
            f"repos/{{owner}}/{{repo}}/git/refs/heads/{branch}",
            payload={"sha": new_sha},
        )
    return new_sha


def _api_create_tag_ref(tag: str, target_sha: str) -> None:
    """Create a tag ref pointing at ``target_sha``.

    The target commit was produced by :func:`_api_push_signed_commit` and is
    therefore already signed by GitHub's ``web-flow`` key, satisfying the
    ``required_signatures`` rule on ``refs/tags/v*``.

    :param tag: Tag name (e.g. ``v0.12.0rc1``).
    :type tag: str
    :param target_sha: Commit SHA to tag.
    :type target_sha: str
    """
    _gh_api(
        "POST",
        "repos/{owner}/{repo}/git/refs",
        payload={"ref": f"refs/tags/{tag}", "sha": target_sha},
    )


def _publish_signed_release_commit_and_tag(
    *,
    branch: str,
    base_branch: str,
    tag: str,
    commit_message: str,
    create_branch: bool,
) -> None:
    """Create the bump commit + tag via the API, then sync the local branch.

    Extracts the "API commit + tag + local sync" block shared by
    :func:`cmd_rc` and :func:`cmd_stable`. The commit is signed by GitHub's
    ``web-flow`` key, so the tag pointing at it satisfies the
    ``required_signatures`` rule on ``refs/tags/v*``.

    :param branch: Target branch to publish the bump commit on.
    :type branch: str
    :param base_branch: Branch whose head is the parent of the new commit
        (equals ``branch`` except on RC=1, where it is ``main``).
    :type base_branch: str
    :param tag: Tag name to create (e.g. ``v0.12.0rc1``).
    :type tag: str
    :param commit_message: Commit message for the bump commit.
    :type commit_message: str
    :param create_branch: When ``True``, create ``branch`` on the remote at
        the new commit; otherwise update an existing branch ref.
    :type create_branch: bool
    """
    base_sha = _api_branch_head_sha(base_branch)
    commit_sha = _api_push_signed_commit(
        branch,
        base_sha=base_sha,
        message=commit_message,
        files={
            "pyproject.toml": PYPROJECT.read_text(encoding="utf-8"),
            "app/__init__.py": APP_INIT.read_text(encoding="utf-8"),
        },
        create_branch=create_branch,
    )
    _api_create_tag_ref(tag, commit_sha)

    print("==> Syncing local state...")
    _run(["git", "fetch", "origin", branch, "--tags"])
    _run(["git", "reset", "--hard", f"origin/{branch}"])


def _publish_release_commit_and_tag(
    *,
    branch: str,
    base_branch: str,
    tag: str,
    commit_message: str,
    create_branch: bool,
    via_github_api: bool,
) -> None:
    """Publish the bump commit + tag, dispatching on the signing mode.

    When ``via_github_api`` is ``True``, delegate to
    :func:`_publish_signed_release_commit_and_tag` (commit signed by
    GitHub's ``web-flow`` key, required by the ``refs/tags/v*`` ruleset).
    Otherwise commit, tag, and push via local ``git`` — ``base_branch``
    and ``create_branch`` are unused in that path because the local
    branch already tracks the intended history.

    :param branch: Target branch to publish the bump commit on.
    :type branch: str
    :param base_branch: API-path parent branch (ignored in the git path).
    :type base_branch: str
    :param tag: Tag name to create (e.g. ``v0.12.0rc1``).
    :type tag: str
    :param commit_message: Commit message for the bump commit.
    :type commit_message: str
    :param create_branch: API-path branch-creation flag (ignored in the
        git path).
    :type create_branch: bool
    :param via_github_api: When ``True``, use the GitHub git-data API;
        otherwise use local ``git commit``/``tag``/``push``.
    :type via_github_api: bool
    """
    if via_github_api:
        print("==> Creating signed bump commit + tag via GitHub API...")
        _publish_signed_release_commit_and_tag(
            branch=branch,
            base_branch=base_branch,
            tag=tag,
            commit_message=commit_message,
            create_branch=create_branch,
        )
        return
    print("==> Committing version bump...")
    _run(["git", "commit", "-am", commit_message])
    print(f"==> Tagging {tag}...")
    _run(["git", "tag", tag])
    print("==> Pushing branch and tag...")
    _run(["git", "push", "origin", branch, tag])


def _bump_version(pep440_version: str, tag_version: str) -> None:
    """Rewrite the version string in ``pyproject.toml`` and ``app/__init__.py``.

    :param pep440_version: The PEP 440 version (no ``v`` prefix) for
        ``pyproject.toml`` (e.g. ``0.12.0rc1``).
    :type pep440_version: str
    :param tag_version: The tag-style version (with ``v`` prefix) for
        ``app/__init__.py`` (e.g. ``v0.12.0rc1``).
    :type tag_version: str
    """
    pyproject_text = PYPROJECT.read_text(encoding="utf-8")
    new_pyproject_text = _VERSION_LINE_RE.sub(
        f'version = "{pep440_version}"',
        pyproject_text,
        count=1,
    )
    PYPROJECT.write_text(new_pyproject_text, encoding="utf-8")

    init_text = APP_INIT.read_text(encoding="utf-8")
    new_init_text = _DUNDER_VERSION_LINE_RE.sub(
        f'__version__ = "{tag_version}"',
        init_text,
        count=1,
    )
    APP_INIT.write_text(new_init_text, encoding="utf-8")


def _invoke_post_jira_webhook(url_env: str, auth_env: str, version_tag: str) -> bool:
    """Run ``scripts/post_jira_webhook.py`` and return whether it succeeded.

    Used by ``cmd_rc`` for the version-create webhook. The stable-release
    webhook is dispatched from the ``trigger-jenkins`` Makefile rule instead,
    so it shares the same Jenkins-env gate as the build trigger.

    :param url_env: The environment-variable name holding the webhook URL.
    :type url_env: str
    :param auth_env: The environment-variable name holding the webhook token.
    :type auth_env: str
    :param version_tag: The version-name payload value (e.g. ``v0.12.0``).
    :type version_tag: str
    :return: ``True`` when the helper script exits ``0``, ``False`` otherwise.
    :rtype: bool
    """
    proc = _run(
        [
            sys.executable,
            str(POST_JIRA_WEBHOOK_SCRIPT),
            "--url-env",
            url_env,
            "--auth-env",
            auth_env,
            "--version-tag",
            version_tag,
        ],
        check=False,
    )
    return proc.returncode == 0


def _print_rc_next_steps(
    version: str,
    rc: int,
    *,
    jira_version_created: bool,
) -> None:
    """Print the RC "Next steps" block on stdout.

    :param version: The X.Y.Z release version (no ``v`` prefix).
    :type version: str
    :param rc: The RC number.
    :type rc: int
    :param jira_version_created: When ``True``, omit the "Create Jira version"
        reminder. Also omitted for RC > 1 (the version was created on RC=1).
    :type jira_version_created: bool
    """
    print()
    print("Next steps:")
    step = 1
    if rc == 1 and not jira_version_created:
        print(f"  {step}. Create Jira version {version} (if not already created)")
        step += 1
    print(f"  {step}. Deploy to staging and verify")
    print(f"  {step + 1}. Notify the team")


def _validate_clean_tree_and_gh(*, sign_via_github_api: bool) -> int | None:
    """Validate working-tree-clean and gh-availability preconditions.

    Shared by ``cmd_prep`` and ``cmd_rc`` to keep the two functions below
    ruff's complexity thresholds. Prints the appropriate error message on
    stderr and returns the exit code to surface; returns ``None`` when both
    preconditions are met.

    :param sign_via_github_api: When ``True``, require the ``gh`` CLI.
    :type sign_via_github_api: bool
    :return: Exit code on failure, ``None`` on success.
    :rtype: int | None
    """
    if not _working_tree_clean():
        print(
            "Error: Working tree is not clean. Commit or stash changes first.",
            file=sys.stderr,
        )
        return 1
    if sign_via_github_api and not _gh_available():
        print(
            "Error: --sign-via-github-api requires the gh CLI "
            "(https://cli.github.com/). Omit the flag to use local git "
            "commit/tag/push instead.",
            file=sys.stderr,
        )
        return 1
    return None


def _prepare_release_branch_for_rc(
    branch: str,
    *,
    rc: int,
    after_prep: bool,
) -> int:
    """Position the working tree on the release branch for ``cmd_rc``.

    Three paths:

    - **rc == 1 + after_prep**: sync to ``origin/{branch}`` non-destructively
      (delegates to :func:`_sync_after_prep_branch`).
    - **rc == 1 + fresh-from-main**: pull main, error if local release
      branch exists, then create the new branch.
    - **rc > 1**: check out the existing release branch and pull.

    :param branch: Release branch name (``release/vX.Y.Z``).
    :type branch: str
    :param rc: The RC number.
    :type rc: int
    :param after_prep: Whether a prior ``prep`` already pushed the branch.
    :type after_prep: bool
    :return: ``0`` on success, non-zero exit code on failure.
    :rtype: int
    """
    if rc == 1 and after_prep:
        return _sync_after_prep_branch(branch)
    if rc == 1:
        print("==> Pulling latest main...")
        _run(["git", "pull", "origin", "main"])
        print(f"==> Creating release branch {branch}...")
        if _local_branch_exists(branch):
            print(
                f"Error: Branch {branch} already exists locally. "
                "Delete it first or use RC>1.",
                file=sys.stderr,
            )
            return 1
        _run(["git", "checkout", "-b", branch])
        return 0
    print(f"==> Checking out existing release branch {branch}...")
    _run(["git", "checkout", branch])
    _run(["git", "pull", "origin", branch])
    return 0


def _create_rc_github_prerelease(rc_tag: str, branch: str, wheel: Path) -> None:
    """Create the RC GitHub pre-release and upload the wheel artifact.

    Skips silently when ``gh`` is unavailable (matches the local-run flow
    where the release manager creates the pre-release in the UI).

    :param rc_tag: The RC tag (e.g. ``v0.12.0rc1``).
    :type rc_tag: str
    :param branch: The release branch to target.
    :type branch: str
    :param wheel: The wheel artifact path to upload.
    :type wheel: pathlib.Path
    """
    if not _gh_available():
        print("Note: gh CLI not found, skipping GitHub release creation.")
        return
    print("==> Creating GitHub pre-release...")
    _run(
        [
            "gh",
            "release",
            "create",
            rc_tag,
            "--prerelease",
            "--generate-notes",
            "--target",
            branch,
        ],
    )
    _run(["gh", "release", "upload", rc_tag, str(wheel)])


def _origin_main_pyproject_version(version_re: re.Pattern[str]) -> str | None:
    """Return ``origin/main``'s ``version = "..."`` value (PEP 440), or None.

    Fetches ``origin/main`` and reads ``pyproject.toml`` from the remote ref
    via ``git show`` so the probe doesn't depend on the local working tree.

    :param version_re: Compiled regex matching the version line in
        ``pyproject.toml`` (passed in to avoid coupling helpers).
    :type version_re: re.Pattern[str]
    :return: The version string, or ``None`` if the fetch / show failed or
        no version line was found.
    :rtype: str | None
    """
    _run(["git", "fetch", "origin", "main"])
    result = _run(
        ["git", "show", "origin/main:pyproject.toml"],
        check=False,
        capture=True,
    )
    if result.returncode != 0:
        return None
    match = version_re.search(result.stdout)
    if match is None:
        return None
    return match.group(0).split('"')[1]


def _maybe_open_dev_bump_pr(version: str) -> int:
    """Open the dev-bump PR on main unless main is already on the next .dev0.

    Scope-lock + dev-bump are atomic. Main runs at the next .dev0 for the
    entire QA window — fixes during QA land on the release branch (no
    main-first cherry-picks under the back-merge model). To stay idempotent
    against a prior ``prep`` whose dev-bump PR has already been merged, probe
    ``origin/main``'s actual version: if main is already at
    ``vX.Y+1.0.dev0``, the PR landed (whether or not GitHub auto-deleted the
    source branch). When main is still at the previous ``.dev0`` but the
    bump branch exists on origin (an open PR awaiting merge), abort with a
    clear operator instruction rather than try to recreate the PR and fail
    at ``git push`` on the duplicate branch.

    :param version: The X.Y.Z release version (no ``v`` prefix).
    :type version: str
    :return: ``0`` on success / skip, ``1`` when an open dev-bump PR blocks
        progress and requires operator action.
    :rtype: int
    """
    parts = version.split(".")
    next_dev_version = f"{parts[0]}.{int(parts[1]) + 1}.0.dev0"
    origin_main_version = _origin_main_pyproject_version(_VERSION_LINE_RE)
    if origin_main_version == next_dev_version:
        print(
            f"==> Skipping dev version bump PR — origin/main is already at "
            f"{next_dev_version} (dev-bump from a prior prep was merged)."
        )
        return 0
    dev_branch = f"bump-dev-version-{next_dev_version}"
    if _remote_branch_exists(dev_branch):
        print(
            f"Error: origin/{dev_branch} exists but origin/main has not "
            f"been bumped to {next_dev_version} yet. The rc1 release has "
            "already been published; the only outstanding step is to merge "
            "the dev-bump PR opened by `make release-prep` (release-manager "
            "bypass) before the next release cycle. No rc1 re-dispatch "
            "needed.",
            file=sys.stderr,
        )
        return 1
    _create_dev_version_bump_pr(version, f"v{version}")
    return 0


def _sync_after_prep_branch(branch: str) -> int:
    """Sync to an existing ``release/vX.Y.Z`` branch after a prior ``prep``.

    Non-destructive: when a local branch already exists and is ahead of
    origin, refuse to reset so the operator can deal with the local
    commits. Otherwise reset to ``origin/{branch}``, or create the local
    branch from origin when no local branch exists.

    :param branch: Release branch name (``release/vX.Y.Z``).
    :type branch: str
    :return: ``0`` on successful sync, ``1`` on refusal-to-reset.
    :rtype: int
    """
    print(f"==> Syncing to existing release branch {branch} (after prep)...")
    _run(["git", "fetch", "origin", branch])
    if not _local_branch_exists(branch):
        _run(["git", "checkout", "-b", branch, f"origin/{branch}"])
        return 0
    ahead = _run(
        ["git", "rev-list", "--count", f"origin/{branch}..{branch}"],
        check=False,
        capture=True,
    )
    if ahead.returncode != 0:
        print(
            f"Error: git rev-list failed (exit {ahead.returncode}) while "
            f"checking if {branch} is ahead of origin/{branch}. Refusing "
            "to reset without a clean ahead-count signal — inspect manually.",
            file=sys.stderr,
        )
        return 1
    ahead_count = int(ahead.stdout.strip() or "0")
    if ahead_count > 0:
        print(
            f"Error: local {branch} is ahead of origin/{branch} by "
            f"{ahead_count} commits — refusing to reset. Push the local "
            "commits or delete the local branch before re-running.",
            file=sys.stderr,
        )
        return 1
    _run(["git", "checkout", branch])
    _run(["git", "reset", "--hard", f"origin/{branch}"])
    return 0


def _print_prep_next_steps(
    version: str,
    head_sha: str,
    *,
    jira_version_created: bool,
) -> None:
    """Print the prep "Next steps" block on stdout.

    :param version: The X.Y.Z release version (no ``v`` prefix).
    :type version: str
    :param head_sha: The HEAD commit SHA of the freshly-pushed release branch
        — the operator needs it to identify the internal-registry image tag.
    :type head_sha: str
    :param jira_version_created: When ``True``, omit the "Create Jira version"
        reminder.
    :type jira_version_created: bool
    """
    print()
    print("Next steps:")
    step = 1
    if not jira_version_created:
        print(f"  {step}. Create Jira version {version} (if not already created)")
        step += 1
    print(
        f"  {step}. Internal image SHA: {head_sha} — "
        "deploy to team01 and announce on #gas-team for internal QA day."
    )
    print(
        f"  {step + 1}. Merge the dev-version-bump PR opened on main so the "
        "next .dev0 lands before any post-scope-freeze PR."
    )
    print(
        f"  {step + 2}. Tomorrow (Day 28), dispatch "
        f"`release_type: rc, rc_number: 1, version: {version}` "
        "to cut rc1 from the release branch."
    )


def _print_stable_next_steps(version: str) -> None:
    """Print the stable "Next steps" block on stdout.

    The "Mark Jira version as released" reminder is always printed: the Jira
    release webhook fires inside the ``trigger-jenkins`` Makefile rule, whose
    success/failure this script no longer observes. Marking an
    already-released version through the Jira UI is a harmless no-op.

    :param version: The X.Y.Z release version (no ``v`` prefix).
    :type version: str
    """
    print()
    print(f"Verified: v{version} is an ancestor of origin/main.")
    print()
    print("Next steps:")
    print("  1. Publish release notes")
    print(f"  2. Mark Jira version {version} as released")


def cmd_prep(version: str, *, sign_via_github_api: bool) -> int:
    """Prepare a release on Day 27: scope-lock without bumping or tagging.

    Carries the scope-lock side effects of today's atomic ``cmd_rc(RC=1)`` —
    create + push the release branch, fire the Jira version-create webhook,
    open the dev-bump PR on main, smoke-build the wheel, and trigger an
    internal-only Jenkins build (``pushImageDocker=false``) tagged by HEAD
    SHA. The follow-up ``cmd_rc(RC=1)`` (Day 28) does the version bump, tag,
    GH pre-release, and the Docker-Hub-pushing Jenkins build.

    :param version: The X.Y.Z release version (no ``v`` prefix).
    :type version: str
    :param sign_via_github_api: Forwarded to the (subsequent) RC1 run; not
        used by ``prep`` itself because ``prep`` does not create a tag.
        Accepted here only so the workflow can pass the same flag through
        a single env-driven path. ``gh`` availability is still validated
        when ``True`` so a missing CLI surfaces at prep time rather than
        the next day.
    :type sign_via_github_api: bool
    :return: Process exit code (``0`` on success).
    :rtype: int
    """
    branch = f"release/v{version}"

    current = _current_branch()
    if current != "main":
        print(
            f"Error: prep requires being on the main branch (currently on {current})",
            file=sys.stderr,
        )
        return 1
    precondition_err = _validate_clean_tree_and_gh(
        sign_via_github_api=sign_via_github_api,
    )
    if precondition_err is not None:
        return precondition_err
    if _local_branch_exists(branch):
        print(
            f"Error: Branch {branch} already exists locally. "
            "Delete it first or proceed directly to rc1.",
            file=sys.stderr,
        )
        return 1
    if _remote_branch_exists(branch):
        print(
            f"Error: Branch {branch} already exists on origin. "
            "Either delete the orphan branch and re-run prep, or proceed "
            "directly to `release_type: rc, rc_number: 1` (after-prep path).",
            file=sys.stderr,
        )
        return 1

    print("==> Pulling latest main...")
    _run(["git", "pull", "origin", "main"])
    print(f"==> Creating release branch {branch}...")
    _run(["git", "checkout", "-b", branch])
    print(f"==> Pushing release branch {branch} to origin...")
    _run(["git", "push", "-u", "origin", branch])

    jira_version_created = _invoke_post_jira_webhook(
        WEBHOOK_CREATE_URL_ENV,
        WEBHOOK_CREATE_AUTH_ENV,
        f"v{version}",
    )

    print("==> Building wheel (smoke build)...")
    _run(["make", "build"])
    current_version = _read_pyproject_version()
    wheel = Path("dist") / f"pmm_extensions-{current_version}-py3-none-any.whl"
    if not wheel.exists():
        print(
            f"Error: Wheel not found at {wheel} after build. Aborting before "
            "internal Jenkins trigger.",
            file=sys.stderr,
        )
        return 1

    head_sha = _run(["git", "rev-parse", "HEAD"], capture=True).stdout.strip()
    print(f"==> Triggering internal Jenkins build for SHA {head_sha}...")
    _run(
        [
            "make",
            "trigger-jenkins",
            f"TAG={head_sha}",
            "PUSH_IMAGE_DOCKER=false",
            "JENKINS_OPTIONAL=1",
        ],
    )

    # Scope-lock + dev-bump are atomic for minor releases. Patch releases
    # already cut from a main branch that is on the next .dev0 version.
    if version.split(".")[2] == "0":
        _create_dev_version_bump_pr(version, f"v{version}")

    print()
    print(f"=== Prep for v{version} completed successfully ===")
    _print_prep_next_steps(
        version=version,
        head_sha=head_sha,
        jira_version_created=jira_version_created,
    )
    return 0


def _read_pyproject_version() -> str:
    """Return the current ``version = "..."`` string from ``pyproject.toml``.

    Used by ``cmd_prep`` to compute the wheel filename for the smoke build —
    no version bump runs in ``prep``, so the wheel is named after main's
    current ``.dev0`` value.

    :return: The PEP 440 version string from ``pyproject.toml``.
    :rtype: str
    """
    match = _VERSION_LINE_RE.search(PYPROJECT.read_text(encoding="utf-8"))
    if match is None:
        raise RuntimeError("Could not find version line in pyproject.toml")
    return match.group(0).split('"')[1]


def cmd_rc(version: str, rc: int, *, sign_via_github_api: bool) -> int:
    """Cut release candidate ``vX.Y.ZrcN``.

    :param version: The X.Y.Z release version (no ``v`` prefix).
    :type version: str
    :param rc: The RC number (positive integer).
    :type rc: int
    :param sign_via_github_api: When ``True``, create the bump commit + tag
        via the GitHub git-data API, producing a ``web-flow``-signed target
        that satisfies the ``refs/tags/v*`` ``required_signatures`` rule.
        Requires the ``gh`` CLI. When ``False`` (the default for local
        runs), commit, tag, and push via local ``git`` — relies on the
        user's own git signing setup.
    :type sign_via_github_api: bool
    :return: Process exit code (``0`` on success).
    :rtype: int
    """
    branch = f"release/v{version}"
    rc_version = f"{version}rc{rc}"
    rc_tag = f"v{rc_version}"

    current = _current_branch()
    after_prep = False
    if rc == 1:
        after_prep = _remote_branch_exists(branch)
        if not after_prep and current != "main":
            print(
                f"Error: RC=1 requires being on the main branch "
                f"(currently on {current}). If a prior `prep` already pushed "
                f"{branch}, this run would have taken the after-prep path "
                "instead — verify the branch exists on origin.",
                file=sys.stderr,
            )
            return 1
    precondition_err = _validate_clean_tree_and_gh(
        sign_via_github_api=sign_via_github_api,
    )
    if precondition_err is not None:
        return precondition_err

    branch_err = _prepare_release_branch_for_rc(branch, rc=rc, after_prep=after_prep)
    if branch_err != 0:
        return branch_err

    jira_version_created = False
    if rc == 1:
        # Re-fire is harmless: rule #16 is naturally idempotent (re-creating
        # an existing Jira version is a no-op per the rule definition) and
        # the helper script is best-effort. The returned bool governs the
        # "Create Jira version" reminder — when after-prep and the local
        # env is missing the webhook secrets, the helper returns False and
        # we correctly print the reminder, because the planner cannot
        # actually verify Day 27's webhook succeeded just from branch
        # existence.
        jira_version_created = _invoke_post_jira_webhook(
            WEBHOOK_CREATE_URL_ENV,
            WEBHOOK_CREATE_AUTH_ENV,
            f"v{version}",
        )

    print(f"==> Bumping version to {rc_version}...")
    _bump_version(rc_version, rc_tag)

    print("==> Building wheel...")
    _run(["make", "build"])
    wheel = Path("dist") / f"pmm_extensions-{rc_version}-py3-none-any.whl"
    if not wheel.exists():
        print(
            f"Error: Wheel not found at {wheel} after build. Aborting before push.",
            file=sys.stderr,
        )
        return 1

    fresh_rc1 = rc == 1 and not after_prep
    _publish_release_commit_and_tag(
        branch=branch,
        base_branch="main" if fresh_rc1 else branch,
        tag=rc_tag,
        commit_message=f"Bump version to {rc_tag}",
        create_branch=fresh_rc1,
        via_github_api=sign_via_github_api,
    )

    _create_rc_github_prerelease(rc_tag, branch, wheel)

    print()
    print(f"=== RC {rc_version} released successfully ===")
    print()
    _run(["make", "trigger-jenkins", f"TAG={rc_tag}", "JENKINS_OPTIONAL=1"])

    dev_bump_exit_code = 0
    # Only minor RC1 opens the next dev-bump PR. Patch releases already cut
    # from a main branch that is on that next .dev0 version.
    if rc == 1 and version.split(".")[2] == "0":
        dev_bump_exit_code = _maybe_open_dev_bump_pr(version)

    _print_rc_next_steps(
        version=version,
        rc=rc,
        jira_version_created=jira_version_created,
    )
    return dev_bump_exit_code


def _create_dev_version_bump_pr(version: str, stable_tag: str) -> None:
    """Branch from ``origin/main``, bump to the next dev version, push and open a PR.

    When ``GH_PR_TOKEN`` is set in the environment, override ``GH_TOKEN`` with
    its value for the ``gh pr create`` call only. This lets the workflow keep a
    PAT in ``GH_TOKEN`` for tag and release operations while delegating the PR
    creation to a token with ``pull-requests: write`` (typically the workflow's
    own ``GITHUB_TOKEN``).

    :param version: The X.Y.Z stable release version (no ``v`` prefix).
    :type version: str
    :param stable_tag: The stable release tag (with ``v`` prefix) for the PR body.
    :type stable_tag: str
    """
    print("==> Creating dev version bump PR on main...")
    parts = version.split(".")
    dev_version = f"{parts[0]}.{int(parts[1]) + 1}.0.dev0"
    dev_branch = f"bump-dev-version-{dev_version}"
    _run(["git", "fetch", "origin", "main"])
    _run(["git", "checkout", "-b", dev_branch, "origin/main"])
    _bump_version(dev_version, f"v{dev_version}")
    _run(["git", "commit", "-am", f"Bump version to v{dev_version}"])
    _run(["git", "push", "-u", "origin", dev_branch])
    if not _gh_available():
        print(
            f"Note: gh CLI not found. Manually create a PR from {dev_branch} to main.",
        )
        return
    pr_token = os.environ.get("GH_PR_TOKEN")
    saved_gh_token = os.environ.get("GH_TOKEN")
    if pr_token:
        os.environ["GH_TOKEN"] = pr_token
    try:
        _run(
            [
                "gh",
                "pr",
                "create",
                "--base",
                "main",
                "--title",
                f"chore(release): bump dev version to v{dev_version}",
                "--body",
                f"Automated dev version bump after scope-locking {stable_tag} into release/{stable_tag}.",
                "--label",
                "qa not required",
            ],
        )
    finally:
        if pr_token:
            if saved_gh_token is None:
                os.environ.pop("GH_TOKEN", None)
            else:
                os.environ["GH_TOKEN"] = saved_gh_token


def _has_unmerged_paths() -> bool:
    """Return ``True`` if any path in the working tree has unresolved merge conflicts.

    :return: ``True`` if ``git ls-files -u`` reports any entries.
    :rtype: bool
    """
    result = _run(["git", "ls-files", "-u"], check=False, capture=True)
    return bool(result.stdout.strip())


def _checkout_ours_if_unmerged(path: str) -> None:
    """Run ``git checkout --ours <path>`` only if ``path`` is unmerged.

    :param path: The repository-relative path to potentially resolve.
    :type path: str
    """
    result = _run(["git", "ls-files", "-u", path], check=False, capture=True)
    if result.stdout.strip():
        _run(["git", "checkout", "--ours", path])


def _back_merge_release_into_main(
    *, version: str, release_branch: str, tag: str
) -> int:
    """Back-merge ``release_branch`` into main and assert the ancestor invariant.

    Sequence:

    1. ``git fetch origin``
    2. ``git checkout main`` (and pull to align with origin/main)
    3. ``git merge --no-ff release_branch`` — expected to conflict on
       ``CHANGELOG.md`` and the version files
    4. Resolve CHANGELOG via ``scripts/changelog.py resolve-backmerge --release X.Y.Z``
    5. Resolve version files by re-checking out main's side (the dev-bumped
       value wins; the release branch's stable version must not overwrite it)
    6. ``git commit`` to finalize the merge
    7. ``git push origin main``
    8. Assert ``git merge-base --is-ancestor vX.Y.Z origin/main`` exits 0

    On step-8 failure, this function returns non-zero **without** deleting the
    release branch, so the operator can inspect / re-attempt before losing
    state.

    :param version: The X.Y.Z release version (no ``v`` prefix).
    :type version: str
    :param release_branch: The release branch (``release/vX.Y.Z``).
    :type release_branch: str
    :param tag: The release tag (``vX.Y.Z``).
    :type tag: str
    :return: ``0`` on success, non-zero if any step or the invariant fails.
    :rtype: int
    """
    print("==> Fetching origin for back-merge...")
    _run(["git", "fetch", "origin"])
    print("==> Checking out main...")
    _run(["git", "checkout", "main"])
    _run(["git", "pull", "origin", "main"])

    print(f"==> Merging {release_branch} into main with --no-ff...")
    # Expect a non-zero exit because of the CHANGELOG / version-file
    # conflict — that's the whole point of this step.
    # Do not use `-s ours`: it records ancestry but drops the release-side
    # commits, which does not fix the SHA-split compare-link problem the
    # back-merge model is solving.
    merge_result = _run(
        ["git", "merge", "--no-ff", "--no-commit", release_branch],
        check=False,
    )
    if merge_result.returncode != 0 and not _has_unmerged_paths():
        print(
            "error: git merge --no-ff exited non-zero but did not leave a "
            "conflict to resolve; aborting back-merge.",
            file=sys.stderr,
        )
        return 1

    print("==> Resolving CHANGELOG.md and changelog.d/ via scripts/changelog.py...")
    resolve_result = _run(
        [
            sys.executable,
            str(Path(__file__).resolve().parent / "changelog.py"),
            "resolve-backmerge",
            "--release",
            version,
        ],
        check=False,
    )
    if resolve_result.returncode != 0:
        print(
            "error: scripts/changelog.py resolve-backmerge failed; aborting "
            "back-merge. The working tree is left in a mid-merge state — run "
            "'git merge --abort' to clean up before retrying.",
            file=sys.stderr,
        )
        return 1

    print(
        "==> Resolving version files by keeping main's dev version (if conflicted)..."
    )
    for path in (str(PYPROJECT), str(APP_INIT)):
        _checkout_ours_if_unmerged(path)

    print("==> Staging resolved files...")
    _run(
        [
            "git",
            "add",
            "CHANGELOG.md",
            "changelog.d",
            str(PYPROJECT),
            str(APP_INIT),
        ]
    )

    print("==> Finalizing the merge commit...")
    _run(
        [
            "git",
            "commit",
            "--no-edit",
            "-m",
            f"Merge release/v{version} into main (back-merge for {tag})",
        ],
    )

    print("==> Pushing main...")
    _run(["git", "push", "origin", "main"])

    print(f"==> Asserting ancestor invariant on local main: {tag} ...")
    local_invariant = _run(
        ["git", "merge-base", "--is-ancestor", tag, "main"],
        check=False,
    )
    if local_invariant.returncode != 0:
        print(
            f"error: post-back-merge invariant failed locally — {tag} is NOT an "
            "ancestor of main. Leaving release branch in place for "
            "investigation.",
            file=sys.stderr,
        )
        return 1

    print(f"==> Refreshing origin/main and asserting invariant remotely: {tag} ...")
    _run(["git", "fetch", "origin", "main"])
    remote_invariant = _run(
        ["git", "merge-base", "--is-ancestor", tag, "origin/main"],
        check=False,
    )
    if remote_invariant.returncode != 0:
        print(
            f"error: post-back-merge invariant failed against origin/main — "
            f"{tag} is NOT an ancestor of origin/main. The push may have been "
            "rejected or rewound. Leaving release branch in place for "
            "investigation.",
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_stable(version: str, *, sign_via_github_api: bool) -> int:
    """Promote ``release/vX.Y.Z`` to stable ``vX.Y.Z``.

    :param version: The X.Y.Z release version (no ``v`` prefix).
    :type version: str
    :param sign_via_github_api: When ``True``, create the bump commit + tag
        via the GitHub git-data API, producing a ``web-flow``-signed target
        that satisfies the ``refs/tags/v*`` ``required_signatures`` rule.
        Requires the ``gh`` CLI. When ``False`` (the default for local
        runs), commit, tag, and push via local ``git`` — relies on the
        user's own git signing setup.
    :type sign_via_github_api: bool
    :return: Process exit code (``0`` on success).
    :rtype: int
    """
    expected_branch = f"release/v{version}"
    tag = f"v{version}"

    current = _current_branch()
    if current != expected_branch:
        print(
            f"Error: Must be on {expected_branch} (currently on {current})",
            file=sys.stderr,
        )
        return 1
    if not _working_tree_clean():
        print(
            "Error: Working tree is not clean. Commit or stash changes first.",
            file=sys.stderr,
        )
        return 1
    if sign_via_github_api and not _gh_available():
        print(
            "Error: --sign-via-github-api requires the gh CLI "
            "(https://cli.github.com/). Omit the flag to use local git "
            "commit/tag/push instead.",
            file=sys.stderr,
        )
        return 1

    print(f"==> Bumping version to {version}...")
    _bump_version(version, tag)

    print("==> Building wheel...")
    _run(["make", "build"])
    wheel = Path("dist") / f"pmm_extensions-{version}-py3-none-any.whl"
    if not wheel.exists():
        print(
            f"Error: Wheel not found at {wheel} after build. Aborting before push.",
            file=sys.stderr,
        )
        return 1

    _publish_release_commit_and_tag(
        branch=expected_branch,
        base_branch=expected_branch,
        tag=tag,
        commit_message=f"Bump version to {tag}",
        create_branch=False,
        via_github_api=sign_via_github_api,
    )

    if _gh_available():
        print("==> Creating GitHub release...")
        _run(["gh", "release", "create", tag, "--generate-notes"])
        _run(["gh", "release", "upload", tag, str(wheel)])
    else:
        print("Note: gh CLI not found, skipping GitHub release creation.")

    back_merge_rc = _back_merge_release_into_main(
        version=version,
        release_branch=expected_branch,
        tag=tag,
    )
    if back_merge_rc != 0:
        return back_merge_rc

    print("==> Deleting release branch (post back-merge)...")
    _run(["git", "push", "origin", "--delete", expected_branch], check=False)
    _run(["git", "branch", "-D", expected_branch], check=False)

    print()
    print(f"=== Stable {version} released successfully ===")
    print()
    _run(
        [
            "make",
            "trigger-jenkins",
            f"TAG={tag}",
            f"WEBHOOK_URL_ENV={WEBHOOK_RELEASE_URL_ENV}",
            f"WEBHOOK_AUTH_ENV={WEBHOOK_RELEASE_AUTH_ENV}",
            "JENKINS_OPTIONAL=1",
        ],
    )

    _print_stable_next_steps(version=version)
    return 0


def _positive_int(raw: str) -> int:
    """Parse ``raw`` as a positive integer for argparse.

    :param raw: The raw CLI argument value.
    :type raw: str
    :return: The parsed positive integer.
    :rtype: int
    :raises argparse.ArgumentTypeError: If ``raw`` is not a positive integer.
    """
    try:
        value = int(raw)
    except ValueError as exc:
        msg = f"expected a positive integer, got {raw!r}"
        raise argparse.ArgumentTypeError(msg) from exc
    if value < 1:
        msg = f"expected a positive integer, got {value}"
        raise argparse.ArgumentTypeError(msg)
    return value


def build_parser() -> argparse.ArgumentParser:
    """Build the argparse parser for the CLI.

    :return: The top-level parser with ``prep``, ``rc``, and ``stable``
        subcommands.
    :rtype: argparse.ArgumentParser
    """
    parser = argparse.ArgumentParser(
        prog="release",
        description="Prep, cut, or promote a PMM Extensions release.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prep_parser = subparsers.add_parser(
        "prep",
        help=(
            "Prep a release on Day 27: create release branch, lock scope, "
            "trigger an internal-only Jenkins build for the team-wide internal "
            "QA day before rc1 is published to Docker Hub."
        ),
    )
    prep_parser.add_argument("--version", required=True, help="X.Y.Z")
    prep_parser.add_argument(
        "--sign-via-github-api",
        action="store_true",
        help=(
            "Validate the gh CLI is available so the subsequent rc1 dispatch "
            "can sign via the GitHub git-data API. Prep itself does not "
            "create a tag."
        ),
    )

    rc_parser = subparsers.add_parser("rc", help="Cut a release candidate.")
    rc_parser.add_argument("--version", required=True, help="X.Y.Z")
    rc_parser.add_argument(
        "--rc",
        required=True,
        type=_positive_int,
        help="RC number (positive integer).",
    )
    rc_parser.add_argument(
        "--sign-via-github-api",
        action="store_true",
        help=(
            "Create the bump commit and tag via the GitHub git-data API "
            "(signed by web-flow). Requires the gh CLI; intended for CI "
            "runs against repositories that require signed tags."
        ),
    )

    stable_parser = subparsers.add_parser(
        "stable",
        help="Promote the release/vX.Y.Z branch to stable vX.Y.Z.",
    )
    stable_parser.add_argument("--version", required=True, help="X.Y.Z")
    stable_parser.add_argument(
        "--sign-via-github-api",
        action="store_true",
        help=(
            "Create the bump commit and tag via the GitHub git-data API "
            "(signed by web-flow). Requires the gh CLI; intended for CI "
            "runs against repositories that require signed tags."
        ),
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """Dispatch to the requested subcommand.

    Catch ``CalledProcessError`` so a failing ``git``/``gh``/``make`` call
    surfaces as a single concise release-tool error line instead of a Python
    stack trace.

    :param argv: Optional argv override for testing.
    :type argv: list[str] | None
    :return: Process exit code.
    :rtype: int
    """
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "prep":
            return cmd_prep(
                args.version,
                sign_via_github_api=args.sign_via_github_api,
            )
        if args.command == "rc":
            return cmd_rc(
                args.version,
                args.rc,
                sign_via_github_api=args.sign_via_github_api,
            )
        return cmd_stable(
            args.version,
            sign_via_github_api=args.sign_via_github_api,
        )
    except subprocess.CalledProcessError as exc:
        cmd = exc.cmd
        if isinstance(cmd, list | tuple):
            cmd_text = " ".join(str(part) for part in cmd)
        else:
            cmd_text = str(cmd)
        print(
            f"release: command failed (exit code {exc.returncode}): {cmd_text}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
