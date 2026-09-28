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

"""Tests for ``scripts/sync_pr_labels.py``."""

import json
import urllib.parse
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
import yaml

from tests.scripts import load_script

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

sync_pr_labels = load_script("sync_pr_labels")

_LABELER_PATH = _PROJECT_ROOT / ".github" / "labeler.yml"
_LABELER_TEXT = _LABELER_PATH.read_text(encoding="utf-8")
_APP_GLOBS = sync_pr_labels.parse_app_globs(_LABELER_TEXT)


def _file(name: str, additions: int = 0, deletions: int = 0):
    return sync_pr_labels.PrFile(
        filename=name, additions=additions, deletions=deletions
    )


_REPOSITORY = "percona/pmm-extensions"
_HUMAN_PULL = sync_pr_labels.PullRequest(
    author_login="yyyyyyyan", head_repository=_REPOSITORY
)
_DEPENDABOT_PULL = sync_pr_labels.PullRequest(
    author_login="dependabot[bot]", head_repository=_REPOSITORY
)


def _pull_payload(login: str) -> dict[str, Any]:
    """Return a pulls-API body for a same-repository PR opened by ``login``."""
    return {"user": {"login": login}, "head": {"repo": {"full_name": _REPOSITORY}}}


_CI_WORKFLOW_PATH = _PROJECT_ROOT / ".github" / "workflows" / "ci.yml"


def _ci_filter_globs(name: str) -> list[str]:
    """Return the globs of one ``dorny/paths-filter`` list in the CI workflow.

    :param name: Filter key, such as ``python``.
    :return: Glob entries declared under that key, in file order.
    """
    workflow = yaml.safe_load(_CI_WORKFLOW_PATH.read_text(encoding="utf-8"))
    for step in workflow["jobs"]["changes"]["steps"]:
        filters = step.get("with", {}).get("filters")
        if filters is not None:
            return yaml.safe_load(filters)[name]
    raise AssertionError("ci.yml declares no paths-filter step")


def test_parse_app_globs_discovers_all_app_labels():
    """Parse every ``app:<name>`` block from the committed labeler config."""
    declared = {
        line[:-1]
        for line in _LABELER_TEXT.splitlines()
        if line.startswith("app:") and line.endswith(":")
    }

    assert declared, "committed labeler config declares no app: labels"
    assert set(_APP_GLOBS) == declared
    assert all(_APP_GLOBS[label] for label in declared)


def test_report_app_assets_resolve_through_labeler_globs():
    """Map the report app's own template and asset files to ``app:report``.

    The PDF template and its logo live inside the app now, so they are covered
    by the plain ``app/extensions/apps/report/**`` glob rather than a template alias.
    """
    for path in (
        "app/extensions/apps/report/templates/result_pdf.html.j2",
        "app/extensions/apps/report/assets/percona-logo.png",
    ):
        assert sync_pr_labels.app_of(path, _APP_GLOBS) == "app:report"


def test_e2e_hyphen_aliases_resolve_to_underscore_app_labels():
    """Map hyphenated Playwright specs to underscore app labels."""
    assert (
        sync_pr_labels.app_of(
            "frontend/packages/e2e/tests/mysql-backups.spec.ts", _APP_GLOBS
        )
        == "app:mysql_backups"
    )
    assert (
        sync_pr_labels.app_of(
            "frontend/packages/e2e/tests/alert-troubleshooting.spec.ts",
            _APP_GLOBS,
        )
        == "app:alert_troubleshooting"
    )


def test_svc_and_cross_cutting_paths_are_not_app_slices():
    """Exclude mounted services and framework paths from app-slice membership."""
    assert sync_pr_labels.app_of("app/tasks/worker.py", _APP_GLOBS) is None
    assert sync_pr_labels.app_of("app/core/config.py", _APP_GLOBS) is None
    assert sync_pr_labels.app_of(".github/labeler.yml", _APP_GLOBS) is None


def test_suffixed_e2e_spec_matches_the_app_label():
    """Match a suffixed e2e spec via its app's ``<app>*.spec.ts`` glob."""
    assert (
        sync_pr_labels.app_of(
            "frontend/packages/e2e/tests/snippets-download.spec.ts", _APP_GLOBS
        )
        == "app:snippets"
    )


def test_match_glob_supports_directory_and_wildcard_patterns():
    """Support ``/**`` directory globs and single-segment ``*`` wildcards."""
    assert sync_pr_labels.match_glob(
        "app/extensions/apps/report/**", "app/extensions/apps/report/routes.py"
    )
    assert sync_pr_labels.match_glob(
        "frontend/packages/e2e/tests/report*.spec.ts",
        "frontend/packages/e2e/tests/report.spec.ts",
    )


def test_is_generated_discounts_lockfiles_and_generated_api_paths():
    """Exclude lockfiles and generated API client paths from diff sizing."""
    assert sync_pr_labels.is_generated("poetry.lock")
    assert sync_pr_labels.is_generated("frontend/pnpm-lock.yaml")
    assert sync_pr_labels.is_generated("frontend/packages/api/src/generated/client.ts")
    assert not sync_pr_labels.is_generated("app/extensions/apps/report/routes.py")


def test_compute_blast_radius_marks_a_single_app_pr_as_isolated():
    """Mark a PR confined to one app slice as ``app-isolated``."""
    files = [
        _file("app/extensions/apps/report/a.py", additions=1),
        _file("frontend/packages/apps/report/b.ts", additions=2),
    ]
    result = sync_pr_labels.compute_blast_radius(files, _APP_GLOBS)
    assert result.app_isolated is True
    assert result.touched_apps == ("app:report",)


@pytest.mark.parametrize(
    "filenames",
    [
        pytest.param(
            ["app/extensions/apps/report/a.py", "app/extensions/apps/alerts/b.py"],
            id="two-app-slices",
        ),
        pytest.param(
            ["app/extensions/apps/report/a.py", "app/core/x.py"],
            id="app-slice-plus-cross-cutting",
        ),
        pytest.param([".github/labeler.yml"], id="cross-cutting-only"),
    ],
)
def test_compute_blast_radius_rejects_mixed_app_and_cross_cutting_prs(filenames):
    """Reject PRs that span apps or touch cross-cutting paths."""
    files = [_file(name, additions=1) for name in filenames]

    result = sync_pr_labels.compute_blast_radius(files, _APP_GLOBS)

    assert result.app_isolated is False


def test_compute_blast_radius_applies_threshold_with_generated_discount():
    """Apply the 1500-line threshold and discount generated paths."""
    at_threshold = [
        _file("app/extensions/apps/report/a.py", additions=1000, deletions=500)
    ]
    assert (
        sync_pr_labels.compute_blast_radius(at_threshold, _APP_GLOBS).large_diff
        is False
    )

    over_threshold = [
        _file("app/extensions/apps/report/a.py", additions=1000, deletions=501)
    ]
    assert (
        sync_pr_labels.compute_blast_radius(over_threshold, _APP_GLOBS).large_diff
        is True
    )

    discounted = [
        _file("app/extensions/apps/report/a.py", additions=100),
        _file("frontend/packages/api/src/generated/x.ts", additions=5000),
        _file("poetry.lock", additions=9000),
    ]
    result = sync_pr_labels.compute_blast_radius(discounted, _APP_GLOBS)
    assert result.changed_lines == discounted[0].additions
    assert result.large_diff is False


def test_sync_blast_radius_labels_adds_and_removes_labels():
    """Add missing labels and remove stale ones."""
    client = Mock(spec=sync_pr_labels.GitHubClient)
    client.list_issue_labels.return_value = {"large-diff"}
    result = sync_pr_labels.BlastRadiusResult(
        changed_lines=10,
        large_diff=False,
        app_isolated=True,
        touched_apps=("app:report",),
    )

    sync_pr_labels.sync_blast_radius_labels(
        client, "percona", "pmm-extensions", 42, result, log=lambda _message: None
    )

    client.add_issue_labels.assert_called_once_with(
        "percona", "pmm-extensions", 42, ["app-isolated"]
    )
    client.remove_issue_label.assert_called_once_with(
        "percona", "pmm-extensions", 42, "large-diff"
    )


def _event(
    *,
    event="labeled",
    label="qa not required",
    actor_type="User",
    created_at="2026-08-27T04:42:09Z",
    event_id=1,
):
    return sync_pr_labels.LabelEvent(
        event=sync_pr_labels.LabelEventKind(event),
        label=label,
        actor_type=actor_type,
        created_at=created_at,
        event_id=event_id,
    )


def _qa_not_required_client(present, events=()):
    """Build a mock client whose PR carries ``present`` labels and ``events``."""
    client = Mock(spec=sync_pr_labels.GitHubClient)
    client.list_issue_labels.return_value = set(present)
    client.list_issue_events.return_value = list(events)
    return client


def _sync_qa_not_required(client, *, eligible):
    """Run the ``qa not required`` sync against a mock client for one eligibility."""
    sync_pr_labels.sync_qa_not_required_label(
        client,
        "percona",
        "pmm-extensions",
        42,
        eligible=eligible,
        log=lambda _message: None,
    )


def test_qa_not_required_eligible_on_dependabot_pull_request():
    """Qualify a same-repository Dependabot PR regardless of which files it changes."""
    assert sync_pr_labels.qa_not_required_eligible(
        [_file("poetry.lock"), _file("pyproject.toml")], _DEPENDABOT_PULL, _REPOSITORY
    )


def test_qa_not_required_not_eligible_for_a_human_author():
    """Reject a code PR opened by a person, whatever its branch is called."""
    assert not sync_pr_labels.qa_not_required_eligible(
        [_file("app/main.py")], _HUMAN_PULL, _REPOSITORY
    )


@pytest.mark.parametrize(
    "head_repository", ["someone/pmm-extensions", ""], ids=["fork", "deleted"]
)
def test_qa_not_required_not_eligible_for_dependabot_from_another_repository(
    head_repository,
):
    """Reject a Dependabot-authored PR whose head is a fork or was deleted."""
    pull = sync_pr_labels.PullRequest(
        author_login="dependabot[bot]", head_repository=head_repository
    )
    assert not sync_pr_labels.qa_not_required_eligible(
        [_file("app/main.py")], pull, _REPOSITORY
    )


def test_dependabot_repository_match_ignores_case():
    """Match the head repository against the base without regard to case."""
    pull = sync_pr_labels.PullRequest(
        author_login="dependabot[bot]", head_repository="Percona/pmm-extensions"
    )
    assert sync_pr_labels.is_dependabot_pull_request(pull, _REPOSITORY)


def test_qa_not_required_eligible_on_doc_only_pr():
    """Qualify a branch whose every changed file is documentation."""
    assert sync_pr_labels.qa_not_required_eligible(
        [_file("README.md"), _file(".gitignore")], _HUMAN_PULL, _REPOSITORY
    )


def test_qa_not_required_eligible_covers_dist_subtree():
    """Qualify built assets at any depth under ``dist/``."""
    assert sync_pr_labels.qa_not_required_eligible(
        [_file("dist/app.js"), _file("dist/a/b.css")], _HUMAN_PULL, _REPOSITORY
    )


def test_qa_not_required_eligible_on_a_codeowners_only_pr():
    """Qualify a CODEOWNERS-only PR through the repaired glob.

    The rule this replaces spelled the path root-relative as ``CODEOWNERS``,
    which never matched, so this case asserts the corrected literal.
    """
    assert sync_pr_labels.qa_not_required_eligible(
        [_file(".github/CODEOWNERS")], _HUMAN_PULL, _REPOSITORY
    )


def test_qa_not_required_glob_targets_the_real_codeowners_path():
    """Point the CODEOWNERS glob at the path the repository actually uses."""
    assert ".github/CODEOWNERS" in sync_pr_labels.QA_NOT_REQUIRED_GLOBS
    assert (_PROJECT_ROOT / ".github" / "CODEOWNERS").is_file()
    assert not (_PROJECT_ROOT / "CODEOWNERS").exists()


def test_qa_not_required_not_eligible_when_codeowners_moves_to_the_root():
    """Reject a root-level ``CODEOWNERS``, which the literal glob excludes."""
    assert not sync_pr_labels.qa_not_required_eligible(
        [_file("CODEOWNERS")], _HUMAN_PULL, _REPOSITORY
    )


def test_qa_not_required_not_eligible_on_mixed_pr():
    """Reject a PR that mixes documentation with code."""
    assert not sync_pr_labels.qa_not_required_eligible(
        [_file("README.md"), _file("app/main.py")], _HUMAN_PULL, _REPOSITORY
    )


def test_qa_not_required_not_eligible_on_empty_file_list():
    """Reject an empty file list instead of matching every glob vacuously."""
    assert not sync_pr_labels.qa_not_required_eligible([], _HUMAN_PULL, _REPOSITORY)


def test_qa_not_required_manual_when_newest_event_is_a_user():
    """Treat the newest non-bot application as a human override."""
    events = [
        _event(actor_type="Bot", created_at="2026-08-27T04:42:09Z", event_id=1),
        _event(actor_type="User", created_at="2026-08-27T13:09:50Z", event_id=2),
    ]

    assert sync_pr_labels.qa_not_required_manually_applied(events)


def test_qa_not_required_not_manual_when_newest_event_is_the_bot():
    """Discard an earlier human application once the bot re-applies the label."""
    events = [
        _event(actor_type="User", created_at="2026-08-27T04:42:09Z", event_id=1),
        _event(
            event="unlabeled",
            actor_type="User",
            created_at="2026-08-27T04:43:00Z",
            event_id=2,
        ),
        _event(actor_type="Bot", created_at="2026-08-27T13:09:50Z", event_id=3),
    ]

    assert not sync_pr_labels.qa_not_required_manually_applied(events)


def test_qa_not_required_not_manual_when_newest_event_is_an_unlabel():
    """Treat a removal as the end of any standing human override."""
    events = [
        _event(actor_type="User", created_at="2026-08-27T04:42:09Z", event_id=1),
        _event(
            event="unlabeled",
            actor_type="Bot",
            created_at="2026-08-27T04:42:18Z",
            event_id=2,
        ),
    ]

    assert not sync_pr_labels.qa_not_required_manually_applied(events)


def test_qa_not_required_manual_when_no_events_exist():
    """Keep a label whose provenance the events API cannot explain."""
    assert sync_pr_labels.qa_not_required_manually_applied([])
    assert sync_pr_labels.qa_not_required_manually_applied(
        [_event(label="large-diff", actor_type="Bot")]
    )


def test_qa_not_required_provenance_ignores_response_order():
    """Reach the same verdict however the events endpoint orders its payload."""
    ascending = [
        _event(actor_type="User", created_at="2026-08-27T04:42:09Z", event_id=1),
        _event(
            event="unlabeled",
            actor_type="Bot",
            created_at="2026-08-27T04:42:18Z",
            event_id=2,
        ),
        _event(actor_type="Bot", created_at="2026-08-27T13:09:50Z", event_id=3),
    ]

    shuffled = [ascending[index] for index in (1, 2, 0)]

    for ordering in (ascending, list(reversed(ascending)), shuffled):
        assert sync_pr_labels.qa_not_required_manually_applied(ordering) is False


def test_qa_not_required_provenance_breaks_same_second_ties_by_id():
    """Break a same-second tie with the monotonic event id."""
    tied = [
        _event(
            actor_type="User", created_at="2026-08-27T13:09:50Z", event_id=30105340084
        ),
        _event(
            event="unlabeled",
            actor_type="Bot",
            created_at="2026-08-27T13:09:50Z",
            event_id=30105340137,
        ),
    ]

    assert sync_pr_labels.qa_not_required_manually_applied(tied) is False
    assert (
        sync_pr_labels.qa_not_required_manually_applied(list(reversed(tied))) is False
    )


def test_qa_not_required_not_manual_for_a_bot_created_pr_label():
    """Classify a label applied under ``GITHUB_TOKEN`` as automatic.

    ``scripts/release.py`` opens the dev-bump PR with the ``qa not required`` label
    through a token whose actor is ``github-actions[bot]``, so that label stays
    removable exactly as it is today.
    """
    events = [_event(actor_type="Bot", created_at="2026-08-27T13:09:50Z", event_id=9)]

    assert not sync_pr_labels.qa_not_required_manually_applied(events)


def test_sync_qa_not_required_adds_when_eligible_and_absent():
    """Apply the label to an eligible PR that does not carry it yet."""
    client = _qa_not_required_client(present=set())

    _sync_qa_not_required(client, eligible=True)

    client.add_issue_labels.assert_called_once_with(
        "percona", "pmm-extensions", 42, ["qa not required"]
    )
    client.remove_issue_label.assert_not_called()


def test_sync_qa_not_required_removes_a_bot_applied_stale_label():
    """Strip a label the bot applied once the PR stops qualifying."""
    client = _qa_not_required_client(
        present={"qa not required"}, events=[_event(actor_type="Bot", event_id=1)]
    )

    _sync_qa_not_required(client, eligible=False)

    client.remove_issue_label.assert_called_once_with(
        "percona", "pmm-extensions", 42, "qa not required"
    )
    client.add_issue_labels.assert_not_called()


def test_sync_qa_not_required_keeps_a_human_applied_label():
    """Leave a human-applied label in place on a PR that no longer qualifies."""
    client = _qa_not_required_client(
        present={"qa not required"}, events=[_event(actor_type="User", event_id=1)]
    )

    _sync_qa_not_required(client, eligible=False)

    client.remove_issue_label.assert_not_called()
    client.add_issue_labels.assert_not_called()


def test_sync_qa_not_required_is_idempotent_when_already_correct():
    """Issue no request when the label already matches eligibility."""
    client = _qa_not_required_client(present={"qa not required"})

    _sync_qa_not_required(client, eligible=True)

    client.add_issue_labels.assert_not_called()
    client.remove_issue_label.assert_not_called()
    client.list_issue_events.assert_not_called()


def test_qa_not_required_bypass_reads_no_label_when_eligible():
    """Let a qualifying pull request skip QA without consulting its labels."""
    client = _qa_not_required_client(present=())

    assert sync_pr_labels.qa_not_required_bypass(
        client, "percona", "pmm-extensions", 42, eligible=True
    )
    client.list_issue_labels.assert_not_called()
    client.list_issue_events.assert_not_called()


def test_labeler_config_declares_no_qa_not_required_rule():
    """Keep the label out of the labeler config that ``sync-labels`` walks.

    A rule here would put the label back into the action's sync loop, which is
    what strips a manually-applied instance.
    """
    declared = {line.strip() for line in _LABELER_TEXT.splitlines()}
    assert f"{sync_pr_labels.QA_NOT_REQUIRED_LABEL}:" not in declared


def test_ci_python_filter_covers_the_labeler_config():
    """Run the pytest tier on a PR that changes only the labeler config.

    Without this entry the guard above never executes on the one PR shape that
    can reintroduce the rule.
    """
    assert ".github/labeler.yml" in _ci_filter_globs("python")


def test_ci_python_filter_covers_itself():
    """Run the pytest tier on a PR that changes only the workflow itself.

    The filter declares which paths reach the tests, so a PR editing only it
    would otherwise drop the guard above without any test observing the loss.
    """
    assert ".github/workflows/ci.yml" in _ci_filter_globs("python")


def test_main_requires_github_token(tmp_path, monkeypatch, capsys):
    """Fail cleanly when the GitHub token environment variable is unset."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    labeler = tmp_path / "labeler.yml"
    labeler.write_text("app:demo:\n- any: []\n", encoding="utf-8")

    assert (
        sync_pr_labels.main(
            [
                "--owner",
                "percona",
                "--repo",
                "pmm-extensions",
                "--pr-number",
                "1",
                "--labeler",
                str(labeler),
            ]
        )
        == 1
    )
    assert "GITHUB_TOKEN is not set" in capsys.readouterr().err


def test_main_reports_missing_labeler_cleanly(monkeypatch, capsys):
    """Report a missing labeler file as a one-line CLI error."""
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    assert (
        sync_pr_labels.main(
            [
                "--owner",
                "percona",
                "--repo",
                "pmm-extensions",
                "--pr-number",
                "1",
                "--labeler",
                "/tmp/does-not-exist-labeler.yml",
                "--token-env",
                "GITHUB_TOKEN",
            ]
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "file not found" in err
    assert "Traceback" not in err


def _requested_page(url: str) -> str:
    """Return the ``page`` query argument of a recorded request URL.

    :param url: Full request URL recorded by the fake ``urlopen``.
    :return: The ``page`` value.
    """
    query = urllib.parse.urlsplit(url).query
    return urllib.parse.parse_qs(query)["page"][0]


def _patch_urlopen_not_found(monkeypatch):
    """Make every request raise ``HTTPError`` 404."""

    def _raise(*_args, **_kwargs):
        raise sync_pr_labels.urllib.error.HTTPError(
            "https://api.github.com", 404, "Not Found", {}, None
        )

    monkeypatch.setattr(sync_pr_labels.urllib.request, "urlopen", _raise)


def _patch_urlopen_pages(monkeypatch, pages):
    """Serve ``pages`` as successive JSON responses and record requested URLs.

    :param monkeypatch: The pytest monkeypatch fixture.
    :param pages: Response payloads to return, one per request in order.
    :return: The list that accumulates each requested URL.
    """
    requested: list[str] = []
    remaining = list(pages)

    class _Response:
        def __init__(self, payload):
            self._payload = json.dumps(payload).encode("utf-8")

        def read(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def _urlopen(request, *_args, **_kwargs):
        requested.append(request.full_url)
        return _Response(remaining.pop(0))

    monkeypatch.setattr(sync_pr_labels.urllib.request, "urlopen", _urlopen)
    return requested


def test_list_pr_files_walks_every_page(monkeypatch):
    """Follow pagination until a short page ends the walk."""
    page_size = sync_pr_labels.GITHUB_PAGE_SIZE
    full_page = [
        {
            "filename": f"app/extensions/apps/report/f{index}.py",
            "additions": 1,
            "deletions": 0,
        }
        for index in range(page_size)
    ]
    last_page = [{"filename": "app/core/x.py", "additions": 2, "deletions": 3}]
    requested = _patch_urlopen_pages(monkeypatch, [full_page, last_page])

    client = sync_pr_labels.UrllibGitHubClient("token")
    files = client.list_pr_files("percona", "pmm-extensions", 1)

    assert len(files) == page_size + 1
    assert files[-1] == sync_pr_labels.PrFile(
        filename="app/core/x.py", additions=2, deletions=3
    )
    assert [_requested_page(url) for url in requested] == ["1", "2"]


def test_list_issue_labels_walks_every_page(monkeypatch):
    """Accumulate label names across every page of the labels endpoint."""
    page_size = sync_pr_labels.GITHUB_PAGE_SIZE
    full_page = [{"name": f"label-{index}"} for index in range(page_size)]
    last_page = [{"name": "large-diff"}]
    requested = _patch_urlopen_pages(monkeypatch, [full_page, last_page])

    client = sync_pr_labels.UrllibGitHubClient("token")
    labels = client.list_issue_labels("percona", "pmm-extensions", 1)

    assert len(labels) == page_size + 1
    assert "large-diff" in labels
    assert [_requested_page(url) for url in requested] == ["1", "2"]


def test_list_issue_events_walks_every_page(monkeypatch):
    """Walk every page before the newest label event is selected.

    A single page can miss the decisive event entirely on a long-lived PR. The
    first page mixes in events the mapper drops, so its raw length is a full
    page while its mapped length is not — a continuation check on the mapped
    count would stop the walk one page early and never reach the newest event.
    """
    page_size = sync_pr_labels.GITHUB_PAGE_SIZE
    unfiltered_on_first_page = 3
    full_page = [
        {
            "id": index,
            "event": "labeled",
            "label": {"name": "large-diff"},
            "actor": {"login": "github-actions[bot]", "type": "Bot"},
            "created_at": "2026-08-27T04:42:09Z",
        }
        for index in range(page_size - unfiltered_on_first_page)
    ] + [
        {"id": 900 + index, "event": "renamed", "created_at": "2026-08-27T04:42:10Z"}
        for index in range(unfiltered_on_first_page)
    ]
    last_page = [
        {
            "id": 30105340137,
            "event": "labeled",
            "label": {"name": "qa not required"},
            "actor": {"login": "yyyyyyyan", "type": "User"},
            "created_at": "2026-08-27T13:09:50Z",
        },
        {
            "id": 30105340200,
            "event": "renamed",
            "created_at": "2026-08-27T13:10:00Z",
        },
    ]
    requested = _patch_urlopen_pages(monkeypatch, [full_page, last_page])

    client = sync_pr_labels.UrllibGitHubClient("token")
    events = client.list_issue_events("percona", "pmm-extensions", 1)

    assert [_requested_page(url) for url in requested] == ["1", "2"]
    assert len(events) == page_size - unfiltered_on_first_page + 1
    assert events[-1] == sync_pr_labels.LabelEvent(
        event="labeled",
        label="qa not required",
        actor_type="User",
        created_at="2026-08-27T13:09:50Z",
        event_id=30105340137,
    )
    assert sync_pr_labels.qa_not_required_manually_applied(events)


def test_list_issue_events_treats_a_missing_actor_as_non_bot(monkeypatch):
    """Keep a label whose actor the API omits, rather than stripping it."""
    page = [
        {
            "id": 7,
            "event": "labeled",
            "label": {"name": "qa not required"},
            "actor": None,
            "created_at": "2026-08-27T13:09:50Z",
        }
    ]
    _patch_urlopen_pages(monkeypatch, [page])

    client = sync_pr_labels.UrllibGitHubClient("token")
    events = client.list_issue_events("percona", "pmm-extensions", 1)

    assert events[0].actor_type == ""
    assert sync_pr_labels.qa_not_required_manually_applied(events)


def test_request_raises_on_unexpected_not_found(monkeypatch):
    """Surface a 404 from the file list instead of reporting zero files."""
    _patch_urlopen_not_found(monkeypatch)
    client = sync_pr_labels.UrllibGitHubClient("token")

    with pytest.raises(sync_pr_labels.urllib.error.HTTPError):
        client.list_pr_files("percona", "pmm-extensions", 1)


def test_get_pull_request_reads_author_and_head_repository(monkeypatch):
    """Map the pulls API body onto the identity fields the predicate reads."""
    requested = _patch_urlopen_pages(monkeypatch, [_pull_payload("dependabot[bot]")])

    client = sync_pr_labels.UrllibGitHubClient("token")
    pull = client.get_pull_request("percona", "pmm-extensions", 7)

    assert pull == _DEPENDABOT_PULL
    assert requested == ["https://api.github.com/repos/percona/pmm-extensions/pulls/7"]


def test_get_pull_request_maps_a_deleted_head_repository_to_empty(monkeypatch):
    """Map a ``null`` head repository to a name that matches no base."""
    _patch_urlopen_pages(
        monkeypatch, [{"user": {"login": "dependabot[bot]"}, "head": {"repo": None}}]
    )

    client = sync_pr_labels.UrllibGitHubClient("token")

    assert client.get_pull_request("percona", "pmm-extensions", 7).head_repository == ""


def test_remove_issue_label_tolerates_a_missing_label(monkeypatch):
    """Treat a 404 on label removal as a benign already-removed race."""
    _patch_urlopen_not_found(monkeypatch)
    client = sync_pr_labels.UrllibGitHubClient("token")

    client.remove_issue_label("percona", "pmm-extensions", 1, "large-diff")


def _patch_urlopen_routes(monkeypatch, routes):
    """Serve responses by URL fragment and record every request made.

    ``_patch_urlopen_pages`` replays one payload per request in order, which
    cannot serve a flow that hits several endpoints; this routes on the URL
    instead.

    :param monkeypatch: The pytest monkeypatch fixture.
    :param routes: ``(fragment, payload)`` pairs; the first fragment contained
        in the request URL wins. A ``None`` payload serves an empty body.
    :return: The list accumulating ``(method, url, body)`` per request.
    """
    recorded: list[tuple[str, str, bytes | None]] = []

    class _Response:
        def __init__(self, payload):
            self._payload = (
                b"" if payload is None else json.dumps(payload).encode("utf-8")
            )

        def read(self):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def _urlopen(request, *_args, **_kwargs):
        recorded.append((request.method, request.full_url, request.data))
        for fragment, payload in routes:
            if fragment in request.full_url:
                return _Response(payload)
        raise AssertionError(f"unrouted request: {request.full_url}")

    monkeypatch.setattr(sync_pr_labels.urllib.request, "urlopen", _urlopen)
    return recorded


def test_main_fetches_the_file_list_once_and_feeds_both_label_syncs(
    tmp_path, monkeypatch
):
    """Reuse a single file fetch across both label syncs.

    ``main`` owns the fetch so that blast-radius and ``qa not required`` share one
    result; this pins that wiring, which no single-function test can observe.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    labeler = tmp_path / "labeler.yml"
    labeler.write_text(
        "app:demo:\n"
        "- any:\n"
        "  - changed-files:\n"
        "    - any-glob-to-any-file:\n"
        "      - 'app/extensions/apps/demo/**'\n",
        encoding="utf-8",
    )
    recorded = _patch_urlopen_routes(
        monkeypatch,
        [
            (
                "/pulls/7/files",
                [{"filename": "poetry.lock", "additions": 9000, "deletions": 0}],
            ),
            ("/pulls/7", _pull_payload("dependabot[bot]")),
            ("/issues/7/labels", []),
        ],
    )

    assert (
        sync_pr_labels.main(
            [
                "--owner",
                "percona",
                "--repo",
                "pmm-extensions",
                "--pr-number",
                "7",
                "--labeler",
                str(labeler),
            ]
        )
        == 0
    )

    file_fetches = [url for _method, url, _body in recorded if "/pulls/7/files" in url]
    assert len(file_fetches) == 1, "the PR file list must be fetched exactly once"

    writes = [
        (method, json.loads(body))
        for method, _url, body in recorded
        if body is not None
    ]
    assert writes == [("POST", {"labels": ["qa not required"]})]


def test_print_eligibility_reports_true_and_writes_no_label(monkeypatch, capsys):
    """Report an eligible pull request without touching its labels.

    ``label-gate`` runs this mode with a read-only token, so a write here would
    fail the merge gate rather than mislabel.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    recorded = _patch_urlopen_routes(
        monkeypatch,
        [
            (
                "/pulls/7/files",
                [{"filename": "app/main.py", "additions": 3, "deletions": 0}],
            ),
            ("/pulls/7", _pull_payload("dependabot[bot]")),
        ],
    )

    assert (
        sync_pr_labels.main(
            [
                "--owner",
                "percona",
                "--repo",
                "pmm-extensions",
                "--pr-number",
                "7",
                "--print-eligibility",
            ]
        )
        == 0
    )

    assert capsys.readouterr().out == "true\n"
    assert {method for method, _url, _body in recorded} == {"GET"}


def test_print_eligibility_reports_false_for_a_code_diff(monkeypatch, capsys):
    """Report a pull request that needs QA as ineligible."""
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    _patch_urlopen_routes(
        monkeypatch,
        [
            (
                "/pulls/7/files",
                [{"filename": "app/main.py", "additions": 3, "deletions": 0}],
            ),
            ("/pulls/7", _pull_payload("yyyyyyyan")),
            ("/issues/7/labels", []),
        ],
    )

    assert (
        sync_pr_labels.main(
            [
                "--owner",
                "percona",
                "--repo",
                "pmm-extensions",
                "--pr-number",
                "7",
                "--print-eligibility",
            ]
        )
        == 0
    )

    assert capsys.readouterr().out == "false\n"


@pytest.mark.parametrize(
    ("actor_type", "expected"),
    [("User", "true\n"), ("Bot", "false\n")],
    ids=["hand-applied", "bot-applied"],
)
def test_print_eligibility_honours_only_a_hand_applied_label(
    monkeypatch, capsys, actor_type, expected
):
    """Let a person's ``qa not required`` skip QA, but not a stale automatic one.

    The automatic label caches an earlier verdict. A push that outdates it cannot
    trigger the CI re-run its removal would need, so the gate must not trust it.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    _patch_urlopen_routes(
        monkeypatch,
        [
            (
                "/pulls/7/files",
                [{"filename": "app/main.py", "additions": 3, "deletions": 0}],
            ),
            ("/pulls/7", _pull_payload("yyyyyyyan")),
            ("/issues/7/labels", [{"name": "qa not required"}]),
            (
                "/issues/7/events",
                [
                    {
                        "id": 1,
                        "event": "labeled",
                        "label": {"name": "qa not required"},
                        "actor": {"login": "someone", "type": actor_type},
                        "created_at": "2026-09-23T17:00:00Z",
                    }
                ],
            ),
        ],
    )

    assert (
        sync_pr_labels.main(
            [
                "--owner",
                "percona",
                "--repo",
                "pmm-extensions",
                "--pr-number",
                "7",
                "--print-eligibility",
            ]
        )
        == 0
    )

    assert capsys.readouterr().out == expected


def test_print_eligibility_needs_no_labeler_file(monkeypatch, capsys):
    """Compute the predicate without the labeler file the sync path requires.

    The predicate reads no ``app:<name>`` glob, and ``label-gate`` resolves the
    default ``--labeler`` against a checkout that need not carry it.
    """
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    _patch_urlopen_routes(
        monkeypatch,
        [
            (
                "/pulls/7/files",
                [{"filename": "README.md", "additions": 1, "deletions": 0}],
            ),
            ("/pulls/7", _pull_payload("yyyyyyyan")),
        ],
    )

    assert (
        sync_pr_labels.main(
            [
                "--owner",
                "percona",
                "--repo",
                "pmm-extensions",
                "--pr-number",
                "7",
                "--labeler",
                "/tmp/does-not-exist-labeler.yml",
                "--print-eligibility",
            ]
        )
        == 0
    )

    assert capsys.readouterr().out == "true\n"
