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

"""Define utilities shared across the task executors."""

from gzip import GzipFile
from io import BytesIO

from python_minifier import minify


def minify_file_content(content: str, file_ext: str = "") -> str:
    """Minify Python file content.

    Minify the given Python code string if the file extension is ".py" or not
    specified. For other file types, return the content unchanged.

    :param content: The content to be minified.
    :type content: str
    :param file_ext: The file extension indicating the type of content.
    :type file_ext: str, optional
    :return: The minified content if applicable, otherwise the original content.
    :rtype: str
    """
    file_ext = file_ext.lstrip(".").lower()
    if file_ext and file_ext != "py":
        return content
    try:
        return minify(
            content,
            remove_annotations=True,
            remove_pass=True,
            remove_literal_statements=True,
            combine_imports=True,
            hoist_literals=True,
            rename_locals=True,
            rename_globals=True,
            remove_object_base=True,
            remove_asserts=True,
            remove_debug=True,
            remove_explicit_return_none=True,
            remove_builtin_exception_brackets=True,
        )
    except SyntaxError:
        return content


def gzip_compress(data: str, encoding: str = "utf-8") -> bytes:
    """Compress a given string using gzip and return the compressed data as bytes.

    :param data: The input string to compress.
    :type data: str
    :param encoding: The encoding to use for the compressed data. Defaults to "utf-8".
    :type encoding: str
    :return: The compressed data in gzip format.
    :rtype: bytes
    """
    buffer = BytesIO()
    with GzipFile(fileobj=buffer, mode="wb", mtime=0) as gz:
        gz.write(data.encode(encoding))
    return buffer.getvalue()
