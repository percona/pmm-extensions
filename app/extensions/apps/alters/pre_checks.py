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

"""Pre-checks script for pt-online-schema-change operations.

This script performs various checks before executing pt-online-schema-change
against a MySQL table to ensure the operation can be safely performed.

Usage:
    python pre_checks.py --schema <database_name> --table <table_name> [options]
    python pre_checks.py --config <config.yaml>
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

import pymysql
import yaml
from pymysql import Error as PyMySQLError


class PreCheckError(Exception):
    """Custom exception for pre-check failures."""


class MySQLPreChecks:
    """Class to handle pre-checks for pt-online-schema-change operations."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 3306,
        schema: str = "",
        table: str = "",
        config_file: str = "~/.my.cnf",
        *,
        skip_filesystem_checks: bool = False,
    ) -> None:
        """Initialize the pre-checks class.

        Args:
            host: MySQL host
            port: MySQL port
            schema: Database name
            table: Table name
            config_file: Path to .my.cnf file
            skip_filesystem_checks: If True, skip disk space check (e.g. when executor
                host is not the DB node, e.g. RDS).

        """
        # Use .my.cnf values as defaults, but allow command line to override
        self.host = host
        self.port = port
        self.schema = schema
        self.table = table
        self.connection = None
        self.config_file = config_file
        self.skip_filesystem_checks = skip_filesystem_checks
        # Setup logging
        self.setup_logging()

    def setup_logging(self) -> None:
        """Set up logging configuration."""
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(levelname)s - %(message)s",
            handlers=[logging.StreamHandler(sys.stdout)],
        )
        self.logger = logging.getLogger(__name__)

    def connect_to_mysql(self) -> bool:
        """Establish connection to MySQL database.

        Returns:
            bool: True if connection successful, False otherwise

        """
        try:
            self.connection = pymysql.connect(
                host=self.host,
                port=self.port,
                database=self.schema,
                read_default_file=self.config_file,
                charset="utf8mb4",
            )
            self.logger.info(
                "Successfully connected to MySQL at %s:%s", self.host, self.port
            )
        except PyMySQLError:
            self.logger.exception("Failed to connect to MySQL")
            return False
        else:
            return True

    def get_table_size_mb(self) -> float | None:
        """Get the size of the table in MB.

        :return: The table size in MB, or ``None`` when the query fails.
        """
        if not self.connection:
            self.logger.error("No MySQL connection available")
            return None

        query = """
        SELECT ROUND((data_length + index_length) / 1024 / 1024, 2)
        FROM information_schema.tables
        WHERE table_schema = %s AND table_name = %s
        """

        try:
            with self.connection.cursor() as cursor:
                cursor.execute(query, (self.schema, self.table))
                result = cursor.fetchone()

                if result and result[0] is not None:
                    size_mb = float(result[0])
                    self.logger.info(
                        "Table %s.%s size: %s MB", self.schema, self.table, size_mb
                    )
                    return size_mb
                self.logger.error(
                    "Table %s.%s not found or has no data", self.schema, self.table
                )
                return None

        except PyMySQLError:
            self.logger.exception("Failed to get table size")
            return None

    def get_mysql_datadir(self) -> str | None:
        """Get the MySQL datadir path.

        :return: The MySQL datadir path, or ``None`` when the query fails.
        """
        if not self.connection:
            self.logger.error("No MySQL connection available")
            return None

        query = "SHOW VARIABLES LIKE 'datadir'"

        try:
            with self.connection.cursor() as cursor:
                cursor.execute(query)
                result = cursor.fetchone()

                if result:
                    datadir = result[1]
                    self.logger.info("MySQL datadir: %s", datadir)
                    return datadir
                self.logger.error("Could not retrieve MySQL datadir")
                return None

        except PyMySQLError:
            self.logger.exception("Failed to get MySQL datadir")
            return None

    def get_disk_space_mb(self, path: str) -> tuple[float, float] | None:
        """Get free and total disk space for a given path.

        :param path: The path to check disk space for.
        :return: The ``(free_space_mb, total_space_mb)`` pair, or ``None`` when
            the lookup fails.
        """
        try:
            # Get disk usage statistics
            total, _, free = shutil.disk_usage(path)

            # Convert bytes to MB
            free_mb = free / (1024 * 1024)
            total_mb = total / (1024 * 1024)

            self.logger.info(
                "Disk space for %s: %.2f MB free / %.2f MB total",
                path,
                free_mb,
                total_mb,
            )
        except OSError:
            self.logger.exception("Failed to get disk space for %s", path)
            return None
        else:
            return free_mb, total_mb

    def check_disk_space(self) -> bool:
        """Check if there's enough free disk space in the MySQL datadir partition.

        Returns:
            bool: True if check passes, False otherwise

        """
        self.logger.info("=== Checking disk space ===")

        # Get table size
        table_size_mb = self.get_table_size_mb()
        if table_size_mb is None:
            return False

        # Get MySQL datadir
        datadir = self.get_mysql_datadir()
        if not datadir:
            return False

        # Get disk space
        disk_info = self.get_disk_space_mb(datadir)
        if not disk_info:
            return False

        free_space_mb, _ = disk_info

        # Check if free space is greater than table size
        if free_space_mb > table_size_mb:
            self.logger.info(
                "[PASS] Free disk space (%.2f MB) > table size (%.2f MB)",
                free_space_mb,
                table_size_mb,
            )
            return True
        self.logger.error(
            "[FAIL] Free disk space (%.2f MB) <= table size (%.2f MB)",
            free_space_mb,
            table_size_mb,
        )
        self.logger.error(
            "Need at least %.2f MB free space for pt-online-schema-change",
            table_size_mb,
        )
        return False

    def check_foreign_key_references(self) -> bool:
        """Check if the table has foreign keys referencing it.

        Returns:
            bool: True if check passes (no foreign keys referencing the table), False otherwise

        """
        self.logger.info("=== Checking for foreign key references ===")

        if not self.connection:
            self.logger.error("No MySQL connection available")
            return False

        query = """
        SELECT
            TABLE_SCHEMA,
            TABLE_NAME,
            CONSTRAINT_NAME,
            COLUMN_NAME,
            REFERENCED_TABLE_SCHEMA,
            REFERENCED_TABLE_NAME,
            REFERENCED_COLUMN_NAME
        FROM
            INFORMATION_SCHEMA.KEY_COLUMN_USAGE
        WHERE
            REFERENCED_TABLE_SCHEMA = %s
            AND REFERENCED_TABLE_NAME = %s
        """

        try:
            with self.connection.cursor() as cursor:
                cursor.execute(query, (self.schema, self.table))
                results = cursor.fetchall()

                if not results:
                    self.logger.info(
                        "[PASS] No foreign keys found referencing table %s.%s",
                        self.schema,
                        self.table,
                    )
                    return True

                # Log all foreign key references found
                self.logger.error(
                    "[FAIL] Found %d foreign key(s) referencing table %s.%s:",
                    len(results),
                    self.schema,
                    self.table,
                )

                for row in results:
                    (
                        table_schema,
                        table_name,
                        constraint_name,
                        column_name,
                        referenced_table_schema,
                        referenced_table_name,
                        referenced_column_name,
                    ) = row
                    self.logger.error(
                        "  - %s.%s.%s -> %s.%s.%s (constraint: %s)",
                        table_schema,
                        table_name,
                        column_name,
                        referenced_table_schema,
                        referenced_table_name,
                        referenced_column_name,
                        constraint_name,
                    )

                self.logger.error(
                    "pt-online-schema-change cannot proceed when foreign keys reference the target table"
                )
                return False

        except PyMySQLError:
            self.logger.exception("Failed to check for foreign key references")
            return False

    def check_table_triggers(self) -> bool:
        """Check if the table has triggers.

        Returns:
            bool: True if check passes (no triggers on the table), False otherwise

        """
        self.logger.info("=== Checking for table triggers ===")

        if not self.connection:
            self.logger.error("No MySQL connection available")
            return False

        query = """
        SELECT
            EVENT_OBJECT_SCHEMA,
            EVENT_OBJECT_TABLE,
            TRIGGER_SCHEMA,
            TRIGGER_NAME,
            EVENT_MANIPULATION
        FROM
            INFORMATION_SCHEMA.TRIGGERS
        WHERE
            EVENT_OBJECT_SCHEMA = %s
            AND EVENT_OBJECT_TABLE = %s
        """

        try:
            with self.connection.cursor() as cursor:
                cursor.execute(query, (self.schema, self.table))
                results = cursor.fetchall()

                if not results:
                    self.logger.info(
                        "[PASS] No triggers found on table %s.%s",
                        self.schema,
                        self.table,
                    )
                    return True

                # Log all triggers found
                self.logger.error(
                    "[FAIL] Found %d trigger(s) on table %s.%s:",
                    len(results),
                    self.schema,
                    self.table,
                )

                for row in results:
                    (
                        _,
                        _,
                        trigger_schema,
                        trigger_name,
                        event_manipulation,
                    ) = row
                    self.logger.error(
                        "  - Trigger: %s.%s (Event: %s)",
                        trigger_schema,
                        trigger_name,
                        event_manipulation,
                    )

                self.logger.error(
                    "pt-online-schema-change cannot proceed when triggers exist on the target table"
                )
                return False

        except PyMySQLError:
            self.logger.exception("Failed to check for table triggers")
            return False

    def run_all_checks(self) -> bool:
        """Run all pre-checks.

        Returns:
            bool: True if all checks pass, False otherwise

        """
        self.logger.info("Starting pre-checks for %s.%s", self.schema, self.table)
        self.logger.info("=" * 50)

        all_passed = True

        # Check disk space (only when running on the same host as the DB)
        if self.skip_filesystem_checks:
            self.logger.info(
                "=== Skipping disk space check (executor host is not the database node) ==="
            )
        elif not self.check_disk_space():
            all_passed = False

        # Check for foreign key references
        if not self.check_foreign_key_references():
            all_passed = False

        # Check for table triggers
        if not self.check_table_triggers():
            all_passed = False

        self.logger.info("=" * 50)
        if all_passed:
            self.logger.info(
                "[PASS] All pre-checks PASSED - pt-online-schema-change can proceed"
            )
        else:
            self.logger.error(
                "[FAIL] Some pre-checks FAILED - pt-online-schema-change should NOT proceed"
            )

        return all_passed

    def close_connection(self) -> None:
        """Close MySQL connection."""
        if self.connection:
            self.connection.close()
            self.logger.info("MySQL connection closed")


def load_yaml_config(config_file: str) -> dict[str, Any]:
    """Load configuration from YAML file.

    Args:
        config_file: Path to YAML configuration file

    Returns:
        dict: Configuration parameters

    Raises:
        PreCheckError: If config file cannot be read or parsed

    """
    try:
        with Path(config_file).open(encoding="utf-8") as file:
            config = yaml.safe_load(file)
            if config is None:
                config = {}
            return config
    except FileNotFoundError as err:
        raise PreCheckError(f"Configuration file not found: {config_file}") from err
    except yaml.YAMLError as err:
        raise PreCheckError(f"Error parsing YAML configuration file: {err}") from err
    except Exception as err:
        raise PreCheckError(f"Error reading configuration file: {err}") from err


def _apply_config_to_args(args: argparse.Namespace, config: dict[str, Any]) -> None:
    """Override parsed args with values from YAML config.

    Schema/table only apply when unset on the CLI (no argparse defaults).
    Host, port, and mysql_config_file always come from YAML when present: the
    Alters router generates that YAML, so those keys supersede CLI defaults
    (127.0.0.1, 3306, ~/.my.cnf), which are not user-provided values.
    """
    if "schema" in config and args.schema is None:
        args.schema = config["schema"]
    if "table" in config and args.table is None:
        args.table = config["table"]
    if "host" in config:
        args.host = config["host"]
    if "port" in config:
        args.port = config["port"]
    if "mysql_config_file" in config:
        args.mysql_config_file = config["mysql_config_file"]
    if "skip_filesystem_checks" in config:
        # Router writes unquoted YAML booleans; PyYAML loads them as Python bool.
        args.skip_filesystem_checks = config["skip_filesystem_checks"]


def parse_arguments() -> argparse.Namespace:
    """Parse command line arguments with support for YAML config file.

    Returns:
        argparse.Namespace: Parsed arguments

    """
    parser = argparse.ArgumentParser(
        description="Pre-checks for pt-online-schema-change operations",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Using command line arguments
  python pre_checks.py --schema mydb --table mytable
  python pre_checks.py --schema mydb --table mytable --host 192.168.1.100
  python pre_checks.py --schema mydb --table mytable --port 3307

  # Using YAML configuration file
  python pre_checks.py --config config.yaml

  # YAML config file format:
  # schema: mydb
  # table: mytable
  # host: 192.168.1.100
  # port: 3306
  # mysql_config_file: ~/.my.cnf
  # skip_filesystem_checks: true  # skip disk check when executor != DB node
        """,
    )

    parser.add_argument(
        "--config",
        type=str,
        help="Path to YAML configuration file (alternative to command line arguments)",
    )
    parser.add_argument("--schema", help="Database name")
    parser.add_argument("--table", help="Table name")
    parser.add_argument(
        "--host", default="127.0.0.1", help="MySQL host (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--port", type=int, default=3306, help="MySQL port (default: 3306)"
    )
    parser.add_argument(
        "--mysql-config-file",
        default="~/.my.cnf",
        type=str,
        help="Path to .my.cnf file (default: ~/.my.cnf)",
    )
    parser.add_argument(
        "--skip-filesystem-checks",
        action="store_true",
        help="Skip disk space check (e.g. when executor host is not the DB node)",
    )

    args = parser.parse_args()

    if args.config:
        try:
            _apply_config_to_args(args, load_yaml_config(args.config))
        except PreCheckError as e:
            parser.error(str(e))

    if not args.schema:
        parser.error("--schema is required (either via command line or config file)")
    if not args.table:
        parser.error("--table is required (either via command line or config file)")

    return args


def main() -> None:
    """Run the pre-checks script."""
    args = parse_arguments()

    # Create pre-checks instance
    pre_checks = MySQLPreChecks(
        host=args.host,
        port=args.port,
        schema=args.schema,
        table=args.table,
        config_file=args.mysql_config_file,
        skip_filesystem_checks=args.skip_filesystem_checks,
    )

    try:
        # Connect to MySQL
        if not pre_checks.connect_to_mysql():
            sys.exit(1)

        # Run all checks
        success = pre_checks.run_all_checks()

        # Exit with appropriate code
        sys.exit(0 if success else 1)

    except KeyboardInterrupt:
        pre_checks.logger.exception("\nOperation cancelled by user")
        sys.exit(1)
    except Exception:
        pre_checks.logger.exception("Unexpected error occurred")
        sys.exit(1)
    finally:
        pre_checks.close_connection()


if __name__ == "__main__":
    main()
