"""Pure verification-policy helpers used by the Guard state machine."""

from __future__ import annotations

from typing import Any, Callable, Iterable


ErrorFactory = Callable[[str, list[str]], Exception]


def normalize_repeat_policy(
    raw: Any, label: str, max_repeat_samples: int, error: ErrorFactory
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise error("VERIFICATION_REPEAT_POLICY_INVALID", [f"{label} must be an object"])
    mode = raw.get("mode")
    if mode == "once":
        if "required_samples" in raw and raw.get("required_samples") != 1:
            raise error("VERIFICATION_REPEAT_POLICY_INVALID", [f"{label}.once requires one sample"])
        return {"mode": "once", "required_samples": 1}
    if mode == "samples":
        required = raw.get("required_samples")
        if isinstance(required, bool) or not isinstance(required, int) or not 2 <= required <= max_repeat_samples:
            raise error(
                "VERIFICATION_REPEAT_POLICY_INVALID",
                [f"{label}.required_samples must be an integer from 2 to {max_repeat_samples}"],
            )
        return {"mode": "samples", "required_samples": required}
    raise error("VERIFICATION_REPEAT_POLICY_UNSUPPORTED", [f"{label}.mode={mode!r}"])


def normalize_execution_binding(value: Any, label: str, error: ErrorFactory) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"binding_digest"}:
        raise error("EXECUTION_BINDING_UNKNOWN", [f"{label} must be {{binding_digest: <sha256>}}"])
    digest = value.get("binding_digest")
    if not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise error("EXECUTION_BINDING_UNKNOWN", [f"{label}.binding_digest must be a lowercase SHA-256 digest"])
    return {"binding_digest": digest}


def execution_binding_reason(execution: dict[str, Any], current_digest: str) -> str | None:
    before = execution.get("before_binding")
    after = execution.get("after_binding")
    if not isinstance(before, dict) or not isinstance(after, dict):
        return "EXECUTION_BINDING_UNAVAILABLE"
    before_digest = before.get("binding_digest")
    after_digest = after.get("binding_digest")
    if before_digest != after_digest:
        return "EXECUTION_BINDING_CHANGED"
    if before_digest != current_digest or execution.get("binding_digest") != current_digest:
        return "EXECUTION_BINDING_STALE"
    return None


def execution_sample_ids(executions: Iterable[dict[str, Any]]) -> tuple[set[str], bool]:
    sample_ids: set[str] = set()
    missing_sample_id = False
    for execution in executions:
        sample_id = execution.get("sample_id")
        if isinstance(sample_id, str) and sample_id.strip():
            sample_ids.add(sample_id)
        else:
            missing_sample_id = True
    return sample_ids, missing_sample_id
