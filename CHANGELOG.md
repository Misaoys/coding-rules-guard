# Changelog

## 0.3.0 - 2026-08-28

- Added a consecutive rework streak with a warning on the second attempt.
- Forced the third consecutive rework back to Plan with an unskippable `replan_required` gate.
- Added `revise-plan` to replace the hypothesis, mode, write scope, and risk before implementation resumes.
- Blocked revised Plans that would orphan existing changed files outside the new write scope.
- Reset only the consecutive streak after a revised Plan or successful verification while preserving lifetime rework count.

## 0.2.1 - 2026-08-28

- Added a guarded Verify-to-Implement rework loop.
- Rework now resets stale results, evidence, gaps authorization, and delivery audit state.
- Added auditable rework count and reason fields while keeping direct backward transitions blocked.

## 0.2.0 - 2026-08-28

- Added machine-readable run state and deterministic phase-transition gates.
- Added write-scope, evidence, gap-authorization, secret-pattern, and Git delivery checks.
- Replaced mandatory delegation and fixed model names with risk-based role selection.
- Centralized shared workflow policy to reduce repeated instructions.
- Limited hash validation to artifact-parity acceptance checks and prohibited unchanged repeat hashing.
- Added unit and CLI tests, Git marketplace metadata, and public installation documentation.

## 0.1.0 - 2026-08-24

- Added FAST/FULL routing and Plan, Implement, Verify, and Deliver phases.
- Added evidence-first validation, cross-feature impact disclosure, and the seven-section Plan card.
