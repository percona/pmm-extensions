
## What Dana knows for this request

Dana asked: "Make Checksums work in PMM."

- What she wants: to run `pt-table-checksum` from PMM, to compare a source server with its replicas. She would like it every week.
- If the assistant says that the app cannot run on a schedule: "Then make it work as it is. The schedule can come later."
- The test server has no replica. She knows that. A run that checksums the tables of one server is enough as a first proof.
- Where the tool gets its login from on her customers' servers: the `[client]` section of `/root/.my.cnf`.
- What a good result looks like: the tool's table, with the columns TS, ERRORS, DIFFS, ROWS and TABLE.
- A ticket number: she has none.
- When the assistant shows a page that describes the app and asks whether it is right: read it, and say yes if it describes `pt-table-checksum`.
