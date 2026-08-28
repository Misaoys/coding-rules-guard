# Coding Rules Guard

Coding Rules Guard is a Codex plugin for risk-routed coding work. It keeps low-risk changes short, escalates cross-layer or high-risk work, records real verification evidence, and blocks out-of-scope delivery with a bundled dependency-free Python CLI.

## What it provides

- `FAST` and `FULL` routing based on risk rather than file count alone.
- Four focused phases: Plan, Implement, Verify, and Deliver.
- A strict seven-section Plan card for readable scope and acceptance criteria.
- Risk-based delegation instead of mandatory subagents for every write.
- Risk-triggered subagents default to `gpt-5.6-luna` with `max` reasoning through one centralized profile.
- Machine-readable run state, write-scope checks, evidence records, and delivery audit.
- Git-baseline change detection that does not trust Agent-declared file lists.
- Separate, audited gap authorization with machine-generated time and external actor identity.
- A guarded `rework` loop that returns failed verification to implementation and invalidates stale evidence.
- No default or repeated hash checks; hash parity runs once only when artifact identity is an acceptance criterion.
- Honest impact labels: no known impact, known impact, or unverified impact.

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

## Delegation profile

Delegation remains opt-in by task shape: direct execution is still the default for simple or tightly coupled work. When delegation has a clear benefit, the bundled `config/model-profiles.json` selects:

```json
{
  "model": "gpt-5.6-luna",
  "reasoning_effort": "max"
}
```

An explicit user model choice takes precedence. If Luna or `max` is unavailable, the agent must use the configured cost-efficient fallback and disclose the substitution.

## Executable gates

The plugin bundles `scripts/guard.py`. It requires Python 3.9 or newer, uses only the standard library, and emits JSON on success or failure.

```powershell
python scripts/guard.py --help
python -m unittest discover -s tests -v
```

Run-state files are task artifacts. Keep them in a temporary or task-work directory and do not stage them in the target repository.

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

`set-changes --file ...` is rejected for new v2 states. Pre-existing dirty files are fingerprinted at init: untouched files remain outside the task, while further task edits to them are detected. A changed Git HEAD invalidates the baseline.

When Verify confirms an implementation defect, record the failed evidence and return through the dedicated rework gate:

```powershell
python scripts/guard.py set-result --state .\work\run-state.json --result fail
python scripts/guard.py rework --state .\work\run-state.json --reason "implementation defect confirmed"
```

Direct `verify → implement` transitions remain blocked so a failed loop cannot silently discard evidence.

Repeated rework uses a bounded streak:

- First consecutive rework returns to Implement.
- Second consecutive rework returns `REPLAN_RECOMMENDED`.
- Third consecutive rework sets `REPLAN_REQUIRED` and returns to Plan.
- `revise-plan` must replace the mode, goal, write scope, and risk before Implement unlocks.
- The revised write scope must still contain every existing changed file.
- A revised Plan or successful verification resets `rework_streak`; lifetime `rework_count` remains auditable.

```powershell
python scripts/guard.py revise-plan `
  --state .\work\run-state.json `
  --mode FULL `
  --goal "revised hypothesis" `
  --write src/target.py `
  --impact known_impact `
  --risk-detail "hypothesis changed"
```

## Evidence boundary

Source inspection, tests, browser checks, installed copies, live hosts, and production are different evidence levels. A lower level must not be reported as a higher one. `pass_with_gaps` can reach delivery only after the gaps are listed and explicitly accepted.

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
