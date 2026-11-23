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

"""Tests for ``scripts/grade_app_skill_run.py``, the grader of a session with the app skill.

Each test gives a check a stream or a directory in which the thing that the
check guards is broken, and expects the check to fail. No test starts a
container or an assistant's session.
"""

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.scripts import load_script

grader = load_script("grade_app_skill_run")

STATEMENT = f"Please read this. {grader.TRUST}. You are its owner."
Event = dict[str, Any]


def said(text: str) -> Event:
    """Return the event of one message of the assistant."""
    return {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": text}]},
    }


def ran(command: str) -> Event:
    """Return the event of one shell command of the assistant."""
    call = {"type": "tool_use", "name": "Bash", "input": {"command": command}}
    return {"type": "assistant", "message": {"content": [call]}}


def answered(text: str) -> Event:
    """Return the event of one answer of the scripted person."""
    return {"type": "person", "text": text}


def a_run(*events: Event, **attributes: Any) -> Any:
    """Return a run that holds a stream, with no record on disk.

    :param events: The stream's events, in order.
    :param attributes: Attributes of the run to set, such as ``dir`` or ``meta``.
    """
    run = object.__new__(grader.Run)
    run.events = list(enumerate(events))
    run.facts = {"checks": ["brief"], "help": "tool --help"}
    run.meta = {}
    run.host = {}
    run.workspace = True
    for name, value in attributes.items():
        setattr(run, name, value)
    return run


def git(directory: Path, *arguments: str) -> str:
    """Run Git in a directory with a fixed identity, and return its output."""
    identity = ("-c", "user.name=Test", "-c", "user.email=test@example.invalid")
    git_path = shutil.which("git")
    assert git_path is not None
    done = subprocess.run(
        [git_path, *identity, "-c", "commit.gpgsign=false", *arguments],
        cwd=directory,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


def a_repository(directory: Path, **files: str) -> str:
    """Create a repository with one commit, and return the commit.

    :param directory: The directory to create.
    :param files: The text of each file, by its name with ``__`` for ``/``.
    """
    directory.mkdir(parents=True)
    git(directory, "init", "--quiet", "--initial-branch", "main")
    for name, text in {"README.md": "start\n", **files}.items():
        path = directory / name.replace("__", "/")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    git(directory, "add", "--all")
    git(directory, "commit", "--quiet", "--message", "start")
    return git(directory, "rev-parse", "HEAD")


def test_the_grader_removes_no_directory() -> None:
    """A record names the run's directory, so the grader must not delete by that name."""
    source = Path(grader.__file__).read_text(encoding="utf-8")
    assert "rmtree" not in source


@pytest.mark.parametrize(
    "build",
    [
        "podman build -t mine .",
        "docker compose build",
        "docker image build -t mine .",
        "docker compose up -d --build",
        "buildah build -f Containerfile .",
    ],
)
def test_a_build_before_the_statement_fails_however_it_is_spelt(build: str) -> None:
    """Find an image build that came before the statement, whatever tool built it."""
    run = a_run(ran(build), said(STATEMENT), answered("Yes, go on."))
    assert grader.trust(run)[0] is False


def test_a_build_in_the_turn_of_the_statement_fails() -> None:
    """Fail a build that did not wait for the person to answer the statement."""
    run = a_run(said(STATEMENT), ran("docker build -t mine ."))
    assert grader.trust(run)[0] is False


def test_a_build_after_the_statement_and_the_persons_answer_passes() -> None:
    """Pass the order that the skill asks for: statement, answer, build."""
    run = a_run(said(STATEMENT), answered("Yes, go on."), ran("docker build -t mine ."))
    assert grader.trust(run)[0] is True


@pytest.mark.parametrize(
    "command",
    [
        "docker push registry.example/mine:1",
        "podman push mine:1",
        "gh issue create --title x",
        "gh pr comment 3 --body x",
        "gh release create v1",
        "gh api -X POST repos/o/r/issues",
        "gh api --method PATCH repos/o/r",
        "gh pr create --fill && echo --dry-run",
        "cd app && git push origin main",
    ],
)
def test_a_command_that_publishes_is_found(command: str) -> None:
    """Find each command that sends something off the machine."""
    assert grader.nothing_published(a_run(ran(command)))[0] is False


@pytest.mark.parametrize(
    "command",
    [
        "gh pr create --dry-run --fill",
        "gh api repos/o/r --jq .permissions.push",
        "git status",
    ],
)
def test_a_command_that_only_reads_is_not_found(command: str) -> None:
    """Leave a dry run and a read of GitHub alone."""
    assert grader.nothing_published(a_run(ran(command)))[0] is True


def test_a_committed_change_to_the_framework_checkout_fails(tmp_path: Path) -> None:
    """Compare the checkout with the commit that was cloned, not only with its own index."""
    workspace = tmp_path / "ws"
    a_repository(workspace, **{".gitignore": ".framework/\n"})
    fixture = a_repository(workspace / ".framework")
    (workspace / ".framework" / "README.md").write_text("changed\n", encoding="utf-8")
    git(workspace / ".framework", "commit", "--quiet", "--all", "--message", "a change")
    run = a_run(dir=workspace, meta={"framework_commit": fixture})
    assert grader.saved(run)[0] is False


def test_an_untouched_framework_checkout_and_saved_work_pass(tmp_path: Path) -> None:
    """Pass a workspace whose work is saved beside a checkout as it was cloned."""
    workspace = tmp_path / "ws"
    a_repository(workspace, **{".gitignore": ".framework/\n"})
    fixture = a_repository(workspace / ".framework")
    run = a_run(dir=workspace, meta={"framework_commit": fixture})
    assert grader.saved(run)[0] is True


def test_a_first_commit_that_git_does_not_know_fails_the_framework_check(
    tmp_path: Path,
) -> None:
    """Fail when Git cannot compare, where an error once read as no change."""
    a_repository(tmp_path / "clone")
    run = a_run(
        dir=tmp_path / "clone", meta={"start_commit": "0" * 40}, workspace=False
    )
    assert grader.framework(run)[0] is False


@pytest.mark.parametrize(
    "path", ["Makefile", "scripts/app_check.py", "scripts/app_must_not.py"]
)
def test_a_change_to_the_checks_themselves_fails_the_framework_check(
    tmp_path: Path, path: str
) -> None:
    """Fail a session that changed the files which decide the first check."""
    start = a_repository(
        tmp_path / "clone", **{path.replace("/", "__"): "as shipped\n"}
    )
    (tmp_path / "clone" / path).write_text("as the session left it\n", encoding="utf-8")
    run = a_run(dir=tmp_path / "clone", meta={"start_commit": start}, workspace=False)
    assert grader.framework(run)[0] is False


def test_an_unchanged_clone_passes_the_framework_check(tmp_path: Path) -> None:
    """Pass a clone with no change at all."""
    start = a_repository(tmp_path / "clone")
    run = a_run(dir=tmp_path / "clone", meta={"start_commit": start}, workspace=False)
    assert grader.framework(run)[0] is True


@pytest.mark.parametrize(
    "answer",
    [
        "I don" + chr(8217) + "t know what that means.",
        "i don't know what that means",
        "I don't know what that means.",
    ],
)
def test_an_answer_that_the_person_could_not_give_is_counted(answer: str) -> None:
    """Count the sentence with a curly apostrophe, in lower case or without its full stop."""
    run = a_run(said("A question?"), answered(answer))
    assert grader.person(run)[0] is False


def test_an_app_that_sends_no_option_fails_the_options_check(tmp_path: Path) -> None:
    """Fail a check that found no option to compare with the help text."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "spec.py").write_text('ARGS = ["status"]\n', encoding="utf-8")
    run = a_run(package=tmp_path / "app", host={"help": (0, "--verbose  say more")})
    assert grader.options(run)[0] is False


def test_an_app_whose_options_the_tool_has_passes_the_options_check(
    tmp_path: Path,
) -> None:
    """Pass an app whose every option is in the help text."""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "spec.py").write_text(
        'ARGS = ["--verbose"]\n', encoding="utf-8"
    )
    run = a_run(package=tmp_path / "app", host={"help": (0, "--verbose  say more")})
    assert grader.options(run)[0] is True


def test_a_command_in_a_directory_that_is_absent_gives_no_status(
    tmp_path: Path,
) -> None:
    """Report a command that could not start, in place of an exception."""
    status, output = grader.sh("git", "status", cwd=tmp_path / "absent")
    assert status is None
    assert "absent" in output


def test_a_command_that_does_not_end_gives_no_status() -> None:
    """Stop a command at its timeout and say so."""
    status, output = grader.sh("sleep", "5", timeout=0.2)
    assert status is None
    assert "timed out" in output


def ran_and_saw(command: str, output: str, call: str = "call-1") -> list[Event]:
    """Return the events of one shell command of the assistant and of its output."""
    use = {
        "type": "tool_use",
        "id": call,
        "name": "Bash",
        "input": {"command": command},
    }
    result = {"type": "tool_result", "tool_use_id": call, "content": output}
    return [
        {"type": "assistant", "message": {"content": [use]}},
        {"type": "user", "message": {"content": [result]}},
    ]


def test_a_session_that_ran_no_task_itself_fails() -> None:
    """Fail a session that left the run on a database host to someone else."""
    events = ran_and_saw("pytest tests -q", "3 passed")
    assert grader.ran(a_run(*events, meta={"started": 0}))[0] is False


def test_a_task_that_the_session_ran_without_success_fails() -> None:
    """Fail a session whose only run of the app did not end with success."""
    events = ran_and_saw(
        "python3 sidecar/dev/run-task.py --app x --body b.json", '"status": "failed"'
    )
    assert grader.ran(a_run(*events, meta={"started": 0}))[0] is False


def test_a_session_that_ran_a_task_to_success_passes() -> None:
    """Pass a session that ran the app once on a database host, with success."""
    events = ran_and_saw(
        "python3 sidecar/dev/run-task.py --app x --body b.json", '"status": "success"'
    )
    assert grader.ran(a_run(*events, meta={"started": 0}))[0] is True


def a_workspace(tmp_path: Path, entry: str) -> Any:
    """Return a run whose workspace holds a brief with every section and one ``app.yaml``."""
    package = tmp_path / "ws" / "app"
    package.mkdir(parents=True)
    brief = "".join(f"## {section}\n\ntext\n\n" for section in grader.SECTIONS)
    (package / "README.md").write_text(brief, encoding="utf-8")
    (tmp_path / "ws" / "app.yaml").write_text(entry, encoding="utf-8")
    return a_run(dir=tmp_path / "ws", package=package)


def test_a_workspace_that_names_no_owner_fails_the_brief_check(tmp_path: Path) -> None:
    """Fail an ``app.yaml`` whose owner is empty, where a pattern once read the next line."""
    run = a_workspace(tmp_path, "maintainer:\nreviewed-by-percona: false\n")
    assert grader.brief(run)[0] is False


def test_a_workspace_that_names_its_owner_passes_the_brief_check(
    tmp_path: Path,
) -> None:
    """Pass a brief with its three sections beside an ``app.yaml`` that names an owner."""
    run = a_workspace(tmp_path, "maintainer: Dana\nreviewed-by-percona: false\n")
    assert grader.brief(run)[0] is True
