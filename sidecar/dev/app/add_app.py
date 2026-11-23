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

"""Add one app to the settings profile of a PMM Extensions image.

A build step of an image derived from a released one. It edits the profile that
the base image holds, as text, so that every other line stays as the base image
wrote it and a newer base keeps its own settings. It refuses a profile that it
does not understand.

Usage: ``python add_app.py <settings profile> <app module name>``. Exit status
0 when the profile lists the app, 1 when the profile cannot be edited.
"""

import sys
from pathlib import Path

import yaml

APPS_LINE = "    APPS:"
ENTRY_INDENT = "      "


class ProfileError(Exception):
    """The profile is not one that this step can edit."""


def listed_apps(profile: Path) -> list[str]:
    """Return the module names in the profile's ``EXTENSIONS.APPS``.

    :param profile: The settings profile.
    :return: The names, in the file's order.
    :raises ProfileError: When the profile has no such list.
    """
    settings = yaml.safe_load(profile.read_text(encoding="utf-8"))
    try:
        return [app["MODULE_NAME"] for app in settings["default"]["EXTENSIONS"]["APPS"]]
    except (KeyError, TypeError) as error:
        message = f"{profile} has no default.EXTENSIONS.APPS list"
        raise ProfileError(message) from error


def add_app(profile: Path, app: str) -> list[str]:
    """Append ``app`` to ``EXTENSIONS.APPS`` of ``profile``, if it is not there.

    :param profile: The settings profile of the image.
    :param app: The app's module name under ``app/extensions/apps``.
    :return: The module names that the profile lists after the call.
    :raises ProfileError: When the profile has no ``APPS`` block where one is
        expected, or the edit did not give the expected list.
    """
    before = listed_apps(profile)
    if app in before:
        return before
    lines = profile.read_text(encoding="utf-8").splitlines(keepends=True)
    starts = [index for index, line in enumerate(lines) if line.rstrip() == APPS_LINE]
    if len(starts) != 1:
        message = f"{profile} has {len(starts)} lines '{APPS_LINE}', expected 1"
        raise ProfileError(message)
    end = starts[0] + 1
    while end < len(lines) and (
        lines[end].startswith(ENTRY_INDENT) or not lines[end].strip()
    ):
        end += 1
    while not lines[end - 1].strip():
        end -= 1
    original = profile.read_text(encoding="utf-8")
    lines.insert(end, f"{ENTRY_INDENT}- MODULE_NAME: {app}\n")
    profile.write_text("".join(lines), encoding="utf-8")
    after = listed_apps(profile)
    if after != [*before, app]:
        profile.write_text(original, encoding="utf-8")
        message = f"unexpected app list after the edit of {profile}: {after}"
        raise ProfileError(message)
    return after


def main(argv: list[str]) -> int:
    """Add one app to a profile and print the list that the profile holds.

    :param argv: The command line's arguments: the profile and the app.
    :return: The exit status.
    """
    profile, app = Path(argv[0]), argv[1]
    try:
        apps = add_app(profile, app)
    except ProfileError as error:
        sys.stderr.write(f"add_app: {error}\n")
        return 1
    sys.stdout.write(f"apps in the profile: {', '.join(apps)}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
