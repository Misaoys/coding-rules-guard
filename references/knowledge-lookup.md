# Bounded knowledge lookup

For Coding Rules tasks, invoke the installed knowledge-base Query workflow when the conditions below apply; this user-authorized integration needs no repeated confirmation. It is read-only and supplements current repository evidence; it does not alter memory or activate M3.

Before planning, query once only when project history, established constraints, known fixes, or a runbook can change the plan. Skip trivial, self-contained tasks. If a phase is entered directly and context is missing, the shared workflow hook may perform that one query there.

Use the documented project KB root when appropriate. Otherwise default to `memories`; do not scan both roots unless needed, and never select a stale candidate root. The default root already supports A and live M3; this integration makes no promise that M3 is activated.

Use task entities/identifiers in the query and pass the current repository through `--cwd`:

```powershell
py -3 -B -X utf8 "C:\Users\admin\.codex\skills\knowledge-base\scripts\kb_query.py" search --root "C:\Users\admin\.codex\memories" --query "<task entities/identifiers>" --cwd "<current repo>" --top 3 --max-chars 3000 --json
```

`--max-chars 3000` means characters, not tokens; make no 192-token runtime promise. Read only the returned line spans needed for the decision: start with Top 1 and stop once scope, status, and evidence are sufficient.

Use `--deep` only when exact historical evidence is required. No match or query error does not prove absence; fall back to current repository evidence without unrelated broad scans.

Carry sufficient paths, line numbers, and brief conclusions from Plan into later phases and delegated work. Do not reload an already-read Skill or open every mode reference/runbook; load only the selected mode and required runbook.
