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

from app.core.utils.cache import ttl_cache
from app.core.utils.date_time import make_datetime_utc, utc_now
from app.core.utils.dict import (
    deep_dict_update,
    filter_dict,
    remove_falsy_values_from_dict,
    sort_dict,
    transform_dict_keys,
)
from app.core.utils.imports import (
    import_var,
    validate_attribute_is_importable,
    validate_importable_settings,
    validate_module_is_importable,
)
from app.core.utils.iterators import unique_everseen
from app.core.utils.pydantic import run_pydantic_type_validator
from app.core.utils.serialization import json_serializer
from app.core.utils.strings import (
    b64decode_str,
    b64encode_str,
    shorten_text,
    slugify,
    to_uppercase,
)
