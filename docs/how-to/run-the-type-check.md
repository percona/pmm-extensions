# Run the type check

This page shows how to run the type check, how to see what your branch adds, and how to read a failure. [Type-check policy](../reference/type-check-policy.md) lists the rules and what CI enforces.

## Prerequisites

- A development environment with the virtualenv active: see [Set up a development environment](set-up-a-development-environment.md).

## Check the whole tree

1. Run the check:

   ```bash
   make typecheck
   ```

   The output lists every diagnostic and ends with a count:

   ```text
   Found 4501 diagnostics
   ```

   The command passes when it exits with status 0. A diagnostic that starts with `warning[` does not fail it. One that starts with `error[` does.

## Check what your branch adds

Do this before you open a pull request. CI runs the same comparison.

1. Compare your branch with the branch it will merge into:

   ```bash
   make typecheck-diff BASE_SHA=origin/main
   ```

   The command checks the Python files that your branch changed, outside `tests/`. It checks them in your working tree and at the merge base, and it reports what your branch adds. For this comparison, a warning counts as an error.

   When your branch adds nothing, the command exits with status 0.

2. If the report is hard to attribute, check one file at a time:

   ```bash
   make typecheck-diff BASE_SHA=origin/main PER_FILE=1
   ```

## Read a failure

Each diagnostic names the rule in brackets, then the file, the line and the column:

```text
warning[unresolved-attribute]: Attribute `get_secret_value` is not defined on `None` in union `SecretStr | None`
   --> tests/sidecar/test_settings_env.py:894:13
```

Fix the code so that the type is correct. In the example, check for `None` before the call.

## Silence one diagnostic

Some diagnostics come from how a dependency declares its types. When the code is right and the fix belongs in the dependency, silence that one site.

1. Add a comment that names the rule, at the end of the reported line:

   <!-- constructed -->
   ```python
   value = settings.PASSWORD.get_secret_value()  # ty: ignore[unresolved-attribute]
   ```

2. Run the check again and confirm that the diagnostic is gone.

Name the rule in every such comment. Do not use the comment for a diagnostic that reports a real defect.

## Verify

`make typecheck` exits with status 0, and `make typecheck-diff BASE_SHA=origin/main` reports nothing new.

## Next steps

- [Type-check policy](../reference/type-check-policy.md)
