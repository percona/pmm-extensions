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

"""Report whether this kernel or emulator serves clone3.

Nomad's ``raw_exec`` issues it to spawn a task it places into a cgroup via the
unified v2 hierarchy; on any other layout it spawns with plain ``clone`` and
never asks for it.

Prints exactly one token. ``CLONE3_OK`` means the kernel answered — ``EINVAL``
for the deliberately undersized argument, which forks nothing. ``CLONE3_ENOSYS``
means nothing implements it: QEMU user-mode emulation, or Docker's default
seccomp profile on an unprivileged container. ``CLONE3_ERR=<errno>`` is anything
else, which the callers report without drawing a conclusion.
"""

import ctypes
import errno
import sys

SYS_CLONE3 = 435

libc = ctypes.CDLL(None, use_errno=True)
libc.syscall(SYS_CLONE3, ctypes.c_void_p(0), ctypes.c_size_t(8))
result = ctypes.get_errno()
if result == errno.EINVAL:
    sys.stdout.write("CLONE3_OK\n")
elif result == errno.ENOSYS:
    sys.stdout.write("CLONE3_ENOSYS\n")
else:
    sys.stdout.write(f"CLONE3_ERR={result}\n")
