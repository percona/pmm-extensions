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

"""Reference-integrity test for the code examples in the documentation.

The pages under ``docs/`` source their code examples from real files. Each
example carries an HTML-comment marker. ``<!-- src: <path> [:: <symbol>] -->``
names the file and the containing symbol, and ``<!-- constructed -->`` marks an
example that no file holds. Prose has no test to keep it honest, so this parser
enforces three contracts against the working tree, on every page that holds a
``python`` block:

* **Every example is cited.** Every fenced ``python`` block is immediately
  preceded (ignoring blank lines) by a ``src:`` or ``constructed`` marker.
* **Every citation resolves.** Each ``src:`` path exists, and each cited symbol
  is still defined in that file (via ``ast``) -- so a framework rename, move, or
  deletion fails CI instead of silently rotting the example.
* **Every cited example is still in its source.** The lines of a ``src:``
  example appear in the cited file, in order and next to each other, from the
  cited symbol onward. Whitespace at either end of a line is ignored, and a
  line that is only ``...`` stands for the lines the example skips. A name-only
  check let examples drift from their source while it kept passing.
"""

import ast
import pathlib
import re

import pytest

_REPO_ROOT = pathlib.Path(__file__).parents[2]
_DOCS_ROOT = _REPO_ROOT / "docs"
_PAGE_GLOBS = ("reference/*.md", "explanation/*.md", "tutorials/*.md", "how-to/*.md")
_ELISION = "..."

_SRC_MARKER_RE = re.compile(
    r"^<!--\s*src:\s*(?P<path>\S+?)"
    r"(?:\s*::\s*(?P<symbol>[A-Za-z_][A-Za-z0-9_]*))?\s*-->$"
)
_CONSTRUCTED_RE = re.compile(r"^<!--\s*constructed\s*-->$")
_SRC_PREFIX_RE = re.compile(r"^<!--\s*src:")
_FENCE_RE = re.compile(r"^```(?P<lang>[A-Za-z0-9_+-]*)(?:\s.*)?$")


class _SrcMarker:
    """Hold a well-formed ``src:`` citation, its page line and its example.

    :ivar page: The page's path relative to the repository root, or ``""`` for
        text parsed outside a page.
    :ivar line: The page line the marker sits on.
    :ivar path: The cited file, relative to the repository root.
    :ivar symbol: The cited symbol, or ``None`` when the marker names a file only.
    :ivar example: The lines of the fenced block the marker introduces.
    """

    def __init__(self, page: str, line: int, path: str, symbol: str | None) -> None:
        self.page = page
        self.line = line
        self.path = path
        self.symbol = symbol
        self.example: list[str] = []

    def __repr__(self) -> str:
        target = f"{self.path} :: {self.symbol}" if self.symbol else self.path
        return f"{target} ({self.page} line {self.line})"


class _ParsedGuide:
    """Hold the markers, constructed tags, malformed comments, and uncited blocks.

    :ivar src_markers: Every well-formed ``src:`` citation.
    :ivar malformed: ``(line, text)`` for each comment that opens ``<!-- src:``
        but does not match the marker grammar.
    :ivar uncited_python_blocks: Page lines of ``python`` fences with no
        immediately preceding ``src:``/``constructed`` marker.
    """

    def __init__(self) -> None:
        self.src_markers: list[_SrcMarker] = []
        self.malformed: list[tuple[int, str]] = []
        self.uncited_python_blocks: list[int] = []


def _parse_guide_text(text: str, page: str = "") -> _ParsedGuide:
    """Parse page Markdown into markers, code fences, and comments.

    Walks the text line by line, tracking whether the cursor is inside a fenced
    code block so that fence content is never mistaken for prose. For each
    opening ``python`` fence it inspects the nearest preceding non-blank line
    (blank lines ignored) and records the block as uncited unless that line is a
    ``src:`` or ``constructed`` marker. The lines of a fenced block that follows
    a ``src:`` marker, in any language, are kept on that marker.

    :param text: The page's Markdown source.
    :param page: The page's path relative to the repository root.
    :return: The parsed page.
    """
    result = _ParsedGuide()
    in_block = False
    last_kind = "other"
    cited: _SrcMarker | None = None
    for lineno, raw in enumerate(text.split("\n"), start=1):
        stripped = raw.strip()
        fence = _FENCE_RE.match(stripped)
        if fence:
            if not in_block:
                if fence.group("lang") == "python" and last_kind not in (
                    "src",
                    "constructed",
                ):
                    result.uncited_python_blocks.append(lineno)
                cited = result.src_markers[-1] if last_kind == "src" else None
                in_block = True
            else:
                in_block = False
                cited = None
            last_kind = "fence"
            continue
        if in_block and cited is not None:
            cited.example.append(raw)
        if in_block or not stripped:
            continue
        if _CONSTRUCTED_RE.match(stripped):
            last_kind = "constructed"
        elif marker := _SRC_MARKER_RE.match(stripped):
            result.src_markers.append(
                _SrcMarker(page, lineno, marker.group("path"), marker.group("symbol"))
            )
            last_kind = "src"
        elif _SRC_PREFIX_RE.match(stripped):
            result.malformed.append((lineno, stripped))
            last_kind = "malformed"
        else:
            last_kind = "other"
    return result


def _pages_with_python_examples() -> list[pathlib.Path]:
    """Return every documentation page that holds a ``python`` block.

    :return: The matching pages under ``docs/``, sorted.
    """
    return sorted(
        path
        for glob in _PAGE_GLOBS
        for path in _DOCS_ROOT.glob(glob)
        if "```python" in path.read_text(encoding="utf-8")
    )


def _parse_pages() -> dict[str, _ParsedGuide]:
    """Parse every page that holds a ``python`` block.

    :return: Each page's parse result, keyed by its repository-relative path.
    """
    pages: dict[str, _ParsedGuide] = {}
    for path in _pages_with_python_examples():
        relative = path.relative_to(_REPO_ROOT).as_posix()
        pages[relative] = _parse_guide_text(path.read_text(encoding="utf-8"), relative)
    return pages


def _target_names(target: ast.expr) -> list[str]:
    """Return the names an assignment target binds, flattening unpacking.

    Recurses into tuple/list unpacking and starred targets so ``a, b = ...``
    binds both ``a`` and ``b``, matching the assignment-target contract.

    :param target: An assignment-target node.
    :return: Every ``Name`` the target binds.
    """
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Starred):
        return _target_names(target.value)
    if isinstance(target, ast.Tuple | ast.List):
        return [name for element in target.elts for name in _target_names(element)]
    return []


def _module_level_names(tree: ast.Module) -> set[str]:
    """Collect names bound at module level, including methods one nesting level down.

    :param tree: The parsed module.
    :return: Every module-level class, function, and assignment-target name, plus
        the method names defined directly inside a module-level class.
    """
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(node.name)
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            targets = []
        names.update(name for t in targets for name in _target_names(t))
        if isinstance(node, ast.ClassDef):
            names.update(
                member.name
                for member in node.body
                if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef)
            )
    return names


def _module_defines(source: str, symbol: str) -> bool:
    """Return whether ``symbol`` is defined at module level in ``source``.

    A match is any module-level class, function, or assignment target named
    ``symbol``, or a method defined one nesting level below a module-level class
    (so a marker may cite ``Class`` or a bare module-level ``name`` alike).

    :param source: The Python source to parse.
    :param symbol: The name the citation claims is defined.
    :return: ``True`` when the name is bound at one of those scopes.
    """
    return symbol in _module_level_names(ast.parse(source))


def _symbol_start_line(source: str, symbol: str) -> int:
    """Return the line where ``symbol`` is defined in ``source``.

    A decorated definition starts at its first decorator, so an example may
    show the decorator. A name the module does not define yields line 1, which
    leaves the report to the citation check.

    :param source: The Python source to parse.
    :param symbol: The cited name: a module-level class, function or assignment
        target, or a method one nesting level below a module-level class.
    :return: The 1-based line of the definition, or ``1``.
    """
    for node in ast.parse(source).body:
        candidates = [node]
        if isinstance(node, ast.ClassDef):
            candidates.extend(node.body)
        for candidate in candidates:
            if isinstance(
                candidate, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
            ):
                if candidate.name == symbol:
                    decorators = [d.lineno for d in candidate.decorator_list]
                    return min([candidate.lineno, *decorators])
            elif candidate is node:
                targets: list[ast.expr] = []
                if isinstance(node, ast.Assign):
                    targets = node.targets
                elif isinstance(node, ast.AnnAssign):
                    targets = [node.target]
                if symbol in [name for t in targets for name in _target_names(t)]:
                    return node.lineno
    return 1


def _split_into_runs(example: list[str]) -> list[list[str]]:
    """Split an example into the runs of lines that ``...`` lines separate.

    :param example: The lines of the example block.
    :return: Each run's stripped, non-blank lines. Empty runs are dropped.
    """
    runs: list[list[str]] = [[]]
    for line in example:
        wanted = line.strip()
        if wanted == _ELISION:
            runs.append([])
        elif wanted:
            runs[-1].append(wanted)
    return [run for run in runs if run]


def _lines_missing_from_source(
    example: list[str], source_lines: list[str], start_line: int = 1
) -> list[str]:
    """Return the example lines that the source does not hold where expected.

    A line that is only ``...`` splits the example into runs. Each run must
    appear in the source as one block of neighbouring lines, after the block of
    the run before it, so an example cannot drop a line without marking the
    gap. Whitespace at either end of a line is ignored, and blank lines are
    skipped on both sides.

    :param example: The lines of the example block.
    :param source_lines: The lines of the cited file.
    :param start_line: The 1-based source line the search starts from.
    :return: For each run with no matching block, the lines after the longest
        prefix that some block does match, in example order.
    """
    source = [line.strip() for line in source_lines[start_line - 1 :]]
    source = [line for line in source if line]
    position = 0
    missing: list[str] = []
    for run in _split_into_runs(example):
        longest_prefix = 0
        for index in range(position, len(source)):
            matched = 0
            while (
                matched < len(run)
                and index + matched < len(source)
                and source[index + matched] == run[matched]
            ):
                matched += 1
            if matched == len(run):
                position = index + matched
                break
            longest_prefix = max(longest_prefix, matched)
        else:
            missing.extend(run[longest_prefix:])
    return missing


_PAGES = _parse_pages()
_SRC_MARKERS = [marker for page in _PAGES.values() for marker in page.src_markers]


def test_pages_with_examples_exist() -> None:
    """Assert the suite found at least one page to check.

    A wrong glob would otherwise leave every parametrized test below with
    nothing to run, and the suite would pass while checking nothing.
    """
    assert _PAGES, f"no page with a python block found under {_DOCS_ROOT}"


def test_at_least_one_src_marker() -> None:
    """Assert the pages still carry source markers.

    Pages that had silently lost every ``src:`` marker would pass the per-block
    and per-citation checks vacuously, so guard the whole safety net.
    """
    assert _SRC_MARKERS, (
        "no `<!-- src: ... -->` markers found in the documentation; the reference "
        "safety net has vanished"
    )


@pytest.mark.parametrize("page", sorted(_PAGES))
def test_no_malformed_src_markers(page: str) -> None:
    """Assert every ``src:``-prefixed comment matches the marker grammar.

    A near-miss marker (missing path, stray text, no closing ``-->``) must fail
    loudly rather than be silently treated as "not a citation".
    """
    malformed = _PAGES[page].malformed
    assert not malformed, f"malformed `src:` markers in {page}:\n" + "\n".join(
        f"  line {line}: {text}" for line, text in malformed
    )


@pytest.mark.parametrize("page", sorted(_PAGES))
def test_every_python_block_is_cited(page: str) -> None:
    """Assert every ``python`` code block carries a source or constructed marker."""
    uncited = _PAGES[page].uncited_python_blocks
    assert not uncited, (
        f"python code blocks in {page} with no preceding `<!-- src: ... -->` or "
        "`<!-- constructed -->` marker at line(s): "
        + ", ".join(str(line) for line in uncited)
    )


@pytest.mark.parametrize("marker", _SRC_MARKERS, ids=repr)
def test_citation_resolves(marker: _SrcMarker) -> None:
    """Assert a cited path exists and, if named, its symbol is still defined."""
    target = _REPO_ROOT / marker.path
    assert target.is_file(), f"cited path does not exist: {marker}"
    if marker.symbol is not None:
        assert _module_defines(target.read_text(), marker.symbol), (
            f"cited symbol not defined in file: {marker}"
        )


@pytest.mark.parametrize("marker", _SRC_MARKERS, ids=repr)
def test_example_text_matches_source(marker: _SrcMarker) -> None:
    """Assert each line of a cited example is in the cited file, in order."""
    target = _REPO_ROOT / marker.path
    if not target.is_file():
        pytest.skip("test_citation_resolves reports the missing file")
    source = target.read_text(encoding="utf-8")
    start_line = 1
    if marker.symbol is not None and target.suffix == ".py":
        start_line = _symbol_start_line(source, marker.symbol)

    missing = _lines_missing_from_source(
        marker.example, source.splitlines(), start_line
    )

    assert not missing, f"example lines not in the source of {marker}:\n" + "\n".join(
        f"  {line}" for line in missing
    )


def test_parse_text_flags_uncited_python_block() -> None:
    """Record a ``python`` block with no preceding marker as uncited."""
    parsed = _parse_guide_text("Some prose.\n```python\nx = 1\n```\n")
    assert parsed.uncited_python_blocks == [2]


def test_parse_text_accepts_marker_then_blank_then_fence() -> None:
    """Accept a citation separated from its fence by a blank line."""
    parsed = _parse_guide_text(
        "<!-- src: some/file.py :: Thing -->\n\n```python\nx = 1\n```\n"
    )
    assert parsed.uncited_python_blocks == []
    assert [marker.path for marker in parsed.src_markers] == ["some/file.py"]


def test_parse_text_flags_malformed_marker() -> None:
    """Record a ``src:``-prefixed comment that omits its path as malformed."""
    parsed = _parse_guide_text("<!-- src: -->\n")
    assert [text for _, text in parsed.malformed] == ["<!-- src: -->"]


def test_parse_text_ignores_non_python_fence() -> None:
    """Leave a non-``python`` fenced block off the uncited list."""
    parsed = _parse_guide_text("Some prose.\n```bash\necho hi\n```\n")
    assert parsed.uncited_python_blocks == []


def test_parse_text_recognizes_info_string_fence() -> None:
    """Treat a fence carrying an info string as a normal opening fence."""
    parsed = _parse_guide_text('Some prose.\n```python title="x"\nx = 1\n```\n')
    assert parsed.uncited_python_blocks == [2]


def test_module_defines_tuple_unpacked_name() -> None:
    """Resolve a name bound by module-level tuple unpacking."""
    assert _module_defines("a, b = object(), object()\n", "b")


def test_parse_text_keeps_the_example_of_a_cited_block() -> None:
    """Attach a fenced block's lines to the ``src:`` marker that introduces it."""
    parsed = _parse_guide_text(
        "<!-- src: some/file.py :: Thing -->\n```python\nx = 1\n    y = 2\n```\n"
    )
    assert parsed.src_markers[0].example == ["x = 1", "    y = 2"]


def test_lines_missing_from_source_accepts_an_elided_excerpt() -> None:
    """Match an excerpt whose ``...`` line skips source lines, whatever the indent."""
    source = ["class Thing:", "    a = 1", "    b = 2", "    c = 3"]
    assert _lines_missing_from_source(["a = 1", "...", "c = 3"], source) == []


def test_lines_missing_from_source_reports_a_changed_line() -> None:
    """Report an example line the source no longer holds."""
    source = ["columns=default_columns(),"]
    assert _lines_missing_from_source(
        ["columns=default_columns(SERVICE_TYPE_COLUMN),"], source
    ) == ["columns=default_columns(SERVICE_TYPE_COLUMN),"]


def test_lines_missing_from_source_requires_source_order() -> None:
    """Report a line that the source holds only before the previous match."""
    assert _lines_missing_from_source(["b = 2", "a = 1"], ["a = 1", "b = 2"]) == [
        "a = 1"
    ]


def test_lines_missing_from_source_starts_at_the_cited_symbol() -> None:
    """Ignore a matching line that sits above the cited symbol."""
    source = ["x = 1", "def later():", "    return 2"]
    assert _lines_missing_from_source(["x = 1"], source, start_line=2) == ["x = 1"]


def test_symbol_start_line_includes_the_decorator() -> None:
    """Start a decorated definition at its first decorator."""
    lines = [
        "import functools",
        "",
        "",
        "@functools.cache",
        "def thing():",
        "    return 1",
    ]
    decorator_line = lines.index("@functools.cache") + 1

    assert _symbol_start_line("\n".join(lines), "thing") == decorator_line


def test_lines_missing_from_source_reports_an_unmarked_gap() -> None:
    """Report a line that follows a source line the example left out."""
    source = ["key='Mydumper',", "collapsible=True,", "collapsed_by_default=True,", ")"]
    assert _lines_missing_from_source(
        ["key='Mydumper',", "collapsible=True,", ")"], source
    ) == [")"]
