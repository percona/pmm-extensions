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

"""Post the ``core-approval`` commit status that weights backend-foundation approvals.

GitHub's review rules count approvals per team and cannot express "one approval
from a lead, or two from the core reviewers". This script computes that rule and
reports it as a commit status, which a ruleset then requires.

A pull request whose changed paths match nothing in ``.github/core-approval-paths``
passes outright. Otherwise every reviewer whose latest decisive review is an
approval adds the weight of their heaviest team in ``TEAM_WEIGHTS``, and the pull
request passes once the total reaches ``REQUIRED_WEIGHT``. A lead who authored the
pull request also contributes their own weight, so a lead's change needs one core
approval rather than two.

Each run also deletes this check's own runs that a Copilot review left waiting for
approval, which would otherwise prompt for approval on every pull request.

``.github/workflows/core-approval.yaml`` runs this from a default-branch checkout
in the ``core-approval`` environment, whose read:org token resolves team
membership; ``GITHUB_TOKEN`` reads the pull request and posts the status. Uses the
standard library only, so the workflow needs no Poetry install.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import sys
import urllib.parse
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATHS_FILE = REPO_ROOT / ".github" / "core-approval-paths"
GITHUB_API_HOST = "api.github.com"
GITHUB_PAGE_SIZE = 100
HTTP_REQUEST_TIMEOUT = 30
HTTP_ERROR_MIN = 300

LEADS_TEAM = "pmm-extensions-leads"
CORE_TEAM = "pmm-extensions-review-core"
TEAM_WEIGHTS = {LEADS_TEAM: 2, CORE_TEAM: 1}
AUTHOR_WEIGHTED_TEAMS = frozenset({LEADS_TEAM})
REQUIRED_WEIGHT = 2

STATUS_CONTEXT = "core-approval"
STATUS_DESCRIPTION_LIMIT = 140
NOT_FOUNDATION_DESCRIPTION = "Not a backend-foundation change"

HELD_RUN_WORKFLOWS = ("core-approval-trigger.yaml", "core-approval.yaml")
COPILOT_ACTOR = "Copilot"


class StatusState(StrEnum):
    """Name the commit-status states this check reports."""

    SUCCESS = "success"
    PENDING = "pending"


class ReviewState(StrEnum):
    """Name the pull-request review states the GitHub API returns."""

    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    DISMISSED = "DISMISSED"
    COMMENTED = "COMMENTED"
    PENDING = "PENDING"


DECISIVE_REVIEW_STATES = frozenset(
    {ReviewState.APPROVED, ReviewState.CHANGES_REQUESTED, ReviewState.DISMISSED}
)


class GitHubApiError(Exception):
    """Signal a GitHub REST API call that returned a non-success status."""

    def __init__(self, method: str, path: str, status: int, body: str) -> None:
        super().__init__(f"{method} {path} returned HTTP {status}: {body}")
        self.status = status


@dataclass(frozen=True, slots=True)
class PathRule:
    """Hold one paths-file line: a compiled glob and whether it excludes."""

    pattern: re.Pattern[str]
    exclude: bool


@dataclass(frozen=True, slots=True)
class PullRequest:
    """Carry the pull-request fields the check needs."""

    number: int
    author: str
    head_sha: str


@dataclass(frozen=True, slots=True)
class Review:
    """Record one submitted review on a pull request."""

    reviewer: str
    state: str


@dataclass(frozen=True, slots=True)
class HeldRun:
    """Identify a workflow run waiting for approval and who started it."""

    run_id: int
    actor: str


@dataclass(frozen=True, slots=True)
class Verdict:
    """Describe the commit status to report for a pull request."""

    state: StatusState
    description: str


class GitHubClient(Protocol):
    """Read and write the repository data the check uses."""

    def list_open_pull_requests(self, owner: str, repo: str) -> list[PullRequest]:
        """Return every open pull request.

        :param owner: Repository owner.
        :param repo: Repository name without owner.
        :return: The open pull requests.
        """
        ...

    def get_pull_request(self, owner: str, repo: str, number: int) -> PullRequest:
        """Return one pull request.

        :param owner: Repository owner.
        :param repo: Repository name without owner.
        :param number: Pull request number.
        :return: The pull request.
        """
        ...

    def list_changed_paths(self, owner: str, repo: str, number: int) -> list[str]:
        """Return every path a pull request changes, including rename sources.

        :param owner: Repository owner.
        :param repo: Repository name without owner.
        :param number: Pull request number.
        :return: The changed paths.
        """
        ...

    def list_reviews(self, owner: str, repo: str, number: int) -> list[Review]:
        """Return a pull request's submitted reviews in submission order.

        :param owner: Repository owner.
        :param repo: Repository name without owner.
        :param number: Pull request number.
        :return: The reviews.
        """
        ...

    def get_status(
        self, owner: str, repo: str, sha: str, context: str
    ) -> tuple[str, str] | None:
        """Return the latest ``(state, description)`` for a status context.

        :param owner: Repository owner.
        :param repo: Repository name without owner.
        :param sha: Commit SHA.
        :param context: Status context name.
        :return: The latest state and description, or ``None`` when unset.
        """
        ...

    def create_status(
        self, owner: str, repo: str, sha: str, verdict: Verdict, target_url: str
    ) -> None:
        """Post a commit status.

        :param owner: Repository owner.
        :param repo: Repository name without owner.
        :param sha: Commit SHA.
        :param verdict: The state and description to report.
        :param target_url: Link shown on the status; omitted when empty.
        """
        ...

    def list_held_runs(self, owner: str, repo: str, workflow: str) -> list[HeldRun]:
        """Return a workflow's runs that are waiting for approval.

        :param owner: Repository owner.
        :param repo: Repository name without owner.
        :param workflow: Workflow file name.
        :return: The held runs.
        """
        ...

    def delete_run(self, owner: str, repo: str, run_id: int) -> None:
        """Delete a workflow run.

        :param owner: Repository owner.
        :param repo: Repository name without owner.
        :param run_id: Workflow run ID.
        """
        ...


class TeamDirectory(Protocol):
    """Resolve organization team membership."""

    def list_team_members(self, org: str, team: str) -> set[str]:
        """Return the logins in a team, including its child teams.

        :param org: Organization owning the team.
        :param team: Team slug.
        :return: Member logins.
        """
        ...


def glob_to_regex(glob: str) -> re.Pattern[str]:
    """Compile a paths-file glob into a regular expression for ``fullmatch``.

    :param glob: A glob where ``*`` stays within a segment and ``**`` crosses them.
    :return: The compiled expression.
    """
    parts: list[str] = []
    for piece in re.split(r"(\*\*|\*)", glob):
        if piece == "**":
            parts.append(".*")
        elif piece == "*":
            parts.append("[^/]*")
        else:
            parts.append(re.escape(piece))
    return re.compile("".join(parts))


def parse_path_rules(text: str) -> list[PathRule]:
    """Parse the paths file, skipping blank lines and ``#`` comments.

    :param text: The paths file's contents.
    :return: The rules in file order.
    """
    rules: list[PathRule] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        exclude = line.startswith("!")
        rules.append(
            PathRule(pattern=glob_to_regex(line.removeprefix("!")), exclude=exclude)
        )
    return rules


def is_foundation_path(path: str, rules: list[PathRule]) -> bool:
    """Return whether the last rule matching ``path`` includes it.

    :param path: A repository-relative path.
    :param rules: The parsed paths file.
    :return: ``True`` when the path needs a weighted approval.
    """
    included = False
    for rule in rules:
        if rule.pattern.fullmatch(path):
            included = not rule.exclude
    return included


def latest_approvers(reviews: Iterable[Review], author: str) -> set[str]:
    """Return the reviewers whose latest decisive review is an approval.

    Comments do not change a reviewer's standing; a later change request or
    dismissal withdraws an earlier approval.

    :param reviews: Reviews in submission order.
    :param author: The pull-request author, never counted as an approver.
    :return: The approving logins.
    """
    latest: dict[str, str] = {}
    for review in reviews:
        if review.state in DECISIVE_REVIEW_STATES:
            latest[review.reviewer] = review.state
    return {
        reviewer
        for reviewer, state in latest.items()
        if state == ReviewState.APPROVED and reviewer != author
    }


def load_team_members(directory: TeamDirectory, org: str) -> dict[str, set[str]]:
    """Return the members of every weighted team.

    :param directory: Resolves team membership.
    :param org: The organization owning the teams.
    :return: Logins keyed by team slug.
    """
    return {team: directory.list_team_members(org, team) for team in TEAM_WEIGHTS}


def member_weights(members: dict[str, set[str]]) -> dict[str, int]:
    """Return each login's weight: that of the heaviest team it belongs to.

    :param members: Logins keyed by team slug.
    :return: Weights keyed by login.
    """
    weights: dict[str, int] = {}
    for team, logins in members.items():
        for login in logins:
            weights[login] = max(weights.get(login, 0), TEAM_WEIGHTS[team])
    return weights


def _truncate(description: str) -> str:
    if len(description) <= STATUS_DESCRIPTION_LIMIT:
        return description
    return description[: STATUS_DESCRIPTION_LIMIT - 1] + "…"


def evaluate(
    changed_paths: Iterable[str],
    rules: list[PathRule],
    reviews: Iterable[Review],
    author: str,
    members: dict[str, set[str]],
) -> Verdict:
    """Decide the ``core-approval`` status for one pull request.

    :param changed_paths: Every path the pull request changes.
    :param rules: The parsed paths file.
    :param reviews: The pull request's reviews in submission order.
    :param author: The pull-request author.
    :param members: Weighted-team members keyed by team slug.
    :return: The status to report.
    """
    if not any(is_foundation_path(path, rules) for path in changed_paths):
        return Verdict(StatusState.SUCCESS, NOT_FOUNDATION_DESCRIPTION)

    weights = member_weights(members)
    credited = {
        login: weights[login]
        for login in latest_approvers(reviews, author)
        if weights.get(login)
    }
    if any(author in members.get(team, set()) for team in AUTHOR_WEIGHTED_TEAMS):
        credited[f"{author} (author)"] = weights[author]

    total = sum(credited.values())
    if total >= REQUIRED_WEIGHT:
        names = ", ".join(sorted(credited))
        return Verdict(
            StatusState.SUCCESS,
            _truncate(f"Weight {total}/{REQUIRED_WEIGHT}: {names}"),
        )
    return Verdict(
        StatusState.PENDING,
        f"Needs a lead approval or 2 core approvals (weight {total}/{REQUIRED_WEIGHT})",
    )


def select_pull_requests(
    client: GitHubClient,
    owner: str,
    repo: str,
    *,
    pr_number: int | None,
    head_sha: str,
) -> list[PullRequest]:
    """Return the pull requests one run evaluates.

    :param client: Repository API client.
    :param owner: Repository owner.
    :param repo: Repository name without owner.
    :param pr_number: A single pull request to evaluate, when given.
    :param head_sha: Evaluate the open pull requests at this head commit, when given.
    :return: One pull request, those at ``head_sha``, or every open one.
    """
    if pr_number is not None:
        return [client.get_pull_request(owner, repo, pr_number)]
    pulls = client.list_open_pull_requests(owner, repo)
    if head_sha:
        return [pull for pull in pulls if pull.head_sha == head_sha]
    return pulls


def sync_status(
    client: GitHubClient,
    owner: str,
    repo: str,
    pull: PullRequest,
    verdict: Verdict,
    target_url: str,
    log: Callable[[str], None],
) -> bool:
    """Post ``verdict`` unless the head commit already carries it.

    Skipping unchanged verdicts keeps scheduled sweeps from growing the commit's
    status history, which GitHub caps per commit and context.

    :param client: Repository API client.
    :param owner: Repository owner.
    :param repo: Repository name without owner.
    :param pull: The pull request whose head commit receives the status.
    :param verdict: The status to report.
    :param target_url: Link shown on the status, typically the workflow run.
    :param log: Progress logger.
    :return: ``True`` when a status was posted.
    """
    current = client.get_status(owner, repo, pull.head_sha, STATUS_CONTEXT)
    if current == (verdict.state, verdict.description):
        log(f"#{pull.number}: unchanged ({verdict.state}: {verdict.description})")
        return False
    client.create_status(owner, repo, pull.head_sha, verdict, target_url)
    log(f"#{pull.number}: {verdict.state}: {verdict.description}")
    return True


def delete_held_copilot_runs(
    client: GitHubClient, owner: str, repo: str, log: Callable[[str], None]
) -> int:
    """Delete this check's runs that a Copilot review left waiting for approval.

    GitHub holds every run whose actor is Copilot, and a Copilot review fires the
    trigger workflow on every pull request, which would leave an approval prompt
    on each one. Copilot never approves, so those runs carry no weight to lose.

    :param client: Repository API client.
    :param owner: Repository owner.
    :param repo: Repository name without owner.
    :param log: Progress logger.
    :return: The number of runs deleted.
    """
    deleted = 0
    for workflow in HELD_RUN_WORKFLOWS:
        for run in client.list_held_runs(owner, repo, workflow):
            if run.actor != COPILOT_ACTOR:
                continue
            client.delete_run(owner, repo, run.run_id)
            log(f"Deleted held {workflow} run {run.run_id}")
            deleted += 1
    return deleted


class HttpGitHubClient:
    """Call the GitHub REST API with stdlib ``http.client``."""

    def __init__(self, token: str, host: str = GITHUB_API_HOST) -> None:
        self._host = host
        self._token = token

    def _request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> Any:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": "pmm-extensions-core-approval",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        payload = None
        if body is not None:
            payload = json.dumps(body)
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPSConnection(
            self._host, timeout=HTTP_REQUEST_TIMEOUT
        )
        try:
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            data = response.read().decode("utf-8")
        finally:
            connection.close()
        if response.status >= HTTP_ERROR_MIN:
            raise GitHubApiError(method, path, response.status, data[:200])
        return json.loads(data) if data else None

    def _paginate(self, path: str) -> list[Any]:
        items: list[Any] = []
        page = 1
        while True:
            query = urllib.parse.urlencode({"per_page": GITHUB_PAGE_SIZE, "page": page})
            batch = self._request("GET", f"{path}?{query}")
            items.extend(batch)
            if len(batch) < GITHUB_PAGE_SIZE:
                return items
            page += 1

    @staticmethod
    def _pull_request(item: dict[str, Any]) -> PullRequest:
        return PullRequest(
            number=item["number"],
            author=(item.get("user") or {}).get("login", ""),
            head_sha=item["head"]["sha"],
        )

    def list_open_pull_requests(self, owner: str, repo: str) -> list[PullRequest]:
        """Return every open pull request.

        :param owner: Repository owner.
        :param repo: Repository name without owner.
        :return: The open pull requests.
        """
        items = self._paginate(f"/repos/{owner}/{repo}/pulls")
        return [self._pull_request(item) for item in items]

    def get_pull_request(self, owner: str, repo: str, number: int) -> PullRequest:
        """Return one pull request.

        :param owner: Repository owner.
        :param repo: Repository name without owner.
        :param number: Pull request number.
        :return: The pull request.
        """
        return self._pull_request(
            self._request("GET", f"/repos/{owner}/{repo}/pulls/{number}")
        )

    def list_changed_paths(self, owner: str, repo: str, number: int) -> list[str]:
        """Return every path a pull request changes, including rename sources.

        :param owner: Repository owner.
        :param repo: Repository name without owner.
        :param number: Pull request number.
        :return: Changed paths, with a renamed file's old path alongside its new one.
        """
        paths: list[str] = []
        for item in self._paginate(f"/repos/{owner}/{repo}/pulls/{number}/files"):
            paths.append(item["filename"])
            if item.get("previous_filename"):
                paths.append(item["previous_filename"])
        return paths

    def list_reviews(self, owner: str, repo: str, number: int) -> list[Review]:
        """Return a pull request's submitted reviews in submission order.

        :param owner: Repository owner.
        :param repo: Repository name without owner.
        :param number: Pull request number.
        :return: The reviews.
        """
        items = self._paginate(f"/repos/{owner}/{repo}/pulls/{number}/reviews")
        return [
            Review(
                reviewer=(item.get("user") or {}).get("login", ""), state=item["state"]
            )
            for item in items
        ]

    def get_status(
        self, owner: str, repo: str, sha: str, context: str
    ) -> tuple[str, str] | None:
        """Return the latest ``(state, description)`` for a status context.

        :param owner: Repository owner.
        :param repo: Repository name without owner.
        :param sha: Commit SHA.
        :param context: Status context name.
        :return: The latest state and description, or ``None`` when unset.
        """
        combined = self._request("GET", f"/repos/{owner}/{repo}/commits/{sha}/status")
        for status in combined.get("statuses", []):
            if status["context"] == context:
                return status["state"], status.get("description") or ""
        return None

    def create_status(
        self, owner: str, repo: str, sha: str, verdict: Verdict, target_url: str
    ) -> None:
        """Post a commit status.

        :param owner: Repository owner.
        :param repo: Repository name without owner.
        :param sha: Commit SHA.
        :param verdict: The state and description to report.
        :param target_url: Link shown on the status; omitted when empty.
        """
        body: dict[str, Any] = {
            "state": verdict.state,
            "description": verdict.description,
            "context": STATUS_CONTEXT,
        }
        if target_url:
            body["target_url"] = target_url
        self._request("POST", f"/repos/{owner}/{repo}/statuses/{sha}", body)

    def list_team_members(self, org: str, team: str) -> set[str]:
        """Return the logins in a team, including its child teams.

        :param org: Organization owning the team.
        :param team: Team slug.
        :return: Member logins.
        """
        items = self._paginate(f"/orgs/{org}/teams/{team}/members")
        return {item["login"] for item in items}

    def list_held_runs(self, owner: str, repo: str, workflow: str) -> list[HeldRun]:
        """Return a workflow's runs that are waiting for approval.

        :param owner: Repository owner.
        :param repo: Repository name without owner.
        :param workflow: Workflow file name.
        :return: The held runs.
        """
        query = urllib.parse.urlencode(
            {"status": "action_required", "per_page": GITHUB_PAGE_SIZE}
        )
        path = f"/repos/{owner}/{repo}/actions/workflows/{workflow}/runs?{query}"
        payload = self._request("GET", path)
        return [
            HeldRun(run_id=run["id"], actor=(run.get("actor") or {}).get("login", ""))
            for run in payload.get("workflow_runs", [])
        ]

    def delete_run(self, owner: str, repo: str, run_id: int) -> None:
        """Delete a workflow run.

        :param owner: Repository owner.
        :param repo: Repository name without owner.
        :param run_id: Workflow run ID.
        """
        self._request("DELETE", f"/repos/{owner}/{repo}/actions/runs/{run_id}")


def main(argv: list[str] | None = None) -> int:
    """Evaluate one pull request, those at a head commit, or every open one.

    :param argv: CLI arguments (defaults to ``sys.argv[1:]``).
    :return: ``0`` on success; ``1`` on error.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True, help="repository owner")
    parser.add_argument("--repo", required=True, help="repository name")
    parser.add_argument(
        "--pr-number", default="", help="evaluate only this pull request"
    )
    parser.add_argument(
        "--head-sha",
        default="",
        help="evaluate the open pull requests whose head is this commit",
    )
    parser.add_argument(
        "--paths",
        type=Path,
        default=DEFAULT_PATHS_FILE,
        help="paths file (default: repo-root .github/core-approval-paths)",
    )
    parser.add_argument(
        "--target-url", default="", help="link shown on the posted status"
    )
    parser.add_argument(
        "--token-env",
        default="GITHUB_TOKEN",
        help="environment variable holding the repository token (default: GITHUB_TOKEN)",
    )
    parser.add_argument(
        "--org-token-env",
        default="CORE_APPROVAL_READ_ORG_TOKEN",
        help=(
            "environment variable holding a read:org token "
            "(default: CORE_APPROVAL_READ_ORG_TOKEN)"
        ),
    )
    args = parser.parse_args(argv)

    token = os.environ.get(args.token_env)
    org_token = os.environ.get(args.org_token_env)
    for name, value in ((args.token_env, token), (args.org_token_env, org_token)):
        if not value:
            print(f"{name} is not set", file=sys.stderr)
            return 1
    if not args.paths.is_file():
        print(f"{args.paths}: file not found", file=sys.stderr)
        return 1
    pr_number = int(args.pr_number) if args.pr_number else None

    rules = parse_path_rules(args.paths.read_text(encoding="utf-8"))
    client = HttpGitHubClient(token or "")
    directory = HttpGitHubClient(org_token or "")

    def log(message: str) -> None:
        print(message, flush=True)

    try:
        delete_held_copilot_runs(client, args.owner, args.repo, log)
        members = load_team_members(directory, args.owner)
        pulls = select_pull_requests(
            client, args.owner, args.repo, pr_number=pr_number, head_sha=args.head_sha
        )
        for pull in pulls:
            verdict = evaluate(
                client.list_changed_paths(args.owner, args.repo, pull.number),
                rules,
                client.list_reviews(args.owner, args.repo, pull.number),
                pull.author,
                members,
            )
            sync_status(
                client, args.owner, args.repo, pull, verdict, args.target_url, log
            )
    except (GitHubApiError, OSError) as exc:
        print(f"GitHub API request failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
