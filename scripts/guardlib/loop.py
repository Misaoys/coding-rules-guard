"""Bounded attempt-loop state helpers.

The state machine supplies the policy-specific callbacks so this module stays
independent from the CLI and Git implementation.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable


ErrorFactory = Callable[[str, list[str]], Exception]
Digest = Callable[[dict[str, Any]], str]


def ensure_attempt_budget(state: dict[str, Any], error: ErrorFactory) -> None:
    loop = state["loop"]
    if loop["attempt_count"] >= loop["policy"]["max_attempts"]:
        raise error(
            "LOOP_BUDGET_EXHAUSTED",
            ["attempts", f"{loop['attempt_count']}/{loop['policy']['max_attempts']}", "no new attempt may be started"],
        )


def failure_signatures(state: dict[str, Any], digest: Digest) -> set[str]:
    """Return bounded, comparable failure observations without proof of progress."""
    signatures: set[str] = set()
    for item in state.get("evidence", []):
        if not isinstance(item, dict) or item.get("result") not in {"fail", "blocked"}:
            continue
        signatures.add(
            digest(
                {
                    "kind": item.get("kind"),
                    "entry": item.get("entry"),
                    "command": item.get("command"),
                    "observed": item.get("observed"),
                    "level": item.get("level"),
                    "result": item.get("result"),
                    "check_id": item.get("check_id"),
                }
            )
        )
    review = state.get("review")
    if isinstance(review, dict) and review.get("result") in {"fail", "blocked"}:
        signatures.add(
            digest(
                {
                    "kind": "review",
                    "observed": review.get("observed"),
                    "result": review.get("result"),
                }
            )
        )
    return signatures


def historical_failure_signatures(state: dict[str, Any], digest: Digest) -> set[str]:
    signatures: set[str] = set()
    for archived in state.get("loop", {}).get("attempt_history", []):
        if not isinstance(archived, dict):
            continue
        historical = {
            "evidence": archived.get("evidence_snapshot", []),
            "review": archived.get("review_snapshot"),
        }
        signatures.update(failure_signatures(historical, digest))
    return signatures


def next_attempt_id(state: dict[str, Any]) -> str:
    used = {item.get("attempt_id") for item in state["loop"]["attempt_history"]}
    active = state["loop"].get("active_attempt")
    if isinstance(active, dict):
        used.add(active.get("attempt_id"))
    index = 1
    while f"a{index:04d}" in used:
        index += 1
    return f"a{index:04d}"


def start_attempt(
    state: dict[str, Any],
    kind: str,
    hypothesis: str,
    source_attempt_id: str | None,
    snapshot: dict[str, Any] | None,
    *,
    attempt_kinds: set[str],
    max_text_chars: int,
    error: ErrorFactory,
    require_write: Callable[[dict[str, Any], str], None],
    ensure_budget: Callable[[dict[str, Any]], None],
    now: Callable[[], str],
    bounded_text: Callable[[Any, str, int], str],
    compute_task_fingerprint: Callable[..., str],
) -> dict[str, Any]:
    require_write(state, "starting an attempt")
    if kind not in attempt_kinds:
        raise error("INVALID_ATTEMPT_KIND", [kind])
    if state["loop"].get("active_attempt") is not None:
        raise error("ATTEMPT_ALREADY_ACTIVE", [state["loop"]["active_attempt"].get("attempt_id", "unknown")])
    ensure_budget(state)
    active = {
        "attempt_id": next_attempt_id(state),
        "plan_revision": state["plan_revision"],
        "kind": kind,
        "started_at": now(),
        "source_attempt_id": source_attempt_id,
        "hypothesis": bounded_text(hypothesis, "hypothesis", max_text_chars),
        "start_fingerprint": compute_task_fingerprint(state, allow_unregistered=True, snapshot=snapshot),
        "diagnosis": None,
        "evidence_refs": [],
        "review_ref": None,
    }
    state["loop"]["active_attempt"] = active
    state["loop"]["attempt_count"] += 1
    return active


def attempt_end_fingerprint(
    state: dict[str, Any], *, compute_task_fingerprint: Callable[..., str], error_type: type[Exception]
) -> str | None:
    try:
        return compute_task_fingerprint(state)
    except error_type:
        return None


def archive_active_attempt(
    state: dict[str, Any],
    outcome: str,
    closure_reason: str,
    snapshot: dict[str, Any] | None,
    *,
    attempt_outcomes: set[str],
    max_text_chars: int,
    error: ErrorFactory,
    error_type: type[Exception],
    now: Callable[[], str],
    bounded_text: Callable[[Any, str, int], str],
    compute_task_fingerprint: Callable[..., str],
) -> dict[str, Any]:
    active = state["loop"].get("active_attempt")
    if not isinstance(active, dict):
        raise error("ATTEMPT_REQUIRED", ["no active attempt to archive"])
    if outcome not in attempt_outcomes:
        raise error("INVALID_ATTEMPT_OUTCOME", [outcome])
    end_fingerprint_error = None
    try:
        end_fingerprint = compute_task_fingerprint(state, snapshot=snapshot)
    except error_type as exc:
        end_fingerprint = None
        end_fingerprint_error = f"{exc.code}: {'; '.join(exc.details)}"
    archived = deepcopy(active)
    archived.update(
        {
            "closed_at": now(),
            "end_fingerprint": end_fingerprint,
            "end_fingerprint_error": end_fingerprint_error,
            "outcome": outcome,
            "closure_reason": bounded_text(closure_reason, "closure_reason", max_text_chars),
            "historical_not_valid_for_gate": True,
            "result": state.get("result"),
            "gaps": deepcopy(state.get("gaps", [])),
            "gap_authorization": deepcopy(state.get("gap_authorization")),
            "evidence_snapshot": deepcopy(state.get("evidence", [])),
            "review_snapshot": deepcopy(state.get("review")),
        }
    )
    state["loop"]["attempt_history"].append(archived)
    state["loop"]["active_attempt"] = None
    return archived


def clear_current_records(state: dict[str, Any]) -> None:
    state["result"] = "pending"
    state["evidence"] = []
    state["gaps"] = []
    state["gaps_authorized"] = False
    state["gap_authorization"] = None
    state["delivery_audit"] = None
    state["review"] = None
    state["verification_registry"]["active_refs"] = []
    telemetry = state.get("telemetry")
    if isinstance(telemetry, dict):
        activity = telemetry.get("activity")
        if isinstance(activity, dict):
            telemetry["activity"] = None


def current_attempt(state: dict[str, Any], error: ErrorFactory) -> dict[str, Any]:
    active = state.get("loop", {}).get("active_attempt")
    if not isinstance(active, dict):
        raise error("ATTEMPT_REQUIRED", ["a current active attempt is required"])
    return active
