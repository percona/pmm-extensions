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

"""POST a Jira version-name payload to an automation webhook.

Best-effort helper invoked from the ``trigger-jenkins`` Makefile rule and from
``scripts/release.py`` for the RC create-version webhook. Exits non-zero on any
failure (missing/empty env vars, non-HTTPS URL, unreadable payload file,
network error, non-2xx response) so callers can treat success/failure via the
process exit code.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

WEBHOOK_TIMEOUT_SECONDS = 10


def load_payload_file(path: Path) -> dict[str, Any] | None:
    """Read the extra webhook fields from a JSON file holding one object.

    The release-stable webhook carries the release email this way: the rule
    reads ``webhookData.release.subject`` and ``webhookData.release.body_html``.

    :param path: The JSON file to read.
    :return: The object's fields, or ``None`` after printing a warning when the
        file is unreadable, is not JSON, or holds something other than an
        object.
    """
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(
            f"warning: cannot read webhook payload file {path}: {type(exc).__name__}",
            file=sys.stderr,
        )
        return None
    if not isinstance(loaded, dict):
        print(
            f"warning: webhook payload file {path} must hold a JSON object",
            file=sys.stderr,
        )
        return None
    return loaded


def post_webhook(
    url_env: str,
    auth_env: str,
    version_tag: str,
    extra_data: dict[str, Any] | None = None,
) -> int:
    """POST the Jira version-name payload to the configured webhook.

    Silently exit ``1`` when either env var is unset or empty — webhook
    configuration is optional maintainer-side and the matching manual
    reminder in the release script's "Next steps" output is sufficient to
    cover that path.

    Reject non-HTTPS URLs up front to avoid shipping the webhook token over
    cleartext if the env var is misconfigured.

    Catch ``HTTPError`` before the broader transport-error branch because
    ``HTTPError`` is a subclass of ``URLError``; the reverse order would
    swallow the status code needed for the warning and tests.

    :param url_env: The environment-variable name holding the webhook URL.
    :param auth_env: The environment-variable name holding the webhook token.
    :param version_tag: The version-name payload value (e.g. ``v0.12.0``).
    :param extra_data: Further fields to send beside ``versionName``, which
        always wins over a field of the same name.
    :return: ``0`` on HTTP 2xx, ``1`` on any error (missing env var, non-HTTPS
        URL, network failure, timeout, or non-2xx response).
    """
    url = os.environ.get(url_env)
    secret = os.environ.get(auth_env)
    if not url or not secret:
        return 1
    if not url.startswith("https://"):
        print(
            f"warning: Jira webhook URL must use https:// scheme ({url_env})",
            file=sys.stderr,
        )
        return 1
    data = {**(extra_data or {}), "versionName": version_tag}
    payload = json.dumps({"data": data}).encode("utf-8")
    try:
        request = urllib.request.Request(  # noqa: S310
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "X-Automation-Webhook-Token": secret,
            },
            method="POST",
        )
        with urllib.request.urlopen(  # noqa: S310
            request,
            timeout=WEBHOOK_TIMEOUT_SECONDS,
        ) as response:
            response.read()
    except urllib.error.HTTPError as exc:
        print(
            f"warning: Jira webhook returned HTTP {exc.code} ({url_env})",
            file=sys.stderr,
        )
        return 1
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        print(
            f"warning: Jira webhook dispatch failed ({url_env}): {type(exc).__name__}",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """Parse CLI args and dispatch ``post_webhook``.

    :param argv: Optional argv override (for tests).
    :type argv: list[str] | None
    :return: Process exit code.
    :rtype: int
    """
    parser = argparse.ArgumentParser(
        prog="post-jira-webhook",
        description="POST a Jira version-name payload to an automation webhook.",
    )
    parser.add_argument(
        "--url-env",
        required=True,
        help="Environment variable name holding the webhook URL.",
    )
    parser.add_argument(
        "--auth-env",
        required=True,
        help="Environment variable name holding the webhook token.",
    )
    parser.add_argument(
        "--version-tag",
        required=True,
        help="Version-name payload value (e.g. v0.12.0).",
    )
    parser.add_argument(
        "--payload-file",
        type=Path,
        help="JSON file holding one object whose fields are sent beside versionName.",
    )
    args = parser.parse_args(argv)
    extra_data = None
    if args.payload_file is not None:
        extra_data = load_payload_file(args.payload_file)
        if extra_data is None:
            return 1
    return post_webhook(args.url_env, args.auth_env, args.version_tag, extra_data)


if __name__ == "__main__":
    sys.exit(main())
