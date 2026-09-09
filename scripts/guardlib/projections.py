"""Bounded, non-gating projections for handoff and user-visible progress."""

from __future__ import annotations

from typing import Any


def safe_display_text(value: Any) -> str:
    """Render untrusted state text without allowing control sequences into text output."""
    text = value if isinstance(value, str) else str(value)
    rendered: list[str] = []
    for char in text:
        codepoint = ord(char)
        if codepoint < 0x20 or codepoint == 0x7F or 0xD800 <= codepoint <= 0xDFFF:
            rendered.append(f"\\u{codepoint:04x}")
        else:
            rendered.append(char)
    return "".join(rendered)


def compact_context_history(
    state: dict[str, Any], limit: int, max_refs: int, max_text_chars: int
) -> dict[str, Any]:
    history = state.get("loop", {}).get("attempt_history", [])
    selected = history[-limit:] if limit else []

    def short(value: Any, fallback: str | None = None) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return fallback
        return value.strip()[:max_text_chars]

    projected: list[dict[str, Any]] = []
    for item in selected:
        if not isinstance(item, dict):
            continue
        diagnosis = item.get("diagnosis") if isinstance(item.get("diagnosis"), dict) else {}
        evidence = item.get("evidence_snapshot") if isinstance(item.get("evidence_snapshot"), list) else []
        evidence_refs: list[str] = []
        record_locations: list[str] = []
        conclusions: list[str] = []
        for evidence_item in evidence[:max_refs]:
            if not isinstance(evidence_item, dict):
                continue
            evidence_id = evidence_item.get("evidence_id")
            if isinstance(evidence_id, str) and evidence_id.strip():
                evidence_refs.append(evidence_id)
            output_ref = evidence_item.get("output_ref")
            if isinstance(output_ref, str) and output_ref.strip():
                record_locations.append(output_ref[:max_text_chars])
            result = evidence_item.get("result")
            if result in {"pass", "fail", "blocked"}:
                conclusion = short(evidence_item.get("observed"))
                if conclusion:
                    conclusions.append(f"{evidence_id or 'evidence'}: {result}; {conclusion}")
                else:
                    conclusions.append(f"{evidence_id or 'evidence'}: {result}")
        review = item.get("review_snapshot") if isinstance(item.get("review_snapshot"), dict) else None
        if review is not None and review.get("result") in {"pass", "fail", "blocked"}:
            conclusions.append(f"review: {review['result']}")
        failure_classification = short(diagnosis.get("classification"))
        if failure_classification is None and item.get("outcome") in {"fail", "blocked"}:
            failure_classification = "evidence_or_review_failure"
        projected.append(
            {
                "attempt_id": short(item.get("attempt_id")),
                "plan_revision": item.get("plan_revision"),
                "started_at": short(item.get("started_at")),
                "hypothesis": short(item.get("hypothesis")),
                "outcome": item.get("outcome"),
                "result": item.get("result"),
                "failure_classification": failure_classification,
                "key_conclusions": conclusions[:max_refs],
                "next_change": short(diagnosis.get("next_action_summary")) or short(item.get("closure_reason")),
                "evidence_refs": evidence_refs,
                "record_locations": record_locations,
                "source_refs": list(diagnosis.get("source_refs", []))[:max_refs]
                if isinstance(diagnosis.get("source_refs"), list)
                else [],
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
