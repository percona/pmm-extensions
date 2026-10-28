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

"""Define tests for the app.extensions.inventory module."""

from app.extensions.inventory import Service, Table
from app.inventory.models import ServiceTypeEnum


class TestRepr:
    """Test suite for the inventory models' ``repr`` used in sync log lines."""

    def test_service_repr_names_its_identity(self):
        """Render the service's name, type, external id and port."""
        service = Service(
            external_id="svc-1", name="orders", type=ServiceTypeEnum.MYSQL, port=3306
        )

        assert repr(service) == (
            f"Service(name='orders', type={ServiceTypeEnum.MYSQL!r},"
            " external_id='svc-1', port=3306)"
        )

    def test_table_repr_omits_the_create_statement(self):
        """Render only the table's name, never its ``CREATE`` statement."""
        table = Table(name="orders", create="CREATE TABLE orders (id INT)")

        assert repr(table) == "Table(name='orders')"
