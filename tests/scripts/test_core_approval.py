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

from tests.scripts import load_script, PROJECT_ROOT

core_approval = load_script("core_approval")

_RULES = core_approval.parse_path_rules(
    core_approval.DEFAULT_PATHS_FILE.read_text(encoding="utf-8")
)
_CODEOWNERS = PROJECT_ROOT / ".github" / "CODEOWNERS"
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
        [_FOUNDATION_PATH],
        _RULES,
        [_approve(login) for login in approvers],
        author,
        _MEMBERS,
    )

    assert verdict.state == expected
    assert len(verdict.description) <= core_approval.STATUS_DESCRIPTION_LIMIT


def test_evaluate_credits_a_lead_author_in_the_description():
    """Count a lead author's own weight and name them as the author."""
    verdict = core_approval.evaluate([_FOUNDATION_PATH], _RULES, [], _LEAD, _MEMBERS)

    assert verdict.description == f"Weight 2/2: {_LEAD} (author)"


def test_evaluate_passes_changes_outside_the_foundation_without_reviews():
    """Pass a change outside the foundation with no reviews at all."""
    verdict = core_approval.evaluate(
        ["app/extensions/apps/atw/app.py", "tests/app/test_x.py"],
        _RULES,
        [],
        _OUTSIDER,
        _MEMBERS,
    )

    assert verdict == core_approval.Verdict(
        core_approval.StatusState.SUCCESS, core_approval.NOT_FOUNDATION_DESCRIPTION
    )


class _FakeClient:
    def __init__(self, pulls, status=None):
        self.pulls = pulls
        self.status = status
        self.posted = []

    def list_open_pull_requests(self, owner, repo):
        return self.pulls

    def get_pull_request(self, owner, repo, number):
        return next(pull for pull in self.pulls if pull.number == number)

    def get_status(self, owner, repo, sha, context):
        return self.status

    def create_status(self, owner, repo, sha, verdict, target_url):
        self.posted.append((sha, verdict, target_url))


_PULLS = [
    core_approval.PullRequest(number=1, author=_OUTSIDER, head_sha="aaa"),
    core_approval.PullRequest(number=2, author=_OUTSIDER, head_sha="bbb"),
]


@pytest.mark.parametrize(
    ("pr_number", "head_sha", "expected"),
    [(2, "", [2]), (None, "bbb", [2]), (None, "zzz", []), (None, "", [1, 2])],
)
def test_select_pull_requests(pr_number, head_sha, expected):
    """Select one pull request, those at a head commit, or every open one."""
    pulls = core_approval.select_pull_requests(
        _FakeClient(_PULLS), "o", "r", pr_number=pr_number, head_sha=head_sha
    )

    assert [pull.number for pull in pulls] == expected


def test_sync_status_skips_an_unchanged_verdict():
    """Post nothing when the head commit already carries the verdict."""
    verdict = core_approval.Verdict(core_approval.StatusState.PENDING, "waiting")
    client = _FakeClient(_PULLS, status=(verdict.state, verdict.description))

    posted = core_approval.sync_status(
        client, "o", "r", _PULLS[0], verdict, "", lambda _: None
    )

    assert not posted
    assert client.posted == []


def test_sync_status_posts_a_changed_verdict():
    """Post the verdict on the head commit when it differs from the current status."""
    verdict = core_approval.Verdict(core_approval.StatusState.SUCCESS, "done")
    client = _FakeClient(_PULLS, status=("pending", "waiting"))

    posted = core_approval.sync_status(
        client, "o", "r", _PULLS[0], verdict, "https://run", lambda _: None
    )

    assert posted
    assert client.posted == [("aaa", verdict, "https://run")]


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
