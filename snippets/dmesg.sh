#!/usr/bin/env bash

# ---
# title: "Kernel Messages (dmesg)"
# description: "This script displays the kernel ring buffer messages with human-readable timestamps."
# allow_extra_args: false
# sudo: true
# diagnostic_categories:
#  - SERVER_CRASHED_RESTART_SUCCESSFUL
#  - SERVER_CRASHED_RESTART_NOT_SUCCESSFUL
#  - NOT_RESPONDING
#  - TEMPORARY_STALLS
# service_type: generic
# alerts:
#   - name: MySQLInstanceNotAvailable
#     service_type: mysql
#   - name: PostgreSQLIsDown
#     service_type: postgresql
#   - HighMemoryUsage
#   - HighIOUtilization
# ---

# This script executes the 'dmesg -T' command with sudo.
# 'dmesg' displays the kernel ring buffer messages.
# The '-T' option adds a human-readable timestamp to each message.

dmesg -T
