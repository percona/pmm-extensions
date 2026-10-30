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

"""Assert the XtraBackup S3 upload provider builds its log file from a str LOGGING_DIR.

``LOGGING_DIR`` reaches the provider as a ``str`` both from the YAML config and
from the ``DEFAULT_LOGGING_DIR`` environment fallback, so the provider has to
build its log path without assuming a ``Path``.
"""

from __future__ import annotations

import time
import types
from typing import TYPE_CHECKING

from tests.app.extensions.apps.mysql_backups.payload_harness import payload_method

if TYPE_CHECKING:
    from pathlib import Path


def test_s3_provider_opens_log_file_under_str_logging_dir(tmp_path: Path) -> None:
    """Open the ``s3cmd-*`` log file inside a ``LOGGING_DIR`` given as a str."""
    init = payload_method(
        "S3UploadProvider",
        "__init__",
        extra_namespace={
            "time": time,
            "boto3": types.SimpleNamespace(client=lambda _name: object()),
            "upload_logging_setup": lambda *_args: object(),
        },
    )
    provider = types.SimpleNamespace()
    settings = {
        "ALIAS": "db1",
        "S3_BUCKET": "s3://bucket/prefix",
        "BACKUP_TYPE": "full",
        "LOGGING_DIR": str(tmp_path),
    }

    init(provider, settings, 20)
    provider.log_stream.close()

    assert [p.name.startswith("s3cmd-db1-full-") for p in tmp_path.iterdir()] == [True]
