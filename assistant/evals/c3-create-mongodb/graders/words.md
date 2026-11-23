---
type: regex
pattern: '\b(scaffold\w*|flavou?rs?|run modes?|form models?|form schemas?|markers?|tasks?|sidecars?|images?|settings\.yaml|conformance|contract tests?|snapshot (files?|tests?)|branch(es)?|commits?|pull requests?|nomad|payloads?)\b'
flags: i
match: not_contains
target: last_message
---

What the person reads holds no word for how an app is built.
