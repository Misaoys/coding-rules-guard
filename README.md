# Coding Rules Guard

Coding Rules Guard is a Codex plugin for risk-routed coding work. It keeps low-risk changes short, escalates cross-layer or high-risk work, records real verification evidence, and blocks out-of-scope delivery with a bundled dependency-free Python CLI.

## What it provides

- `FAST` and `FULL` routing based on risk rather than file count alone.
- A focused current-session-main Plan → Luna Implement → Verify → current-session-main review → Deliver workflow.
- A strict seven-section Plan card written directly to a task-work Markdown file, with later requirements prepended above the existing Plan.
- Read-only work stays direct and session-audited; every task with `WRITE`, including FAST and small changes, uses the current session's main model for Plan and `executor_default` (`gpt-5.6-luna` + `max`) for implementation.
- Every write task requires a review by the current session's main model before Complete or Deliver.
- v5 machine-readable run state with a planner record, bounded attempts/replans, Plan fingerprint, write-scope checks, evidence records, telemetry, and delivery audit.
- Bounded `status`, `context`, and `progress` projections for handoff and user-visible milestones; all are read-only and never release gates.
- Git-baseline change detection that does not trust Agent-declared file lists.
- Content-index Git fingerprints with NUL-safe path parsing, bounded argument batches, and an explicit legacy slow path marker.
- Evidence fingerprints tied to the current task worktree, plus active `revise-plan --reason` support in Plan, Implement, and Verify.
- Separate, audited gap authorization with machine-generated time and external actor identity.
- A guarded `rework` loop that returns failed verification to implementation and invalidates stale evidence.
- Source-bound `record-diagnosis`, `retry-verify`, and `revise-plan` lifecycle gates; old attempts remain historical and cannot satisfy a new attempt.
- Optional verification definitions and conservative `check-verification` reuse queries; the CLI does not execute saved commands or pretend to intercept a host executor.
- No default or repeated hash checks; hash parity runs once only when artifact identity is an acceptance criterion.
- Honest impact labels: no known impact, known impact, or unverified impact.
- Strict minimal-change gates: new tests and defensive code need a stated contract, observed trigger or trusted boundary, and one discriminating verification path.

## Install from GitHub

Add this repository as a Git marketplace, then install the plugin:

```powershell
codex plugin marketplace add Misaoys/coding-rules-guard
codex plugin add coding-rules-guard@coding-rules-guard
```

Restart or reload Codex if the current session does not pick up newly installed skills.

## Skills

- `$coding-rules`: choose FAST/FULL and the current phase.
- `$coding-rules-plan`: produce the seven-section Plan card.
- `$coding-rules-implement`: make only the declared changes and record actual files.
- `$coding-rules-verify`: record success, boundary evidence, results, and gaps.
- `$coding-rules-deliver`: audit Diff, Git, versions, and evidence before delivery.

## Markdown Plan files

The Plan card is a file, not a chat-only artifact. Write its exact seven-section Markdown to an explicit path in the task work directory, such as `work/plan.md`; do not create it in the business repository or stage it.

```powershell
python scripts/guard.py write-plan `
  --file .\work\plan.md `
  --content-file .\work\plan-card.md
```

`write-plan` refuses to overwrite an existing file. When a user adds a requirement, keep the same Plan file and prepend the requirement above all existing Markdown:

```powershell
python scripts/guard.py prepend-requirement `
  --file .\work\plan.md `
  --content-file .\work\new-requirement.md
```

The command preserves the existing Plan below a Markdown separator. If that requirement changes the task's scope or risk, generate a revised Plan and update the guarded run-state before implementation resumes. Bind the same Plan file to the run state; it is deleted only after the final complete gate passes.

## Delegation profile

The bundled `config/model-profiles.json` fixes the executor and records the current session's main model for both Plan and review:

```json
{
  "executor_default": {
    "model": "gpt-5.6-luna",
    "reasoning_effort": "max"
  },
  "session_main": {
    "source": "session_main"
  }
}
```

Read-only tasks do not delegate and normally do not create run-state. Plan and review must use the current session's main model and record its displayed model and reasoning effort; missing information blocks the gate and never falls back to Sol/xhigh. The executor retains its configured-role contract. The local CLI records auditable claims but cannot cryptographically prove the actual model identity.

After the current session's main model produces the seven-section Plan, initialize a v5 run-state and record that exact Plan before implementation:

```powershell
python scripts/guard.py init `
  --state .\work\run-state.json `
  --repo . `
  --plan-file .\work\plan.md `
  --mode FAST `
  --goal "bounded change" `
  --write src/target.py `
  --impact no_known_impact

python scripts/guard.py record-plan `
  --state .\work\run-state.json `
  --profile session_main `
  --model <CURRENT_SESSION_MAIN_MODEL> `
  --reasoning-effort <CURRENT_SESSION_MAIN_REASONING>

python scripts/guard.py transition --state .\work\run-state.json --to implement
```

`record-plan` binds the session-main profile, model, reasoning effort, timestamp, Plan revision, the bound Plan Markdown path and its UTF-8 bytes, and a fingerprint over `run_id`, repository, baseline HEAD, MODE, GOAL, WRITE, RISK, delivery flag, revision, and any registered verification definitions. `plan → implement` rejects missing, expired, tampered, or drifted records, missing or changed Plan content, and missing current-session model information. After every complete gate passes, the CLI deletes the bound Plan Markdown; a blocked task keeps it. The record is an audit claim, not cryptographic proof of which model actually ran.

After verification, the current session's main model is recorded with:

```powershell
git add -- src/target.py tests/test_target.py

python scripts/guard.py record-review `
  --state .\work\run-state.json `
  --profile session_main `
  --model <CURRENT_SESSION_MAIN_MODEL> `
  --reasoning-effort <CURRENT_SESSION_MAIN_REASONING> `
  --result pass `
  --observed "Current session main model checked the current task diff"
```

For formal delivery, task files must be explicitly staged before current-session-main review, with no index/worktree split on task paths. The review binds the exact staged path set and Git mode/blob identities. Audit records the same fingerprint, and Complete recomputes the final baseline-to-HEAD tree delta. A hidden staged version, later edit, extra committed path, mode/blob change, evidence change, result change, rework, or revised Plan makes the review stale and blocks completion. Non-delivery FAST work binds the worktree delta instead and rejects task paths left only in the staged index; staged deletions use their baseline tree identity. The local CLI validates this auditable record but cannot cryptographically prove which model or person actually invoked the command; host-level identity enforcement remains a separate boundary.

## Executable gates

The plugin bundles `scripts/guard.py`. It requires Python 3.9 or newer, uses only the standard library, and emits JSON on success or failure.

```powershell
python scripts/guard.py --help
python -m unittest discover -s tests -v
```

Run-state files are task artifacts. Keep them in a temporary or task-work directory and do not stage them in the target repository.

Read-only tasks stop after the session-level Plan audit and do not create a run-state file or subagent unless the user explicitly requests machine-audited state. Run-state initialization is for tasks with `WRITE`.

Initialize before modifying the repository, then let Git baseline detection discover task changes:

```powershell
python scripts/guard.py init `
  --state .\work\run-state.json `
  --repo . `
  --mode FAST `
  --goal "bounded change" `
  --write src/target.py `
  --impact no_known_impact

python scripts/guard.py set-changes --state .\work\run-state.json
```

`set-changes --file ...` is rejected for new v5 states. Pre-existing dirty files are fingerprinted at init: untouched files remain outside the task, while further task edits to them are detected. Every Verify, evidence, review, audit, and Complete gate recomputes the real Git scope. During Plan/Implement/Verify, a changed HEAD stops the run; after a formally reviewed delivery, only the exact audited baseline-to-HEAD delta may complete. Directory scopes remain valid, but directory objects, gitlinks, conflicts, and unsupported special files are explicitly blocked when a concrete identity is required.

Legacy v1-v4 states are readable by the new CLI through `status`, `context`, `progress`, and other read-only projections. Every state-writing command returns `STATE_UPGRADE_REQUIRED`; create a new v5 run-state after manually reconciling the old task's baseline and ownership. A revised Plan clears the old `plan_record`; run `revise-plan` and then `record-plan` again. Changing a Plan after recording it requires calling planner again rather than editing the card in the router. The old CLI rejects v5 states.

When Verify confirms an implementation defect, record the failed evidence and return through the dedicated rework gate:

```powershell
python scripts/guard.py set-result --state .\work\run-state.json --result fail
python scripts/guard.py rework --state .\work\run-state.json --reason "implementation defect confirmed"
```

Before `rework`, the current attempt must contain a bounded diagnosis that cites current failed evidence or a failed review. The diagnosis is a routing record, not authorization or proof of a root cause:

```powershell
python scripts/guard.py record-diagnosis `
  --state .\work\run-state.json `
  --input @.\work\diagnosis.json
```

`retry-verify` is reserved for a blocked environment or input-data diagnosis with a genuinely new external observation. It archives the blocked attempt and starts a `verify_only` attempt; it never runs a test, changes code, or writes `pass`:

```powershell
python scripts/guard.py retry-verify `
  --state .\work\run-state.json `
  --reason "host recovery was observed"
```

Direct `verify → implement` transitions remain blocked so a failed loop cannot silently discard evidence.

Repeated rework uses a bounded streak:

- First consecutive rework returns to Implement.
- Second consecutive rework returns `REPLAN_RECOMMENDED`.
- Third consecutive rework sets `REPLAN_REQUIRED` and returns to Plan.
- `revise-plan` must replace the mode, goal, write scope, and risk before Implement unlocks.
- Third consecutive rework clears the old planner record; the revised Plan must be recorded again before Implement.
- The revised write scope must still contain every existing changed file.
- A revised Plan or successful verification resets `rework_streak`; lifetime `rework_count` remains auditable.

```powershell
python scripts/guard.py revise-plan `
  --state .\work\run-state.json `
  --mode FULL `
  --goal "revised hypothesis" `
  --write src/target.py `
  --impact known_impact `
  --reason "new evidence changed the hypothesis" `
  --risk-detail "hypothesis changed"
```

`revise-plan --reason` is also available before the third failed rework, but only in `plan`, `implement`, or `verify`; it clears stale evidence/review/authorization state, increments the Plan revision, and checks current Git paths before accepting the new scope. If `--delivery-required` or `--no-delivery-required` is omitted, the existing delivery intent is preserved rather than silently cancelled. A forced third-rework revision may reuse its recorded failure reason when the option is omitted.

## State snapshots and upgrades

Use `status` for a compact handoff summary, not for authorization:

```powershell
python scripts/guard.py status --state .\work\run-state.json --limit 20
$env:CODING_GUARD_COMPACT = '1'
python scripts/guard.py status --state .\work\run-state.json --limit 20
```

Use `context` for a bounded deterministic handoff and `progress` for short user-visible milestones. Neither reads Git, runs tests, calls a model, or claims current evidence is valid:

```powershell
python scripts/guard.py context --state .\work\run-state.json --history-limit 3 --path-limit 20
python scripts/guard.py progress --state .\work\run-state.json --format text --event-limit 5
python scripts/guard.py set-activity --state .\work\run-state.json --text "核对当前验证入口"
```

`--limit` accepts 1–200 and only bounds displayed path samples. The response keeps `snapshot: state_only_not_a_gate`, full risk and gaps, path totals, truncation flags, and `review_recorded` (which means a record exists, not that it is still valid). Invalid limits and unreadable state fail without changing the state.

New run states use `schema_version: 5` and `git_baseline.fingerprint_format: content-index-v1`. `init` accepts `--max-attempts 1..20` (default 6) and `--max-replans 0..8` (default 3); these are total run budgets and cannot be reset by rework, replan, recovery, or model changes. A missing baseline format marker uses the explicit compatibility slow path; an unknown marker blocks. Finish or pause a legacy run before upgrading, keep the old state for audit, and create a new state with the new CLI; never pass a new state to an old CLI or reuse an old state as if it had new bindings. Plan and state files must stay outside the business repository, and `init` never overwrites an existing state.

For a registered verification, pass a small machine-readable subset of the Plan's VERIFY section to `record-plan --verification-spec <JSON>`. After the real execution is recorded with `record-evidence --check-id <ID> --execution-record <JSON>`, query before deciding whether another execution is needed:

```powershell
python scripts/guard.py check-verification --state .\work\run-state.json --check-id unit
python scripts/guard.py check-verification --state .\work\run-state.json --check-id unit --force --reason "user requested a fresh sample"
```

The result is only `reuse`, `run`, `diagnose`, `unknown`, or `blocked`. Reuse preserves the original evidence/execution ID and is limited to the same attempt, Plan, definition, complete input binding, and trusted `host_receipt`/`adapter` source. `agent_report` never earns automatic reuse. This CLI has no host-level execution-interception API, so the actual adapter or Skill must consume the query; no command is automatically skipped merely because a query returned `reuse`.

The supported repeat policies are `{"mode":"once"}` and `{"mode":"samples","required_samples":2..16}`. Sample runs need distinct `sample_id` values. A reusable execution record must include matching `before_binding` and `after_binding` objects of the form `{ "binding_digest": "<64 lowercase hex characters>" }`; the query also compares that digest with the current input binding. Missing or changed observations return `unknown`, and a static Plan environment identity is not a current environment observation. One execution may support multiple evidence assertions; only the same assertion is idempotent. An unresolved matching failure is diagnosed before `--force` is honored.

## Evidence boundary

Source inspection, tests, browser checks, installed copies, live hosts, and production are different evidence levels. A lower level must not be reported as a higher one. `pass_with_gaps` can reach delivery only after the gaps are listed and explicitly accepted.

The optional graph projection described by the development adjustments is not implemented in this release. The run-state, history, telemetry, and verification registry remain the only authorities; no graph file or graph agent is created.

## Strict test and defensive-code scope

Do not write tests merely to increase coverage, preserve a private implementation, explore a hypothetical edge case, or make a change appear safer. A new test requires a documented behavior contract or observed defect, a concrete reason existing validation cannot distinguish it, and one named regression it prevents.

Likewise, defensive code requires an observed failure, documented host/protocol contract, or explicit external trust boundary. Scope it to the known trigger, preserve useful errors, and verify that trigger. Catch-all handlers, swallowed errors, default fallbacks, blind retries, unconditional compatibility branches, and speculative null guards are out of scope without that evidence.

Gap approval is a separate command:

```powershell
python scripts/guard.py set-result `
  --state .\work\run-state.json `
  --result pass_with_gaps `
  --gap "live host unavailable"

python scripts/guard.py authorize-gaps `
  --state .\work\run-state.json `
  --authorized-by "user:viola" `
  --reason "accepted missing live-host evidence"
```

The authorization record contains an ID, `authorized_by`, machine-generated UTC `authorized_at`, and a reason. The local CLI rejects `agent:` identities. A host that needs cryptographic actor authentication must additionally restrict who can invoke `authorize-gaps`.

## Development validation

```powershell
$env:PYTHONUTF8 = '1'
python C:\Users\admin\.codex\skills\.system\plugin-creator\scripts\validate_plugin.py .
Get-ChildItem skills -Directory | ForEach-Object {
  python C:\Users\admin\.codex\skills\.system\skill-creator\scripts\quick_validate.py $_.FullName
}
python -m unittest discover -s tests -v
```

## License

No open-source license has been selected yet. The repository is public, but reuse rights are not granted until the owner adds a license.
