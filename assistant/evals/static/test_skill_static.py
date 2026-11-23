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

"""Tests of the app skill that need no model.

The skill must work in any assistant that reads the Agent Skills format, so
these tests hold it to the specification and to nothing that one assistant
alone has. They also install the skill as a person would, with
``gh skill install``, and check where it lands for Claude Code and for Cursor.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_NAME = "pmm-extensions-app"
SKILL_PATH = f".agents/skills/{SKILL_NAME}"
SKILL_DIR = REPO_ROOT / SKILL_PATH
LINK = f".claude/skills/{SKILL_NAME}"
EVALS_DIR = REPO_ROOT / "assistant" / "evals"
HELPER_RE = re.compile(r"\.\./(lib/[\w.-]+)")
SKILL_FILE = SKILL_DIR / "SKILL.md"
PERSON_FILE = SKILL_DIR / "references" / "the-person.md"
MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
MAX_SKILL_LINES = 250
NAME_RE = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
ONE_ASSISTANT_ONLY = ("${CLAUDE_", "subagent", "hook", "allowed-tools")
BANNED_WORDS = (
    "scaffold",
    "flavor",
    "run mode",
    "form model",
    "form schema",
    "marker",
    "spec",
    "task",
    "payload",
    "sidecar",
    "image",
    "container",
    "settings.yaml",
    "conformance",
    "contract test",
    "snapshot test",
    "snapshot file",
    "virtualenv",
    "branch",
    "commit",
    "pull request",
    "repository",
    "endpoint",
)
INSTALL_TARGETS = {"claude-code": ".claude/skills", "cursor": ".agents/skills"}


def frontmatter() -> dict[str, str]:
    """Return the YAML frontmatter of the skill's main file.

    :return: The frontmatter's keys and values.
    """
    text = SKILL_FILE.read_text(encoding="utf-8")
    assert text.startswith("---\n"), "SKILL.md must start with YAML frontmatter"
    return yaml.safe_load(text.split("---\n", 2)[1])


def skill_files() -> list[Path]:
    """Return every file of the skill.

    :return: The files under the skill's directory.
    """
    return sorted(path for path in SKILL_DIR.rglob("*") if path.is_file())


def question_templates() -> list[str]:
    """Return the questions that the skill gives the assistant word for word.

    A question template is a block-quoted line of the reference about the person.

    :return: The lines, without the quote mark.
    """
    lines = PERSON_FILE.read_text(encoding="utf-8").splitlines()
    return [line.lstrip("> ").strip() for line in lines if line.startswith(">")]


def test_the_name_meets_the_specification() -> None:
    """Hold the name to the directory's name and to the specification's form."""
    name = frontmatter()["name"]
    assert name == SKILL_DIR.name == SKILL_NAME
    assert len(name) <= MAX_NAME_LENGTH
    assert NAME_RE.fullmatch(name)


def test_the_description_meets_the_specification() -> None:
    """Keep the description present and within the specification's limit."""
    description = frontmatter()["description"]
    assert 0 < len(description) <= MAX_DESCRIPTION_LENGTH


def test_the_frontmatter_names_no_tool_of_one_assistant() -> None:
    """Leave out ``allowed-tools``, which other assistants read differently."""
    assert "allowed-tools" not in frontmatter()


def test_the_skill_file_is_short() -> None:
    """Keep the skill's main file small enough to be read whole at every start."""
    lines = SKILL_FILE.read_text(encoding="utf-8").splitlines()
    assert len(lines) < MAX_SKILL_LINES


def test_no_file_uses_a_feature_of_one_assistant() -> None:
    """Fail on a variable, a tool list or a mechanism that one assistant has."""
    found = {
        str(path.relative_to(SKILL_DIR)): [
            word
            for word in ONE_ASSISTANT_ONLY
            if word.lower() in path.read_text(encoding="utf-8").lower()
        ]
        for path in skill_files()
        if path.suffix == ".md"
    }
    assert {path: words for path, words in found.items() if words} == {}


def test_no_question_template_holds_a_banned_word() -> None:
    """Keep the words of the framework out of what the person is asked."""
    if not PERSON_FILE.is_file():
        pytest.skip("references/the-person.md is not written yet")
    templates = question_templates()
    assert templates, "the-person.md has no block-quoted question template"
    found = {
        template: [
            word
            for word in BANNED_WORDS
            if re.search(rf"\b{re.escape(word)}s?\b", template, flags=re.IGNORECASE)
        ]
        for template in templates
    }
    assert {template: hits for template, hits in found.items() if hits} == {}


def test_git_holds_every_file_of_the_skill() -> None:
    """Fail for a file that a clone would lack, such as one that an ignore rule hides."""
    git = shutil.which("git")
    assert git is not None
    listed = subprocess.run(
        [git, "ls-files", "--cached", "--", str(SKILL_DIR)],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    on_disk = sorted(
        str(path.relative_to(REPO_ROOT))
        for path in SKILL_DIR.rglob("*")
        if path.is_file()
    )
    assert on_disk == sorted(listed)


def index_entry(path: str) -> tuple[str, str]:
    """Return what Git's index holds for one path.

    The index is read, and not the directory, because a contributor's checkout
    may leave a path out of its working tree.

    :param path: The path, from the repository's root.
    :return: The entry's mode and the text of its blob. Two empty strings when
        Git tracks no such path.
    """
    git = shutil.which("git")
    assert git is not None
    listed = subprocess.run(
        [git, "ls-files", "--stage", "--", path],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    if not listed:
        return "", ""
    blob = subprocess.run(
        [git, "cat-file", "blob", listed[1]],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return listed[0], blob


def test_the_skill_is_a_directory_of_files_where_assistants_read_skills() -> None:
    """Keep the skill's files themselves in the directory that most assistants read."""
    assert index_entry(f"{SKILL_PATH}/SKILL.md")[0] == "100644"


def test_the_other_place_is_a_link_to_the_skill() -> None:
    """Keep one copy of the skill, with a link where Claude Code looks for it."""
    assert index_entry(LINK) == ("120000", f"../../{SKILL_PATH}")


def test_git_holds_every_helper_that_a_case_runs() -> None:
    """Fail for a helper that a clone would lack, such as one that an ignore rule hides."""
    scripts = sorted(EVALS_DIR.glob("*/place-files.sh"))
    assert scripts
    helpers = {
        f"assistant/evals/{helper}"
        for script in scripts
        for helper in HELPER_RE.findall(script.read_text(encoding="utf-8"))
    }
    assert helpers
    assert sorted(h for h in helpers if not index_entry(h)[0]) == []


@pytest.mark.parametrize("agent", sorted(INSTALL_TARGETS))
def test_gh_skill_install_puts_the_whole_skill_where_the_assistant_reads_it(
    tmp_path: Path, agent: str
) -> None:
    """Install from this checkout and compare every file with its source.

    :param tmp_path: The directory that stands for the person's project.
    :param agent: The assistant to install for, as ``gh skill install`` names it.
    """
    gh = shutil.which("gh")
    if gh is None or subprocess.run([gh, "skill", "--help"], check=False).returncode:
        pytest.skip("the GitHub CLI has no `gh skill` command on this machine")
    git = shutil.which("git")
    assert git is not None
    subprocess.run([git, "init", "--quiet", str(tmp_path)], check=True)
    subprocess.run(
        [
            gh,
            "skill",
            "install",
            str(REPO_ROOT),
            SKILL_NAME,
            "--from-local",
            "--allow-hidden-dirs",
            "--agent",
            agent,
            "--scope",
            "project",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    installed = tmp_path / INSTALL_TARGETS[agent] / SKILL_NAME
    missing = [
        str(path.relative_to(SKILL_DIR))
        for path in skill_files()
        if not (installed / path.relative_to(SKILL_DIR)).is_file()
    ]
    assert missing == []


def test_the_cases_run_against_a_plugin_built_from_the_one_copy_of_the_skill() -> None:
    """Build the plugin that the cases run against, and compare its skill with the tracked one."""
    bash = shutil.which("bash")
    assert bash is not None
    built = Path(
        subprocess.run(
            [bash, str(EVALS_DIR / "run-tier1.sh"), "--build-only"],
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    )
    copies = {
        str(path.relative_to(SKILL_DIR)): path.read_bytes() for path in skill_files()
    }
    copied = built / "skills" / SKILL_NAME
    assert copies
    assert {
        str(path.relative_to(copied)): path.read_bytes()
        for path in sorted(copied.rglob("*"))
        if path.is_file()
    } == copies
    assert (built / ".claude-plugin" / "plugin.json").is_file()
    assert (built / "evals" / "lib" / "place-repo.sh").is_file()
    assert sorted(path.name for path in (built / "evals").glob("*/prompt.md"))
    assert not (built / "evals" / "static").exists()
