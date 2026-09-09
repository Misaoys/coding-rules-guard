"""Bounded, non-gating projections for handoff and user-visible progress."""

from __future__ import annotations

from typing import Any


def safe_display_text(value: Any) -> str:
    """Render untrusted state text without allowing control sequences into text output."""
    text = value if isinstance(value, str) else str(value)
    rendered: list[str] = []
    for char in text:
        codepoint = ord(char)
        if (
            codepoint < 0x20
            or 0x7F <= codepoint <= 0x9F
            or codepoint in {0x2028, 0x2029}
            or 0xD800 <= codepoint <= 0xDFFF
        ):
            rendered.append(f"\\u{codepoint:04x}")
        else:
            rendered.append(char)
    return "".join(rendered)


def _short(value: Any, max_text_chars: int, fallback: str | None = None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return fallback
    return value.strip()[:max_text_chars]


def _refs(value: Any, max_refs: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item.strip()][:max_refs]


def _refs_metadata(value: Any, max_refs: int, field: str) -> dict[str, Any]:
    total = sum(isinstance(item, str) and bool(item.strip()) for item in value) if isinstance(value, list) else 0
    return {f"{field}_total": total, f"{field}_truncated": total > max_refs}


def _clipped_fields(source: dict[str, Any], fields: tuple[str, ...], max_text_chars: int) -> list[str]:
    return [
        field for field in fields
        if isinstance(source.get(field), str) and len(source[field].strip()) > max_text_chars
    ]


def compact_diagnosis(
    diagnosis: Any, max_refs: int, max_text_chars: int
) -> dict[str, Any] | None:
    if not isinstance(diagnosis, dict):
        return None
    projected: dict[str, Any] = {
        "diagnosis_id": _short(diagnosis.get("diagnosis_id"), max_text_chars),
        "attempt_id": _short(diagnosis.get("attempt_id"), max_text_chars),
        "plan_revision": diagnosis.get("plan_revision"),
        "recorded_at": _short(diagnosis.get("recorded_at"), max_text_chars),
        "classification": _short(diagnosis.get("classification"), max_text_chars),
        "cause_summary": _short(diagnosis.get("cause_summary"), max_text_chars),
        "source_refs": _refs(diagnosis.get("source_refs"), max_refs),
        "next_action_summary": _short(diagnosis.get("next_action_summary"), max_text_chars),
        "next_hypothesis": _short(diagnosis.get("next_hypothesis"), max_text_chars),
        "expected_observation": _short(diagnosis.get("expected_observation"), max_text_chars),
        "source_snapshot_digest": _short(diagnosis.get("source_snapshot_digest"), max_text_chars),
        "worktree_fingerprint": _short(diagnosis.get("worktree_fingerprint"), max_text_chars),
        **_refs_metadata(diagnosis.get("source_refs"), max_refs, "source_refs"),
        "text_truncated_fields": _clipped_fields(diagnosis, (
            "diagnosis_id", "attempt_id", "recorded_at", "classification", "cause_summary",
            "next_action_summary", "next_hypothesis", "expected_observation",
            "source_snapshot_digest", "worktree_fingerprint",
        ), max_text_chars),
    }
    information = diagnosis.get("new_information")
    if isinstance(information, dict):
        projected["new_information"] = {
            "kind": _short(information.get("kind"), max_text_chars),
            "summary": _short(information.get("summary"), max_text_chars),
            "source_refs": _refs(information.get("source_refs"), max_refs),
            **_refs_metadata(information.get("source_refs"), max_refs, "source_refs"),
            "text_truncated_fields": _clipped_fields(information, ("kind", "summary"), max_text_chars),
        }
    else:
        projected["new_information"] = None
    return projected


def compact_attempt(attempt: Any, max_refs: int, max_text_chars: int) -> dict[str, Any] | None:
    """Project the bounded handoff fields of an active attempt."""
    if not isinstance(attempt, dict):
        return None
    return {
        "attempt_id": _short(attempt.get("attempt_id"), max_text_chars),
        "plan_revision": attempt.get("plan_revision"),
        "kind": _short(attempt.get("kind"), max_text_chars),
        "started_at": _short(attempt.get("started_at"), max_text_chars),
        "source_attempt_id": _short(attempt.get("source_attempt_id"), max_text_chars),
        "hypothesis": _short(attempt.get("hypothesis"), max_text_chars),
        "start_fingerprint": _short(attempt.get("start_fingerprint"), max_text_chars),
        "diagnosis": compact_diagnosis(attempt.get("diagnosis"), max_refs, max_text_chars),
        "evidence_refs": _refs(attempt.get("evidence_refs"), max_refs),
        "review_ref": _short(attempt.get("review_ref"), max_text_chars),
        **_refs_metadata(attempt.get("evidence_refs"), max_refs, "evidence_refs"),
        "text_truncated_fields": _clipped_fields(attempt, (
            "attempt_id", "kind", "started_at", "source_attempt_id", "hypothesis",
            "start_fingerprint", "review_ref",
        ), max_text_chars),
    }


def _prioritized_evidence(
    evidence: list[Any], diagnosis: dict[str, Any], max_refs: int
) -> list[dict[str, Any]]:
    diagnosis_refs = set(_refs(diagnosis.get("source_refs"), max_refs))
    ranked: list[tuple[int, int, dict[str, Any]]] = []
    for index, evidence_item in enumerate(evidence):
        if not isinstance(evidence_item, dict):
            continue
        evidence_id = evidence_item.get("evidence_id")
        if evidence_id in diagnosis_refs:
            priority = 0
        elif evidence_item.get("result") in {"fail", "blocked"}:
            priority = 1
        else:
            priority = 2
        ranked.append((priority, index, evidence_item))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [item for _, _, item in ranked[:max_refs]]


def compact_context_history(
    state: dict[str, Any], limit: int, max_refs: int, max_text_chars: int
) -> dict[str, Any]:
    history = state.get("loop", {}).get("attempt_history", [])
    selected = history[-limit:] if limit else []

    projected: list[dict[str, Any]] = []
    for item in selected:
        if not isinstance(item, dict):
            continue
        diagnosis = item.get("diagnosis") if isinstance(item.get("diagnosis"), dict) else {}
        evidence = item.get("evidence_snapshot") if isinstance(item.get("evidence_snapshot"), list) else []
        evidence_refs: list[str] = []
        record_locations: list[str] = []
        conclusions: list[str] = []
        text_truncated = _clipped_fields(item, ("attempt_id", "started_at", "hypothesis"), max_text_chars)
        selected_evidence = _prioritized_evidence(evidence, diagnosis, max_refs)
        for evidence_item in selected_evidence:
            evidence_id = evidence_item.get("evidence_id")
            if isinstance(evidence_id, str) and evidence_id.strip():
                evidence_refs.append(evidence_id)
            execution = evidence_item.get("execution") if isinstance(evidence_item.get("execution"), dict) else None
            output_ref = execution.get("output_ref") if execution else None
            if isinstance(output_ref, str) and output_ref.strip():
                if len(output_ref) > max_text_chars:
                    text_truncated.append(f"record_locations[{len(record_locations)}]")
                record_locations.append(output_ref[:max_text_chars])
            result = evidence_item.get("result")
            if result in {"pass", "fail", "blocked"}:
                conclusion = _short(evidence_item.get("observed"), max_text_chars)
                if _clipped_fields(evidence_item, ("observed",), max_text_chars):
                    text_truncated.append(f"evidence[{evidence_id}].observed")
                if conclusion:
                    conclusions.append(f"{evidence_id or 'evidence'}: {result}; {conclusion}")
                else:
                    conclusions.append(f"{evidence_id or 'evidence'}: {result}")
        review = item.get("review_snapshot") if isinstance(item.get("review_snapshot"), dict) else None
        review_result = review.get("result") if review else None
        if review_result in {"pass", "fail", "blocked"}:
            # A failed review must not be displaced by a full list of passing checks.
            review_conclusion = f"review: {review_result}"
            if review_result in {"fail", "blocked"}:
                conclusions.insert(0, review_conclusion)
            else:
                conclusions.append(review_conclusion)
        conclusions_total = sum(
            isinstance(entry, dict) and entry.get("result") in {"pass", "fail", "blocked"}
            for entry in evidence
        ) + int(review_result in {"pass", "fail", "blocked"})
        next_change_source = diagnosis.get("next_action_summary")
        if not isinstance(next_change_source, str) or not next_change_source.strip():
            next_change_source = item.get("closure_reason")
        if isinstance(next_change_source, str) and len(next_change_source.strip()) > max_text_chars:
            text_truncated.append("next_change")
        if _clipped_fields(diagnosis, ("classification",), max_text_chars):
            text_truncated.append("failure_classification")
        failure_classification = _short(diagnosis.get("classification"), max_text_chars)
        if failure_classification is None and item.get("outcome") in {"fail", "blocked"}:
            failure_classification = "evidence_or_review_failure"
        projected.append(
            {
                "attempt_id": _short(item.get("attempt_id"), max_text_chars),
                "plan_revision": item.get("plan_revision"),
                "started_at": _short(item.get("started_at"), max_text_chars),
                "hypothesis": _short(item.get("hypothesis"), max_text_chars),
                "outcome": item.get("outcome"),
                "result": item.get("result"),
                "failure_classification": failure_classification,
                "key_conclusions": conclusions[:max_refs],
                "conclusions_total": conclusions_total,
                "conclusions_truncated": conclusions_total > len(conclusions[:max_refs]),
                "review_result": review_result,
                "review_ref": review.get("review_id") if review else None,
                "historical_not_valid_for_gate": True,
                "next_change": _short(diagnosis.get("next_action_summary"), max_text_chars)
                or _short(item.get("closure_reason"), max_text_chars),
                "evidence_refs": evidence_refs,
                "record_locations": record_locations,
                "source_refs": _refs(diagnosis.get("source_refs"), max_refs),
                "evidence_total": len(evidence),
                "evidence_truncated": len(evidence) > len(selected_evidence),
                **_refs_metadata(diagnosis.get("source_refs"), max_refs, "source_refs"),
                "text_truncated_fields": text_truncated,
            }
        )
    return {"items": projected, "total": len(history), "truncated": len(history) > len(selected)}


def progress_text_lines(state: dict[str, Any], projection: dict[str, Any]) -> list[str]:
    risk = state.get("risk", {})
    risk_details = risk.get("details", []) if isinstance(risk, dict) else []
    gaps = state.get("gaps", [])
    lines = [
        projection["display_line"],
        f"goal={safe_display_text(state.get('goal', ''))}",
        f"review={safe_display_text(projection.get('review_node', 'unknown'))}",
        f"review_result={safe_display_text(projection.get('review_result', 'unknown'))}",
        f"risk.details={' | '.join(safe_display_text(item) for item in risk_details) or 'none'}",
        f"gaps={' | '.join(safe_display_text(item) for item in gaps) or 'none'}",
    ]
    activity = projection.get("activity")
    if isinstance(activity, dict):
        lines.append(
            "activity="
            + safe_display_text(activity.get("text", ""))
            + " (source="
            + safe_display_text(activity.get("source", "unknown"))
            + "; recorded_at="
            + safe_display_text(activity.get("recorded_at", "unknown"))
            + ")"
        )
    for event in projection.get("events", []):
        if isinstance(event, dict):
            lines.append(
                f"event[{event.get('seq', '?')}] {safe_display_text(event.get('type', 'unknown'))}: "
                f"{safe_display_text(event.get('summary', ''))}"
            )
    if projection.get("events_truncated"):
        lines.append(
            f"events=truncated; dropped_before_seq={safe_display_text(projection.get('events_dropped_before_seq', 0))}"
        )
    return lines
