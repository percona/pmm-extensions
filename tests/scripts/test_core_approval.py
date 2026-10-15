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

"""Tests for ``scripts/core_approval.py``."""

from pathlib import Path

import pytest

from tests.scripts import ci_filter_globs, load_script, PROJECT_ROOT

core_approval = load_script("core_approval")

_RULES = core_approval.parse_path_rules(
    core_approval.DEFAULT_PATHS_FILE.read_text(encoding="utf-8")
)
_CODEOWNERS = PROJECT_ROOT / ".github" / "CODEOWNERS"
_PATHS_FILE_GLOB = ".github/core-approval-paths"
_CODEOWNERS_GLOB = ".github/CODEOWNERS"
_CORE_OWNER = f"@percona/{core_approval.CORE_TEAM}"

_LEAD = "lead"
_CORE_A = "core-a"
_CORE_B = "core-b"
_OUTSIDER = "outsider"
_MEMBERS = {
    core_approval.LEADS_TEAM: {_LEAD},
    core_approval.CORE_TEAM: {_LEAD, _CORE_A, _CORE_B},
}
_FOUNDATION_PATH = "app/core/auth/base.py"


def _changed(*paths: str, complete: bool = True):
    return core_approval.ChangedFiles(paths=list(paths), complete=complete)


def _review(reviewer: str, state: str):
    return core_approval.Review(reviewer=reviewer, state=state)


def _approve(reviewer: str):
    return _review(reviewer, core_approval.ReviewState.APPROVED)


def _codeowners_owner(path: str) -> str | None:
    """Return the owners of ``path`` from the last matching CODEOWNERS line."""
    owner = None
    for raw in _CODEOWNERS.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        pattern, *owners = line.split()
        glob = pattern.removeprefix("/")
        if glob.endswith("/"):
            glob += "**"
        if core_approval.glob_to_regex(glob).fullmatch(path):
            owner = " ".join(owners)
    return owner


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("app/main.py", True),
        ("app/core/auth/base.py", True),
        ("app/core/config.py", True),
        ("app/core/utils/fields.py", True),
        ("app/core/exceptions.py", False),
        ("app/core/pagination/deps.py", False),
        ("app/core/utils/strings.py", False),
        ("app/tasks/execution/models.py", True),
        ("app/extensions/deps.py", True),
        ("app/extensions/sync/models.py", False),
        ("app/extensions/apps/framework/api.py", True),
        ("app/extensions/apps/framework/scaffold.py", False),
        ("app/extensions/apps/framework/form_dsl/model.py", False),
        ("app/extensions/apps/atw/app.py", False),
        (".github/CODEOWNERS", True),
        (".github/workflows/ci.yml", True),
        (".github/labeler.yml", False),
        ("tests/app/core/test_config.py", False),
        ("poetry.lock", False),
    ],
)
def test_paths_file_classifies_foundation_and_carve_outs(path, expected):
    """Classify foundation paths, their carve-outs, and paths outside the foundation."""
    assert core_approval.is_foundation_path(path, _RULES) is expected


def test_paths_file_mirrors_codeowners_backend_foundation():
    """Keep the paths file and the CODEOWNERS foundation block in agreement under ``app/``."""
    app_files = [
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in (PROJECT_ROOT / "app").rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    ]
    assert app_files
    mismatched = [
        path
        for path in app_files
        if core_approval.is_foundation_path(path, _RULES)
        != (_codeowners_owner(path) == _CORE_OWNER)
    ]
    assert not mismatched


def test_ci_python_filter_covers_the_drift_test_inputs():
    """Run the drift test on a PR that changes only the paths file or CODEOWNERS.

    A skipped Python job counts as green in ci-success, so without these entries
    the test comparing the two files never executes on the PR that desyncs them.
    """
    globs = ci_filter_globs("python")

    assert _PATHS_FILE_GLOB in globs
    assert _CODEOWNERS_GLOB in globs


def test_glob_star_stays_within_a_segment():
    """Match ``*`` within one path segment only."""
    pattern = core_approval.glob_to_regex("app/*.py")

    assert pattern.fullmatch("app/main.py")
    assert not pattern.fullmatch("app/core/config.py")


def test_later_rule_overrides_an_earlier_exclusion():
    """Let the last matching line win, re-including a path an earlier line excluded."""
    rules = core_approval.parse_path_rules("a/**\n!a/b/**\na/b/keep.py\n")

    assert core_approval.is_foundation_path("a/x.py", rules)
    assert not core_approval.is_foundation_path("a/b/drop.py", rules)
    assert core_approval.is_foundation_path("a/b/keep.py", rules)


def test_latest_approvers_follows_each_reviewers_last_decisive_review():
    """Count a reviewer only when their latest decisive review approves, never the author."""
    states = core_approval.ReviewState
    reviews = [
        _approve(_CORE_A),
        _review(_CORE_A, states.CHANGES_REQUESTED),
        _review(_CORE_B, states.CHANGES_REQUESTED),
        _approve(_CORE_B),
        _review(_CORE_B, states.COMMENTED),
        _approve(_LEAD),
        _review(_LEAD, states.DISMISSED),
        _approve("author"),
    ]

    assert core_approval.latest_approvers(reviews, "author") == {_CORE_B}


def test_member_weights_take_the_heaviest_team():
    """Give a login in several weighted teams the heaviest team's weight."""
    weights = core_approval.member_weights(_MEMBERS)

    assert weights == {_LEAD: 2, _CORE_A: 1, _CORE_B: 1}


@pytest.mark.parametrize(
    ("author", "approvers", "expected"),
    [
        (_OUTSIDER, [_LEAD], core_approval.StatusState.SUCCESS),
        (_OUTSIDER, [_CORE_A, _CORE_B], core_approval.StatusState.SUCCESS),
        (_OUTSIDER, [_CORE_A], core_approval.StatusState.PENDING),
        (_OUTSIDER, [_CORE_A, "someone"], core_approval.StatusState.PENDING),
        (_CORE_A, [_CORE_B], core_approval.StatusState.PENDING),
        (_CORE_A, [_LEAD], core_approval.StatusState.SUCCESS),
        (_LEAD, [], core_approval.StatusState.SUCCESS),
    ],
)
def test_evaluate_weights_foundation_approvals(author, approvers, expected):
    """Pass a foundation change at one lead approval or two core approvals."""
    verdict = core_approval.evaluate(
        _changed(_FOUNDATION_PATH),
        _RULES,
        [_approve(login) for login in approvers],
        author,
        _MEMBERS,
    )

    assert verdict.state == expected
    assert len(verdict.description) <= core_approval.STATUS_DESCRIPTION_LIMIT


def test_evaluate_credits_a_lead_author_in_the_description():
    """Count a lead author's own weight and name them as the author."""
    verdict = core_approval.evaluate(
        _changed(_FOUNDATION_PATH), _RULES, [], _LEAD, _MEMBERS
    )

    assert verdict.description == f"Weight 2/2: {_LEAD} (author)"


def test_evaluate_passes_changes_outside_the_foundation_without_reviews():
    """Pass a change outside the foundation with no reviews at all."""
    verdict = core_approval.evaluate(
        _changed("app/extensions/apps/atw/app.py", "tests/app/test_x.py"),
        _RULES,
        [],
        _OUTSIDER,
        _MEMBERS,
    )

    assert verdict == core_approval.Verdict(
        core_approval.StatusState.SUCCESS, core_approval.NOT_FOUNDATION_DESCRIPTION
    )


def test_evaluate_treats_an_incomplete_listing_as_foundation():
    """Withhold the pass for a capped file listing, whose missing paths may be foundation."""
    changed = _changed("tests/app/test_x.py", complete=False)

    verdict = core_approval.evaluate(changed, _RULES, [], _OUTSIDER, _MEMBERS)

    assert verdict.state == core_approval.StatusState.PENDING


def test_evaluate_passes_an_incomplete_listing_once_approved():
    """Fall through to the weighted rule for a capped listing, so a lead approval passes it."""
    changed = _changed("tests/app/test_x.py", complete=False)

    verdict = core_approval.evaluate(
        changed, _RULES, [_approve(_LEAD)], _OUTSIDER, _MEMBERS
    )

    assert verdict.state == core_approval.StatusState.SUCCESS
    assert verdict.description != core_approval.NOT_FOUNDATION_DESCRIPTION


class _FakeClient:
    def __init__(self, pulls, status=None, held=None):
        self.pulls = pulls
        self.status = status
        self.posted = []
        self.held = held or {}
        self.deleted = []

    def list_held_runs(self, owner, repo, workflow):
        return self.held.get(workflow, [])

    def delete_run(self, owner, repo, run_id):
        self.deleted.append(run_id)

    def list_open_pull_requests(self, owner, repo):
        return self.pulls

    def get_status(self, owner, repo, sha, context):
        return self.status

    def create_status(self, owner, repo, sha, verdict, target_url):
        self.posted.append((sha, verdict, target_url))


_PULLS = [
    core_approval.PullRequest(number=1, author=_OUTSIDER, head_sha="aaa"),
    core_approval.PullRequest(number=2, author=_OUTSIDER, head_sha="bbb"),
]


_PENDING = core_approval.Verdict(core_approval.StatusState.PENDING, "waiting")
_SUCCESS = core_approval.Verdict(core_approval.StatusState.SUCCESS, "done")


def test_combine_verdicts_keeps_a_single_verdict_unchanged():
    """Return a lone pull request's verdict without a number prefix."""
    assert core_approval.combine_verdicts({7: _PENDING}) == _PENDING


def test_combine_verdicts_reports_the_pending_pull_request():
    """Let a pending pull request decide, naming it, when another at the commit passes."""
    combined = core_approval.combine_verdicts({3: _SUCCESS, 5: _PENDING})

    assert combined == core_approval.Verdict(
        core_approval.StatusState.PENDING, "#5: waiting"
    )


def test_combine_verdicts_picks_the_lowest_numbered_pending_pull_request():
    """Report the lowest-numbered of several pending pull requests."""
    combined = core_approval.combine_verdicts({9: _PENDING, 4: _PENDING, 6: _SUCCESS})

    assert combined.description == "#4: waiting"


def test_combine_verdicts_passes_when_every_pull_request_passes():
    """Pass only when all pull requests at the commit pass, naming the lowest number."""
    combined = core_approval.combine_verdicts({8: _SUCCESS, 2: _SUCCESS})

    assert combined == core_approval.Verdict(
        core_approval.StatusState.SUCCESS, "#2: done"
    )


def test_combine_verdicts_keeps_a_prefixed_description_within_the_limit():
    """Truncate a prefixed description to the status description limit."""
    long = core_approval.Verdict(
        core_approval.StatusState.PENDING, "x" * core_approval.STATUS_DESCRIPTION_LIMIT
    )

    combined = core_approval.combine_verdicts({1: long, 2: _SUCCESS})

    assert len(combined.description) == core_approval.STATUS_DESCRIPTION_LIMIT


def test_sync_status_skips_an_unchanged_verdict():
    """Post nothing when the head commit already carries the verdict."""
    verdict = core_approval.Verdict(core_approval.StatusState.PENDING, "waiting")
    client = _FakeClient(_PULLS, status=(verdict.state, verdict.description))

    posted = core_approval.sync_status(
        client, "o", "r", "aaa", [1], verdict, "", lambda _: None
    )

    assert not posted
    assert client.posted == []


def test_sync_status_posts_a_changed_verdict():
    """Post the verdict on the head commit when it differs from the current status."""
    verdict = core_approval.Verdict(core_approval.StatusState.SUCCESS, "done")
    client = _FakeClient(_PULLS, status=("pending", "waiting"))

    posted = core_approval.sync_status(
        client, "o", "r", "aaa", [1], verdict, "https://run", lambda _: None
    )

    assert posted
    assert client.posted == [("aaa", verdict, "https://run")]


def test_delete_held_copilot_runs_spares_runs_others_started():
    """Delete held runs Copilot started in either workflow, and only those."""
    trigger, evaluation = core_approval.HELD_RUN_WORKFLOWS
    copilot = core_approval.COPILOT_ACTOR
    client = _FakeClient(
        _PULLS,
        held={
            trigger: [
                core_approval.HeldRun(run_id=1, actor=copilot),
                core_approval.HeldRun(run_id=2, actor="first-time-contributor"),
            ],
            evaluation: [core_approval.HeldRun(run_id=3, actor=copilot)],
        },
    )

    deleted = core_approval.delete_held_copilot_runs(client, "o", "r", lambda _: None)

    assert client.deleted == [1, 3]
    assert deleted == len(client.deleted)


def _api_error():
    return core_approval.GitHubApiError("DELETE", "/runs/1", 409, "in progress")


class _RefusingClient(_FakeClient):
    """Refuse to delete the held runs listed in ``refused``."""

    def __init__(self, held, refused):
        super().__init__(_PULLS, held=held)
        self.refused = refused

    def delete_run(self, owner, repo, run_id):
        if run_id in self.refused:
            raise _api_error()
        super().delete_run(owner, repo, run_id)


def test_delete_held_copilot_runs_skips_a_run_it_cannot_delete():
    """Log and skip a failed deletion, so the cleanup never stops the evaluation."""
    trigger, _ = core_approval.HELD_RUN_WORKFLOWS
    copilot = core_approval.COPILOT_ACTOR
    client = _RefusingClient(
        held={
            trigger: [
                core_approval.HeldRun(run_id=1, actor=copilot),
                core_approval.HeldRun(run_id=2, actor=copilot),
            ]
        },
        refused={1},
    )

    deleted = core_approval.delete_held_copilot_runs(client, "o", "r", lambda _: None)

    assert client.deleted == [2]
    assert deleted == len(client.deleted)


def test_evaluate_errors_on_a_foundation_change_when_membership_is_unknown():
    """Fail a foundation change closed when team membership could not be resolved."""
    verdict = core_approval.evaluate(
        _changed(_FOUNDATION_PATH), _RULES, [_approve(_LEAD)], _OUTSIDER, None
    )

    assert verdict == core_approval.Verdict(
        core_approval.StatusState.ERROR,
        core_approval.MEMBERSHIP_UNAVAILABLE_DESCRIPTION,
    )


class _FakeRepository:
    """Serve every API call ``main`` makes from in-memory pull requests."""

    def __init__(self, pulls, files, *, members=None, cleanup_fails=False):
        self.pulls = pulls
        self.files = files
        self.members = members
        self.cleanup_fails = cleanup_fails
        self.posted = []

    def list_held_runs(self, owner, repo, workflow):
        if self.cleanup_fails:
            raise _api_error()
        return []

    def delete_run(self, owner, repo, run_id):
        raise AssertionError("no held run was listed")

    def list_team_members(self, org, team):
        if self.members is None:
            raise core_approval.GitHubApiError("GET", "/teams", 404, "Not Found")
        return self.members[team]

    def list_open_pull_requests(self, owner, repo):
        return self.pulls

    def list_changed_paths(self, owner, repo, number):
        return _changed(*self.files[number])

    def list_reviews(self, owner, repo, number):
        return []

    def get_status(self, owner, repo, sha, context):
        return None

    def create_status(self, owner, repo, sha, verdict, target_url):
        self.posted.append((sha, verdict))


_SHARED_SHA = "abc"
_FOUNDATION_PULL = core_approval.PullRequest(
    number=11, author=_OUTSIDER, head_sha=_SHARED_SHA
)
_APP_PULL = core_approval.PullRequest(number=12, author=_OUTSIDER, head_sha=_SHARED_SHA)
_FILES = {11: [_FOUNDATION_PATH], 12: ["app/extensions/apps/atw/app.py"]}


def _run_main(monkeypatch, repository):
    monkeypatch.setenv("GITHUB_TOKEN", "repo-token")
    monkeypatch.setenv("CORE_APPROVAL_READ_ORG_TOKEN", "org-token")
    monkeypatch.setattr(core_approval, "HttpGitHubClient", lambda _token: repository)
    return core_approval.main(["--owner", "o", "--repo", "r"])


def test_main_posts_one_combined_status_per_shared_commit(monkeypatch):
    """Post a single status for pull requests sharing a commit, held to the stricter."""
    repository = _FakeRepository(
        [_FOUNDATION_PULL, _APP_PULL], _FILES, members=_MEMBERS
    )

    assert _run_main(monkeypatch, repository) == 0
    [(sha, verdict)] = repository.posted
    assert sha == _SHARED_SHA
    assert verdict.state == core_approval.StatusState.PENDING
    assert verdict.description.startswith(f"#{_FOUNDATION_PULL.number}: ")


def test_main_still_evaluates_when_the_held_run_cleanup_fails(monkeypatch):
    """Post statuses even when listing held runs fails."""
    repository = _FakeRepository(
        [_APP_PULL], _FILES, members=_MEMBERS, cleanup_fails=True
    )

    assert _run_main(monkeypatch, repository) == 0
    assert repository.posted == [
        (
            _SHARED_SHA,
            core_approval.Verdict(
                core_approval.StatusState.SUCCESS,
                core_approval.NOT_FOUNDATION_DESCRIPTION,
            ),
        )
    ]


def test_main_fails_closed_when_team_membership_is_unavailable(monkeypatch):
    """Post ``error`` on foundation changes and fail the run when teams cannot be read."""
    repository = _FakeRepository([_FOUNDATION_PULL], _FILES, members=None)

    assert _run_main(monkeypatch, repository) == 1
    assert repository.posted == [
        (
            _SHARED_SHA,
            core_approval.Verdict(
                core_approval.StatusState.ERROR,
                core_approval.MEMBERSHIP_UNAVAILABLE_DESCRIPTION,
            ),
        )
    ]


def test_main_refuses_to_run_without_the_org_token(monkeypatch, capsys):
    """Fail fast when the read:org token is absent."""
    monkeypatch.setenv("GITHUB_TOKEN", "repo-token")
    monkeypatch.delenv("CORE_APPROVAL_READ_ORG_TOKEN", raising=False)

    assert core_approval.main(["--owner", "o", "--repo", "r"]) == 1
    assert "CORE_APPROVAL_READ_ORG_TOKEN is not set" in capsys.readouterr().err


def test_main_rejects_a_missing_paths_file(monkeypatch, capsys, tmp_path: Path):
    """Fail fast when the paths file does not exist."""
    monkeypatch.setenv("GITHUB_TOKEN", "repo-token")
    monkeypatch.setenv("CORE_APPROVAL_READ_ORG_TOKEN", "org-token")
    missing = tmp_path / "absent"

    argv = ["--owner", "o", "--repo", "r", "--paths", str(missing)]
    assert core_approval.main(argv) == 1
    assert "file not found" in capsys.readouterr().err
