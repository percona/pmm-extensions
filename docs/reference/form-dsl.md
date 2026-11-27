# Form DSL

The form DSL is the set of Python markers that describe an app's form. One model class is both the request body that the server validates and the source of the form that `GET /schema` serves. This page lists each marker, its fields and one example.

For how the pieces fit together, see [The app framework](../explanation/app-framework.md).

## How to read an entry

Every form field has this shape:

```text
name: Annotated[type, marker, marker, ...] = default
```

The first element of `Annotated` is the field's type. Each later element is a marker that the framework reads. A field with no default is required, and the server rejects a body that omits it. A field with a default is optional.

Each example names the file and the symbol it comes from. Examples marked "constructed" show a feature that no app in the repository uses. A line that holds only `...` stands for lines the example skips.

## Where each name is imported from

| Module | Names |
|---|---|
| `app.extensions.apps.framework.form_dsl` | `AppFormModel`, `TaskFormModel`, `Ui`, `Hidden`, `ArgFormat`, `Choices`, `Option`, `RemoteChoices`, `FieldWidget`, `ServiceRef`, `SchemaRef`, `TableRef`, `HostRef`, `FormLayout`, `SectionLayout`, `FormRules`, `SectionRules`, `Requires`, `Forbidden` |
| `app.extensions.apps.framework.rules` | `F`, the predicates, the combinators, `FailRule`, `FieldGate`, `CardinalityRule` |
| `app.core.utils.fields` | The field types that the examples use: `NonEmptyStr`, `StrippedNonEmptyStr`, `EmptyStrToNone` |

`Requires` and `Forbidden` take predicates, but they are markers. Import them from `form_dsl`, not from `rules`.

## Form models

### `AppFormModel`

The base class of every form model. It carries one field, `alert_on_fail`, which is marked `Hidden()`. Subclass it directly only for a form that has no Task section.

### `TaskFormModel`

The base class for a task app's form. It extends `AppFormModel` with the two fields of the Task section, `task_name` and `hostname`. A subclass declares only its own fields, and its layout must include the Task section. The `task` flavor of the scaffolder generates a `TaskFormModel` subclass.

## Field markers

### `Ui`

Carries what the type cannot say about how a field is shown. Nothing on `Ui` changes what the server accepts.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `section` | `str` | required | The key of the section that holds the field. It must match a `SectionLayout` key exactly. |
| `label` | `str` | the field name in title case, with spaces for underscores | The label shown beside the field. |
| `description` | `str` | none | Help text for the field. |
| `depends_on` | `str` | none | The name of the field whose value scopes this field's options. Required on `SchemaRef` and `TableRef` fields. Used by `HostRef` and `RemoteChoices` fields, and ignored on others. |
| `order` | `int` | `0` | The position in the section. Fields with the same value keep the order they are declared in. |
| `required` | `bool` | derived from the default | Overrides the `required` flag in the schema, for a field that has a default and must still be shown as required. |
| `widget` | `FieldWidget` | derived from the type | The input widget. |
| `default` | any | the model default | The value the form shows first, when it differs from the model default. It does not change what the server validates against. |
| `destructive` | `str` | none | Marks a field whose value destroys something the operator cannot get back. The value is the sentence a confirmation shows. An empty string is rejected. |
| `parent` | `str` | none | The name of a `bool` field in the same section that this field belongs to. The form shows the field indented under that toggle, and inactive until the toggle is on. |
| `help_placement` | `HelpPlacement` | chosen by length | Where the description is shown: `"inline"` puts it under the input, and `"tooltip"` puts it behind a help icon. |

[Layout](#layout) says how the sections are ordered.

<!-- src: app/extensions/apps/checksums/models.py :: ChecksumsForm -->
```python
    service_id: Annotated[
        int,
        ServiceRef(service_types=(ServiceTypeEnum.MYSQL,), check_connectivity=True),
        Ui(label="Database Host", section="Task"),
    ]
```

Mark a field `destructive` when setting it is what causes the loss. A field whose annotation is a discriminated union cannot carry the mark. Put it on a field inside each branch model.

<!-- src: app/extensions/apps/archives/models.py :: ArchivesCreate -->
```python
    delete_data: Annotated[
        bool | None,
        Ui(
            label="Delete Without Archiving",
            section="Advanced",
            destructive=(
                "The matched source rows are deleted without being archived "
                "anywhere. There is no copy to restore from."
            ),
        ),
    ] = None
```

### `Hidden`

Keeps a field on the model and leaves it out of the form. It has no fields. `AppFormModel.alert_on_fail` uses it.

### `ArgFormat`

Marks a field as a command-line argument of a `run-command` task, and says how the value is written into the command.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `template` | `str` | derived from the field name | The text of the argument. |

The field's type decides the kind of argument:

| Field type | Kind | Template | Written when |
|---|---|---|---|
| `bool` | flag | No placeholder. Default: `--field-name`. | The value is `True`. |
| any other | value | Ends in `=${value}`. Default: `--field-name=${value}`. | The value is not empty. The value is quoted for the shell. |

A form cannot pass a bare positional argument. A value template must end in `=${value}`, because the framework reads a command back by splitting each argument at its first `=`.

These rules are checked when the app is imported. A template with no `${value}` on a field that is not a `bool` is rejected, and the app does not load.

<!-- src: app/extensions/apps/checksums/models.py :: ChecksumsForm -->
```python
    explain_arg: Annotated[
        bool,
        ArgFormat("--explain"),
        Ui(
            label="Explain (dry run)",
            section="Flags",
            description="Show but do not execute checksum queries",
        ),
    ] = False
```

A value argument with an explicit template, constructed:

<!-- constructed -->
```python
databases: Annotated[
    str,
    ArgFormat("--databases=${value}"),
    Ui(label="Databases", section="Task"),
] = ""
```

With `databases="sales,orders"` and `explain_arg=True`, the command gets `--databases=sales,orders --explain`.

### `Choices` and `Option`

`Choices` gives a field a fixed list of options. It takes precedence over options derived from the type.

`Choices` fills the list that the form shows, and it does not restrict what the server accepts. A field of type `str` with `Choices` accepts any string. The field's type restricts the value: a `Literal` or an enum type rejects a value that is not one of its members.

| Field | Type | Meaning |
|---|---|---|
| `options` | a tuple of `(value, label)` pairs or `Option` instances | The options, in display order. |

<!-- src: app/extensions/apps/checksums/models.py :: ChecksumsForm -->
```python
    recursion_method: Annotated[
        str,
        Choices(
            (
                ("default", "Default"),
                ("processlist", "Processlist"),
                ("hosts", "Hosts"),
                ("dsn", "DSN"),
                ("none", "None"),
            )
        ),
        Ui(section="Recursion", required=True),
    ] = "processlist"
```

`Option` describes one option that needs more than a value and a label.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `value` | any | required | The submitted value. |
| `label` | `str` | required | The text shown. |
| `disabled` | `bool` | `False` | Shows the option and prevents its selection. |
| `disabled_reason` | `str` | none | The tooltip for a disabled option. |

`disabled` is a hint to the UI only. To reject the value on the server, add a rule.

<!-- constructed -->
```python
strategy: Annotated[
    str,
    Choices(
        (
            Option(value="fast", label="Fast"),
            Option(
                value="thorough",
                label="Thorough",
                disabled=True,
                disabled_reason="Not available on this service tier.",
            ),
        )
    ),
    Ui(label="Strategy", section="Task"),
] = "fast"
```

### `RemoteChoices`

Marks a field whose options the form fetches from a route that the app serves.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `endpoint` | `str` | required | The route that returns the options. |
| `allow_custom` | `bool` | `False` | Also accepts a typed value. |

MySQL Restores uses it for `backup_source`, in `app/extensions/apps/mysql_backups/restore/models.py`.

### `FieldWidget`

An enumeration that overrides the input widget of a field, through `Ui(widget=...)`.

| Member | Widget |
|---|---|
| `TEXTAREA` | A text area with several lines |
| `YAML` | A YAML editor |
| `CHOICE` | A list with one selection |
| `MULTI_CHOICE` | A list with several selections |

<!-- src: app/extensions/apps/backup_mongo/models.py :: BackupForm -->
```python
    backup_priority: Annotated[
        str | None,
        Ui(
            label="Node Priority (YAML)",
            section="BackupOptions",
            widget=FieldWidget.TEXTAREA,
            description=(
                "YAML mapping of mongod addresses to backup priority (highest wins). "
                "One entry per line, e.g.:\n"
                '"host1:27018": 2\n'
                '"host2:27018": 1'
            ),
        ),
    ] = None
```

## Reference markers

Four markers bind a field to the inventory. The field becomes a list whose options come from the inventory.

| Marker | Options | Fields beyond the shared ones |
|---|---|---|
| `ServiceRef` | Services | `service_types` (required): the service types to offer. `check_connectivity`: after the task is created, the create route checks that the service can be reached. It also makes this service the task's primary one. `primary`: names the task's primary service among several `ServiceRef` fields, with no check. |
| `SchemaRef` | Schemas of the selected service | none |
| `TableRef` | Tables of the selected schema | none |
| `HostRef` | Hosts that can run a task | `target_service`: the name of a service field. The form warns when the selected host is not the host of that service. |

All four share two fields:

| Field | Type | Default | Meaning |
|---|---|---|---|
| `allow_custom` | `bool` | `False` | Also accepts a typed name. The field's type is then `int \| str`: an inventory identifier or a name. |
| `multiple` | `bool` | `False` | Accepts several selections. The field's type is then a list. |

`Ui(depends_on=...)` chains the lists. A selected service scopes the schema options, and a selected schema scopes the table options.

<!-- src: app/extensions/apps/alters/models.py :: AltersCreate -->
```python
    hostname: Annotated[
        NonEmptyStr,
        HostRef(target_service="service_id"),
        Ui(label=EXECUTION_HOST_LABEL, section="Task"),
    ]
    service_id: Annotated[
        int,
        ServiceRef(service_types=[ServiceTypeEnum.MYSQL]),
        Ui(label="Database Host", section="Task"),
    ]
    ...
    db_schema: Annotated[
        int | StrippedNonEmptyStr,
        SchemaRef(allow_custom=True),
        Ui(
            label="Schema",
            section="data",
            depends_on="service_id",
            description="Schema to alter; pick from inventory or type a name.",
        ),
    ]
    db_table: Annotated[
        int | StrippedNonEmptyStr,
        TableRef(allow_custom=True),
        Ui(
            label="Table",
            section="data",
            depends_on="db_schema",
            description="Table to alter; pick from inventory or type a name.",
        ),
    ]
```

## Layout

### `FormLayout` and `SectionLayout`

The model says which section a field is in. The layout, inside the app's `Views`, gives each section its title and its behavior.

`FormLayout` has one field, `sections`: a tuple of `SectionLayout` objects. The order of the tuple does not set the display order. Sections appear in the order of each section's first field on the model.

| `SectionLayout` field | Type | Default | Meaning |
|---|---|---|---|
| `key` | `str` | required | The section key. Every `Ui(section="X")` needs a `SectionLayout(key="X")`, with the same case. |
| `title` | `str` | required | The heading shown. |
| `description` | `str` | none | Help text for the section. A renderer may leave it out. |
| `advanced` | `bool` | `False` | The section holds expert options. The form keeps such sections behind one **Show advanced options** control. |
| `collapsible` | `bool` | `False` | The section can be collapsed. |
| `collapsed_by_default` | `bool` | `False` | A collapsible section starts collapsed. |
| `render_after_submit` | `bool` | `False` | The section is shown after the submit button. |
| `forbidden` | a tuple of `FieldGate` | none | Hides the whole section when a gate's predicate holds. |

`TASK_SECTION_LAYOUT` is the layout of the Task section that `TaskFormModel` supplies.

<!-- src: app/extensions/apps/checksums/views.py :: checksums_views -->
```python
checksums_views = Views(
    layout=FormLayout(
        sections=(
            TASK_SECTION_LAYOUT,
            SectionLayout(key="Data", title="Data"),
            SectionLayout(key="Recursion", title="Recursion"),
            SectionLayout(key="Flags", title="Flags"),
            SectionLayout(key="Advanced", title="Advanced"),
        )
    ),
    list_view=ListView(
        columns=default_columns(),
    ),
    ...
    capabilities=Capabilities(
        chaining=True,
        alert_on_fail=True,
        scheduling=True,
        stats=True,
        pii_anonymization=True,
    ),
)
```

### `Capabilities`

The UI features of an app's pages, set on `Views`. Every flag defaults to `False`. It is a different class from `AppCapabilities`, which turns derived routes on and off.

| Flag | When `True` |
|---|---|
| `chaining` | Tasks can be chained. |
| `alert_on_fail` | The form offers a PMM alert for a failed task. The hidden `alert_on_fail` field stores the choice. |
| `scheduling` | Tasks can run on a schedule. |
| `stats` | The detail page shows the execution statistics card. |
| `pii_anonymization` | The detail page shows the **PII Anonymization** section. The flag controls the display only. |

## Rules

Rules span more than one field. They are built from predicates.

### Predicates

| Name | Meaning |
|---|---|
| `F("name")` | A reference to a field's submitted value. A comparison turns it into a predicate: `F("severity") == "critical"`. The operators are `==`, `!=`, `>`, `>=`, `<` and `<=`. |
| `truthy(name)`, `falsy(name)` | The value is truthy, or falsy, as Python defines it. |
| `present(name)`, `absent(name)` | The value is, or is not, set. `None`, `False` and an empty string or collection count as not set. The number `0` counts as set. |
| `any_truthy(*names)`, `all_truthy(*names)`, `any_falsy(*names)`, `all_falsy(*names)` | The same tests over several fields. |
| `any_present(*names)`, `all_present(*names)`, `none_present(*names)` | The presence tests over several fields. |
| `all_equal(*names)` | The fields hold the same value. |
| `p & q`, `p \| q`, `p ^ q`, `~p` | Combine predicates. `all_`, `any_`, `xor_` and `not_` are the function forms. |

The operators `&`, `|`, `^` and `~` work on predicates. On a bare `F` they raise `TypeError`.

A presence test sees a `bool` field only when it is `True`. A submitted `False` and an unset field look the same.

### `FailRule`

Rejects the form when its predicate holds.

| Field | Type | Meaning |
|---|---|---|
| `fail_when` | predicate | The condition that rejects the form. |
| `error_fields` | `list[str]` | The fields that the message is attached to. |
| `message` | `str` | The message shown. Optional. |

Rules can be generated. MySQL Backups builds one rule for each field that belongs to a single backup type:

<!-- src: app/extensions/apps/mysql_backups/forms.py :: _BACKUP_BOOL_FAIL_RULES -->
```python
_BACKUP_BOOL_FAIL_RULES = (
    *(
        FailRule(
            fail_when=truthy(name) & (F("backup_type") != owner_mode),
            error_fields=[name],
            message=(
                f"{name!r} must not be set when backup_type is not {owner_mode!r}."
            ),
        )
        for owner_mode, names in _MODE_BOOL_FIELDS.items()
        for name in names
    ),
```

### `FormRules` and `SectionRules`

`FormRules` collects the rules of one form. Assign it to `__form_rules__` on the model.

| Field | Type | Meaning |
|---|---|---|
| `fail_when` | a tuple of `FailRule` | Rules for the whole form. They are checked when the form is submitted. |
| `cardinality_rules` | a tuple of `CardinalityRule` | Limits on how many fields of a set are present. |
| `sections` | a mapping from a section key to `SectionRules` | Rules for one section. The form also checks them while the user types. |

`SectionRules` has two fields, `fail_when` and `cardinality_rules`, with the same meanings.

<!-- src: app/extensions/apps/mysql_backups/forms.py :: BackupCreate -->
```python
    __form_rules__: ClassVar[FormRules] = FormRules(
        fail_when=(*_BACKUP_BOOL_FAIL_RULES, *UPLOAD_REACHABILITY_FAIL_RULES),
        ...
        sections={"General": SectionRules(fail_when=_BINARY_COMPRESSION_FAIL_RULES)},
```

### `Requires` and `Forbidden`

Markers that gate one field. `Requires` makes the field mandatory when its predicate holds. `Forbidden` rejects a value when its predicate holds.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `when` | predicate | required | The condition. |
| `message` | `str` | none | The message shown. |

<!-- src: app/extensions/apps/mysql_backups/forms.py :: BackupCreate -->
```python
    encryption_recipient: Annotated[
        NonEmptyStr | EmptyStrToNone,
        Requires(
            when=AnyTruthy(_GPG_TIMING_FIELDS),
            message=(
                "'encryption_recipient' is required when 'encrypt' or "
                "'post_run_encrypt' is enabled."
            ),
        ),
        Forbidden(
            when=AllFalsy(_GPG_TIMING_FIELDS),
            message=(
                "'encryption_recipient' must not be set when no encryption mode is "
                "enabled."
            ),
        ),
```

One marker object can gate several fields. The same module binds `Forbidden(when=F("backup_type") != "M")` to a name and attaches it to each field that only Mydumper uses.

A `Forbidden` marker on a `bool` field fires only on `True`, because a presence test does not see `False`.

### `FieldGate`

Hides a whole section, through `SectionLayout(forbidden=...)`. It has the same two fields as `Forbidden`.

<!-- src: app/extensions/apps/mysql_backups/views.py :: mysql_backups_views -->
```python
mysql_backups_views = Views(
    layout=FormLayout(
        sections=(
            ...
            SectionLayout(
                key="Mydumper",
                title="Mydumper",
                collapsible=True,
                collapsed_by_default=True,
                forbidden=(FieldGate(when=F("backup_type") != "M"),),
            ),
```

### `CardinalityRule`

Limits how many fields of a set may be present.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `fields` | `list[str]` | required | The fields that are counted. The list cannot be empty. |
| `min` | `int` | none | The smallest count allowed. |
| `max` | `int` | none | The largest count allowed. |
| `when` | predicate | none | Applies the rule only when the predicate holds. |
| `message` | `str` | none | The message shown. |

<!-- constructed -->
```python
__form_rules__: ClassVar[FormRules] = FormRules(
    cardinality_rules=(
        CardinalityRule(
            fields=["primary_host", "secondary_host"],
            min=1,
            message="Select at least one target host.",
        ),
    )
)
```
