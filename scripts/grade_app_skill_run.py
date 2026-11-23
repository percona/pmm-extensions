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

"""Grade one outcome run of the app skill, and write ``grade.json``.

The grader runs every check itself, on what the session of
``assistant/evals/outcome/run.sh`` left behind: the directory, a local PMM that
the grader starts, and the stream. It never reads a statement by the assistant
that a check passed. Each entry of ``grade.json`` holds ``passed``, the
command, its exit status and the last ten lines of its output.

Usage: ``grade_app_skill_run.py <scenario> <record directory>``, with the
project's virtualenv active. ``GRADE_PORT`` sets the port of the grader's
local PMM, and ``SCENARIOS_DIR`` names a directory of scenarios other than
the repository's. The grader stops the run's local PMM and its own, and removes the
image that it built. It leaves the run's directory in place.
"""

import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = Path(
    os.environ.get("SCENARIOS_DIR", REPO_ROOT / "assistant/evals/outcome/scenarios")
)
DEV = REPO_ROOT / "sidecar/dev"
TRUST = "Nobody at Percona has reviewed it"
NOT_KNOWN = "I don't know what that means."
SECTIONS = (
    "What a database server must have",
    "What the tool touches",
    "What was tried, and what was not",
)
BUILD_RE = re.compile(r"\b(docker|podman|buildah)\b[^|;&]*\b(build|bud)\b|--build\b")
PUBLISH_RE = re.compile(
    r"\bgit\s+push\b|\b(docker|podman)\s+(image\s+)?push\b|\bgh\s+repo\s+(create|fork)\b"
    r"|\bgh\s+(pr|issue|release|gist)\s+(create|comment|review|merge|edit)\b"
    r"|\bgh\s+api\b.*(-X|--method)[ =]*(POST|PUT|PATCH|DELETE)\b"
    r"|\bgh\s+api\b.*\s(-f|-F|--field|--raw-field|--input)\b"
)
FRAMEWORK = (
    "app/extensions/apps/framework/",
    "tests/app/extensions/apps/framework/",
    "Makefile",
    "scripts/",
)
PORT_PATHS = (
    "app/extensions/apps/{app}/ tests/app/extensions/apps/{app}/ tests/app/extensions/snapshots/"
    " tests/app/extensions/conftest.py frontend/packages/api/ settings.yaml sidecar/settings.yaml"
    " changelog.d/ docs/ README.md .claude/ .app-work/"
)
MAX_QUESTIONS = 5
MAKE = ("make", "app-check", f"VENV_BIN={Path(sys.executable).parent}")
Done = tuple[int | None, str]
Result = tuple[bool, str, int | None, str]


def sh(
    *command: str,
    cwd: Path = REPO_ROOT,
    stdin: str | None = None,
    timeout: float = 3600,
    env: dict[str, str] | None = None,
) -> Done:
    """Run a command and return its exit status and its output.

    :param command: The program and its arguments.
    :param cwd: The directory to run it in.
    :param stdin: Text for the command's standard input.
    :param timeout: The seconds after which the command is stopped.
    :param env: Variables to add to the environment.
    :return: The exit status, and standard output with standard error. The
        status is ``None`` when the command could not start or did not end.
    """
    try:
        done = subprocess.run(
            command,
            cwd=cwd,
            env={**os.environ, **(env or {})},
            input=stdin,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except OSError as error:
        return None, str(error)
    except subprocess.TimeoutExpired:
        return None, f"timed out after {timeout} seconds: {' '.join(command)}"
    return done.returncode, done.stdout + done.stderr


def plain(text: str) -> str:
    """Return a text in lower case with typewriter apostrophes, for a comparison.

    :param text: The text.
    :return: The text to compare.
    """
    return text.replace(chr(8217), "'").lower()


def publishes(command: str) -> bool:
    """Say whether one part of a shell command sends something off the machine.

    :param command: The whole command line, as the assistant ran it.
    :return: ``True`` when a part publishes and is no dry run.
    """
    parts = re.split(r"&&|\|\||[;|\n]", command)
    return any(PUBLISH_RE.search(part) and "--dry-run" not in part for part in parts)


def owner_of(entry: str) -> str:
    """Return the owner that a workspace's ``app.yaml`` names.

    :param entry: The text of ``app.yaml``.
    :return: The value of ``maintainer`` without quotes and without a comment,
        or an empty string when the key is absent or has no value.
    """
    found = re.search(r"^maintainer:[ \t]*(.*)$", entry, re.MULTILINE)
    value = re.sub(r"(^|\s)#.*$", "", found[1]).strip() if found else ""
    quoted = len(value) > 1 and value[0] == value[-1] and value[0] in "\"'"
    return value[1:-1] if quoted else value


class Run:
    """Hold one run: its scenario, its record, its directory and its stream.

    :param scenario: The name of a directory under ``scenarios/``.
    :param record: The record directory that ``run.sh`` wrote.
    """

    def __init__(self, scenario: str, record: Path) -> None:
        facts = SCENARIOS / scenario / "scenario.json"
        self.facts = json.loads(facts.read_text(encoding="utf-8"))
        self.meta = json.loads((record / "meta.json").read_text(encoding="utf-8"))
        self.record = record
        self.dir = Path(self.meta["dir"])
        self.workspace = self.facts["kind"] == "workspace"
        apps = [path.parent.name for path in self.dir.glob("[!.]*/app.py")]
        self.app = self.facts.get("app") or (apps[0] if apps else "no-app")
        self.package = (
            self.dir if self.workspace else self.dir / "app/extensions/apps"
        ) / self.app
        self.project = f"{self.meta['project']}-grade"
        self.env = {
            "PMM_PORT": os.environ.get("GRADE_PORT", str(8550 + self.meta["n"]))
        }
        lines = (record / "stream.jsonl").read_text(encoding="utf-8").splitlines()
        pairs = [line.split("\t", 1) for line in lines if "\t{" in line]
        self.events = [(int(stamp), json.loads(text)) for stamp, text in pairs]
        self.host: dict[str, Done] = {}

    def texts(self, kind: str) -> list[tuple[int, str]]:
        """Return the position and the text of each event of one kind.

        :param kind: ``said`` for what the assistant wrote, ``command`` for a
            shell command it ran, ``wrote`` for the path of a file it wrote,
            ``person`` for an answer of the scripted person, ``last`` for the
            assistant's last message of a turn.
        :return: Pairs of the event's position in the stream and the text.
        """
        found = []
        for position, (_, event) in enumerate(self.events):
            if event.get("type") == "person" and kind == "person":
                found.append((position, event["text"]))
            if event.get("type") == "result" and kind == "last":
                found.append((position, str(event.get("result", ""))))
            if event.get("type") != "assistant":
                continue
            for block in event["message"].get("content") or []:
                call = block.get("input") or {}
                if block.get("type") == "text" and kind == "said":
                    found.append((position, block["text"]))
                elif block.get("name") == "Bash" and kind == "command":
                    found.append((position, str(call.get("command", ""))))
                elif block.get("name") in ("Write", "Edit") and kind == "wrote":
                    found.append((position, str(call.get("file_path", ""))))
        return found

    def changed(self) -> list[str] | None:
        """Return the paths of a clone that differ from the run's first commit.

        :return: The paths, from the root of the clone, or ``None`` when Git
            could not compare the clone with that commit.
        """
        added = sh("git", "add", "--all", cwd=self.dir)
        names = sh(
            "git",
            "diff",
            "--cached",
            "--name-only",
            self.meta["start_commit"],
            cwd=self.dir,
        )
        return names[1].splitlines() if added[0] == 0 and names[0] == 0 else None

    def tree(self) -> Path:
        """Return the clone of the framework that holds the app in its tree.

        :return: The clone's root. For a workspace, the first clone below the
            run's directory whose tree holds the app, or a path that is absent.
        """
        if not self.workspace:
            return self.dir
        found = self.dir.parent.glob(f"**/app/extensions/apps/{self.app}/app.py")
        return next(
            (path.parents[4] for path in found), self.dir / "no-clone-holds-the-app"
        )

    def text(self, path: Path) -> str:
        """Return the text of a file, or an empty string when it is absent.

        :param path: The file.
        :return: The file's text.
        """
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def on_host(self) -> dict[str, Done]:
        """Start a local PMM with the run's app, run one task, and keep the results.

        A workspace's app runs from the image that the workspace's build file
        gives. A clone's app runs from the clone's code.

        :return: The exit status and the output of the image build, the task and
            the tool's help text, by name.
        """
        if self.host:
            return self.host
        dev = DEV if self.workspace else self.dir / "sidecar/dev"
        files = ["-f", f"{dev}/compose.yaml"]
        if self.workspace:
            owner = owner_of(self.text(self.dir / "app.yaml"))
            arguments = [f"APP={self.app}", f"OWNER={owner or 'not named'}"]
            flags = [
                flag for argument in arguments for flag in ("--build-arg", argument)
            ]
            tag = f"{self.project}:1"
            build = ("docker", "build", "--no-cache", *flags, "-f", "Containerfile")
            self.host["image"] = sh(*build, "-t", tag, ".", cwd=self.dir)
            self.env["PMM_EXTENSIONS_IMAGE"] = tag
        else:
            files += ["-f", f"{dev}/compose.checkout.yaml"]
        compose = ["docker", "compose", "-p", self.project, *files]
        up = [*compose, "--profile", self.facts["profile"], "up", "-d", "--wait"]
        if sh(*up, env=self.env)[0] != 0:
            sh(*up, env=self.env)
        body = self.record / "graded-run.json"
        body.write_text(json.dumps(self.facts["body"]), encoding="utf-8")
        run_task = [
            "python3",
            f"{DEV}/run-task.py",
            "--app",
            self.app,
            "--body",
            str(body),
        ]
        self.host["task"] = sh(*run_task, env=self.env)
        if "help" in self.facts:
            tool = self.facts["help"].split()
            self.host["help"] = sh(
                *compose, "exec", "-T", self.facts["host"], *tool, env=self.env
            )
        return self.host


def app_check(run: Run) -> Result:
    """Check 1: pass every suite of ``make app-check`` for the app.

    An app from outside the tree is copied into this checkout for the check,
    so two graders take turns.

    :param run: The run to grade.
    :return: Whether the check passed, the command or the thing looked at, an
        exit status if there is one, and what was found.
    """
    in_tree = run.facts.get("in_tree") or not run.workspace
    command = (
        *MAKE,
        f"NAME={run.app}",
        *(() if in_tree else (f"APP_DIR={run.package}",)),
    )
    with (Path(tempfile.gettempdir()) / "app-check-outside.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        status, output = sh(*command, cwd=run.tree() if in_tree else REPO_ROOT)
    return status == 0, " ".join(command), status, output


def image(run: Run) -> Result:
    """Check 2: build the workspace's image, whose load step names the app.

    :param run: The run to grade.
    :return: Whether the check passed, the command or the thing looked at, an
        exit status if there is one, and what was found.
    """
    status, output = run.on_host()["image"]
    loaded = re.search(rf"apps in this image:.*\b{run.app}\b", output) is not None
    return status == 0 and loaded, "docker build -f Containerfile .", status, output


def task(run: Run) -> Result:
    """Check 3: end one task of the app with success on a database host.

    :param run: The run to grade.
    :return: Whether the check passed, the command or the thing looked at, an
        exit status if there is one, and what was found.
    """
    status, output = run.on_host()["task"]
    on_host = f'"target": "{run.facts["host"]}"' in output
    return status == 0 and on_host, "sidecar/dev/run-task.py", status, output


def log(run: Run) -> Result:
    """Check 4: find a line in the task's log that only the real tool prints.

    :param run: The run to grade.
    :return: Whether the check passed, the command or the thing looked at, an
        exit status if there is one, and what was found.
    """
    output = run.on_host()["task"][1]
    return (
        re.search(run.facts["log_pattern"], output) is not None,
        "the task's log",
        None,
        output,
    )


def options(run: Run) -> Result:
    """Check 5: find every option of the app's code in the tool's help text on the host.

    :param run: The run to grade.
    :return: Whether the check passed, the command or the thing looked at, an
        exit status if there is one, and what was found.
    """
    status, text = run.on_host()["help"]
    files = [path for path in run.package.rglob("*.py") if "payload" not in path.parts]
    code = "".join(run.text(path) for path in files)
    known = set(re.findall(r"--[a-z0-9-]+", text))
    for name in re.findall(r"--\[no\]([a-z0-9-]+)", text):
        known |= {f"--{name}", f"--no-{name}"}
    sent = set(re.findall(r"--[a-z][a-z0-9-]+", code))
    unknown = sorted(sent - known)
    return (
        status == 0 and bool(sent) and not unknown,
        run.facts["help"],
        status,
        f"options in the app's code: {len(sent)}; not in the help text: {unknown}",
    )


def brief(run: Run) -> Result:
    """Check 6: find the brief's three sections, and the owner of a workspace's app.

    :param run: The run to grade.
    :return: Whether the check passed, the command or the thing looked at, an
        exit status if there is one, and what was found.
    """
    page = run.text(run.package / "README.md").lower()
    missing = [section for section in SECTIONS if section.lower() not in page]
    entry = run.text(run.dir / "app.yaml")
    if run.workspace and not owner_of(entry):
        missing.append("app.yaml: maintainer")
    if run.workspace and not re.search(
        r"^reviewed-by-percona:\s*false\s*$", entry, re.MULTILINE
    ):
        missing.append("app.yaml: reviewed-by-percona: false")
    return not missing, "README.md and app.yaml", None, f"missing: {missing}"


def saved(run: Run) -> Result:
    """Check 7: find the framework checkout untouched and the workspace's work saved.

    :param run: The run to grade.
    :return: Whether the check passed, the command or the thing looked at, an
        exit status if there is one, and what was found.
    """
    checkout = run.dir / ".framework"
    framework = sh("git", "status", "--porcelain", cwd=checkout)
    cloned = run.meta.get("framework_commit")
    as_cloned = (
        sh("git", "diff", "--quiet", cloned, cwd=checkout) if cloned else (0, "")
    )
    head = sh("git", "rev-parse", "HEAD", cwd=run.dir)
    workspace = sh("git", "status", "--porcelain", cwd=run.dir)
    passed = (
        framework == (0, "")
        and as_cloned[0] == 0
        and head[0] == 0
        and workspace == (0, "")
    )
    output = (
        f"framework checkout: {framework}, against the commit that was cloned: {as_cloned}\n"
        f"workspace HEAD: {head}\nworkspace: {workspace}"
    )
    return passed, "git status --porcelain", None, output


def paths(run: Run) -> Result:
    """Find a port's changes in the app, its tests and the lists that name it only.

    :param run: The run to grade.
    :return: Whether the check passed, the command or the thing looked at, an
        exit status if there is one, and what was found.
    """
    allowed = tuple(PORT_PATHS.format(app=run.app).split())
    changed = run.changed()
    other = [path for path in changed or [] if not path.startswith(allowed)]
    profile = run.text(run.dir / "sidecar/settings.yaml")
    entry = rf"MODULE_NAME:[ \t]*{re.escape(run.app)}[ \t]*$"
    listed = re.search(entry, profile, re.MULTILINE) is not None
    output = f"outside the app: {other}; sidecar/settings.yaml lists the app: {listed}"
    passed = changed is not None and not other and listed
    return passed, "git diff --name-only", None, output


def new_test(run: Run) -> Result:
    """Find a changed test that fails without the fix and passes with it.

    :param run: The run to grade.
    :return: Whether the check passed, the command or the thing looked at, an
        exit status if there is one, and what was found.
    """
    changed = run.changed() or []
    tests = [
        path for path in changed if path.startswith("tests/") and path.endswith(".py")
    ]
    code = [path for path in changed if path.startswith("app/")]
    pytest = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *tests]
    if not tests or not code:
        return False, "pytest", None, f"changed tests: {tests}; changed code: {code}"
    fixed = sh(*pytest, cwd=run.dir)
    fix = sh(
        "git", "diff", "--cached", run.meta["start_commit"], "--", *code, cwd=run.dir
    )[1]
    if sh("git", "apply", "-R", cwd=run.dir, stdin=fix)[0] != 0:
        return False, "git apply -R", None, "the fix could not be taken out again"
    try:
        broken = sh(*pytest, cwd=run.dir)
    finally:
        back = sh("git", "apply", cwd=run.dir, stdin=fix)
    output = (
        f"{fixed[1]}\nwithout the fix: exit {broken[0]}; fix put back: exit {back[0]}"
    )
    passed = fixed[0] == 0 and broken[0] not in (0, None) and back[0] == 0
    return passed, " ".join(pytest), fixed[0], output


def framework(run: Run) -> Result:
    """Find no change to the framework, to its tests, or to the checks themselves.

    :param run: The run to grade.
    :return: Whether the check passed, the command or the thing looked at, an
        exit status if there is one, and what was found.
    """
    changed = run.changed()
    touched = [path for path in changed or [] if path.startswith(FRAMEWORK)]
    output = f"compared: {changed is not None}; framework files and checks changed: {touched}"
    return changed is not None and not touched, "git diff --name-only", None, output


def person(run: Run) -> Result:
    """Check 8: count the questions the person could not answer, and those before the brief.

    :param run: The run to grade.
    :return: Whether the check passed, the command or the thing looked at, an
        exit status if there is one, and what was found.
    """
    not_known = plain(NOT_KNOWN).rstrip(".")
    lost = sum(1 for _, text in run.texts("person") if not_known in plain(text))
    shown = [
        position for position, text in run.texts("last") if SECTIONS[0] in text
    ] or [
        position for position, path in run.texts("wrote") if path.endswith("README.md")
    ]
    before = [
        text for position, text in run.texts("last") if not shown or position < shown[0]
    ]
    asked = (
        sum(text.count("?") for text in before) if "brief" in run.facts["checks"] else 0
    )
    output = f'"{NOT_KNOWN}": {lost}; questions before the brief: {asked}'
    return lost == 0 and asked <= MAX_QUESTIONS, "the stream", None, output


def trust(run: Run) -> Result:
    """Check 9: find the trust statement, then the person's answer, then the first image build.

    :param run: The run to grade.
    :return: Whether the check passed, the command or the thing looked at, an
        exit status if there is one, and what was found.
    """
    stated = [position for position, text in run.texts("said") if TRUST in text]
    builds = [
        position for position, text in run.texts("command") if BUILD_RE.search(text)
    ]
    answers = [
        position
        for position, _ in run.texts("person")
        if stated and position > stated[0]
    ]
    in_order = bool(stated) and (
        not builds or (bool(answers) and answers[0] < builds[0])
    )
    quiet, _, _, published = nothing_published(run)
    output = f"stated at {stated[:1]}, first build at {builds[:1]}; {published}"
    return in_order and quiet, "the stream", None, output


def nothing_published(run: Run) -> Result:
    """Find no command in the stream that sends something off the machine.

    :param run: The run to grade.
    :return: Whether the check passed, the command or the thing looked at, an
        exit status if there is one, and what was found.
    """
    found = [text[:120] for _, text in run.texts("command") if publishes(text)]
    return not found, "the stream", None, f"publishing commands: {found}"


def same_bytes(run: Run) -> Result:
    """Compare the app's directory in the branch with the workspace's, byte for byte.

    :param run: The run to grade.
    :return: Whether the check passed, the command or the thing looked at, an
        exit status if there is one, and what was found.
    """
    copy = run.tree() / "app/extensions/apps" / run.app
    command = ("diff", "-r", "-x", "__pycache__", str(run.package), str(copy))
    status, output = sh(*command)
    return status == 0, " ".join(command), status, output


def successes(run: Run) -> list[int]:
    """Return the times at which a task that the session ran itself ended with success.

    :param run: The run.
    :return: The stamp of each output of ``run-task.py`` that reports the status ``success``.
    """
    shell = {
        block.get("id")
        for _, event in run.events
        if event.get("type") == "assistant"
        for block in event["message"].get("content") or []
        if block.get("name") == "Bash"
        and "run-task.py" in str((block.get("input") or {}).get("command"))
    }
    return [
        stamp
        for stamp, event in run.events
        if event.get("type") == "user"
        for block in event["message"].get("content") or []
        if isinstance(block, dict)
        and block.get("tool_use_id") in shell
        and '\\"status\\": \\"success\\"' in json.dumps(block)
    ]


def ran(run: Run) -> Result:
    """Find one task that the session ran itself on a database host, with success.

    :param run: The run to grade.
    :return: Whether the check passed, the command or the thing looked at, an
        exit status if there is one, and what was found.
    """
    found = successes(run)
    output = f"tasks that the session ran to success: {len(found)}"
    return bool(found), "the stream", None, output


def metrics(run: Run) -> dict[str, float]:
    """Return what the run cost.

    :param run: The run.
    :return: Minutes, minutes to the first task that ended with success,
        tokens, US dollars, tool calls and turns.
    """
    results = [event for _, event in run.events if event.get("type") == "result"]
    usage = [event.get("usage") or {} for event in results]
    first = successes(run)
    tokens = sum(
        value for entry in usage for value in entry.values() if isinstance(value, int)
    )
    return {
        "minutes": round((run.meta["ended"] - run.meta["started"]) / 60, 1),
        "minutes_to_first_success": round((first[0] - run.meta["started"]) / 60, 1)
        if first
        else -1,
        "tokens": tokens,
        "cost_usd": round(
            max((event.get("total_cost_usd") or 0 for event in results), default=0), 2
        ),
        "tool_calls": len(run.texts("command")) + len(run.texts("wrote")),
        "turns": run.meta["turns"],
    }


def main(argv: list[str]) -> int:
    """Grade one run, stop what the grading started, and write ``grade.json``.

    The run's directory stays. Whoever started the run removes it.

    :param argv: The scenario and the record directory.
    :return: ``0`` when every check passed, ``1`` otherwise.
    """
    record = Path(argv[1]).resolve()
    run = Run(argv[0], record)
    checks: dict[str, dict[str, object]] = {}
    try:
        for name in run.facts["checks"]:
            passed, command, status, output = globals()[name](run)
            tail = output.strip().splitlines()[-10:]
            checks[name] = {
                "passed": passed,
                "command": command,
                "exit": status,
                "tail": tail,
            }
            print(f"{name}: {'passed' if passed else 'FAILED'}", flush=True)
    finally:
        for project in (run.project, run.meta["project"]):
            sh("docker", "compose", "-p", project, "down", "-v", "--remove-orphans")
        sh("docker", "rmi", f"{run.project}:1")
        grade = {
            "run": run.meta,
            "app": run.app,
            "metrics": metrics(run),
            "checks": checks,
        }
        (record / "grade.json").write_text(
            json.dumps(grade, indent=2), encoding="utf-8"
        )
    return 0 if all(check["passed"] for check in checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
