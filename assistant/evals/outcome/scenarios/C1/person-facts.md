
## What Dana knows for this request

Dana asked: "I want pt-duplicate-key-checker as an app in PMM."

- What she wants: to find duplicate and redundant indexes on a customer's MySQL server from PMM, without a shell on the server. A report is enough.
- How she runs the tool by hand: `pt-duplicate-key-checker --databases=<names>`, or with no option for every database. She reads the summary at the end.
- The options she uses: the list of databases. She does not use the others, and does not mind if the app offers more.
- Where the tool gets its login from on her customers' servers: the `[client]` section of `/root/.my.cnf`. She never types a password.
- Whether the tool changes anything: no. It only reads. The app must never drop an index.
- What a good result looks like: the tool's own report, with "Summary of indexes" at the end.
- What the app is called: "Duplicate Indexes".
- Who owns the app: Dana, for the database team.
- A ticket number: she has none.
- When the assistant shows what the app can reach and asks whether to go on: "Yes, go on."
- When the assistant asks what to do with the app now that it works, or where it should live: "I am not sure."
