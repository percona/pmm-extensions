# Write a diagnostic script

This page shows how to add a script to the library under `snippets/`. You declare its categories, make it report a failure that the operator can act on, and run the check.

Support diagnostics offers the scripts in two ways. Search finds every script. The category browser shows only the scripts that declare a category.

## Prerequisites

- A development environment with the virtualenv active: see [Set up a development environment](set-up-a-development-environment.md).
- A script in `snippets/` with a YAML header. Each header line starts with `#`, and the header opens and closes with `# ---`. `snippets/disk_usage.sh` is a short example.

## Declare the categories

1. Decide whether the script belongs in the browser.

   A script belongs in the browser when an operator who knows only the symptom would use it. Such a script collects logs, configuration or state while the cause is still unknown.

   A script is for search only when the operator must already know the cause to choose it. A check for one condition, named after the alert that reports it, is an example.

   Decide from the script's name and description. An `alerts:` entry in the header does not decide it: most scripts in the browser have one too.

2. Add the key to the header.

   For a script in the browser, list one or more member names from [Diagnostic categories](../reference/diagnostic-categories.md):

   ```yaml
   # diagnostic_categories:
   #  - SERVER_CRASHED_RESTART_NOT_SUCCESSFUL
   #  - NOT_RESPONDING
   ```

   For a script that is for search only, write the empty list on one line:

   ```yaml
   # diagnostic_categories: []
   ```

   Do not leave the key without a value. The script then becomes search-only by accident, with only a warning in the log.

## Report a failure the operator can act on

An operator runs a script from a form. When the script fails because of a value in that form, the output must name the field to change. It must also keep what the tool said.

1. Capture the tool's error output and branch on the exit status:

   ```bash
   if ! out=$($MYSQL -e "SELECT @@gtid_mode;" 2>&1); then
       echo "Could not read GTID mode (check --defaults-file): $out"
   else
       printf '%s\n' "$out"
   fi
   ```

   Do not discard the error output and print a fixed message. A message such as `GTID mode not available.` hides refused credentials, a missing socket and a stopped server behind one wrong cause.

2. When the command filters its result, use three branches, and keep the error output in its own file:

   ```bash
   err=$(mktemp)
   if ! out=$($MYSQL -e "SELECT ... WHERE state LIKE '%lock%';" 2> "$err"); then
       echo "Could not query threads waiting for locks (check --defaults-file): $(cat "$err")"
   elif [ -z "$out" ]; then
       echo "No threads waiting for locks."
   else
       printf '%s\n' "$out"
   fi
   rm -f "$err"
   ```

   With two branches, a query that failed looks like a query that found nothing. The script then reports "No threads waiting for locks" on no evidence.

   Do not merge the two streams here. These clients also write notices to the error stream on a run that succeeds. A notice would then count as a result.

3. Name the parameter that the failing command received, as the operator types it:

   | Header of the parameter | Write |
   |---|---|
   | A plain parameter | `--dbname` |
   | `arg_format: --defaults-file=${value}` | `--defaults-file` |
   | `type: bool` | `--mask` |
   | `positional: true` | The parameter's `label`, because it has no flag |

   Never name a parameter that the failing command did not receive. When the failure comes from the host, such as a service that is stopped or a log file that cannot be read, name no parameter.

Two cases need no message. A probe such as `command -v psql` answers a question by failing. A script that tries several sources in turn reports only the last failure, and that message names the parameter to pass.

No test enforces this section. A reviewer checks it.

## Run the check

1. Check every script header against the authoring rules:

   ```bash
   pytest tests/app/extensions/snippets/test_frontmatter_authoring.py -q
   ```

   The output ends with a line such as:

   ```text
   1069 passed in 3.57s
   ```

## Verify

The check passes, and the script's header has a `diagnostic_categories` key with a list value.

## Next steps

- [Diagnostic categories](../reference/diagnostic-categories.md)
