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

"""Tests for the ``scripts/release.py`` CLI."""

import os
import subprocess
from pathlib import Path

import pytest

from tests.scripts import load_script

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

release = load_script("release")


SAMPLE_PYPROJECT = """\
[project]
name = "pmm-extensions"
version = "0.12.0.dev0"
description = "PMM Extensions"
"""

SAMPLE_APP_INIT = '''\
"""PMM Extensions package."""

__version__ = "v0.12.0.dev0"
'''

ARGPARSE_ERROR_EXIT_CODE = 2
LS_REMOTE_NETWORK_ERROR_EXIT_CODE = 128


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Set up a fresh repo layout in ``tmp_path`` and chdir into it.

    :param tmp_path: pytest's per-test temporary directory.
    :type tmp_path: pathlib.Path
    :param monkeypatch: pytest monkeypatch fixture.
    :type monkeypatch: pytest.MonkeyPatch
    :return: The temporary repo root.
    :rtype: pathlib.Path
    """
    (tmp_path / "pyproject.toml").write_text(SAMPLE_PYPROJECT, encoding="utf-8")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text(SAMPLE_APP_INIT, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


# --- _bump_version ---------------------------------------------------------


def test_bump_version_rewrites_pyproject_and_init(repo):
    """``_bump_version`` rewrites the version lines in both files."""
    release._bump_version("0.12.0rc1", "v0.12.0rc1")
    pyproject_text = (repo / "pyproject.toml").read_text(encoding="utf-8")
    init_text = (repo / "app" / "__init__.py").read_text(encoding="utf-8")
    assert 'version = "0.12.0rc1"' in pyproject_text
    assert 'version = "0.12.0.dev0"' not in pyproject_text
    assert '__version__ = "v0.12.0rc1"' in init_text
    assert '__version__ = "v0.12.0.dev0"' not in init_text
    # surrounding lines preserved
    assert 'name = "pmm-extensions"' in pyproject_text
    assert '"""PMM Extensions package."""' in init_text


def test_bump_version_handles_dev_suffix(repo):
    """``_bump_version`` correctly writes PEP 440 dev versions."""
    release._bump_version("0.13.0.dev0", "v0.13.0.dev0")
    assert 'version = "0.13.0.dev0"' in (repo / "pyproject.toml").read_text(
        encoding="utf-8"
    )
    assert '__version__ = "v0.13.0.dev0"' in (repo / "app" / "__init__.py").read_text(
        encoding="utf-8"
    )


# --- cmd_rc end-to-end -----------------------------------------------------


class _FakeRunner:
    """Record ``_run`` invocations and synthesise ``subprocess.CompletedProcess`` results."""

    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def __call__(self, cmd, *, check=True, capture=False):
        self.calls.append(tuple(cmd))
        key = tuple(cmd)
        stdout = self.responses.get(key, "")
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=stdout,
            stderr="",
        )


def _make_rc_preconditions(*, branch="main"):
    """Return canned responses for RC-flow preconditions (branch + clean tree)."""
    return {
        ("git", "rev-parse", "--abbrev-ref", "HEAD"): f"{branch}\n",
        ("git", "status", "--porcelain"): "",
    }


def _make_stable_preconditions(version):
    """Return canned responses for stable-flow preconditions."""
    return {
        ("git", "rev-parse", "--abbrev-ref", "HEAD"): f"release/v{version}\n",
        ("git", "status", "--porcelain"): "",
    }


def _install_rc_probes(
    monkeypatch, *, after_prep, bump_branch_exists, main_at_next_dev
):
    """Install the branch-existence and origin-main-version probes used by cmd_rc."""

    def fake_remote_branch_exists(probe_branch):
        if probe_branch.startswith("release/"):
            return after_prep
        if probe_branch.startswith("bump-dev-version-"):
            return bump_branch_exists
        return False

    monkeypatch.setattr(release, "_remote_branch_exists", fake_remote_branch_exists)

    def fake_origin_main_version(_re):
        parts = ["0", "12", "0"]
        next_dev = f"{parts[0]}.{int(parts[1]) + 1}.0.dev0"
        return next_dev if main_at_next_dev else "0.12.0.dev0"

    monkeypatch.setattr(
        release, "_origin_main_pyproject_version", fake_origin_main_version
    )


def _install_rc_api_spies(monkeypatch, call_order):
    """Install spies for the GitHub-API publish path used by cmd_rc."""

    def spy_push_commit(*_a, **kwargs):
        call_order.append(("api_commit", kwargs.get("message")))
        return "fakecommitsha"

    def spy_create_tag(tag, target_sha):
        call_order.append(("api_tag", tag, target_sha))

    monkeypatch.setattr(
        release,
        "_api_branch_head_sha",
        lambda *_a, **_kw: "fakebasesha",
    )
    monkeypatch.setattr(release, "_api_push_signed_commit", spy_push_commit)
    monkeypatch.setattr(release, "_api_create_tag_ref", spy_create_tag)


def _patch_rc_ok(
    monkeypatch,
    *,
    rc=1,
    webhook_result=True,
    webhook_calls=None,
    after_prep=False,
    bump_branch_exists=False,
    main_at_next_dev=False,
):
    """Patch ``_run``, webhook, ``_bump_version`` and probes for cmd_rc.

    :param after_prep: When ``True`` (RC=1 only), simulate prep already
        pushed the release branch.
    :type after_prep: bool
    :param bump_branch_exists: When ``True`` (RC=1 only), simulate the
        ``bump-dev-version-*`` branch on origin (an open PR).
    :type bump_branch_exists: bool
    :param main_at_next_dev: When ``True`` (RC=1 only), simulate ``origin/main``
        being already at the next ``vX.Y+1.0.dev0``.
    :type main_at_next_dev: bool
    """
    branch = "main" if rc == 1 else "release/v0.12.0"
    runner = _FakeRunner(_make_rc_preconditions(branch=branch))
    monkeypatch.setattr(release, "_bump_version", lambda *_a, **_kw: None)
    monkeypatch.setattr(release, "_local_branch_exists", lambda _branch: False)
    monkeypatch.setattr(release, "_gh_available", lambda: True)

    _install_rc_probes(
        monkeypatch,
        after_prep=after_prep,
        bump_branch_exists=bump_branch_exists,
        main_at_next_dev=main_at_next_dev,
    )

    call_order = []
    runner_orig_call = runner.__call__

    def ordered_runner(cmd, *, check=True, capture=False):
        call_order.append(("run", tuple(cmd)))
        return runner_orig_call(cmd, check=check, capture=capture)

    monkeypatch.setattr(release, "_run", ordered_runner)

    def spy_webhook(url_env, secret_env, version):
        call_order.append(("webhook", url_env, secret_env, version))
        if webhook_calls is not None:
            webhook_calls.append((url_env, secret_env, version))
        return webhook_result

    monkeypatch.setattr(release, "_invoke_post_jira_webhook", spy_webhook)
    _install_rc_api_spies(monkeypatch, call_order)
    monkeypatch.setattr(release.Path, "exists", lambda _self: True)
    monkeypatch.setattr(release.Path, "read_text", lambda _self, **_kw: "")
    return runner, call_order


def test_rc_webhook_success_omits_reminder(monkeypatch, capsys):
    """With RC=1 and a successful webhook, the reminder is omitted."""
    _patch_rc_ok(monkeypatch, rc=1, webhook_result=True)
    assert release.cmd_rc("0.12.0", 1, sign_via_github_api=True) == 0
    out = capsys.readouterr().out
    assert "Create Jira version" not in out


def test_rc_webhook_failure_includes_reminder(monkeypatch, capsys):
    """With RC=1 and a failed webhook, the reminder is present."""
    _patch_rc_ok(monkeypatch, rc=1, webhook_result=False)
    assert release.cmd_rc("0.12.0", 1, sign_via_github_api=True) == 0
    out = capsys.readouterr().out
    assert "Create Jira version 0.12.0" in out


def test_rc_with_rc_gt_1_skips_webhook(monkeypatch, capsys):
    """RC > 1 never calls the webhook nor emits the reminder."""
    calls = []
    _patch_rc_ok(monkeypatch, rc=2, webhook_result=True, webhook_calls=calls)
    assert release.cmd_rc("0.12.0", 2, sign_via_github_api=True) == 0
    out = capsys.readouterr().out
    assert calls == []
    assert "Create Jira version" not in out


def test_rc_with_rc_1_calls_webhook(monkeypatch, capsys):
    """RC=1 calls the webhook exactly once with the create-URL env vars."""
    calls = []
    _patch_rc_ok(monkeypatch, rc=1, webhook_result=True, webhook_calls=calls)
    assert release.cmd_rc("0.12.0", 1, sign_via_github_api=True) == 0
    assert calls == [
        (
            release.WEBHOOK_CREATE_URL_ENV,
            release.WEBHOOK_CREATE_AUTH_ENV,
            "v0.12.0",
        ),
    ]


def test_rc_fires_webhook_after_branch_creation_and_before_build(monkeypatch):
    """RC=1 webhook fires after ``git checkout -b`` and before build/API commit/tag."""
    _, call_order = _patch_rc_ok(monkeypatch, rc=1, webhook_result=True)
    assert release.cmd_rc("0.12.0", 1, sign_via_github_api=True) == 0
    webhook_idx = next(i for i, c in enumerate(call_order) if c[0] == "webhook")
    checkout_idx = next(
        i
        for i, c in enumerate(call_order)
        if c[0] == "run" and c[1][:3] == ("git", "checkout", "-b")
    )
    build_idx = next(
        i
        for i, c in enumerate(call_order)
        if c[0] == "run" and c[1] == ("make", "build")
    )
    api_commit_idx = next(i for i, c in enumerate(call_order) if c[0] == "api_commit")
    api_tag_idx = next(i for i, c in enumerate(call_order) if c[0] == "api_tag")
    assert checkout_idx < webhook_idx < build_idx < api_commit_idx < api_tag_idx


# --- cmd_rc preconditions --------------------------------------------------


def test_rc1_rejects_non_main_branch(monkeypatch, capsys):
    """RC=1 aborts with exit 1 when not on ``main`` AND no release branch on origin."""
    runner = _FakeRunner(_make_rc_preconditions(branch="feature/x"))
    monkeypatch.setattr(release, "_run", runner)
    monkeypatch.setattr(release, "_remote_branch_exists", lambda _b: False)
    assert release.cmd_rc("0.12.0", 1, sign_via_github_api=True) == 1
    assert "RC=1 requires being on the main branch" in capsys.readouterr().err


def test_rc_rejects_dirty_working_tree(monkeypatch, capsys):
    """``cmd_rc`` aborts when the working tree has uncommitted changes."""
    responses = _make_rc_preconditions(branch="main")
    responses[("git", "status", "--porcelain")] = " M some_file.py\n"
    runner = _FakeRunner(responses)
    monkeypatch.setattr(release, "_run", runner)
    assert release.cmd_rc("0.12.0", 1, sign_via_github_api=True) == 1
    assert "Working tree is not clean" in capsys.readouterr().err


def test_rc1_rejects_existing_local_branch(monkeypatch, capsys):
    """RC=1 fresh-from-main aborts when the target branch already exists locally.

    The after-prep path is allowed to take over a local release branch
    (non-destructively when the local is even-or-behind origin); this guard
    is specifically for the fresh-from-main path where a stale local branch
    would clash with ``git checkout -b``.
    """
    runner = _FakeRunner(_make_rc_preconditions(branch="main"))
    monkeypatch.setattr(release, "_run", runner)
    monkeypatch.setattr(release, "_local_branch_exists", lambda _branch: True)
    monkeypatch.setattr(release, "_remote_branch_exists", lambda _b: False)
    assert release.cmd_rc("0.12.0", 1, sign_via_github_api=True) == 1
    assert "already exists locally" in capsys.readouterr().err


def test_rc_via_git_commits_tags_and_pushes(monkeypatch):
    """``sign_via_github_api=False`` uses local ``git commit``/``tag``/``push``."""
    _, call_order = _patch_rc_ok(monkeypatch, rc=1, webhook_result=True)
    assert release.cmd_rc("0.12.0", 1, sign_via_github_api=False) == 0

    run_cmds = [c[1] for c in call_order if c[0] == "run"]
    assert ("git", "commit", "-am", "Bump version to v0.12.0rc1") in run_cmds
    assert ("git", "tag", "v0.12.0rc1") in run_cmds
    assert (
        "git",
        "push",
        "origin",
        "release/v0.12.0",
        "v0.12.0rc1",
    ) in run_cmds
    assert not any(c[0] == "api_commit" for c in call_order)
    assert not any(c[0] == "api_tag" for c in call_order)


def test_rc_via_github_api_errors_without_gh(monkeypatch, capsys):
    """``--sign-via-github-api`` aborts when the ``gh`` CLI is not installed."""
    runner = _FakeRunner(_make_rc_preconditions(branch="main"))
    monkeypatch.setattr(release, "_run", runner)
    monkeypatch.setattr(release, "_gh_available", lambda: False)
    assert release.cmd_rc("0.12.0", 1, sign_via_github_api=True) == 1
    assert "--sign-via-github-api requires the gh CLI" in capsys.readouterr().err


# --- cmd_stable end-to-end -------------------------------------------------


def _patch_stable_ok(monkeypatch):
    """Patch ``_run``, ``_bump_version`` for cmd_stable."""
    runner = _FakeRunner(_make_stable_preconditions("0.12.0"))
    monkeypatch.setattr(release, "_bump_version", lambda *_a, **_kw: None)
    monkeypatch.setattr(release, "_gh_available", lambda: True)

    call_order = []
    runner_orig_call = runner.__call__

    def ordered_runner(cmd, *, check=True, capture=False):
        call_order.append(("run", tuple(cmd)))
        return runner_orig_call(cmd, check=check, capture=capture)

    monkeypatch.setattr(release, "_run", ordered_runner)

    monkeypatch.setattr(
        release,
        "_api_branch_head_sha",
        lambda *_a, **_kw: "fakebasesha",
    )
    monkeypatch.setattr(
        release,
        "_api_push_signed_commit",
        lambda *_a, **_kw: "fakecommitsha",
    )
    monkeypatch.setattr(release, "_api_create_tag_ref", lambda *_a, **_kw: None)

    monkeypatch.setattr(release.Path, "exists", lambda _self: True)
    monkeypatch.setattr(release.Path, "read_text", lambda _self, **_kw: "")
    return runner, call_order


def test_stable_invokes_make_trigger_jenkins_with_release_webhook_envs(monkeypatch):
    """Stable flow forwards the release-webhook env names to ``make trigger-jenkins``."""
    _, call_order = _patch_stable_ok(monkeypatch)
    assert release.cmd_stable("0.12.0", sign_via_github_api=True) == 0
    run_cmds = [c[1] for c in call_order if c[0] == "run"]
    assert (
        "make",
        "trigger-jenkins",
        "TAG=v0.12.0",
        f"WEBHOOK_URL_ENV={release.WEBHOOK_RELEASE_URL_ENV}",
        f"WEBHOOK_AUTH_ENV={release.WEBHOOK_RELEASE_AUTH_ENV}",
    ) in run_cmds


def test_stable_always_prints_mark_jira_reminder(monkeypatch, capsys):
    """Stable flow always prints the "Mark Jira version" reminder.

    The Jira release webhook fires inside ``make trigger-jenkins`` whose
    success/failure ``cmd_stable`` no longer observes — so the reminder is
    unconditional.
    """
    _patch_stable_ok(monkeypatch)
    assert release.cmd_stable("0.12.0", sign_via_github_api=True) == 0
    assert "Mark Jira version 0.12.0 as released" in capsys.readouterr().out


def test_stable_via_git_commits_tags_and_pushes(monkeypatch):
    """``sign_via_github_api=False`` uses local ``git commit``/``tag``/``push``."""
    _, call_order = _patch_stable_ok(monkeypatch)
    assert release.cmd_stable("0.12.0", sign_via_github_api=False) == 0

    run_cmds = [c[1] for c in call_order if c[0] == "run"]
    assert ("git", "commit", "-am", "Bump version to v0.12.0") in run_cmds
    assert ("git", "tag", "v0.12.0") in run_cmds
    assert ("git", "push", "origin", "release/v0.12.0", "v0.12.0") in run_cmds


def test_stable_via_github_api_errors_without_gh(monkeypatch, capsys):
    """``--sign-via-github-api`` aborts when the ``gh`` CLI is not installed."""
    runner = _FakeRunner(_make_stable_preconditions("0.12.0"))
    monkeypatch.setattr(release, "_run", runner)
    monkeypatch.setattr(release, "_gh_available", lambda: False)
    assert release.cmd_stable("0.12.0", sign_via_github_api=True) == 1
    assert "--sign-via-github-api requires the gh CLI" in capsys.readouterr().err


# --- _create_dev_version_bump_pr GH_TOKEN swap -----------------------------


def _patch_dev_pr_runner(monkeypatch, observed_tokens, observed_argvs=None):
    """Patch ``_run`` to record ``GH_TOKEN`` at the time ``gh pr create`` runs.

    When ``observed_argvs`` is provided, the full argv of every ``gh pr create``
    invocation is appended to it as a ``list`` (one entry per call).
    """
    monkeypatch.setattr(release, "_bump_version", lambda *_a, **_kw: None)
    monkeypatch.setattr(release, "_gh_available", lambda: True)

    def runner(cmd, *, check=True, capture=False):
        if tuple(cmd[:3]) == ("gh", "pr", "create"):
            observed_tokens.append(os.environ.get("GH_TOKEN"))
            if observed_argvs is not None:
                observed_argvs.append(list(cmd))
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(release, "_run", runner)


def test_dev_pr_uses_gh_pr_token_when_set_and_restores_gh_token(monkeypatch):
    """``GH_PR_TOKEN`` overrides ``GH_TOKEN`` for ``gh pr create`` only."""
    monkeypatch.setenv("GH_TOKEN", "pat-token")
    monkeypatch.setenv("GH_PR_TOKEN", "github-token")
    observed = []
    _patch_dev_pr_runner(monkeypatch, observed)

    release._create_dev_version_bump_pr("0.12.0", "v0.12.0")

    assert observed == ["github-token"]
    assert os.environ["GH_TOKEN"] == "pat-token"


def test_dev_pr_keeps_gh_token_when_pr_token_unset(monkeypatch):
    """Without ``GH_PR_TOKEN``, ``GH_TOKEN`` is unchanged for ``gh pr create``."""
    monkeypatch.setenv("GH_TOKEN", "pat-token")
    monkeypatch.delenv("GH_PR_TOKEN", raising=False)
    observed = []
    _patch_dev_pr_runner(monkeypatch, observed)

    release._create_dev_version_bump_pr("0.12.0", "v0.12.0")

    assert observed == ["pat-token"]
    assert os.environ["GH_TOKEN"] == "pat-token"


def test_dev_pr_passes_qa_not_required_label(monkeypatch):
    """``gh pr create`` is invoked with the ``qa not required`` label."""
    monkeypatch.setenv("GH_TOKEN", "pat-token")
    monkeypatch.delenv("GH_PR_TOKEN", raising=False)
    observed_tokens: list[str | None] = []
    observed_argvs: list[list[str]] = []
    _patch_dev_pr_runner(monkeypatch, observed_tokens, observed_argvs)

    release._create_dev_version_bump_pr("0.12.0", "v0.12.0")

    assert len(observed_argvs) == 1
    argv = observed_argvs[0]
    assert "--label" in argv
    assert argv[argv.index("--label") + 1] == "qa not required"


# --- cmd_rc dev-version-bump call-site -------------------------------------


def test_cmd_rc_rc1_invokes_dev_version_bump(repo, monkeypatch):
    """RC1 dev-bumps main as part of scope-lock (atomic with the RC cut)."""

    def fake_run(cmd, **kwargs):
        wheel = Path("dist") / "pmm_extensions-0.13.0rc1-py3-none-any.whl"
        wheel.parent.mkdir(exist_ok=True)
        wheel.write_text("", encoding="utf-8")
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:

            class R:
                stdout = "main\n"
                returncode = 0

            return R()
        if cmd[:2] == ["git", "status"]:

            class R:
                stdout = ""
                returncode = 0

            return R()

        class R:
            stdout = ""
            returncode = 0

        return R()

    bump_calls = []

    def fake_dev_bump(version, stable_tag):
        bump_calls.append((version, stable_tag))

    monkeypatch.setattr(release, "_run", fake_run)
    monkeypatch.setattr(release, "_gh_available", lambda: False)
    monkeypatch.setattr(release, "_local_branch_exists", lambda _b: False)
    monkeypatch.setattr(release, "_remote_branch_exists", lambda _b: False)
    monkeypatch.setattr(release, "_invoke_post_jira_webhook", lambda *_a, **_kw: True)
    monkeypatch.setattr(release, "_create_dev_version_bump_pr", fake_dev_bump)

    rc = release.cmd_rc("0.13.0", 1, sign_via_github_api=False)
    assert rc == 0
    assert bump_calls == [("0.13.0", "v0.13.0")]


def test_cmd_rc_rc1_patch_does_not_invoke_dev_version_bump(repo, monkeypatch):
    """Skip the dev-bump step for patch RC1 releases.

    Main is already at X.(Y+1).0.dev0 from the prior minor release, so
    recomputing the next .dev0 produces the value main already carries
    and the bump commit would fail with "nothing to commit". cmd_rc must
    skip the dev-bump call site entirely on patch RC1.
    """

    def fake_run(cmd, **kwargs):
        wheel = Path("dist") / "pmm_extensions-0.12.1rc1-py3-none-any.whl"
        wheel.parent.mkdir(exist_ok=True)
        wheel.write_text("", encoding="utf-8")
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:

            class R:
                stdout = "main\n"
                returncode = 0

            return R()
        if cmd[:2] == ["git", "status"]:

            class R:
                stdout = ""
                returncode = 0

            return R()

        class R:
            stdout = ""
            returncode = 0

        return R()

    bump_calls = []

    def fake_dev_bump(version, stable_tag):
        bump_calls.append((version, stable_tag))

    monkeypatch.setattr(release, "_run", fake_run)
    monkeypatch.setattr(release, "_gh_available", lambda: False)
    monkeypatch.setattr(release, "_local_branch_exists", lambda _b: False)
    monkeypatch.setattr(release, "_remote_branch_exists", lambda _b: False)
    monkeypatch.setattr(release, "_invoke_post_jira_webhook", lambda *_a, **_kw: True)
    monkeypatch.setattr(release, "_create_dev_version_bump_pr", fake_dev_bump)

    rc = release.cmd_rc("0.12.1", 1, sign_via_github_api=False)
    assert rc == 0
    assert bump_calls == []


def test_cmd_rc_rc2_does_not_invoke_dev_version_bump(repo, monkeypatch):
    """RC2+ must NOT redo the dev-bump (it was done atomically with RC1)."""

    def fake_run(cmd, **kwargs):
        wheel = Path("dist") / "pmm_extensions-0.13.0rc2-py3-none-any.whl"
        wheel.parent.mkdir(exist_ok=True)
        wheel.write_text("", encoding="utf-8")
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:

            class R:
                stdout = "release/v0.13.0\n"
                returncode = 0

            return R()
        if cmd[:2] == ["git", "status"]:

            class R:
                stdout = ""
                returncode = 0

            return R()

        class R:
            stdout = ""
            returncode = 0

        return R()

    bump_calls = []
    monkeypatch.setattr(release, "_run", fake_run)
    monkeypatch.setattr(release, "_gh_available", lambda: False)
    monkeypatch.setattr(
        release,
        "_create_dev_version_bump_pr",
        lambda v, t: bump_calls.append((v, t)),
    )

    rc = release.cmd_rc("0.13.0", 2, sign_via_github_api=False)
    assert rc == 0
    assert bump_calls == []


# --- cmd_prep --------------------------------------------------------------


def _make_prep_preconditions(branch="main", head_sha="abc1234deadbeef"):
    """Return canned responses for cmd_prep preconditions + head-sha lookup."""
    return {
        ("git", "rev-parse", "--abbrev-ref", "HEAD"): f"{branch}\n",
        ("git", "status", "--porcelain"): "",
        ("git", "rev-parse", "HEAD"): f"{head_sha}\n",
    }


def _patch_prep_ok(
    monkeypatch,
    *,
    head_sha="abc1234deadbeef",
    webhook_result=True,
    webhook_calls=None,
    dev_bump_calls=None,
):
    """Patch ``_run``, webhook, ``_create_dev_version_bump_pr`` and probes for cmd_prep."""
    runner = _FakeRunner(_make_prep_preconditions(head_sha=head_sha))
    monkeypatch.setattr(release, "_bump_version", lambda *_a, **_kw: None)
    monkeypatch.setattr(release, "_local_branch_exists", lambda _branch: False)
    monkeypatch.setattr(release, "_remote_branch_exists", lambda _branch: False)
    monkeypatch.setattr(release, "_gh_available", lambda: True)
    monkeypatch.setattr(
        release,
        "_read_pyproject_version",
        lambda: "0.13.0.dev0",
    )

    call_order = []
    runner_orig_call = runner.__call__

    def ordered_runner(cmd, *, check=True, capture=False):
        call_order.append(("run", tuple(cmd)))
        return runner_orig_call(cmd, check=check, capture=capture)

    monkeypatch.setattr(release, "_run", ordered_runner)

    def spy_webhook(url_env, secret_env, version):
        call_order.append(("webhook", url_env, secret_env, version))
        if webhook_calls is not None:
            webhook_calls.append((url_env, secret_env, version))
        return webhook_result

    monkeypatch.setattr(release, "_invoke_post_jira_webhook", spy_webhook)

    def spy_dev_bump(version, stable_tag):
        call_order.append(("dev_bump", version, stable_tag))
        if dev_bump_calls is not None:
            dev_bump_calls.append((version, stable_tag))

    monkeypatch.setattr(release, "_create_dev_version_bump_pr", spy_dev_bump)

    monkeypatch.setattr(release.Path, "exists", lambda _self: True)
    monkeypatch.setattr(release.Path, "read_text", lambda _self, **_kw: "")
    return runner, call_order


def test_prep_happy_path(monkeypatch):
    """cmd_prep fires webhook once, opens dev-bump PR, triggers internal Jenkins."""
    webhook_calls = []
    dev_bump_calls = []
    _, call_order = _patch_prep_ok(
        monkeypatch,
        webhook_calls=webhook_calls,
        dev_bump_calls=dev_bump_calls,
    )
    assert release.cmd_prep("0.13.0", sign_via_github_api=True) == 0

    assert webhook_calls == [
        (
            release.WEBHOOK_CREATE_URL_ENV,
            release.WEBHOOK_CREATE_AUTH_ENV,
            "v0.13.0",
        ),
    ]
    assert dev_bump_calls == [("0.13.0", "v0.13.0")]

    run_cmds = [c[1] for c in call_order if c[0] == "run"]
    assert ("git", "checkout", "-b", "release/v0.13.0") in run_cmds
    assert ("git", "push", "-u", "origin", "release/v0.13.0") in run_cmds
    assert (
        "make",
        "trigger-jenkins",
        "TAG=abc1234deadbeef",
        "PUSH_IMAGE_DOCKER=false",
    ) in run_cmds


def test_prep_ordering(monkeypatch):
    """cmd_prep runs branch create → push → webhook → build → Jenkins → dev-bump."""
    _, call_order = _patch_prep_ok(monkeypatch)
    assert release.cmd_prep("0.13.0", sign_via_github_api=True) == 0

    checkout_idx = next(
        i
        for i, c in enumerate(call_order)
        if c[0] == "run" and c[1][:3] == ("git", "checkout", "-b")
    )
    push_idx = next(
        i
        for i, c in enumerate(call_order)
        if c[0] == "run" and c[1][:4] == ("git", "push", "-u", "origin")
    )
    webhook_idx = next(i for i, c in enumerate(call_order) if c[0] == "webhook")
    build_idx = next(
        i
        for i, c in enumerate(call_order)
        if c[0] == "run" and c[1] == ("make", "build")
    )
    jenkins_idx = next(
        i
        for i, c in enumerate(call_order)
        if c[0] == "run" and c[1][:2] == ("make", "trigger-jenkins")
    )
    dev_bump_idx = next(i for i, c in enumerate(call_order) if c[0] == "dev_bump")

    assert (
        checkout_idx < push_idx < webhook_idx < build_idx < jenkins_idx < dev_bump_idx
    )


def test_prep_patch_release_skips_dev_bump(monkeypatch):
    """Skip the dev-bump PR during patch-release prep."""
    dev_bump_calls = []
    _, call_order = _patch_prep_ok(
        monkeypatch,
        dev_bump_calls=dev_bump_calls,
    )
    assert release.cmd_prep("0.13.1", sign_via_github_api=True) == 0

    assert dev_bump_calls == []
    assert not any(c[0] == "dev_bump" for c in call_order)


def test_prep_rejects_non_main_branch(monkeypatch, capsys):
    """cmd_prep aborts when the current branch is not ``main``."""
    runner = _FakeRunner(_make_prep_preconditions(branch="feature/x"))
    monkeypatch.setattr(release, "_run", runner)
    assert release.cmd_prep("0.13.0", sign_via_github_api=True) == 1
    assert "prep requires being on the main branch" in capsys.readouterr().err


def test_prep_rejects_dirty_working_tree(monkeypatch, capsys):
    """cmd_prep aborts when the working tree has uncommitted changes."""
    responses = _make_prep_preconditions(branch="main")
    responses[("git", "status", "--porcelain")] = " M file.py\n"
    runner = _FakeRunner(responses)
    monkeypatch.setattr(release, "_run", runner)
    assert release.cmd_prep("0.13.0", sign_via_github_api=True) == 1
    assert "Working tree is not clean" in capsys.readouterr().err


def test_prep_rejects_existing_local_branch(monkeypatch, capsys):
    """cmd_prep aborts when the target release branch exists locally."""
    runner = _FakeRunner(_make_prep_preconditions(branch="main"))
    monkeypatch.setattr(release, "_run", runner)
    monkeypatch.setattr(release, "_local_branch_exists", lambda _b: True)
    monkeypatch.setattr(release, "_remote_branch_exists", lambda _b: False)
    monkeypatch.setattr(release, "_gh_available", lambda: True)
    assert release.cmd_prep("0.13.0", sign_via_github_api=True) == 1
    assert "already exists locally" in capsys.readouterr().err


def test_prep_rejects_existing_remote_branch(monkeypatch, capsys):
    """cmd_prep aborts when the target release branch is already on origin."""
    runner = _FakeRunner(_make_prep_preconditions(branch="main"))
    monkeypatch.setattr(release, "_run", runner)
    monkeypatch.setattr(release, "_local_branch_exists", lambda _b: False)
    monkeypatch.setattr(release, "_remote_branch_exists", lambda _b: True)
    monkeypatch.setattr(release, "_gh_available", lambda: True)
    assert release.cmd_prep("0.13.0", sign_via_github_api=True) == 1
    assert "already exists on origin" in capsys.readouterr().err


def test_prep_webhook_failure_includes_reminder(monkeypatch, capsys):
    """cmd_prep still returns 0 on webhook failure but prints the reminder."""
    _patch_prep_ok(monkeypatch, webhook_result=False)
    assert release.cmd_prep("0.13.0", sign_via_github_api=True) == 0
    out = capsys.readouterr().out
    assert "Create Jira version 0.13.0" in out


def test_prep_webhook_success_omits_reminder(monkeypatch, capsys):
    """cmd_prep omits the reminder when the webhook helper succeeds."""
    _patch_prep_ok(monkeypatch, webhook_result=True)
    assert release.cmd_prep("0.13.0", sign_via_github_api=True) == 0
    out = capsys.readouterr().out
    assert "Create Jira version" not in out


def test_prep_passes_head_sha_to_internal_jenkins(monkeypatch):
    """cmd_prep invokes ``make trigger-jenkins TAG=<head_sha> PUSH_IMAGE_DOCKER=false``."""
    _, call_order = _patch_prep_ok(monkeypatch, head_sha="deadbeefcafe1234")
    assert release.cmd_prep("0.13.0", sign_via_github_api=True) == 0
    run_cmds = [c[1] for c in call_order if c[0] == "run"]
    assert (
        "make",
        "trigger-jenkins",
        "TAG=deadbeefcafe1234",
        "PUSH_IMAGE_DOCKER=false",
    ) in run_cmds


def _run_trigger_jenkins_with_fake_curl(tmp_path, tag, *make_args):
    """Run the real Makefile target and return recorded curl arguments."""
    curl_args = tmp_path / "curl-args.txt"
    fake_curl = tmp_path / "curl"
    fake_curl.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$CURL_ARGS_FILE"\n',
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    env = {
        **os.environ,
        "CURL_ARGS_FILE": str(curl_args),
        "JENKINS_API_TOKEN": "token",
        "JENKINS_URL": "https://jenkins.example",
        "JENKINS_USER": "user",
        "WEBHOOK_AUTH_ENV": "",
        "WEBHOOK_URL_ENV": "",
        "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    subprocess.run(
        ["make", "trigger-jenkins", f"TAG={tag}", *make_args],
        cwd=_PROJECT_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return curl_args.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("tag", "make_args", "expected_job", "unexpected_job"),
    [
        (
            "deadbeefcafe1234",
            ("PUSH_IMAGE_DOCKER=false",),
            "Build",
            "Release",
        ),
        ("v0.13.0rc1", (), "Release", "Build"),
        ("$(shell printf v0.13.0rc1)", (), "Build", "Release"),
    ],
)
def test_trigger_jenkins_routes_by_tag_prefix(
    tmp_path, tag, make_args, expected_job, unexpected_job
):
    """SHA refs go to Build; literal v* tags stay on Release."""
    curl_args = _run_trigger_jenkins_with_fake_curl(tmp_path, tag, *make_args)

    assert (
        f"https://jenkins.example/job/PMM-Extensions/job/{expected_job}/buildWithParameters"
        in curl_args
    )
    assert (
        f"https://jenkins.example/job/PMM-Extensions/job/{unexpected_job}/buildWithParameters"
        not in curl_args
    )
    assert f"releaseTag={tag}" in curl_args


def test_prep_via_github_api_errors_without_gh(monkeypatch, capsys):
    """cmd_prep with ``--sign-via-github-api`` aborts when gh is not installed."""
    runner = _FakeRunner(_make_prep_preconditions(branch="main"))
    monkeypatch.setattr(release, "_run", runner)
    monkeypatch.setattr(release, "_gh_available", lambda: False)
    assert release.cmd_prep("0.13.0", sign_via_github_api=True) == 1
    assert "--sign-via-github-api requires the gh CLI" in capsys.readouterr().err


# --- cmd_rc after-prep idempotency -----------------------------------------


def test_rc1_after_prep_still_fires_webhook(monkeypatch):
    """Re-fire the version-create webhook under after-prep (rule is idempotent)."""
    calls = []
    _patch_rc_ok(
        monkeypatch,
        rc=1,
        webhook_result=True,
        webhook_calls=calls,
        after_prep=True,
    )
    assert release.cmd_rc("0.12.0", 1, sign_via_github_api=True) == 0
    assert calls == [
        (
            release.WEBHOOK_CREATE_URL_ENV,
            release.WEBHOOK_CREATE_AUTH_ENV,
            "v0.12.0",
        ),
    ]


def test_rc1_after_prep_skips_dev_bump_when_main_already_on_next_dev(monkeypatch):
    """Skip the dev-bump PR when ``origin/main`` is already at the next .dev0.

    Covers the normal flow where the prep-opened dev-bump PR has already been
    merged. GitHub's branch-auto-delete-after-merge setting (the recommended
    setting) removes the source branch on merge, so by rc1 day the
    bump-dev-version branch may be gone even though main is already at the
    next .dev0 — probing the branch alone would incorrectly re-fire the
    dev-bump call.
    """
    dev_bump_calls = []
    monkeypatch.setattr(
        release,
        "_create_dev_version_bump_pr",
        lambda v, t: dev_bump_calls.append((v, t)),
    )
    _patch_rc_ok(
        monkeypatch,
        rc=1,
        webhook_result=True,
        after_prep=True,
        bump_branch_exists=False,
        main_at_next_dev=True,
    )
    assert release.cmd_rc("0.12.0", 1, sign_via_github_api=True) == 0
    assert dev_bump_calls == []


def test_rc1_after_prep_errors_when_dev_bump_pr_unmerged(monkeypatch, capsys):
    """Abort when the dev-bump branch is on origin but main is not yet bumped.

    The operator forgot to merge the prep-opened dev-bump PR before
    dispatching rc1. ``cmd_rc`` returns non-zero with an explicit operator
    instruction rather than try to recreate the PR and fail at ``git push``.
    """
    dev_bump_calls = []
    monkeypatch.setattr(
        release,
        "_create_dev_version_bump_pr",
        lambda v, t: dev_bump_calls.append((v, t)),
    )
    _patch_rc_ok(
        monkeypatch,
        rc=1,
        webhook_result=True,
        after_prep=True,
        bump_branch_exists=True,
        main_at_next_dev=False,
    )
    assert release.cmd_rc("0.12.0", 1, sign_via_github_api=True) == 1
    err = capsys.readouterr().err
    assert "merge the dev-bump PR opened by `make release-prep`" in err
    assert "before the next release cycle" in err
    assert dev_bump_calls == []


def test_rc1_after_prep_repairs_missing_dev_bump_pr(monkeypatch):
    """Under after-prep with bump-dev-version branch absent, dev_bump_pr IS opened.

    Recovers from a ``prep`` that crashed before reaching the dev-bump step.
    """
    dev_bump_calls = []
    monkeypatch.setattr(
        release,
        "_create_dev_version_bump_pr",
        lambda v, t: dev_bump_calls.append((v, t)),
    )
    _patch_rc_ok(
        monkeypatch,
        rc=1,
        webhook_result=True,
        after_prep=True,
        bump_branch_exists=False,
    )
    assert release.cmd_rc("0.12.0", 1, sign_via_github_api=True) == 0
    assert dev_bump_calls == [("0.12.0", "v0.12.0")]


def test_rc1_after_prep_repairs_missing_jira_version(monkeypatch, capsys):
    """Under after-prep with webhook returning False, the Jira-version reminder fires."""
    _patch_rc_ok(
        monkeypatch,
        rc=1,
        webhook_result=False,
        after_prep=True,
    )
    assert release.cmd_rc("0.12.0", 1, sign_via_github_api=True) == 0
    out = capsys.readouterr().out
    assert "Create Jira version 0.12.0" in out


def test_rc1_after_prep_uses_existing_release_branch_non_destructive(monkeypatch):
    """Under after-prep with no local branch, fetch + checkout from origin."""
    monkeypatch.setattr(release, "_local_branch_exists", lambda _b: False)
    _, call_order = _patch_rc_ok(
        monkeypatch,
        rc=1,
        webhook_result=True,
        after_prep=True,
    )
    assert release.cmd_rc("0.12.0", 1, sign_via_github_api=True) == 0
    run_cmds = [c[1] for c in call_order if c[0] == "run"]
    # When no local branch, `git checkout -b release/v0.12.0 origin/release/v0.12.0`
    assert (
        "git",
        "checkout",
        "-b",
        "release/v0.12.0",
        "origin/release/v0.12.0",
    ) in run_cmds
    # Should NOT have used the fresh-from-main `git checkout -b release/v0.12.0`
    # (which is the bare three-arg form).
    assert ("git", "checkout", "-b", "release/v0.12.0") not in run_cmds


def test_rc1_after_prep_resets_local_branch_when_even_or_behind(monkeypatch):
    """Under after-prep with local branch behind/even with origin, reset --hard."""
    _, call_order = _patch_rc_ok(
        monkeypatch,
        rc=1,
        webhook_result=True,
        after_prep=True,
    )
    # Apply this AFTER _patch_rc_ok, which itself stubs _local_branch_exists.
    monkeypatch.setattr(release, "_local_branch_exists", lambda _b: True)
    # _FakeRunner returns "" stdout for git rev-list --count, so ahead_count = 0
    assert release.cmd_rc("0.12.0", 1, sign_via_github_api=True) == 0
    run_cmds = [c[1] for c in call_order if c[0] == "run"]
    assert ("git", "checkout", "release/v0.12.0") in run_cmds
    assert ("git", "reset", "--hard", "origin/release/v0.12.0") in run_cmds


def test_rc1_after_prep_refuses_to_reset_when_local_ahead(monkeypatch, capsys):
    """Under after-prep with local branch ahead of origin, abort instead of resetting."""
    responses = _make_rc_preconditions(branch="release/v0.12.0")
    responses[
        ("git", "rev-list", "--count", "origin/release/v0.12.0..release/v0.12.0")
    ] = "2\n"
    runner = _FakeRunner(responses)
    monkeypatch.setattr(release, "_run", runner)
    monkeypatch.setattr(release, "_local_branch_exists", lambda _b: True)
    monkeypatch.setattr(release, "_remote_branch_exists", lambda _b: True)
    monkeypatch.setattr(release, "_gh_available", lambda: True)

    assert release.cmd_rc("0.12.0", 1, sign_via_github_api=True) == 1
    err = capsys.readouterr().err
    assert "is ahead of origin" in err
    cmds = list(runner.calls)
    assert ("git", "reset", "--hard", "origin/release/v0.12.0") not in cmds


def test_rc1_after_prep_aborts_when_rev_list_fails(monkeypatch, capsys):
    """Under after-prep, refuse to reset --hard when ``git rev-list`` exits non-zero.

    A failing rev-list leaves the ahead-count ambiguous; resetting would be
    the most dangerous fallback. The script must error out and let the
    operator inspect manually.
    """

    def fake_run(cmd, *, check=True, capture=False):
        # The local-branch-exists path runs `git rev-list --count
        # origin/release/v0.12.0..release/v0.12.0`. Force exit 128 to
        # simulate a corrupt ref or unexpected git failure.
        if cmd[:3] == ["git", "rev-list", "--count"] and any(
            arg.startswith("origin/release/") for arg in cmd[3:]
        ):
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=128,
                stdout="",
                stderr="fatal: ambiguous argument\n",
            )
        stdout_overrides = {
            ("git", "rev-parse", "--abbrev-ref", "HEAD"): "main\n",
            ("git", "status", "--porcelain"): "",
        }
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=stdout_overrides.get(tuple(cmd), ""),
            stderr="",
        )

    monkeypatch.setattr(release, "_run", fake_run)
    monkeypatch.setattr(release, "_local_branch_exists", lambda _b: True)
    monkeypatch.setattr(release, "_remote_branch_exists", lambda _b: True)
    monkeypatch.setattr(release, "_gh_available", lambda: True)

    assert release.cmd_rc("0.12.0", 1, sign_via_github_api=True) == 1
    err = capsys.readouterr().err
    assert "git rev-list failed" in err
    assert "Refusing to reset" in err


def test_rc1_after_prep_still_bumps_and_tags(monkeypatch):
    """Under after-prep, RC=1 still bumps version, commits and tags the release."""
    _, call_order = _patch_rc_ok(
        monkeypatch,
        rc=1,
        webhook_result=True,
        after_prep=True,
    )
    assert release.cmd_rc("0.12.0", 1, sign_via_github_api=True) == 0
    api_commits = [c for c in call_order if c[0] == "api_commit"]
    api_tags = [c for c in call_order if c[0] == "api_tag"]
    assert any("v0.12.0rc1" in str(c) for c in api_commits)
    assert any("v0.12.0rc1" in str(c) for c in api_tags)
    # Trigger-jenkins for rc1 uses the rc tag, not a SHA, and inherits the
    # default ``PUSH_IMAGE_DOCKER=true`` (not passed explicitly).
    run_cmds = [c[1] for c in call_order if c[0] == "run"]
    assert ("make", "trigger-jenkins", "TAG=v0.12.0rc1") in run_cmds


def test_rc1_after_prep_does_not_create_branch_on_origin(monkeypatch):
    """Under after-prep, the API commit does NOT pass ``create_branch=True``.

    The release branch already exists on origin from prep; re-creating it
    would fail with a 422 from the GitHub git-data API.
    """
    created_with = []

    def spy_push_commit(branch, *, base_sha, message, files, create_branch):
        created_with.append(create_branch)
        return "fakecommitsha"

    monkeypatch.setattr(release, "_api_push_signed_commit", spy_push_commit)
    _patch_rc_ok(
        monkeypatch,
        rc=1,
        webhook_result=True,
        after_prep=True,
    )
    # Re-stub after _patch_rc_ok overrode it (it patches both).
    monkeypatch.setattr(release, "_api_push_signed_commit", spy_push_commit)

    assert release.cmd_rc("0.12.0", 1, sign_via_github_api=True) == 0
    assert created_with == [False]


# --- _remote_branch_exists -------------------------------------------------


def test_remote_branch_exists_returns_true_on_zero(monkeypatch):
    """``git ls-remote --exit-code`` exit 0 → branch found."""

    def fake_run(cmd, *, check=True, capture=False):
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout="abc\trefs/heads/release/v0.12.0\n",
            stderr="",
        )

    monkeypatch.setattr(release, "_run", fake_run)
    assert release._remote_branch_exists("release/v0.12.0") is True


def test_remote_branch_exists_returns_false_on_two(monkeypatch):
    """``git ls-remote --exit-code`` exit 2 → branch missing (happy path)."""

    def fake_run(cmd, *, check=True, capture=False):
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=2,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr(release, "_run", fake_run)
    assert release._remote_branch_exists("release/v0.12.0") is False


def test_remote_branch_exists_raises_on_network_error(monkeypatch):
    """``git ls-remote --exit-code`` exit 128 → network/auth error → raise."""

    def fake_run(cmd, *, check=True, capture=False):
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=LS_REMOTE_NETWORK_ERROR_EXIT_CODE,
            stdout="",
            stderr="fatal: unable to access\n",
        )

    monkeypatch.setattr(release, "_run", fake_run)
    with pytest.raises(subprocess.CalledProcessError) as exc:
        release._remote_branch_exists("release/v0.12.0")
    assert exc.value.returncode == LS_REMOTE_NETWORK_ERROR_EXIT_CODE


# --- argparse --------------------------------------------------------------


def test_argparse_rc_requires_version_and_rc(capsys):
    """The ``rc`` subcommand requires both ``--version`` and ``--rc``."""
    with pytest.raises(SystemExit) as exc_info:
        release.main(["rc", "--version", "0.12.0"])
    assert exc_info.value.code == ARGPARSE_ERROR_EXIT_CODE
    assert "--rc" in capsys.readouterr().err


def test_argparse_stable_requires_version(capsys):
    """The ``stable`` subcommand requires ``--version``."""
    with pytest.raises(SystemExit) as exc_info:
        release.main(["stable"])
    assert exc_info.value.code == ARGPARSE_ERROR_EXIT_CODE
    assert "--version" in capsys.readouterr().err


def test_argparse_rc_rejects_zero(capsys):
    """The ``rc`` subcommand rejects ``--rc 0``."""
    with pytest.raises(SystemExit) as exc_info:
        release.main(["rc", "--version", "0.12.0", "--rc", "0"])
    assert exc_info.value.code == ARGPARSE_ERROR_EXIT_CODE


def test_argparse_rc_rejects_negative(capsys):
    """The ``rc`` subcommand rejects negative ``--rc`` values."""
    with pytest.raises(SystemExit) as exc_info:
        release.main(["rc", "--version", "0.12.0", "--rc", "-1"])
    assert exc_info.value.code == ARGPARSE_ERROR_EXIT_CODE


def test_argparse_prep_requires_version(capsys):
    """The ``prep`` subcommand requires ``--version``."""
    with pytest.raises(SystemExit) as exc_info:
        release.main(["prep"])
    assert exc_info.value.code == ARGPARSE_ERROR_EXIT_CODE
    assert "--version" in capsys.readouterr().err


def test_argparse_prep_rejects_rc_flag(capsys):
    """The ``prep`` subcommand does NOT accept ``--rc`` (no tag is created)."""
    with pytest.raises(SystemExit) as exc_info:
        release.main(["prep", "--version", "0.13.0", "--rc", "1"])
    assert exc_info.value.code == ARGPARSE_ERROR_EXIT_CODE


def test_argparse_prep_dispatches_to_cmd_prep(monkeypatch):
    """``release.main(['prep', '--version', X])`` dispatches to cmd_prep."""
    calls = []

    def fake_prep(version, *, sign_via_github_api):
        calls.append((version, sign_via_github_api))
        return 0

    monkeypatch.setattr(release, "cmd_prep", fake_prep)
    assert release.main(["prep", "--version", "0.13.0"]) == 0
    assert calls == [("0.13.0", False)]


def test_argparse_prep_passes_sign_via_github_api(monkeypatch):
    """``--sign-via-github-api`` is forwarded to cmd_prep."""
    calls = []

    def fake_prep(version, *, sign_via_github_api):
        calls.append((version, sign_via_github_api))
        return 0

    monkeypatch.setattr(release, "cmd_prep", fake_prep)
    assert release.main(["prep", "--version", "0.13.0", "--sign-via-github-api"]) == 0
    assert calls == [("0.13.0", True)]


# --- main (CalledProcessError handling) ------------------------------------


def test_main_catches_called_process_error_with_list_cmd(monkeypatch, capsys):
    """``main`` surfaces a failing subprocess as a single concise stderr line."""

    def raise_cpe(version, rc, *, sign_via_github_api):
        raise subprocess.CalledProcessError(returncode=128, cmd=["git", "push"])

    monkeypatch.setattr(release, "cmd_rc", raise_cpe)
    assert release.main(["rc", "--version", "0.12.0", "--rc", "1"]) == 1
    err = capsys.readouterr().err
    assert "release: command failed (exit code 128): git push" in err
    assert "Traceback" not in err


def test_main_catches_called_process_error_with_string_cmd(monkeypatch, capsys):
    """``main`` handles a ``CalledProcessError`` whose ``cmd`` is a plain string."""

    def raise_cpe(version, *, sign_via_github_api):
        raise subprocess.CalledProcessError(returncode=1, cmd="make build")

    monkeypatch.setattr(release, "cmd_stable", raise_cpe)
    assert release.main(["stable", "--version", "0.12.0"]) == 1
    err = capsys.readouterr().err
    assert "release: command failed (exit code 1): make build" in err
    assert "Traceback" not in err


# --- back-merge -----------------------------------------------------------


def test_cmd_stable_back_merges_into_main(repo, monkeypatch):
    """cmd_stable ends with a back-merge of release/vX.Y.Z into main.

    Asserts the ordered sequence: tag/push on release branch, then fetch,
    checkout main, merge --no-ff, resolve CHANGELOG, commit, push, then
    delete the release branch. The ancestor-invariant assertion runs after
    push and before delete.
    """
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(tuple(cmd))
        wheel = Path("dist") / "pmm_extensions-0.13.0-py3-none-any.whl"
        wheel.parent.mkdir(exist_ok=True)
        wheel.write_text("", encoding="utf-8")
        _is_ancestor_prefix = ["git", "merge-base", "--is-ancestor", "v0.13.0"]
        if cmd[:4] == _is_ancestor_prefix:

            class R:
                stdout = ""
                returncode = 0

            return R()
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:

            class R:
                stdout = "release/v0.13.0\n"
                returncode = 0

            return R()
        if cmd[:2] == ["git", "status"]:

            class R:
                stdout = ""
                returncode = 0

            return R()

        class R:
            stdout = ""
            returncode = 0

        return R()

    monkeypatch.setattr(release, "_run", fake_run)
    monkeypatch.setattr(release, "_gh_available", lambda: False)

    rc = release.cmd_stable("0.13.0", sign_via_github_api=False)
    assert rc == 0

    merge_idx = next(
        i for i, c in enumerate(calls) if c[:3] == ("git", "merge", "--no-ff")
    )
    resolve_idx = next(
        i
        for i, c in enumerate(calls[merge_idx:], start=merge_idx)
        if "resolve-backmerge" in c
    )
    commit_idx = next(
        i
        for i, c in enumerate(calls[resolve_idx:], start=resolve_idx)
        if c[:2] == ("git", "commit")
    )
    push_idx = next(
        i
        for i, c in enumerate(calls[commit_idx:], start=commit_idx)
        if c[:3] == ("git", "push", "origin") and "main" in c
    )
    ancestor_idx = next(
        i
        for i, c in enumerate(calls[push_idx:], start=push_idx)
        if c[:4] == ("git", "merge-base", "--is-ancestor", "v0.13.0")
    )
    delete_idx = next(
        i
        for i, c in enumerate(calls[ancestor_idx:], start=ancestor_idx)
        if c[:4] == ("git", "push", "origin", "--delete")
    )
    assert merge_idx < resolve_idx < commit_idx < push_idx < ancestor_idx < delete_idx


def test_cmd_stable_aborts_if_ancestor_invariant_fails(repo, monkeypatch):
    """Abort when the ancestor invariant fails after the back-merge push.

    If ``git merge-base --is-ancestor vX.Y.Z origin/main`` exits non-zero,
    cmd_stable returns non-zero and does NOT delete the release branch.
    """
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(tuple(cmd))
        wheel = Path("dist") / "pmm_extensions-0.13.0-py3-none-any.whl"
        wheel.parent.mkdir(exist_ok=True)
        wheel.write_text("", encoding="utf-8")
        _is_ancestor_prefix = ["git", "merge-base", "--is-ancestor", "v0.13.0"]
        if cmd[:4] == _is_ancestor_prefix:

            class R:
                stdout = ""
                returncode = 1

            return R()
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:

            class R:
                stdout = "release/v0.13.0\n"
                returncode = 0

            return R()
        if cmd[:2] == ["git", "status"]:

            class R:
                stdout = ""
                returncode = 0

            return R()

        class R:
            stdout = ""
            returncode = 0

        return R()

    monkeypatch.setattr(release, "_run", fake_run)
    monkeypatch.setattr(release, "_gh_available", lambda: False)

    rc = release.cmd_stable("0.13.0", sign_via_github_api=False)
    assert rc != 0
    assert not any(c[:4] == ("git", "push", "origin", "--delete") for c in calls)


def test_cmd_stable_aborts_if_unexpected_merge_failure(repo, monkeypatch):
    """Abort cmd_stable when git merge fails AND no unmerged paths exist."""

    def fake_run(cmd, **kwargs):
        wheel = Path("dist") / "pmm_extensions-0.13.0-py3-none-any.whl"
        wheel.parent.mkdir(exist_ok=True)
        wheel.write_text("", encoding="utf-8")
        # Simulate git merge failing unexpectedly (e.g., lockfile)
        if cmd[:3] == ["git", "merge", "--no-ff"]:

            class R:
                stdout = ""
                returncode = 1

            return R()
        # Simulate "no unmerged paths" — git ls-files -u returns empty
        if cmd[:3] == ["git", "ls-files", "-u"]:

            class R:
                stdout = ""
                returncode = 0

            return R()
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:

            class R:
                stdout = "release/v0.13.0\n"
                returncode = 0

            return R()
        if cmd[:2] == ["git", "status"]:

            class R:
                stdout = ""
                returncode = 0

            return R()

        class R:
            stdout = ""
            returncode = 0

        return R()

    monkeypatch.setattr(release, "_run", fake_run)
    monkeypatch.setattr(release, "_gh_available", lambda: False)

    rc = release.cmd_stable("0.13.0", sign_via_github_api=False)
    assert rc != 0
