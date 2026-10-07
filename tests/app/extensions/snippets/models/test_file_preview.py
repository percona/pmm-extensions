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

"""Tests for FilePreview frontmatter parsing."""

import pytest

from app.extensions.snippets.models.snippet import FilePreview

PREVIEW_MAX_CHARS = 10000
PREVIEW_MAX_LINES = 500
TRUNCATION_MAX_LINES = 5
TRUNCATION_MAX_CHARS = 50


class TestFilePreviewFromPath:
    """Test FilePreview._from_path frontmatter splitting."""

    @pytest.mark.asyncio
    async def test_no_frontmatter(self, tmp_path):
        """Return empty preamble and frontmatter when file has no frontmatter."""
        f = tmp_path / "plain.sh"
        f.write_text("echo hello\necho world\n")
        result = await FilePreview._from_path(
            f, max_chars=PREVIEW_MAX_CHARS, max_lines=PREVIEW_MAX_LINES
        )
        assert result.preamble == ""
        assert result.frontmatter == ""
        assert result.content == "echo hello\necho world\n"
        assert result.is_truncated is False

    @pytest.mark.asyncio
    async def test_frontmatter_only(self, tmp_path):
        """Parse frontmatter delimiters and content lines."""
        f = tmp_path / "fm.sh"
        f.write_text("# ---\n# key: val\n# ---\ncode here\n")
        result = await FilePreview._from_path(
            f, max_chars=PREVIEW_MAX_CHARS, max_lines=PREVIEW_MAX_LINES
        )
        assert result.preamble == ""
        assert result.frontmatter == "# ---\n# key: val\n# ---\n"
        assert result.content == "code here\n"
        assert result.is_truncated is False

    @pytest.mark.asyncio
    async def test_shebang_plus_frontmatter(self, tmp_path):
        """Collect shebang as preamble before frontmatter."""
        f = tmp_path / "shebang.sh"
        f.write_text("#!/bin/bash\n# ---\n# k: v\n# ---\ncode\n")
        result = await FilePreview._from_path(
            f, max_chars=PREVIEW_MAX_CHARS, max_lines=PREVIEW_MAX_LINES
        )
        assert result.preamble == "#!/bin/bash\n"
        assert result.frontmatter == "# ---\n# k: v\n# ---\n"
        assert result.content == "code\n"

    @pytest.mark.asyncio
    async def test_truncation_applies_to_code_body_only(self, tmp_path):
        """Apply max_lines only to the code body, preserving preamble and frontmatter."""
        lines = ["# ---\n", "# title: test\n", "# ---\n"]
        code_lines = [f"line {i}\n" for i in range(20)]
        f = tmp_path / "trunc.sh"
        f.write_text("".join(lines + code_lines))
        result = await FilePreview._from_path(
            f, max_chars=PREVIEW_MAX_CHARS, max_lines=TRUNCATION_MAX_LINES
        )
        assert result.frontmatter == "# ---\n# title: test\n# ---\n"
        assert result.is_truncated is True
        assert result.content.count("\n") == TRUNCATION_MAX_LINES

    @pytest.mark.asyncio
    async def test_frontmatter_at_eof(self, tmp_path):
        """Handle frontmatter with no code body after it."""
        f = tmp_path / "eof.sh"
        f.write_text("# ---\n# k: v\n# ---\n")
        result = await FilePreview._from_path(
            f, max_chars=PREVIEW_MAX_CHARS, max_lines=PREVIEW_MAX_LINES
        )
        assert result.frontmatter == "# ---\n# k: v\n# ---\n"
        assert result.content == ""
        assert result.is_truncated is False

    @pytest.mark.asyncio
    async def test_unclosed_frontmatter(self, tmp_path):
        """Include all lines in frontmatter when closing delimiter is missing."""
        f = tmp_path / "unclosed.sh"
        f.write_text("# ---\n# key: val\ncode here\n")
        result = await FilePreview._from_path(
            f, max_chars=PREVIEW_MAX_CHARS, max_lines=PREVIEW_MAX_LINES
        )
        assert result.frontmatter == "# ---\n# key: val\ncode here\n"
        assert result.content == ""
        assert result.is_truncated is False

    @pytest.mark.asyncio
    async def test_non_comment_before_delimiter_stops_search(self, tmp_path):
        """Treat everything as code when a non-comment line appears before # ---."""
        f = tmp_path / "nocomment.sh"
        f.write_text("echo hi\n# ---\n# k: v\n# ---\n")
        result = await FilePreview._from_path(
            f, max_chars=PREVIEW_MAX_CHARS, max_lines=PREVIEW_MAX_LINES
        )
        assert result.preamble == ""
        assert result.frontmatter == ""
        assert "echo hi" in result.content

    @pytest.mark.asyncio
    async def test_max_chars_truncation_on_code_body(self, tmp_path):
        """Apply max_chars only to the code body."""
        f = tmp_path / "chars.sh"
        f.write_text("# ---\n# k: v\n# ---\n" + "x" * 100 + "\n")
        result = await FilePreview._from_path(
            f, max_chars=TRUNCATION_MAX_CHARS, max_lines=PREVIEW_MAX_LINES
        )
        assert result.frontmatter == "# ---\n# k: v\n# ---\n"
        assert result.is_truncated is True
        assert len(result.content) == TRUNCATION_MAX_CHARS


class TestFilePreviewFullContent:
    """Test FilePreview.full_content."""

    def test_full_content_combines_all_segments(self):
        """Return concatenation of preamble, frontmatter, and content."""
        fp = FilePreview(
            preamble="#!/bin/bash\n",
            frontmatter="# ---\n# k: v\n# ---\n",
            content="echo hello\n",
            is_truncated=False,
        )
        assert fp.full_content == "#!/bin/bash\n# ---\n# k: v\n# ---\necho hello\n"
