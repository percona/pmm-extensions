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

"""Compute and sync the pull-request labels ``actions/labeler`` cannot express.

The action matches path globs only, so three labels need code: ``large-diff``
(a changed-line count), ``app-isolated`` (every file inside one app slice, bar
changelog fragments and generated OpenAPI artifacts), and
``qa not required`` (a Dependabot or doc-only PR). This script reads ``app:<name>``
globs from the default-branch ``.github/labeler.yml``, fetches the PR file list via
the GitHub REST API, and adds or removes those labels.

``qa not required`` is computed here rather than declared in ``.github/labeler.yml``
because the action's ``sync-labels`` loop removes any label it holds a rule for,
which strips an instance a maintainer applied by hand and leaves the merge
gate's only QA bypass unusable. Provenance comes from the issue-events API, and
the signal it offers is *identity class*, never intent: the predicate
implemented below is "the newest ``qa not required`` event applied the label and its
actor was not a bot". That is exact for the two actors that matter — the labeler
and this script are both ``github-actions[bot]``, a maintainer is a ``User`` —
and deliberately inexact in one direction, since automation authenticating with
a user-owned token would earn a permanent label.

The ``label-gate`` job in ``.github/workflows/ci.yml`` computes the same predicate
in its own run via ``--print-eligibility``, so a pull request no longer needs the
label written here to clear the gate. Both runs start from one pull-request
activity and proceed concurrently, so the label may not exist yet when the gate
evaluates; and a label applied here authenticates with ``GITHUB_TOKEN``, which
GitHub bars from triggering the CI re-run that would refresh a stale verdict.
For the same reason the gate does not trust a ``qa not required`` label on sight:
one this script applied caches a verdict that a later push can outdate, and its
removal cannot trigger the re-run that would withdraw the approval. The gate
therefore honours the label only when a person applied it, and otherwise decides
from the predicate alone. So a hand-applied label remains the reviewer-facing
record and the bypass for a pull request the predicate does not cover.

Two callers, two trust contexts. ``.github/workflows/labels.yaml`` runs on
``pull_request_target``, which is privileged, and so invokes this after a sparse
checkout of the default branch's ``.github/`` and ``scripts/`` trees only — never
PR-head code. ``label-gate`` runs on ``pull_request`` with a read-only token, where
the workflow and its scripts already come from the pull request's merge commit, so
there this file is PR-head code and is deliberately not pinned. Uses stdlib
``urllib`` so neither workflow step needs a Poetry install.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LABELER = REPO_ROOT / ".github" / "labeler.yml"
GITHUB_API = "https://api.github.com"
GITHUB_PAGE_SIZE = 100
HTTP_NOT_FOUND = 404

LARGE_DIFF_THRESHOLD = 1500

GENERATED_PREFIXES = (
    "frontend/packages/api/src/generated/",
    "frontend/packages/api/specs/",
)
GENERATED_EXACT = frozenset({"poetry.lock", "frontend/pnpm-lock.yaml"})
ISOLATION_NEUTRAL_PREFIXES = (
    *GENERATED_PREFIXES,
    "tests/app/extensions/snapshots/openapi/",
)
CHANGELOG_FRAGMENT_RE = re.compile(
    r"^changelog\.d/(?:SEP|PMM)-\d+\.(?:added|changed|breaking|config|fixed|security)\.md$",
)

QA_NOT_REQUIRED_LABEL = "qa not required"
QA_NOT_REQUIRED_GLOBS = (".github/CODEOWNERS", "README.md", ".gitignore", "dist/**")
DEPENDABOT_LOGIN = "dependabot[bot]"
BOT_ACTOR_TYPE = "Bot"

_LABEL_KEY = re.compile(r"^([A-Za-z0-9:_-]+):\s*$")
_GLOB_LINE = re.compile(r"^\s*-\s*'([^']+)'\s*$")
_REGEX_ESCAPE = re.compile(r"[.*+?^${}()|[\]\\]")


@dataclass(frozen=True, slots=True)
class PrFile:
    """Represent one changed file entry from the pulls list-files API."""

    filename: str
    additions: int = 0
    deletions: int = 0


@dataclass(frozen=True, slots=True)
class PullRequest:
    """Carry the pull-request identity fields the ``qa not required`` predicate reads.

    Both come from the pulls API rather than the head branch name, which the
    pull-request author chooses and so proves nothing about who opened it.
    """

    author_login: str
    head_repository: str


@dataclass(frozen=True, slots=True)
class BlastRadiusResult:
    """Carry the blast-radius signals derived from a PR file list."""

    changed_lines: int
    large_diff: bool
    app_isolated: bool
    touched_apps: tuple[str, ...]


class LabelEventKind(StrEnum):
    """Name the issue-event kinds that carry a label change."""

    LABELED = "labeled"
    UNLABELED = "unlabeled"


LABEL_EVENT_TYPES = frozenset(LabelEventKind)


@dataclass(frozen=True, slots=True)
class LabelEvent:
    """Represent one ``labeled`` / ``unlabeled`` entry from the issue-events API."""

    event: LabelEventKind
    label: str
    actor_type: str
    created_at: str
    event_id: int

    @property
    def order_key(self) -> tuple[str, int]:
        """Return the sort key that identifies the newest event.

        ``created_at`` is ISO-8601 UTC with second granularity, so it sorts
        lexicographically but ties; the monotonic event id breaks those ties.

        :return: The ``(created_at, event_id)`` pair to sort on.
        """
        return (self.created_at, self.event_id)


class GitHubClient(Protocol):
    """Describe the subset of the GitHub REST API used by this script."""

    def get_pull_request(self, owner: str, repo: str, pr_number: int) -> PullRequest:
        """Return the author and head repository of a pull request.

        :param owner: Repository owner.
        :param repo: Repository name without owner.
        :param pr_number: Pull request number.
        :return: The pull request's identity fields.
        """

    def list_pr_files(self, owner: str, repo: str, pr_number: int) -> list[PrFile]:
        """Return every changed file for a pull request.

        :param owner: Repository owner.
        :param repo: Repository name without owner.
        :param pr_number: Pull request number.
        :return: Every changed file, across all result pages.
        """

    def list_issue_labels(self, owner: str, repo: str, issue_number: int) -> set[str]:
        """Return label names currently on an issue or pull request.

        :param owner: Repository owner.
        :param repo: Repository name without owner.
        :param issue_number: Issue or pull request number.
        :return: Label names currently attached.
        """

    def list_issue_events(
        self, owner: str, repo: str, issue_number: int
    ) -> list[LabelEvent]:
        """Return every label event recorded against an issue or pull request.

        :param owner: Repository owner.
        :param repo: Repository name without owner.
        :param issue_number: Issue or pull request number.
        :return: Label events, across all result pages, in API order.
        """

    def add_issue_labels(
        self, owner: str, repo: str, issue_number: int, labels: list[str]
    ) -> None:
        """Attach one or more labels to an issue or pull request.

        :param owner: Repository owner.
        :param repo: Repository name without owner.
        :param issue_number: Issue or pull request number.
        :param labels: Label names to attach.
        """

    def remove_issue_label(
        self, owner: str, repo: str, issue_number: int, name: str
    ) -> None:
        """Remove a single label from an issue or pull request.

        :param owner: Repository owner.
        :param repo: Repository name without owner.
        :param issue_number: Issue or pull request number.
        :param name: Label name to detach.
        """


def is_generated(filename: str) -> bool:
    """Return whether ``filename`` is excluded from the changed-line count.

    :param filename: Path relative to the repository root.
    :return: ``True`` for lockfiles and generated API client paths.
    """
    if filename in GENERATED_EXACT:
        return True
    return filename.startswith(GENERATED_PREFIXES)


def is_isolation_neutral(filename: str) -> bool:
    """Return whether ``filename`` is ignored by the ``app-isolated`` predicate.

    These paths are changelog fragments and artifacts regenerated from backend
    source, so they cannot widen a change beyond the slices its source files
    already touch.

    :param filename: Path relative to the repository root.
    :return: ``True`` for changelog fragments and generated OpenAPI artifacts.
    """
    return (
        filename.startswith(ISOLATION_NEUTRAL_PREFIXES)
        or CHANGELOG_FRAGMENT_RE.match(filename) is not None
    )


def parse_app_globs(labeler_text: str) -> dict[str, list[str]]:
    """Build a map of ``app:<name>`` label to its path globs.

    :param labeler_text: Full ``.github/labeler.yml`` contents.
    :return: Parsed ``app:<name>`` globs in file order.
    """
    app_globs: dict[str, list[str]] = {}
    current_label: str | None = None

    for line in labeler_text.splitlines():
        key_match = _LABEL_KEY.match(line)
        if key_match:
            label = key_match.group(1)
            current_label = label if label.startswith("app:") else None
            if current_label is not None:
                app_globs[current_label] = []
            continue
        if current_label is None:
            continue
        glob_match = _GLOB_LINE.match(line)
        if glob_match:
            app_globs[current_label].append(glob_match.group(1))

    return app_globs


def match_glob(glob: str, filename: str) -> bool:
    """Return whether ``filename`` matches a labeler path glob.

    :param glob: Labeler path glob from ``labeler.yml``.
    :param filename: Path relative to the repository root.
    :return: ``True`` when the path matches.
    """
    if glob.endswith("/**"):
        return filename.startswith(glob[:-2])
    pattern = "[^/]*".join(
        _REGEX_ESCAPE.sub(r"\\\g<0>", part) for part in glob.split("*")
    )
    return re.fullmatch(pattern, filename) is not None


def app_of(filename: str, app_globs: dict[str, list[str]]) -> str | None:
    """Return the ``app:<name>`` label for ``filename``, if any.

    :param filename: Path relative to the repository root.
    :param app_globs: Parsed labeler app globs.
    :return: The matching label name, or ``None``.
    """
    for label, globs in app_globs.items():
        if any(match_glob(glob, filename) for glob in globs):
            return label
    return None


def compute_blast_radius(
    files: list[PrFile], app_globs: dict[str, list[str]]
) -> BlastRadiusResult:
    """Derive blast-radius labels from a PR file list.

    :param files: Changed files from the pulls list-files API.
    :param app_globs: Parsed ``app:<name>`` globs from ``labeler.yml``.
    :return: Changed-line count and label predicates.
    """
    changed_lines = sum(
        file.additions + file.deletions
        for file in files
        if not is_generated(file.filename)
    )
    large_diff = changed_lines > LARGE_DIFF_THRESHOLD

    touched_apps: set[str] = set()
    has_non_app_file = False
    for file in files:
        if is_isolation_neutral(file.filename):
            continue
        app = app_of(file.filename, app_globs)
        if app is None:
            has_non_app_file = True
        else:
            touched_apps.add(app)

    app_isolated = not has_non_app_file and len(touched_apps) == 1
    return BlastRadiusResult(
        changed_lines=changed_lines,
        large_diff=large_diff,
        app_isolated=app_isolated,
        touched_apps=tuple(sorted(touched_apps)),
    )


def sync_blast_radius_labels(
    client: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    result: BlastRadiusResult,
    *,
    log: Callable[[str], None] = print,
) -> None:
    """Add or remove blast-radius labels to match ``result``.

    :param client: GitHub REST client.
    :param owner: Repository owner.
    :param repo: Repository name without owner.
    :param pr_number: Pull request number.
    :param result: Computed blast-radius signals.
    :param log: Callable for informational messages.
    """
    desired = {"large-diff": result.large_diff, "app-isolated": result.app_isolated}
    present = client.list_issue_labels(owner, repo, pr_number)

    for name, want in desired.items():
        if want and name not in present:
            client.add_issue_labels(owner, repo, pr_number, [name])
            log(f"Added {name}")
        elif not want and name in present:
            client.remove_issue_label(owner, repo, pr_number, name)
            log(f"Removed {name}")


def is_dependabot_pull_request(pull: PullRequest, repository: str) -> bool:
    """Return whether Dependabot opened ``pull`` from a branch of ``repository``.

    :param pull: Identity fields of the pull request.
    :param repository: ``owner/name`` of the base repository.
    :return: ``True`` only for a same-repository Dependabot pull request.
    """
    return (
        pull.author_login == DEPENDABOT_LOGIN
        and pull.head_repository.casefold() == repository.casefold()
    )


def qa_not_required_eligible(
    files: list[PrFile], pull: PullRequest, repository: str
) -> bool:
    """Return whether a pull request qualifies for an automatic ``qa not required``.

    :param files: Changed files from the pulls list-files API.
    :param pull: Identity fields of the pull request.
    :param repository: ``owner/name`` of the base repository.
    :return: ``True`` for a Dependabot pull request or an all-documentation diff.
    """
    if is_dependabot_pull_request(pull, repository):
        return True
    return bool(files) and all(
        any(match_glob(glob, file.filename) for glob in QA_NOT_REQUIRED_GLOBS)
        for file in files
    )


def qa_not_required_manually_applied(events: list[LabelEvent]) -> bool:
    """Return whether the newest ``qa not required`` event was a non-bot application.

    An empty history yields ``True``: a label this script cannot account for is
    never stripped.

    :param events: Label events recorded against the pull request.
    :return: ``True`` when the label must survive an automatic removal.
    """
    relevant = [event for event in events if event.label == QA_NOT_REQUIRED_LABEL]
    if not relevant:
        return True
    newest = max(relevant, key=lambda event: event.order_key)
    return (
        newest.event == LabelEventKind.LABELED and newest.actor_type != BOT_ACTOR_TYPE
    )


def sync_qa_not_required_label(
    client: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    *,
    eligible: bool,
    log: Callable[[str], None] = print,
) -> None:
    """Add or remove ``qa not required``, leaving a human-applied label in place.

    The events endpoint is consulted only on the removal branch, so the common
    path costs no extra request.

    :param client: GitHub REST client.
    :param owner: Repository owner.
    :param repo: Repository name without owner.
    :param pr_number: Pull request number.
    :param eligible: Whether the pull request qualifies for the label.
    :param log: Callable for informational messages.
    """
    present = QA_NOT_REQUIRED_LABEL in client.list_issue_labels(owner, repo, pr_number)

    if eligible and not present:
        client.add_issue_labels(owner, repo, pr_number, [QA_NOT_REQUIRED_LABEL])
        log(f"Added {QA_NOT_REQUIRED_LABEL}")
    elif not eligible and present:
        events = client.list_issue_events(owner, repo, pr_number)
        if qa_not_required_manually_applied(events):
            log(f"Kept {QA_NOT_REQUIRED_LABEL} (applied by hand)")
        else:
            client.remove_issue_label(owner, repo, pr_number, QA_NOT_REQUIRED_LABEL)
            log(f"Removed {QA_NOT_REQUIRED_LABEL}")


def qa_not_required_bypass(
    client: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    *,
    eligible: bool,
) -> bool:
    """Return whether the merge gate may let a pull request skip QA.

    A pull request the predicate qualifies always may. Otherwise only a
    ``qa not required`` label a person applied counts: one this script applied
    caches an earlier verdict, and a new push can outdate it without any
    corrective CI run, because the removal authenticates with ``GITHUB_TOKEN``.

    :param client: GitHub REST client.
    :param owner: Repository owner.
    :param repo: Repository name without owner.
    :param pr_number: Pull request number.
    :param eligible: Whether the pull request qualifies for the automatic label.
    :return: ``True`` when the pull request qualifies or carries a hand-applied label.
    """
    if eligible:
        return True
    if QA_NOT_REQUIRED_LABEL not in client.list_issue_labels(owner, repo, pr_number):
        return False
    return qa_not_required_manually_applied(
        client.list_issue_events(owner, repo, pr_number)
    )


class UrllibGitHubClient:
    """Wrap the GitHub REST API using stdlib ``urllib``."""

    def __init__(self, token: str, api_url: str = GITHUB_API) -> None:
        self._api_url = api_url.rstrip("/")
        self._token = token

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        tolerate_missing: bool = False,
    ) -> Any:
        url = f"{self._api_url}{path}"
        data = None
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(  # noqa: S310
            url, data=data, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
                payload = response.read().decode("utf-8")
                if not payload:
                    return None
                return json.loads(payload)
        except urllib.error.HTTPError as exc:
            if tolerate_missing and exc.code == HTTP_NOT_FOUND:
                return None
            raise

    def get_pull_request(self, owner: str, repo: str, pr_number: int) -> PullRequest:
        """Return the author and head repository of a pull request.

        A deleted head repository yields an empty name, which matches no base
        repository.

        :param owner: Repository owner.
        :param repo: Repository name without owner.
        :param pr_number: Pull request number.
        :return: The pull request's identity fields.
        """
        item = self._request("GET", f"/repos/{owner}/{repo}/pulls/{pr_number}")
        return PullRequest(
            author_login=(item.get("user") or {}).get("login", ""),
            head_repository=(item["head"].get("repo") or {}).get("full_name", ""),
        )

    def list_pr_files(self, owner: str, repo: str, pr_number: int) -> list[PrFile]:
        """Return every changed file for a pull request.

        :param owner: Repository owner.
        :param repo: Repository name without owner.
        :param pr_number: Pull request number.
        :return: Every changed file, across all result pages.
        """
        files: list[PrFile] = []
        page = 1
        while True:
            query = urllib.parse.urlencode({"per_page": GITHUB_PAGE_SIZE, "page": page})
            path = f"/repos/{owner}/{repo}/pulls/{pr_number}/files?{query}"
            batch = self._request("GET", path)
            if not batch:
                break
            files.extend(
                PrFile(
                    filename=item["filename"],
                    additions=item.get("additions", 0),
                    deletions=item.get("deletions", 0),
                )
                for item in batch
            )
            if len(batch) < GITHUB_PAGE_SIZE:
                break
            page += 1
        return files

    def list_issue_labels(self, owner: str, repo: str, issue_number: int) -> set[str]:
        """Return label names currently on an issue or pull request.

        :param owner: Repository owner.
        :param repo: Repository name without owner.
        :param issue_number: Issue or pull request number.
        :return: Label names currently attached.
        """
        labels: set[str] = set()
        page = 1
        while True:
            query = urllib.parse.urlencode({"per_page": GITHUB_PAGE_SIZE, "page": page})
            path = f"/repos/{owner}/{repo}/issues/{issue_number}/labels?{query}"
            batch = self._request("GET", path)
            if not batch:
                break
            labels.update(item["name"] for item in batch)
            if len(batch) < GITHUB_PAGE_SIZE:
                break
            page += 1
        return labels

    def list_issue_events(
        self, owner: str, repo: str, issue_number: int
    ) -> list[LabelEvent]:
        """Return every label event recorded against an issue or pull request.

        An absent ``actor`` yields an empty actor type, which classifies the
        event as non-bot and so preserves the label.

        :param owner: Repository owner.
        :param repo: Repository name without owner.
        :param issue_number: Issue or pull request number.
        :return: Label events, across all result pages, in API order.
        """
        events: list[LabelEvent] = []
        page = 1
        while True:
            query = urllib.parse.urlencode({"per_page": GITHUB_PAGE_SIZE, "page": page})
            path = f"/repos/{owner}/{repo}/issues/{issue_number}/events?{query}"
            batch = self._request("GET", path)
            if not batch:
                break
            events.extend(
                LabelEvent(
                    event=LabelEventKind(item["event"]),
                    label=item["label"]["name"],
                    actor_type=(item.get("actor") or {}).get("type", ""),
                    created_at=item["created_at"],
                    event_id=item["id"],
                )
                for item in batch
                if item.get("event") in LABEL_EVENT_TYPES and item.get("label")
            )
            if len(batch) < GITHUB_PAGE_SIZE:
                break
            page += 1
        return events

    def add_issue_labels(
        self, owner: str, repo: str, issue_number: int, labels: list[str]
    ) -> None:
        """Attach one or more labels to an issue or pull request.

        :param owner: Repository owner.
        :param repo: Repository name without owner.
        :param issue_number: Issue or pull request number.
        :param labels: Label names to attach.
        """
        path = f"/repos/{owner}/{repo}/issues/{issue_number}/labels"
        self._request("POST", path, body={"labels": labels})

    def remove_issue_label(
        self, owner: str, repo: str, issue_number: int, name: str
    ) -> None:
        """Remove a single label from an issue or pull request.

        A ``404`` is tolerated here: the label may already be gone when the
        workflow re-runs on a new push.

        :param owner: Repository owner.
        :param repo: Repository name without owner.
        :param issue_number: Issue or pull request number.
        :param name: Label name to detach.
        """
        encoded = urllib.parse.quote(name, safe="")
        path = f"/repos/{owner}/{repo}/issues/{issue_number}/labels/{encoded}"
        self._request("DELETE", path, tolerate_missing=True)


def apply_blast_radius_labels(
    client: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    files: list[PrFile],
    labeler_path: Path,
    *,
    log: Callable[[str], None] = print,
) -> BlastRadiusResult:
    """Compute blast-radius signals from a PR file list and sync the labels.

    :param client: GitHub REST client.
    :param owner: Repository owner.
    :param repo: Repository name without owner.
    :param pr_number: Pull request number.
    :param files: Changed files from the pulls list-files API.
    :param labeler_path: Path to the default-branch ``.github/labeler.yml``.
    :param log: Callable for informational messages.
    :return: Computed blast-radius signals.
    """
    labeler_text = labeler_path.read_text(encoding="utf-8")
    app_globs = parse_app_globs(labeler_text)
    result = compute_blast_radius(files, app_globs)
    log(
        f"changedLines={result.changed_lines} largeDiff={result.large_diff} "
        f"appIsolated={result.app_isolated} apps=[{', '.join(result.touched_apps)}]"
    )
    sync_blast_radius_labels(client, owner, repo, pr_number, result, log=log)
    return result


def main(argv: list[str] | None = None) -> int:
    """Sync one pull request's code-computed labels, or report one and stop.

    ``--print-eligibility`` prints whether the merge gate may let the pull request
    skip QA, which is the automatic ``qa not required`` predicate or a hand-applied
    ``qa not required`` label, and adds or removes no label.

    :param argv: CLI arguments (defaults to ``sys.argv[1:]``).
    :return: ``0`` on success; ``1`` on error.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True, help="repository owner")
    parser.add_argument("--repo", required=True, help="repository name")
    parser.add_argument(
        "--pr-number", type=int, required=True, help="pull request number"
    )
    parser.add_argument(
        "--labeler",
        type=Path,
        default=DEFAULT_LABELER,
        help="path to .github/labeler.yml (default: repo-root .github/labeler.yml)",
    )
    parser.add_argument(
        "--token-env",
        default="GITHUB_TOKEN",
        help="environment variable holding the GitHub API token (default: GITHUB_TOKEN)",
    )
    parser.add_argument(
        "--print-eligibility",
        action="store_true",
        help=(
            "print 'true' when the pull request may skip QA (the automatic "
            "'qa not required' predicate, or that label applied by a person), "
            "else 'false', and exit, adding and removing no label"
        ),
    )
    args = parser.parse_args(argv)

    token = os.environ.get(args.token_env)
    if not token:
        print(f"{args.token_env} is not set", file=sys.stderr)
        return 1
    if not args.print_eligibility and not args.labeler.is_file():
        print(f"{args.labeler}: file not found", file=sys.stderr)
        return 1

    client = UrllibGitHubClient(token)

    def log(message: str) -> None:
        print(message, flush=True)

    try:
        pull = client.get_pull_request(args.owner, args.repo, args.pr_number)
        files = client.list_pr_files(args.owner, args.repo, args.pr_number)
        eligible = qa_not_required_eligible(files, pull, f"{args.owner}/{args.repo}")
        if args.print_eligibility:
            bypass = qa_not_required_bypass(
                client, args.owner, args.repo, args.pr_number, eligible=eligible
            )
            print("true" if bypass else "false")
            return 0
        apply_blast_radius_labels(
            client, args.owner, args.repo, args.pr_number, files, args.labeler, log=log
        )
        sync_qa_not_required_label(
            client,
            args.owner,
            args.repo,
            args.pr_number,
            eligible=eligible,
            log=log,
        )
    except urllib.error.URLError as exc:
        print(f"GitHub API request failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
