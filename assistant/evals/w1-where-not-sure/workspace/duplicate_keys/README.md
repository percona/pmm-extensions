# Duplicate Indexes

## What the app does

It runs `pt-duplicate-key-checker` on a MySQL server and reports duplicate and redundant indexes. It changes nothing.

## What the form asks

- The database server.
- The databases to check. Empty means all.

## What the result looks like

The tool's own report, with a summary at the end: the number of indexes, and of duplicates.

## What a database server must have

- Percona Toolkit 3.5 or later.
- A `[client]` section with a user and a password in the option file of the user that runs the tool.

## What the tool touches

It reads the definitions of tables and indexes. It writes nothing and does not reach the internet.

## What was tried, and what was not

One run on the test server ended with success and reported 15 indexes in the `employees` sample database. No run was made on a server with duplicate indexes.
