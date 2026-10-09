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

"""Tests for snippet utility functions."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from app.extensions.snippets.utils import (
    generate_unique_identifiers,
    guess_mime_type,
    mime_type_to_highlighter_language,
)

EXPECTED_TWO_CHAR_ID_LENGTH = 2


class TestGuessMimeType:
    """Test the guess_mime_type function."""

    def test_known_extension_without_magic(self):
        """Verify correct MIME type for known file extension using mimetypes."""
        result = guess_mime_type(Path("script.sh"))
        assert result in ("text/x-sh", "application/x-sh")

    def test_python_extension_without_magic(self):
        """Verify correct MIME type for .py extension using mimetypes."""
        result = guess_mime_type(Path("script.py"))
        assert result == "text/x-python"

    def test_unknown_extension_returns_text_plain(self):
        """Verify unknown extension returns text/plain as default."""
        result = guess_mime_type(Path("file.xyznonexistent"))
        assert result == "text/plain"

    def test_with_magic_enabled(self, tmp_path):
        """Verify MIME type detection using python-magic when enabled."""
        import sys  # noqa: PLC0415

        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")

        mock_magic = MagicMock()
        mock_magic.from_file.return_value = "text/plain"
        sys.modules["magic"] = mock_magic

        try:
            with patch(
                "app.extensions.snippets.utils.snippets_settings"
            ) as mock_settings:
                mock_settings.USE_MAGIC = True
                guess_mime_type.__wrapped__(test_file)
                mock_magic.from_file.assert_called_once_with(test_file, mime=True)
        finally:
            del sys.modules["magic"]

    def test_magic_returns_none_falls_back_to_text_plain(self, tmp_path):
        """Verify fallback to text/plain when magic returns None."""
        import sys  # noqa: PLC0415

        test_file = tmp_path / "test.dat"
        test_file.write_bytes(b"\x00\x00")

        mock_magic = MagicMock()
        mock_magic.from_file.return_value = None
        sys.modules["magic"] = mock_magic

        try:
            with patch(
                "app.extensions.snippets.utils.snippets_settings"
            ) as mock_settings:
                mock_settings.USE_MAGIC = True
                result = guess_mime_type.__wrapped__(test_file)
                assert result == "text/plain"
        finally:
            del sys.modules["magic"]


class TestGenerateUniqueIdentifiers:
    """Test the generate_unique_identifiers generator."""

    def test_first_52_are_single_letters(self):
        """Verify first 52 identifiers are single letters a-z, A-Z."""
        gen = generate_unique_identifiers()
        first_52 = [next(gen) for _ in range(52)]
        assert first_52[:26] == list("abcdefghijklmnopqrstuvwxyz")
        assert first_52[26:] == list("ABCDEFGHIJKLMNOPQRSTUVWXYZ")

    def test_identifiers_are_valid_python(self):
        """Verify generated identifiers are valid Python identifiers."""
        gen = generate_unique_identifiers()
        for _ in range(200):
            identifier = next(gen)
            assert identifier.isidentifier(), (
                f"{identifier!r} is not a valid identifier"
            )

    def test_identifiers_after_single_chars(self):
        """Verify identifiers after single chars have correct format."""
        gen = generate_unique_identifiers()
        for _ in range(52):
            next(gen)
        next_id = next(gen)
        assert len(next_id) == EXPECTED_TWO_CHAR_ID_LENGTH
        assert next_id[0].isalpha()
        assert next_id[-1].isdigit()

    def test_no_duplicates_in_first_500(self):
        """Verify no duplicate identifiers in first 500 generated."""
        gen = generate_unique_identifiers()
        identifiers = [next(gen) for _ in range(500)]
        assert len(identifiers) == len(set(identifiers))


class TestMimeTypeToHighlighterLanguage:
    """Test the mime_type_to_highlighter_language mapping."""

    def test_shellscript_variants_map_to_bash(self):
        """Each known shellscript MIME variant maps to bash."""
        for mime_type in (
            "text/x-shellscript",
            "application/x-sh",
            "application/x-shellscript",
        ):
            assert mime_type_to_highlighter_language(mime_type) == "bash"

    def test_python_variants_map_to_python(self):
        """Each known Python MIME variant maps to python."""
        for mime_type in ("text/x-python", "application/x-python-code"):
            assert mime_type_to_highlighter_language(mime_type) == "python"

    def test_perl_variants_map_to_perl(self):
        """Each known Perl MIME variant maps to perl."""
        for mime_type in ("text/x-perl", "application/x-perl"):
            assert mime_type_to_highlighter_language(mime_type) == "perl"

    def test_ruby_variants_map_to_ruby(self):
        """Each known Ruby MIME variant maps to ruby."""
        for mime_type in ("text/x-ruby", "application/x-ruby"):
            assert mime_type_to_highlighter_language(mime_type) == "ruby"

    def test_php_variants_map_to_php(self):
        """Each known PHP MIME variant maps to php."""
        for mime_type in ("text/x-php", "application/x-php"):
            assert mime_type_to_highlighter_language(mime_type) == "php"

    def test_unknown_mime_type_falls_back_to_plaintext(self):
        """Unknown MIME types fall back to the plaintext language."""
        assert mime_type_to_highlighter_language("text/plain") == "plaintext"
        assert mime_type_to_highlighter_language("application/octet-stream") == (
            "plaintext"
        )
        assert mime_type_to_highlighter_language("") == "plaintext"
