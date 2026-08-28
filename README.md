# Coding Rules Guard

Coding Rules Guard is a Codex plugin for risk-routed coding work. It keeps low-risk changes short, escalates cross-layer or high-risk work, records real verification evidence, and blocks out-of-scope delivery with a bundled dependency-free Python CLI.

## What it provides

- `FAST` and `FULL` routing based on risk rather than file count alone.
- Four focused phases: Plan, Implement, Verify, and Deliver.
- A strict seven-section Plan card for readable scope and acceptance criteria.
- Risk-based delegation instead of mandatory subagents for every write.
- Machine-readable run state, write-scope checks, evidence records, and delivery audit.
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

## Executable gates

The plugin bundles `scripts/guard.py`. It requires Python 3.9 or newer, uses only the standard library, and emits JSON on success or failure.

```powershell
python scripts/guard.py --help
python -m unittest discover -s tests -v
```

Run-state files are task artifacts. Keep them in a temporary or task-work directory and do not stage them in the target repository.

## Evidence boundary

Source inspection, tests, browser checks, installed copies, live hosts, and production are different evidence levels. A lower level must not be reported as a higher one. `pass_with_gaps` can reach delivery only after the gaps are listed and explicitly accepted.

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
