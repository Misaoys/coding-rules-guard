# Changelog

## Unreleased

- Reject stale rework diagnoses after new evidence or result changes, route hypothesis/scope diagnoses to `revise-plan`, and carry `next_hypothesis` into the next implementation attempt.
- Require `sample_id` when registering executions for multi-sample checks, handle legacy incomplete sample records explicitly, and prioritize diagnosis-linked failures plus nested execution output references in context projections.
- Upgrade new run states to schema v5 with bounded total `max_attempts`/`max_replans` budgets, append-only attempt and Plan history, and explicit legacy read-only rejection (`STATE_UPGRADE_REQUIRED`) for writes.
- Add source-bound diagnosis, bounded `rework`, `revise-plan`, and environment-only `retry-verify` lifecycle gates; failed, blocked, superseded, and completed attempts are archived without making historical evidence current.
- Add deterministic read-only `context`/`progress`, short `set-activity`, bounded telemetry events, and display-line output. Progress does not run Git/tests/models, create percentages/ETAs, or change release gates.
- Add optional Plan-bound verification definitions, execution/input bindings, idempotent execution registration, and read-only `check-verification` decisions (`reuse`, `run`, `diagnose`, `unknown`, `blocked`). Only trusted host/adapter receipts can be reused; this release has no host execution-interception API.
- Harden verification reuse: require stable before/after/current input bindings, support only explicit `once` or bounded multi-sample policies, diagnose matching failures before `--force`, and allow one execution to back multiple distinct evidence assertions.
- Compact historical context projections, include risk details and gaps in text progress, document distinct FAST/FULL Plan expansions, correct file-backed JSON examples, and split the CLI entrypoint into `scripts/guardlib` modules.
- Complete G-01/G-02/G-03 Git hardening: porcelain-v1 NUL status parsing, conflict/stage rejection, index/tree/content/type/mode identities, unsupported object errors, scoped command-local snapshots, and post-commit residual checks.
- Tighten V2.2 boundary handling: malformed explicit execution timestamps are rejected, `--force` cannot override unavailable environment observations, progress text escapes untrusted control characters and exposes Review failures, and v5 verification registry references are checked against their evidence records at load time.
- Make the Deliver Skill consume `check-verification` before repeating registered checks, while preserving independent Review, live Git, authorization, and completion gates.
- Keep the original Plan/evidence/review/delivery gates, atomic state save, non-overwriting `init`, content-index baseline marker, and active `revise-plan --reason` behavior. The optional G-11 graph projection is intentionally not implemented.

## 0.13.0 - 2026-09-09

- First v5 development-adjustments release candidate for the bounded loop, progress projection, and conservative verification-query contracts.

## 0.11.0 - 2026-09-05

- Replaced the fixed GPT-5.6 Sol/xhigh planner with the current session's main model under the `session_main` planning role.
- Require Plan records to name the current session model and reasoning effort; missing data blocks implementation without a fixed-model fallback.

## 0.10.0 - 2026-09-05

- Replaced the fixed GPT-5.6 Sol/xhigh reviewer with the current session's main model under the `session_main` audit role.
- Require every review record to name the current session model and reasoning effort; missing data blocks completion without a fixed-model fallback.

## 0.9.0 - 2026-09-04

- Required an explicit contract, evidence gap, and named regression before a new test is allowed.
- Restricted defensive code to observed failures, documented contracts, or explicit trust boundaries, and prohibited speculative fallbacks and catch-all handling.

## 0.8.0 - 2026-08-30

- Bound the task-work Plan Markdown path into new run states and Plan fingerprints.
- Delete that file only after the complete gate passes; a blocked completion keeps the Plan for follow-up work.

## 0.7.0 - 2026-08-30

- Made the seven-section Plan a canonical Markdown file in the task work directory instead of a chat-only artifact.
- Added `write-plan` for safe initial creation and `prepend-requirement` for adding later requirements above the existing Markdown without overwriting it.

## 0.6.0 - 2026-08-29

- Added the protected `planner_default` (`gpt-5.6-sol` + `xhigh`) role alongside the Luna executor and independent Sol reviewer; missing protected roles now block instead of silently falling back.
- Added schema v4 run states with `planner_profile`, `plan_record`, `record-plan`, and a Plan fingerprint bound to run identity, repository baseline, Plan fields, delivery intent, and revision.
- Required a current planner record and matching executor profile before `plan → implement`; configuration drift, Plan edits, expired revisions, and invalid timestamps block the gate.
- Cleared planner records on the third consecutive rework and required `revise-plan` followed by a new `record-plan`; legacy v1-v3 Plan states fail closed.
- Documented the strict Sol Plan → Luna WRITE → Verify → independent Sol Review chain, session-only read-only audits, and the local CLI's inability to prove real model identity cryptographically.

## 0.5.0 - 2026-08-29

- Made `gpt-5.6-luna` with `max` reasoning the default executor for every WRITE task, including FAST and small changes; read-only tasks do not delegate.
- Added a centralized `gpt-5.6-sol` with `xhigh` reasoning reviewer profile and an independent review gate for every WRITE task.
- Added schema v3 review state and `record-review`; missing, failed, stale, or configuration-mismatched reviews block Complete and Deliver.
- Bound review and audit to the complete baseline-to-final Git delta so post-audit extra commits invalidate review while a legal reviewed commit can complete.
- Required formal delivery reviews to bind the staged index mode/blob identities, rejecting index/worktree splits and comparing the final HEAD tree to the reviewed delta.
- Used baseline tree identities for staged paths, including tracked deletions, and rejected non-delivery reviews with task-only staged content.
- Kept legacy state loading, but fail closed for v1 write review and require v3 rebuild when v2 cannot bind a changed HEAD.
- Documented that the local CLI records auditable claims but cannot cryptographically prove the actual reviewer identity.

## 0.4.1 - 2026-08-29

- Added a centralized, risk-triggered subagent model profile.
- Set the delegated subagent default to `gpt-5.6-luna` with `max` reasoning effort.
- Preserved direct execution for simple work and allowed explicit user overrides or reported availability fallback.

## 0.4.0 - 2026-08-28

- Added Git baseline capture at init and automatic task-diff calculation in `set-changes`.
- Excluded untouched pre-existing dirty files while detecting further task edits to those files.
- Invalidated run state when Git HEAD moves and rejected Agent-declared files for new v2 states.
- Split `pass_with_gaps` requests from the independent `authorize-gaps` command.
- Recorded authorization ID, external actor, machine-generated UTC time, and reason.
- Kept legacy v1 state loading for in-progress runs while all new states use schema v2.

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
