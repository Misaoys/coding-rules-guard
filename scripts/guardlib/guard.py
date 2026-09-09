#!/usr/bin/env python3
"""Deterministic phase gates for Coding Rules Guard."""

from __future__ import annotations

import argparse
import copy
import fnmatch
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from .projections import compact_context_history, progress_text_lines
from .verification import (
    execution_binding_reason as _execution_binding_reason,
    execution_sample_ids as _execution_sample_ids,
    execution_timestamp as _execution_timestamp,
    normalize_execution_binding as _normalize_execution_binding,
    normalize_repeat_policy as _normalize_repeat_policy,
)


PHASES = {"plan", "implement", "verify", "deliver", "complete"}
IMPACTS = {"no_known_impact", "known_impact", "unverified"}
RESULTS = {"pending", "pass", "pass_with_gaps", "blocked", "fail"}
LEVELS = {"source", "test", "browser", "installed", "host", "production"}
REVIEW_RESULTS = {"pass", "fail", "blocked"}
CURRENT_SCHEMA_VERSION = 5
LEGACY_SCHEMA_VERSIONS = {1, 2, 3, 4}
SUPPORTED_SCHEMA_VERSIONS = LEGACY_SCHEMA_VERSIONS | {CURRENT_SCHEMA_VERSION}
GIT_STATE_VERSIONS = {2, 3, 4, CURRENT_SCHEMA_VERSION}
BASELINE_FINGERPRINT_FORMAT = "content-index-v1"
GIT_BATCH_MAX_PATHS = 128
GIT_BATCH_MAX_BYTES = 8192
REWORK_WARN_AT = 2
REWORK_REPLAN_AT = 3
PLAN_RECORD_MAX_AGE = timedelta(hours=24)
DEFAULT_MAX_ATTEMPTS = 6
DEFAULT_MAX_REPLANS = 3
MAX_ATTEMPTS_RANGE = (1, 20)
MAX_REPLANS_RANGE = (0, 8)
MAX_HISTORY_LIMIT = 200
MAX_EVENTS = 20
MAX_ACTIVITY_CHARS = 120
MAX_EVENT_CHARS = 240
MAX_ATTEMPT_TEXT_CHARS = 512
MAX_COMMAND_CHARS = 2048
MAX_OBSERVED_CHARS = 2048
MAX_ACTIVE_EVIDENCE = 32
MAX_VERIFICATION_DEFINITIONS = 32
MAX_REPEAT_SAMPLES = 16
MAX_CONTEXT_EVIDENCE_REFS = 8
MAX_CONTEXT_TEXT_CHARS = 512
MODEL_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "model-profiles.json"
SECRET_NAME_PATTERNS = (".env", "*.pem", "*.p12", "*.pfx", "id_rsa", "id_ed25519")
SECRET_CONTENT = re.compile(
    r"(?:-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b|\bsk-[A-Za-z0-9]{20,}\b|\bAKIA[0-9A-Z]{16}\b)"
)

ATTEMPT_KINDS = {"implement", "verify_only"}
ATTEMPT_OUTCOMES = {"fail", "blocked", "superseded", "pass", "pass_with_gaps"}
DIAGNOSIS_CLASSIFICATIONS = {
    "implementation",
    "hypothesis_or_scope",
    "verification_contract",
    "environment",
    "input_data",
    "unknown",
}
DIAGNOSIS_INFORMATION_KINDS = {
    "code_change_planned",
    "contract_correction",
    "external_change",
    "scope_change",
    "none",
}
VERIFICATION_DECISIONS = {"reuse", "run", "diagnose", "unknown", "blocked"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_v5_state(state: dict[str, Any]) -> bool:
    return state.get("schema_version") == CURRENT_SCHEMA_VERSION


def is_git_state(state: dict[str, Any]) -> bool:
    return state.get("schema_version") in GIT_STATE_VERSIONS


def require_v5_write(state: dict[str, Any], action: str) -> None:
    if state.get("schema_version") != CURRENT_SCHEMA_VERSION:
        raise GateError("STATE_UPGRADE_REQUIRED", [f"{action} requires run-state schema v{CURRENT_SCHEMA_VERSION}"])


def bounded_text(value: Any, field: str, limit: int, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise GateError("INVALID_INPUT", [f"{field} must be a string"])
    value = value.strip()
    if required and not value:
        raise GateError("INVALID_INPUT", [f"{field} must be non-empty"])
    if len(value) > limit:
        raise GateError("INPUT_TOO_LARGE", [f"{field} exceeds {limit} Unicode characters"])
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class GateError(Exception):
    def __init__(self, code: str, details: Iterable[str]):
        super().__init__(code)
        self.code = code
        self.details = list(details)


def emit(payload: dict[str, Any], exit_code: int = 0) -> None:
    if os.environ.get("CODING_GUARD_COMPACT") == "1":
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    raise SystemExit(exit_code)


def normalize_path(value: str) -> str:
    normalized = value.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.rstrip("/")


def normalize_git_path(value: str) -> str:
    """Preserve Git path text; do not trim or interpret it as a scope glob."""
    return value


def path_exists_including_broken_symlink(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        return False
    return True


def ensure_external_artifact(path: Path, repo: Path, label: str) -> Path:
    """Require an artifact to be outside the repository by literal and resolved paths."""
    absolute = path.absolute()
    resolved = absolute.resolve(strict=False)
    repo_resolved = repo.resolve(strict=False)
    if path_is_within(absolute, repo) or path_is_within(resolved, repo_resolved):
        raise GateError(f"{label.upper()}_IN_REPOSITORY", [str(path), str(repo)])
    return absolute


def ensure_not_symlink(path: Path, label: str) -> None:
    if path.is_symlink():
        raise GateError(f"{label.upper()}_SYMLINK_FORBIDDEN", [str(path)])


def load_state(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GateError("STATE_NOT_FOUND", [str(path)]) from exc
    except json.JSONDecodeError as exc:
        raise GateError("STATE_INVALID_JSON", [str(exc)]) from exc
    validate_shape(state)
    return state


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise GateError("STATE_PATH_SYMLINK_FORBIDDEN", [str(path)])
    content = (json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary_name).replace(path)
        temporary_name = None
    except OSError as exc:
        raise GateError("STATE_SAVE_FAILED", [str(exc)]) from exc
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink()
            except OSError:
                pass


def read_markdown_content(args: argparse.Namespace) -> str:
    """Read non-empty UTF-8 Markdown from an explicit file or standard input."""
    if args.content_file is not None:
        try:
            content = args.content_file.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise GateError("PLAN_CONTENT_NOT_FOUND", [str(args.content_file)]) from exc
        except OSError as exc:
            raise GateError("PLAN_CONTENT_UNREADABLE", [str(exc)]) from exc
    else:
        content = sys.stdin.read()
    content = content.lstrip("\ufeff").strip()
    if not content:
        raise GateError("PLAN_CONTENT_EMPTY", ["Markdown content must not be empty"])
    return content + "\n"


def replace_markdown_file(path: Path, content: str) -> None:
    """Atomically replace an existing plan document without leaving a partial file."""
    ensure_not_symlink(path, "plan_file")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def command_write_plan(args: argparse.Namespace) -> None:
    content = read_markdown_content(args)
    try:
        args.file.parent.mkdir(parents=True, exist_ok=True)
        with args.file.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
    except FileExistsError as exc:
        raise GateError("PLAN_FILE_EXISTS", [str(args.file), "use prepend-requirement for a new requirement"]) from exc
    except OSError as exc:
        raise GateError("PLAN_FILE_UNWRITABLE", [str(exc)]) from exc
    emit({"ok": True, "action": "write-plan", "plan_file": str(args.file)})


def command_prepend_requirement(args: argparse.Namespace) -> None:
    content = read_markdown_content(args)
    ensure_not_symlink(args.file, "plan_file")
    try:
        existing = args.file.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise GateError("PLAN_FILE_NOT_FOUND", [str(args.file), "write the initial Plan first"]) from exc
    except OSError as exc:
        raise GateError("PLAN_FILE_UNREADABLE", [str(exc)]) from exc
    try:
        replace_markdown_file(args.file, content + "\n---\n\n" + existing)
    except OSError as exc:
        raise GateError("PLAN_FILE_UNWRITABLE", [str(exc)]) from exc
    emit({"ok": True, "action": "prepend-requirement", "plan_file": str(args.file)})


def load_model_config() -> dict[str, Any]:
    """Load the bundled role profiles without claiming that a process used them."""
    try:
        config = json.loads(MODEL_CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GateError("MODEL_CONFIG_MISSING", [str(MODEL_CONFIG_PATH)]) from exc
    except json.JSONDecodeError as exc:
        raise GateError("MODEL_CONFIG_INVALID", [str(exc)]) from exc

    if not isinstance(config, dict):
        raise GateError("MODEL_CONFIG_INVALID", ["config must be an object"])
    if config.get("schema_version") != 4:
        raise GateError("MODEL_CONFIG_INVALID", ["schema_version must be 4"])
    delegation = config.get("delegation")
    profiles = config.get("profiles")
    if not isinstance(delegation, dict) or not isinstance(profiles, dict):
        raise GateError("MODEL_CONFIG_INVALID", ["delegation and profiles are required objects"])

    # Executor is a fixed protected role. Planner and reviewer use the
    # current session's main model and must be recorded dynamically rather
    # than being silently substituted with fixed models.
    role_names = {
        "planner": delegation.get("planner_profile"),
        "executor": delegation.get("executor_profile"),
        "reviewer": delegation.get("reviewer_profile"),
    }
    errors: list[str] = []
    for role, name in role_names.items():
        if not isinstance(name, str) or not name.strip():
            errors.append(f"delegation.{role}_profile is required")

    normalized_profiles: dict[str, dict[str, str]] = {}
    for role, name in role_names.items():
        if not isinstance(name, str) or not name.strip():
            continue
        profile = profiles.get(name)
        if not isinstance(profile, dict):
            errors.append(f"{role} profile {name!r} is missing")
            continue
        source = profile.get("source")
        is_session_main = role in {"planner", "reviewer"} and source == "session_main"
        if source is not None and not is_session_main:
            errors.append(f"profile {name!r}.source is invalid for {role}")
            continue
        if is_session_main:
            normalized_profiles[name] = {"source": "session_main"}
            continue
        model = profile.get("model")
        effort = profile.get("reasoning_effort")
        if not isinstance(model, str) or not model.strip():
            errors.append(f"profile {name!r}.model is required")
        if not isinstance(effort, str) or not effort.strip():
            errors.append(f"profile {name!r}.reasoning_effort is required")
        if isinstance(model, str) and model.strip() and isinstance(effort, str) and effort.strip():
            normalized_profiles[name] = {"model": model, "reasoning_effort": effort}
    if errors:
        raise GateError("MODEL_CONFIG_INVALID", errors)
    return {
        "delegation": {
            "planner_profile": role_names["planner"],
            "executor_profile": role_names["executor"],
            "reviewer_profile": role_names["reviewer"],
        },
        "profiles": normalized_profiles,
    }


def configured_role(config: dict[str, Any], role: str) -> tuple[str, dict[str, str]]:
    name = config["delegation"][f"{role}_profile"]
    return name, config["profiles"][name]


def is_session_main_profile(profile: dict[str, str]) -> bool:
    return profile.get("source") == "session_main"


def compute_plan_fingerprint(state: dict[str, Any]) -> str:
    """Bind a recorded Plan to the run identity and all Plan-controlled fields."""
    if state.get("schema_version") != CURRENT_SCHEMA_VERSION:
        raise GateError("PLAN_STATE_UPGRADE_REQUIRED", ["plan fingerprints require schema v5"])
    baseline = state.get("git_baseline")
    if not isinstance(baseline, dict) or not isinstance(baseline.get("head"), str):
        raise GateError("PLAN_STATE_INVALID", ["git_baseline.head is required for the Plan fingerprint"])
    payload = {
        "run_id": state.get("run_id"),
        "repo": state.get("repo"),
        "baseline_head": baseline["head"],
        "mode": state.get("mode"),
        "goal": state.get("goal"),
        "write_scope": state.get("write_scope"),
        "risk": state.get("risk"),
        "delivery_required": state.get("delivery_required"),
        "plan_revision": state.get("plan_revision"),
    }
    if "plan_file" in state:
        payload["plan_file"] = state.get("plan_file")
        if state.get("plan_file") is not None:
            plan_path = Path(state["plan_file"])
            repo_path = Path(state["repo"])
            ensure_external_artifact(plan_path, repo_path, "plan_file")
            ensure_not_symlink(plan_path, "plan_file")
            try:
                plan_bytes = plan_path.read_bytes()
            except FileNotFoundError as exc:
                raise GateError("PLAN_FILE_MISSING", [str(plan_path)]) from exc
            except OSError as exc:
                raise GateError("PLAN_FILE_UNREADABLE", [str(exc)]) from exc
            if not plan_path.is_file():
                raise GateError("PLAN_FILE_INVALID", [f"Plan path is not a file: {plan_path}"])
            payload["plan_content_sha256"] = hashlib.sha256(plan_bytes).hexdigest()
    if state.get("schema_version") == CURRENT_SCHEMA_VERSION:
        registry = state.get("verification_registry")
        if not isinstance(registry, dict):
            raise GateError("PLAN_STATE_INVALID", ["verification_registry is required for a v5 Plan fingerprint"])
        payload["verification_spec_digest"] = sha256_json(
            {
                "plan_revision": registry.get("plan_revision"),
                "definitions": registry.get("definitions", []),
            }
        )
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def ensure_plan_record(state: dict[str, Any]) -> list[str]:
    """Require a current, configured, non-expired planner record before WRITE."""
    errors: list[str] = []
    if state.get("schema_version") != CURRENT_SCHEMA_VERSION:
        return ["PLAN_STATE_UPGRADE_REQUIRED: plan -> implement requires schema v5"]
    try:
        config = load_model_config()
        planner_name, planner = configured_role(config, "planner")
    except GateError as exc:
        return [f"planner configuration unavailable: {detail}" for detail in exc.details]

    if state.get("planner_profile") != planner_name:
        errors.append(
            f"planner profile mismatch: state={state.get('planner_profile')!r}, configured={planner_name!r}"
        )
    record = state.get("plan_record")
    if not isinstance(record, dict):
        return errors + ["a current planner record is required before implementation"]
    if record.get("profile") != planner_name:
        errors.append("plan record does not use the configured planner profile")
    if is_session_main_profile(planner):
        if not isinstance(record.get("model"), str) or not record["model"].strip():
            errors.append("session-main Plan record must identify the current main model")
        if not isinstance(record.get("reasoning_effort"), str) or not record["reasoning_effort"].strip():
            errors.append("session-main Plan record must identify the current main reasoning effort")
    else:
        if record.get("model") != planner["model"]:
            errors.append("plan record model does not match the configured planner profile")
        if record.get("reasoning_effort") != planner["reasoning_effort"]:
            errors.append("plan record reasoning effort does not match the configured planner profile")
    if not review_timestamp_is_valid(record.get("recorded_at")):
        errors.append("plan record must contain a timezone-aware recorded_at timestamp")
    else:
        try:
            recorded_at = datetime.fromisoformat(record["recorded_at"])
            now = datetime.now(recorded_at.tzinfo)
            if now - recorded_at > PLAN_RECORD_MAX_AGE:
                errors.append("PLAN_RECORD_EXPIRED: planner record is older than 24 hours")
        except (TypeError, ValueError):
            errors.append("plan record recorded_at is invalid")
    if record.get("plan_revision") != state.get("plan_revision"):
        errors.append("PLAN_RECORD_EXPIRED: plan record revision no longer matches the current Plan")
    try:
        current_plan_fingerprint = compute_plan_fingerprint(state)
    except GateError as exc:
        errors.extend(f"PLAN_STALE: {exc.code}: {detail}" for detail in exc.details)
    else:
        if record.get("plan_fingerprint") != current_plan_fingerprint:
            errors.append("PLAN_STALE: plan fingerprint no longer matches the current Plan")
    return errors


def ensure_plan_file_binding(state: dict[str, Any]) -> None:
    """Reject completion if the state-bound Plan file changed after Plan recording."""
    if "plan_file" not in state:
        return
    record = state.get("plan_record")
    if not isinstance(record, dict):
        raise GateError("PLAN_FILE_STALE", ["a recorded Plan is required to delete its Plan file"])
    if record.get("plan_fingerprint") != compute_plan_fingerprint(state):
        raise GateError("PLAN_FILE_STALE", ["the bound Plan file no longer matches the recorded Plan"])


def delete_plan_file(state: dict[str, Any]) -> str | None:
    plan_file = state.get("plan_file")
    if plan_file is None:
        return None
    path = Path(plan_file)
    try:
        if path.exists():
            if not path.is_file():
                raise GateError("PLAN_FILE_DELETE_FAILED", [f"Plan path is not a file: {path}"])
            path.unlink()
    except OSError as exc:
        raise GateError("PLAN_FILE_DELETE_FAILED", [str(exc)]) from exc
    return str(path)


def review_is_required(state: dict[str, Any]) -> bool:
    # Legacy v1/v2 write states remain review-gated even though they predate
    # the explicit v3 review fields.
    return bool(state.get("review_required", state.get("write_scope")))


def review_timestamp_is_valid(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        timestamp = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return False
    return timestamp.utcoffset() is not None


def ensure_review(state: dict[str, Any], snapshot: dict[str, Any] | None = None) -> list[str]:
    if not review_is_required(state):
        return []
    errors: list[str] = []
    try:
        config = load_model_config()
        reviewer_name, reviewer = configured_role(config, "reviewer")
    except GateError as exc:
        return [f"review configuration unavailable: {detail}" for detail in exc.details]

    if state.get("reviewer_profile") != reviewer_name:
        errors.append(
            f"reviewer profile mismatch: state={state.get('reviewer_profile')!r}, configured={reviewer_name!r}"
        )
    review = state.get("review")
    if not isinstance(review, dict):
        return errors + ["a reviewer record is required"]
    if review.get("result") != "pass":
        errors.append("independent reviewer result must be pass")
    if state.get("schema_version") == CURRENT_SCHEMA_VERSION:
        active = state.get("loop", {}).get("active_attempt")
        if not isinstance(active, dict) or review.get("attempt_id") != active.get("attempt_id"):
            errors.append("review record does not belong to the active attempt")
    if review.get("profile") != reviewer_name:
        errors.append("review record does not use the configured reviewer profile")
    if is_session_main_profile(reviewer):
        if not isinstance(review.get("model"), str) or not review["model"].strip():
            errors.append("session-main review record must identify the current main model")
        if not isinstance(review.get("reasoning_effort"), str) or not review["reasoning_effort"].strip():
            errors.append("session-main review record must identify the current main reasoning effort")
    else:
        if review.get("model") != reviewer["model"]:
            errors.append("review record model does not match the configured reviewer profile")
        if review.get("reasoning_effort") != reviewer["reasoning_effort"]:
            errors.append("review record reasoning effort does not match the configured reviewer profile")
    if not review_timestamp_is_valid(review.get("reviewed_at")):
        errors.append("review record must contain a timezone-aware reviewed_at timestamp")
    try:
        current_task_fingerprint = compute_task_fingerprint(state, snapshot=snapshot)
    except GateError as exc:
        errors.extend(f"REVIEW_STALE: {exc.code}: {detail}" for detail in exc.details)
    else:
        if review.get("task_fingerprint") != current_task_fingerprint:
            errors.append("REVIEW_STALE: task fingerprint no longer matches the current Git task state")
    audit = state.get("delivery_audit")
    if state.get("phase") == "deliver" and isinstance(audit, dict) and audit.get("passed"):
        if audit.get("task_fingerprint") != review.get("task_fingerprint"):
            errors.append("REVIEW_STALE: delivery audit fingerprint does not match the reviewer record")
    return errors


def ensure_executor_profile(state: dict[str, Any]) -> list[str]:
    if state.get("schema_version") != CURRENT_SCHEMA_VERSION or not state.get("write_scope"):
        return []
    try:
        config = load_model_config()
        executor_name, _ = configured_role(config, "executor")
    except GateError as exc:
        return [f"executor configuration unavailable: {detail}" for detail in exc.details]
    if state.get("executor_profile") != executor_name:
        return [
            f"executor profile mismatch: state={state.get('executor_profile')!r}, configured={executor_name!r}"
        ]
    return []


def validate_v5_fields(state: dict[str, Any]) -> list[str]:
    """Validate the bounded loop and non-gating projections of a v5 state."""
    errors: list[str] = []
    loop = state.get("loop")
    if not isinstance(loop, dict):
        return ["loop is required for schema v5"]
    if loop.get("format_version") != 1:
        errors.append("loop.format_version must be 1")
    policy = loop.get("policy")
    if not isinstance(policy, dict):
        errors.append("loop.policy is required")
    else:
        for key, bounds in (("max_attempts", MAX_ATTEMPTS_RANGE), ("max_replans", MAX_REPLANS_RANGE)):
            value = policy.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or not bounds[0] <= value <= bounds[1]:
                errors.append(f"loop.policy.{key} must be within {bounds[0]}..{bounds[1]}")
    for key in ("attempt_count", "replan_count"):
        value = loop.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            errors.append(f"loop.{key} must be a non-negative integer")
    attempt_history = loop.get("attempt_history")
    if not isinstance(attempt_history, list):
        errors.append("loop.attempt_history must be an array")
        attempt_history = []
    plan_history = loop.get("plan_history")
    if not isinstance(plan_history, list):
        errors.append("loop.plan_history must be an array")
        plan_history = []
    active = loop.get("active_attempt")
    if active is not None:
        if not isinstance(active, dict):
            errors.append("loop.active_attempt must be an object or null")
        else:
            required = {"attempt_id", "plan_revision", "kind", "started_at", "hypothesis", "start_fingerprint"}
            missing = sorted(required - active.keys())
            if missing:
                errors.append("active attempt missing fields: " + ", ".join(missing))
            if active.get("kind") not in ATTEMPT_KINDS:
                errors.append("loop.active_attempt.kind is invalid")
            if not review_timestamp_is_valid(active.get("started_at")):
                errors.append("loop.active_attempt.started_at must be timezone-aware")
            if active.get("plan_revision") != state.get("plan_revision"):
                errors.append("loop.active_attempt.plan_revision must match state plan_revision")
            for field, limit in (("attempt_id", 128), ("hypothesis", MAX_ATTEMPT_TEXT_CHARS), ("start_fingerprint", 256)):
                value = active.get(field)
                if not isinstance(value, str) or not value.strip() or len(value) > limit:
                    errors.append(f"loop.active_attempt.{field} is invalid")
            for field in ("evidence_refs",):
                value = active.get(field, [])
                if not isinstance(value, list) or len(value) > MAX_ACTIVE_EVIDENCE:
                    errors.append(f"loop.active_attempt.{field} is invalid")
            source_attempt_id = active.get("source_attempt_id")
            if source_attempt_id is not None and (
                not isinstance(source_attempt_id, str) or not source_attempt_id.strip()
            ):
                errors.append("loop.active_attempt.source_attempt_id must be null or a non-empty string")
    history_ids: set[str] = set()
    for index, item in enumerate(attempt_history):
        if not isinstance(item, dict):
            errors.append(f"loop.attempt_history[{index}] must be an object")
            continue
        attempt_id = item.get("attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id.strip() or attempt_id in history_ids:
            errors.append(f"loop.attempt_history[{index}].attempt_id is missing or duplicated")
        else:
            history_ids.add(attempt_id)
        if item.get("outcome") not in ATTEMPT_OUTCOMES:
            errors.append(f"loop.attempt_history[{index}].outcome is invalid")
        if item.get("historical_not_valid_for_gate") is not True:
            errors.append(f"loop.attempt_history[{index}] must be historical-only")
        for field in ("plan_revision", "kind", "hypothesis", "start_fingerprint", "closure_reason", "evidence_snapshot", "review_snapshot"):
            if field not in item:
                errors.append(f"loop.attempt_history[{index}].{field} is required")
        if item.get("kind") not in ATTEMPT_KINDS:
            errors.append(f"loop.attempt_history[{index}].kind is invalid")
        if not isinstance(item.get("plan_revision"), int) or item.get("plan_revision", 0) < 1:
            errors.append(f"loop.attempt_history[{index}].plan_revision is invalid")
        if not isinstance(item.get("hypothesis"), str) or not item["hypothesis"].strip():
            errors.append(f"loop.attempt_history[{index}].hypothesis is required")
        if not isinstance(item.get("start_fingerprint"), str) or not item["start_fingerprint"].strip():
            errors.append(f"loop.attempt_history[{index}].start_fingerprint is required")
        if not isinstance(item.get("evidence_snapshot"), list):
            errors.append(f"loop.attempt_history[{index}].evidence_snapshot must be an array")
        for field in ("started_at", "closed_at"):
            if not review_timestamp_is_valid(item.get(field)):
                errors.append(f"loop.attempt_history[{index}].{field} must be timezone-aware")
    if active is not None and isinstance(active, dict) and active.get("attempt_id") in history_ids:
        errors.append("active attempt id is already archived")
    if isinstance(active, dict) and active.get("source_attempt_id") is not None:
        if active.get("source_attempt_id") not in history_ids:
            errors.append("active attempt source_attempt_id must reference archived history")
        if active.get("source_attempt_id") == active.get("attempt_id"):
            errors.append("active attempt cannot source itself")
    if isinstance(loop.get("attempt_count"), int) and loop.get("attempt_count") != len(attempt_history) + (1 if active else 0):
        errors.append("loop.attempt_count does not match active and historical attempts")
    if isinstance(loop.get("replan_count"), int) and loop.get("replan_count") != len(plan_history):
        errors.append("loop.replan_count does not match plan_history")
    if isinstance(policy, dict) and isinstance(loop.get("attempt_count"), int) and loop["attempt_count"] > policy.get("max_attempts", 0):
        errors.append("loop.attempt_count exceeds the attempt budget")
    if isinstance(policy, dict) and isinstance(loop.get("replan_count"), int) and loop["replan_count"] > policy.get("max_replans", 0):
        errors.append("loop.replan_count exceeds the replan budget")
    if state.get("phase") in {"implement", "verify", "deliver"} and not isinstance(active, dict):
        errors.append("an active attempt is required in the current phase")
    if state.get("phase") in {"plan", "complete"} and active is not None:
        errors.append("plan and complete phases cannot retain an active attempt")
    plan_revisions: set[int] = set()
    previous_revision = 1
    for index, item in enumerate(plan_history):
        if not isinstance(item, dict):
            errors.append(f"loop.plan_history[{index}] must be an object")
            continue
        revision = item.get("new_revision")
        if not isinstance(revision, int) or revision < 2 or revision in plan_revisions:
            errors.append(f"loop.plan_history[{index}].new_revision is invalid or duplicated")
        else:
            plan_revisions.add(revision)
        if not review_timestamp_is_valid(item.get("recorded_at")):
            errors.append(f"loop.plan_history[{index}].recorded_at must be timezone-aware")
        if not isinstance(item.get("reason"), str) or not item["reason"].strip():
            errors.append(f"loop.plan_history[{index}].reason is required")
        if not isinstance(item.get("old_revision"), int) or item.get("old_revision", 0) < 1:
            errors.append(f"loop.plan_history[{index}].old_revision is invalid")
        if not isinstance(item.get("attempt_id"), (str, type(None))):
            errors.append(f"loop.plan_history[{index}].attempt_id is invalid")
        if item.get("old_plan_fingerprint") is not None and not isinstance(item.get("old_plan_fingerprint"), str):
            errors.append(f"loop.plan_history[{index}].old_plan_fingerprint is invalid")
        if not isinstance(item.get("old_goal"), str) or not item["old_goal"].strip():
            errors.append(f"loop.plan_history[{index}].old_goal is required")
        if item.get("new_revision") != item.get("old_revision", 0) + 1:
            errors.append(f"loop.plan_history[{index}].new_revision must follow old_revision")
        if item.get("old_revision") != previous_revision:
            errors.append(f"loop.plan_history[{index}].old_revision is not the next revision in history")
        if isinstance(item.get("new_revision"), int):
            previous_revision = item["new_revision"]
    if plan_history and isinstance(state.get("plan_revision"), int):
        revisions = [item.get("new_revision") for item in plan_history if isinstance(item, dict)]
        if revisions and revisions[-1] != state["plan_revision"]:
            errors.append("loop.plan_history must end at state plan_revision")

    telemetry = state.get("telemetry")
    if not isinstance(telemetry, dict) or telemetry.get("format_version") != 1:
        errors.append("telemetry.format_version must be 1")
    else:
        if not isinstance(telemetry.get("event_seq"), int) or telemetry["event_seq"] < 0:
            errors.append("telemetry.event_seq must be a non-negative integer")
        if not isinstance(telemetry.get("events_dropped_before_seq"), int) or telemetry["events_dropped_before_seq"] < 0:
            errors.append("telemetry.events_dropped_before_seq must be a non-negative integer")
        events = telemetry.get("events")
        if not isinstance(events, list) or len(events) > MAX_EVENTS:
            errors.append("telemetry.events must contain at most 20 events")
        else:
            for index, event in enumerate(events):
                if not isinstance(event, dict) or not isinstance(event.get("seq"), int) or not isinstance(event.get("summary"), str):
                    errors.append(f"telemetry.events[{index}] is invalid")
                elif len(event["summary"]) > MAX_EVENT_CHARS:
                    errors.append(f"telemetry.events[{index}].summary is too long")
                elif event["seq"] > telemetry.get("event_seq", 0) or (index and event["seq"] <= events[index - 1].get("seq", 0)):
                    errors.append(f"telemetry.events[{index}].seq is not monotonic")
                else:
                    if event["seq"] < 1 or not review_timestamp_is_valid(event.get("time")):
                        errors.append(f"telemetry.events[{index}] has an invalid timestamp or sequence")
                    if not isinstance(event.get("type"), str) or not event["type"].strip():
                        errors.append(f"telemetry.events[{index}].type is invalid")
                    if event.get("phase") not in PHASES:
                        errors.append(f"telemetry.events[{index}].phase is invalid")
                    if not isinstance(event.get("plan_revision"), int) or event["plan_revision"] < 1:
                        errors.append(f"telemetry.events[{index}].plan_revision is invalid")
                    if not isinstance(event.get("source_refs"), list) or len(event.get("source_refs", [])) > 8 or any(
                        not isinstance(ref, str) or not ref.strip() for ref in event.get("source_refs", [])
                    ):
                        errors.append(f"telemetry.events[{index}].source_refs is invalid")
        activity = telemetry.get("activity")
        if activity is not None and (
            not isinstance(activity, dict)
            or not isinstance(activity.get("text"), str)
            or len(activity.get("text", "")) > MAX_ACTIVITY_CHARS
        ):
            errors.append("telemetry.activity is invalid")
        elif isinstance(activity, dict):
            if not review_timestamp_is_valid(activity.get("recorded_at")) or activity.get("source") != "agent_report":
                errors.append("telemetry.activity has an invalid source or timestamp")
            if activity.get("phase") not in PHASES or activity.get("plan_revision") != state.get("plan_revision"):
                errors.append("telemetry.activity is not bound to the current phase and Plan revision")
            active_id = active.get("attempt_id") if isinstance(active, dict) else None
            if activity.get("attempt_id") != active_id:
                errors.append("telemetry.activity is not bound to the current attempt")

    registry = state.get("verification_registry")
    if not isinstance(registry, dict) or registry.get("format_version") != 1:
        errors.append("verification_registry.format_version must be 1")
    else:
        if registry.get("plan_revision") != state.get("plan_revision"):
            errors.append("verification_registry.plan_revision must match state plan_revision")
        definitions = registry.get("definitions")
        if not isinstance(definitions, list) or len(definitions) > MAX_VERIFICATION_DEFINITIONS:
            errors.append("verification_registry.definitions is invalid or too large")
        else:
            definition_ids: set[str] = set()
            for index, definition in enumerate(definitions):
                if not isinstance(definition, dict) or not isinstance(definition.get("check_id"), str) or not definition["check_id"].strip():
                    errors.append(f"verification_registry.definitions[{index}] is invalid")
                    continue
                if definition["check_id"] in definition_ids:
                    errors.append("verification_registry contains duplicate check_id")
                definition_ids.add(definition["check_id"])
                try:
                    normalize_repeat_policy(definition.get("repeat_policy"), f"{definition['check_id']}.repeat_policy")
                except GateError as exc:
                    errors.extend(f"verification_registry.definitions[{index}]: {detail}" for detail in exc.details)
        refs = registry.get("active_refs")
        if not isinstance(refs, list):
            errors.append("verification_registry.active_refs must be an array")
    evidence = state.get("evidence", [])
    if isinstance(evidence, list) and len(evidence) > MAX_ACTIVE_EVIDENCE:
        errors.append("v5 evidence exceeds the active evidence limit")
    evidence_ids: set[str] = set()
    historical_evidence_ids: set[str] = set()
    historical_review_ids: set[str] = set()
    for archived in attempt_history:
        if not isinstance(archived, dict):
            continue
        for item in archived.get("evidence_snapshot", []):
            if isinstance(item, dict) and isinstance(item.get("evidence_id"), str):
                historical_evidence_ids.add(item["evidence_id"])
        review_snapshot = archived.get("review_snapshot")
        if isinstance(review_snapshot, dict) and isinstance(review_snapshot.get("review_id"), str):
            historical_review_ids.add(review_snapshot["review_id"])
    for index, item in enumerate(evidence if isinstance(evidence, list) else []):
        if not isinstance(item, dict):
            continue
        evidence_id = item.get("evidence_id")
        if not isinstance(evidence_id, str) or not evidence_id.strip() or evidence_id in evidence_ids:
            errors.append(f"evidence[{index}].evidence_id is missing or duplicated")
        else:
            evidence_ids.add(evidence_id)
            if evidence_id in historical_evidence_ids and state.get("phase") != "complete":
                errors.append(f"evidence[{index}].evidence_id was reused from history")
        if not isinstance(item.get("attempt_id"), str) or not item["attempt_id"].strip():
            errors.append(f"evidence[{index}].attempt_id is required")
        else:
            expected_attempt_ids = set()
            if isinstance(active, dict):
                expected_attempt_ids.add(active.get("attempt_id"))
            elif state.get("phase") == "complete" and attempt_history:
                expected_attempt_ids.add(attempt_history[-1].get("attempt_id"))
            if expected_attempt_ids and item.get("attempt_id") not in expected_attempt_ids:
                errors.append(f"evidence[{index}].attempt_id is not current")
        if not review_timestamp_is_valid(item.get("recorded_at")):
            errors.append(f"evidence[{index}].recorded_at must be timezone-aware")
        for field, limit in (("command", MAX_COMMAND_CHARS), ("observed", MAX_OBSERVED_CHARS)):
            if isinstance(item.get(field), str) and len(item[field]) > limit:
                errors.append(f"evidence[{index}].{field} is too long")
    review = state.get("review")
    if review is not None and isinstance(review, dict):
        for field in ("review_id", "attempt_id"):
            if not isinstance(review.get(field), str) or not review[field].strip():
                errors.append(f"review.{field} is required for v5")
        if review.get("review_id") in historical_review_ids and state.get("phase") != "complete":
            errors.append("review.review_id was reused from history")
        if isinstance(active, dict) and review.get("attempt_id") != active.get("attempt_id"):
            errors.append("review must belong to the active attempt")
        elif active is None and state.get("phase") == "complete" and attempt_history:
            if review.get("attempt_id") != attempt_history[-1].get("attempt_id"):
                errors.append("completed review must belong to the final attempt")
    if isinstance(active, dict):
        current_evidence_ids = {item.get("evidence_id") for item in evidence if isinstance(item, dict)}
        refs = active.get("evidence_refs", [])
        if isinstance(refs, list) and any(ref not in current_evidence_ids for ref in refs):
            errors.append("active attempt evidence_refs contains a non-current evidence ID")
        if active.get("review_ref") != (review.get("review_id") if isinstance(review, dict) else None):
            errors.append("active attempt review_ref does not match the current review")
        diagnosis = active.get("diagnosis")
        current_review_id = review.get("review_id") if isinstance(review, dict) else None
        if diagnosis is not None:
            if not isinstance(diagnosis, dict) or diagnosis.get("attempt_id") != active.get("attempt_id"):
                errors.append("active attempt diagnosis does not belong to the active attempt")
            elif any(
                ref not in current_evidence_ids and ref != current_review_id
                for ref in diagnosis.get("source_refs", [])
            ):
                errors.append("diagnosis references a non-current source")
            elif diagnosis.get("plan_revision") != state.get("plan_revision") or not isinstance(
                diagnosis.get("source_snapshot_digest"), str
            ):
                errors.append("active attempt diagnosis is missing its current binding")
    registry = state.get("verification_registry")
    if isinstance(registry, dict) and isinstance(registry.get("active_refs"), list):
        definition_ids = {
            item.get("check_id") for item in registry.get("definitions", []) if isinstance(item, dict)
        }
        seen_refs: set[tuple[Any, Any, Any]] = set()
        evidence_by_id = {
            item.get("evidence_id"): item
            for item in evidence
            if isinstance(item, dict) and isinstance(item.get("evidence_id"), str)
        }
        current_ids = set(evidence_by_id)
        indexed_evidence: set[tuple[Any, Any]] = set()
        for index, ref in enumerate(registry["active_refs"]):
            if not isinstance(ref, dict):
                errors.append(f"verification_registry.active_refs[{index}] is invalid")
                continue
            key = (ref.get("check_id"), ref.get("evidence_id"), ref.get("attempt_id"))
            if key in seen_refs:
                errors.append("verification_registry.active_refs contains a duplicate reference")
            seen_refs.add(key)
            if ref.get("check_id") not in definition_ids or ref.get("evidence_id") not in current_ids:
                errors.append(f"verification_registry.active_refs[{index}] references an unknown definition or evidence")
                continue
            evidence_item = evidence_by_id.get(ref.get("evidence_id"))
            indexed_evidence.add((ref.get("check_id"), ref.get("evidence_id")))
            if evidence_item.get("check_id") != ref.get("check_id"):
                errors.append(f"verification_registry.active_refs[{index}] check_id does not match evidence")
            if evidence_item.get("attempt_id") != ref.get("attempt_id"):
                errors.append(f"verification_registry.active_refs[{index}] attempt_id does not match evidence")
            execution = evidence_item.get("execution") if isinstance(evidence_item.get("execution"), dict) else None
            execution_id = execution.get("execution_id") if execution else None
            if ref.get("execution_id") != execution_id:
                errors.append(f"verification_registry.active_refs[{index}] execution_id does not match evidence")
            if isinstance(active, dict) and ref.get("attempt_id") != active.get("attempt_id"):
                errors.append(f"verification_registry.active_refs[{index}] is not bound to the active attempt")
        for item in evidence if isinstance(evidence, list) else []:
            if not isinstance(item, dict) or not item.get("check_id"):
                continue
            pair = (item.get("check_id"), item.get("evidence_id"))
            if pair not in indexed_evidence:
                errors.append("verification_registry.active_refs is missing a current checked evidence reference")
                break
    return errors


def validate_shape(state: dict[str, Any]) -> None:
    if not isinstance(state, dict):
        raise GateError("STATE_SCHEMA_INVALID", ["state must be an object"])
    errors: list[str] = []
    schema_version = state.get("schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        errors.append("schema_version must be 1, 2, 3, 4, or 5")
    if state.get("mode") not in {"FAST", "FULL"}:
        errors.append("mode must be FAST or FULL")
    run_id = state.get("run_id")
    if not isinstance(run_id, str) or not run_id.strip():
        errors.append("run_id must be a non-empty string")
    if state.get("phase") not in PHASES:
        errors.append("phase is invalid")
    if not str(state.get("goal", "")).strip():
        errors.append("goal is required")
    risk = state.get("risk")
    if not isinstance(risk, dict) or risk.get("impact") not in IMPACTS:
        errors.append("risk.impact is invalid")
    elif not isinstance(risk.get("details"), list):
        errors.append("risk.details must be an array")
    if state.get("result") not in RESULTS:
        errors.append("result is invalid")
    rework_count = state.get("rework_count", 0)
    if not isinstance(rework_count, int) or isinstance(rework_count, bool) or rework_count < 0:
        errors.append("rework_count must be a non-negative integer")
    rework_reason = state.get("last_rework_reason")
    if rework_reason is not None and (not isinstance(rework_reason, str) or not rework_reason.strip()):
        errors.append("last_rework_reason must be null or a non-empty string")
    replan_reason = state.get("last_replan_reason")
    if replan_reason is not None and (not isinstance(replan_reason, str) or not replan_reason.strip()):
        errors.append("last_replan_reason must be null or a non-empty string")
    rework_streak = state.get("rework_streak", 0)
    if not isinstance(rework_streak, int) or isinstance(rework_streak, bool) or rework_streak < 0:
        errors.append("rework_streak must be a non-negative integer")
    plan_revision = state.get("plan_revision", 1)
    if not isinstance(plan_revision, int) or isinstance(plan_revision, bool) or plan_revision < 1:
        errors.append("plan_revision must be a positive integer")
    if "plan_file" in state:
        plan_file = state.get("plan_file")
        if plan_file is not None and (not isinstance(plan_file, str) or not Path(plan_file).is_absolute()):
            errors.append("plan_file must be null or an absolute path")
    if not isinstance(state.get("replan_required", False), bool):
        errors.append("replan_required must be a boolean")
    if schema_version in GIT_STATE_VERSIONS:
        repo = state.get("repo")
        baseline = state.get("git_baseline")
        if not isinstance(repo, str) or not Path(repo).is_absolute():
            errors.append("repo must be an absolute path")
        if not isinstance(baseline, dict) or not isinstance(baseline.get("head"), str):
            errors.append("git_baseline.head is required")
        elif not isinstance(baseline.get("files"), dict):
            errors.append("git_baseline.files must be an object")
        elif schema_version in {3, 4, 5} and not isinstance(baseline.get("content_files"), dict):
            errors.append("git_baseline.content_files must be an object")
        elif schema_version in {3, 4, 5} and not isinstance(baseline.get("index_files"), dict):
            errors.append("git_baseline.index_files must be an object")
        fingerprint_format = baseline.get("fingerprint_format")
        if fingerprint_format is not None and (not isinstance(fingerprint_format, str) or not fingerprint_format.strip()):
            errors.append("git_baseline.fingerprint_format must be null or a non-empty string")
        if state.get("change_detection") != "git_baseline":
            errors.append("change_detection must be git_baseline")
        authorization = state.get("gap_authorization")
        if authorization is not None:
            required_auth = {"authorization_id", "authorized_by", "authorized_at", "reason"}
            if not isinstance(authorization, dict) or not required_auth.issubset(authorization):
                errors.append("gap_authorization is incomplete")
            else:
                if not isinstance(authorization["authorization_id"], str) or not authorization["authorization_id"].strip():
                    errors.append("gap_authorization.authorization_id is invalid")
                if not isinstance(authorization["authorized_by"], str) or not re.fullmatch(
                    r"(?:user|host):[^\s].*", authorization["authorized_by"]
                ):
                    errors.append("gap_authorization.authorized_by is invalid")
                if not isinstance(authorization["reason"], str) or not authorization["reason"].strip():
                    errors.append("gap_authorization.reason is invalid")
                try:
                    authorized_at = datetime.fromisoformat(authorization["authorized_at"])
                    if authorized_at.utcoffset() is None:
                        raise ValueError("timezone required")
                except (TypeError, ValueError):
                    errors.append("gap_authorization.authorized_at is invalid")
        if bool(state.get("gaps_authorized")) != (authorization is not None):
            errors.append("gaps_authorized must match gap_authorization")
    if schema_version in {3, 4, 5}:
        required_review_fields = {"review_required", "executor_profile", "reviewer_profile", "review"}
        missing_review_fields = sorted(required_review_fields - state.keys())
        if missing_review_fields:
            errors.append("missing review fields: " + ", ".join(missing_review_fields))
        review_required = state.get("review_required")
        if not isinstance(review_required, bool):
            errors.append("review_required must be a boolean")
        executor_profile = state.get("executor_profile")
        if executor_profile is not None and (not isinstance(executor_profile, str) or not executor_profile.strip()):
            errors.append("executor_profile must be null or a non-empty string")
        reviewer_profile = state.get("reviewer_profile")
        if not isinstance(reviewer_profile, str) or not reviewer_profile.strip():
            errors.append("reviewer_profile must be a non-empty string")
        if isinstance(review_required, bool) and review_required != bool(state.get("write_scope")):
            errors.append("review_required must match whether write_scope is non-empty")
        if review_required and executor_profile is None:
            errors.append("executor_profile is required for write tasks")
        review = state.get("review")
        if review is not None:
            if not isinstance(review, dict):
                errors.append("review must be an object or null")
            else:
                required_review_record = {
                    "profile",
                    "model",
                    "reasoning_effort",
                    "result",
                    "reviewed_at",
                    "observed",
                    "task_fingerprint",
                }
                missing_record_fields = sorted(required_review_record - review.keys())
                if missing_record_fields:
                    errors.append("missing review record fields: " + ", ".join(missing_record_fields))
                for field in ("profile", "model", "reasoning_effort", "observed", "reviewed_at"):
                    if field in review and (not isinstance(review[field], str) or not review[field].strip()):
                        errors.append(f"review.{field} must be a non-empty string")
                if review.get("result") not in REVIEW_RESULTS:
                    errors.append("review.result is invalid")
                if "reviewed_at" in review and not review_timestamp_is_valid(review.get("reviewed_at")):
                    errors.append("review.reviewed_at must be timezone-aware")
    if schema_version == CURRENT_SCHEMA_VERSION:
        required_plan_fields = {"planner_profile", "plan_record"}
        missing_plan_fields = sorted(required_plan_fields - state.keys())
        if missing_plan_fields:
            errors.append("missing plan fields: " + ", ".join(missing_plan_fields))
        planner_profile = state.get("planner_profile")
        if not isinstance(planner_profile, str) or not planner_profile.strip():
            errors.append("planner_profile must be a non-empty string")
        plan_record = state.get("plan_record")
        if plan_record is not None:
            if not isinstance(plan_record, dict):
                errors.append("plan_record must be an object or null")
            else:
                required_plan_record = {
                    "profile",
                    "model",
                    "reasoning_effort",
                    "recorded_at",
                    "plan_revision",
                    "plan_fingerprint",
                }
                missing_plan_record = sorted(required_plan_record - plan_record.keys())
                if missing_plan_record:
                    errors.append("missing plan record fields: " + ", ".join(missing_plan_record))
                for field in ("profile", "model", "reasoning_effort", "recorded_at", "plan_fingerprint"):
                    if field in plan_record and (
                        not isinstance(plan_record[field], str) or not plan_record[field].strip()
                    ):
                        errors.append(f"plan_record.{field} must be a non-empty string")
                if "recorded_at" in plan_record and not review_timestamp_is_valid(plan_record.get("recorded_at")):
                    errors.append("plan_record.recorded_at must be timezone-aware")
                plan_record_revision = plan_record.get("plan_revision")
                if not isinstance(plan_record_revision, int) or isinstance(plan_record_revision, bool) or plan_record_revision < 1:
                    errors.append("plan_record.plan_revision must be a positive integer")
    for key in ("write_scope", "changed_files", "evidence", "gaps"):
        if not isinstance(state.get(key), list):
            errors.append(f"{key} must be an array")
    for key in ("write_scope", "changed_files", "gaps"):
        values = state.get(key)
        if isinstance(values, list) and any(not isinstance(item, str) or not item.strip() for item in values):
            errors.append(f"{key} must contain non-empty strings")
    evidence = state.get("evidence")
    if isinstance(evidence, list):
        required = {"kind", "entry", "command", "observed", "level", "result"}
        for index, item in enumerate(evidence):
            if not isinstance(item, dict) or not required.issubset(item):
                errors.append(f"evidence[{index}] is incomplete")
                continue
            if schema_version == CURRENT_SCHEMA_VERSION and (
                not isinstance(item.get("worktree_fingerprint"), str) or not item["worktree_fingerprint"].strip()
            ):
                errors.append(f"evidence[{index}] is missing worktree_fingerprint")
            if item["kind"] not in {"success", "boundary"} or item["level"] not in LEVELS:
                errors.append(f"evidence[{index}] has an invalid kind or level")
            if item["result"] not in {"pass", "fail", "blocked"}:
                errors.append(f"evidence[{index}].result is invalid")
            if any(not isinstance(item[field], str) or not item[field].strip() for field in ("entry", "command", "observed")):
                errors.append(f"evidence[{index}] text fields must be non-empty")
    if schema_version == CURRENT_SCHEMA_VERSION:
        errors.extend(validate_v5_fields(state))
    if errors:
        raise GateError("STATE_SCHEMA_INVALID", errors)


def in_scope(path: str, scopes: Iterable[str]) -> bool:
    candidate = normalize_git_path(path)
    for raw_scope in scopes:
        scope = normalize_path(raw_scope)
        if not scope:
            continue
        # First honor exact/directory paths so literal '*' and '[' filenames are
        # never reinterpreted as glob syntax.
        if candidate == scope or candidate.startswith(scope + "/"):
            return True
        has_glob = "*" in scope or "?" in scope or ("[" in scope and "]" in scope)
        if has_glob and fnmatch.fnmatchcase(candidate, scope):
            return True
    return False


def ensure_evidence(state: dict[str, Any], snapshot: dict[str, Any] | None = None) -> list[str]:
    errors: list[str] = []
    evidence = state["evidence"]
    if not any(item.get("kind") == "success" and item.get("result") == "pass" for item in evidence):
        errors.append("a passing success-path evidence record is required")
    if not any(item.get("kind") == "boundary" for item in evidence):
        errors.append("a boundary-path evidence record is required")
    if any(item.get("result") == "fail" for item in evidence):
        errors.append("failed evidence prevents a passing result")
    if state["result"] == "pass" and any(item.get("result") != "pass" for item in evidence):
        errors.append("pass requires every evidence record to pass")
    if state["result"] == "pass" and state["gaps"]:
        errors.append("pass cannot contain gaps; use pass_with_gaps")
    if state["result"] == "pass_with_gaps" and not any(item.get("result") == "blocked" for item in evidence):
        errors.append("pass_with_gaps requires a blocked evidence boundary")
    if state.get("schema_version") == CURRENT_SCHEMA_VERSION:
        active = state.get("loop", {}).get("active_attempt")
        active_id = active.get("attempt_id") if isinstance(active, dict) else None
        if active_id is None:
            errors.append("active attempt is required for v5 evidence")
        for item in evidence:
            if item.get("attempt_id") != active_id:
                errors.append("evidence does not belong to the active attempt")
                break
    if state.get("schema_version") in GIT_STATE_VERSIONS and evidence:
        fingerprints = {item.get("worktree_fingerprint") for item in evidence}
        if None in fingerprints or "" in fingerprints:
            errors.append("EVIDENCE_STATE_UPGRADE_REQUIRED: evidence lacks worktree_fingerprint")
        else:
            try:
                current_fingerprint = compute_evidence_fingerprint(state, snapshot)
            except GateError as exc:
                errors.extend(f"EVIDENCE_STALE: {exc.code}: {detail}" for detail in exc.details)
            else:
                for item in evidence:
                    if item.get("worktree_fingerprint") != current_fingerprint:
                        errors.append("EVIDENCE_STALE: evidence no longer matches the current worktree")
                        break
    return errors


def check_transition(state: dict[str, Any], target: str, snapshot: dict[str, Any] | None = None) -> None:
    current = state["phase"]
    allowed = {
        "plan": {"implement"},
        "implement": {"verify"},
        "verify": {"deliver", "complete"},
        "deliver": {"complete"},
        "complete": set(),
    }
    if target not in allowed[current]:
        raise GateError("INVALID_TRANSITION", [f"{current} -> {target}"])

    errors: list[str] = []
    if target == "implement":
        if not state["write_scope"]:
            errors.append("write_scope is empty")
        if state["risk"]["impact"] == "unverified":
            errors.append("risk impact must be assessed before implementation")
        if state.get("replan_required", False):
            errors.append("revise-plan is required before implementation")
        errors.extend(ensure_plan_record(state))
        errors.extend(ensure_executor_profile(state))
    elif target == "verify":
        if not state["changed_files"]:
            errors.append("changed_files is empty")
        outside = [path for path in state["changed_files"] if not in_scope(path, state["write_scope"])]
        if outside:
            errors.append("out-of-scope changes: " + ", ".join(outside))
        if state.get("schema_version") in GIT_STATE_VERSIONS:
            try:
                ensure_current_scope(state, snapshot=snapshot)
            except GateError as exc:
                errors.extend(f"{exc.code}: {detail}" for detail in exc.details)
        if state.get("schema_version") == CURRENT_SCHEMA_VERSION and not isinstance(state.get("loop", {}).get("active_attempt"), dict):
            errors.append("an active attempt is required before verify")
    elif target in {"deliver", "complete"}:
        if state.get("schema_version") in GIT_STATE_VERSIONS:
            try:
                ensure_current_scope(state, snapshot=snapshot)
            except GateError as exc:
                errors.extend(f"{exc.code}: {detail}" for detail in exc.details)
        errors.extend(ensure_evidence(state, snapshot))
        errors.extend(ensure_review(state, snapshot=snapshot))
        if state.get("schema_version") == CURRENT_SCHEMA_VERSION and not isinstance(state.get("loop", {}).get("active_attempt"), dict):
            errors.append("an active attempt is required before completion")
        if state["result"] not in {"pass", "pass_with_gaps"}:
            errors.append("verification result must be pass or pass_with_gaps")
        if state["result"] == "pass_with_gaps" and (not state["gaps"] or not state["gaps_authorized"]):
            errors.append("pass_with_gaps requires listed and explicitly authorized gaps")
        if state.get("schema_version") in GIT_STATE_VERSIONS and state["result"] == "pass_with_gaps":
            if not isinstance(state.get("gap_authorization"), dict):
                errors.append("pass_with_gaps requires a separate gap authorization record")
        if state["risk"]["impact"] == "unverified":
            errors.append("impact remains unverified")
        if target == "deliver" and not state["delivery_required"]:
            errors.append("delivery was not requested")
        if target == "complete" and state["delivery_required"]:
            audit = state.get("delivery_audit") or {}
            if current != "deliver" or not audit.get("passed"):
                errors.append("required delivery audit has not passed")
    if errors:
        raise GateError("TRANSITION_BLOCKED", errors)


def git_lines(repo: Path, *args: str) -> list[str]:
    result = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if result.returncode != 0:
        raise GateError("GIT_COMMAND_FAILED", [result.stderr.strip() or "git command failed"])
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def git_bytes(repo: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "--no-optional-locks", "-C", str(repo), *args], capture_output=True)
    if result.returncode != 0:
        details = result.stderr.decode("utf-8", errors="replace").strip() or "git command failed"
        raise GateError("GIT_COMMAND_FAILED", [details])
    return result.stdout


def decode_git_path(value: bytes) -> str:
    return value.decode("utf-8", errors="surrogateescape")


def git_paths(repo: Path, *args: str) -> list[str]:
    """Read NUL-delimited Git paths without line or whitespace normalization."""
    output = git_bytes(repo, *args)
    return [normalize_git_path(decode_git_path(item)) for item in output.split(b"\0") if item]


def parse_status_paths(output: bytes) -> list[str]:
    """Parse porcelain-v1 -z records without trimming or interpreting path text."""
    paths: list[str] = []
    offset = 0
    while offset < len(output):
        if len(output) - offset < 3 or output[offset + 2] != 0x20:
            raise GateError("GIT_STATUS_INVALID", ["porcelain status record is shorter than XY + space"])
        end = output.find(b"\0", offset + 3)
        if end < 0:
            raise GateError("GIT_STATUS_INVALID", ["porcelain status output is not NUL terminated"])
        paths.append(normalize_git_path(decode_git_path(output[offset + 3 : end])))
        offset = end + 1
    return paths


def git_status_paths(repo: Path) -> list[str]:
    output = git_bytes(
        repo,
        "--no-optional-locks",
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--no-renames",
        "--ignore-submodules=none",
    )
    conflicts = git_bytes(repo, "--no-optional-locks", "ls-files", "-u", "-z")
    if conflicts:
        conflict_paths = sorted(
            {
                decode_git_path(record.split(b"\t", 1)[1])
                for record in conflicts.split(b"\0")
                if record and b"\t" in record
            }
        )
        raise GateError("GIT_INDEX_CONFLICT", conflict_paths)
    return sorted(set(parse_status_paths(output)))


def literal_pathspec(rel: str) -> str:
    return f":(literal){rel}"


def path_batches(paths: Iterable[str]) -> Iterable[list[str]]:
    batch: list[str] = []
    size = 0
    for rel in sorted(set(paths)):
        encoded_size = len(literal_pathspec(rel).encode("utf-8", errors="surrogatepass")) + 1
        if encoded_size > GIT_BATCH_MAX_BYTES:
            raise GateError("GIT_PATH_TOO_LONG", [rel, str(GIT_BATCH_MAX_BYTES)])
        if batch and (len(batch) >= GIT_BATCH_MAX_PATHS or size + encoded_size > GIT_BATCH_MAX_BYTES):
            yield batch
            batch = []
            size = 0
        batch.append(rel)
        size += encoded_size
    if batch:
        yield batch


def git_index_identities(repo: Path, paths: Iterable[str]) -> dict[str, str]:
    requested = sorted(set(paths))
    identities: dict[str, list[str]] = {rel: [] for rel in requested}
    for batch in path_batches(requested):
        output = git_bytes(
            repo,
            "ls-files",
            "-s",
            "-z",
            "--",
            *(literal_pathspec(rel) for rel in batch),
        )
        for record in output.split(b"\0"):
            if not record:
                continue
            try:
                header, raw_path = record.split(b"\t", 1)
            except ValueError as exc:
                raise GateError("GIT_INDEX_INVALID", [decode_git_path(record)]) from exc
            fields = header.split()
            if len(fields) < 3:
                raise GateError("GIT_INDEX_INVALID", [decode_git_path(record)])
            rel = decode_git_path(raw_path)
            mode = fields[0].decode()
            object_id = fields[1].decode()
            stage = fields[2].decode()
            if stage != "0":
                raise GateError("GIT_INDEX_CONFLICT", [rel])
            identities.setdefault(rel, []).append(
                f"{mode}:{object_id}"
            )
    return {rel: "|".join(sorted(values)) if values else "missing" for rel, values in identities.items()}


def git_tree_identities(repo: Path, treeish: str, paths: Iterable[str]) -> dict[str, str]:
    requested = sorted(set(paths))
    identities: dict[str, list[str]] = {rel: [] for rel in requested}
    for batch in path_batches(requested):
        output = git_bytes(
            repo,
            "ls-tree",
            "-z",
            treeish,
            "--",
            *(literal_pathspec(rel) for rel in batch),
        )
        for record in output.split(b"\0"):
            if not record:
                continue
            try:
                header, raw_path = record.split(b"\t", 1)
            except ValueError as exc:
                raise GateError("GIT_TREE_INVALID", [decode_git_path(record)]) from exc
            fields = header.split()
            if len(fields) < 3:
                raise GateError("GIT_TREE_INVALID", [decode_git_path(record)])
            rel = decode_git_path(raw_path)
            identities.setdefault(rel, []).append(f"{fields[0].decode()}:{fields[2].decode()}")
    return {rel: "|".join(sorted(values)) if values else "missing" for rel, values in identities.items()}


def git_collection_snapshot(repo: Path, treeish: str, paths: Iterable[str]) -> dict[str, Any]:
    """Collect one bounded, non-persistent Git/content snapshot for a command."""
    requested = sorted(set(paths))
    index = git_index_identities(repo, requested)
    tree = git_tree_identities(repo, treeish, requested)
    for rel in requested:
        if index.get(rel, "missing").startswith("160000:") or tree.get(rel, "missing").startswith("160000:"):
            raise GateError("GIT_UNSUPPORTED_OBJECT", [f"gitlink is not supported for path {rel}"])
    content = {rel: filesystem_path_fingerprint(repo, rel) for rel in requested}
    states = {
        rel: path_state_fingerprint(
            repo,
            rel,
            index.get(rel, "missing"),
            tree.get(rel, "missing"),
            content_fingerprint=content[rel],
        )
        for rel in requested
    }
    return {"treeish": treeish, "paths": requested, "index": index, "tree": tree, "content": content, "state": states}


def snapshot_status_paths(repo: Path, snapshot: dict[str, Any] | None) -> list[str]:
    if snapshot is None:
        return actual_git_files(repo)
    if "status_paths" not in snapshot:
        snapshot["status_paths"] = actual_git_files(repo)
    return list(snapshot["status_paths"])


def snapshot_git_collection(
    repo: Path, treeish: str, paths: Iterable[str], snapshot: dict[str, Any] | None
) -> dict[str, Any]:
    requested = set(paths)
    if snapshot is None:
        return git_collection_snapshot(repo, treeish, requested)
    cached = snapshot.get("git_collection")
    if isinstance(cached, dict) and cached.get("treeish") == treeish and requested.issubset(cached.get("paths", [])):
        return cached
    union = requested
    if isinstance(cached, dict) and cached.get("treeish") == treeish:
        union |= set(cached.get("paths", []))
    cached = git_collection_snapshot(repo, treeish, union)
    snapshot["git_collection"] = cached
    return cached


def git_state_fingerprints(repo: Path, treeish: str, paths: Iterable[str], snapshot: dict[str, Any] | None = None) -> dict[str, str]:
    requested = sorted(set(paths))
    if snapshot is not None and snapshot.get("treeish") == treeish and set(requested).issubset(snapshot.get("state", {})):
        return {rel: snapshot["state"][rel] for rel in requested}
    index = git_index_identities(repo, requested)
    tree = git_tree_identities(repo, treeish, requested)
    for rel in requested:
        if index.get(rel, "missing").startswith("160000:") or tree.get(rel, "missing").startswith("160000:"):
            raise GateError("GIT_UNSUPPORTED_OBJECT", [f"gitlink is not supported for path {rel}"])
    return {
        rel: path_state_fingerprint(repo, rel, index.get(rel, "missing"), tree.get(rel, "missing"))
        for rel in requested
    }


def actual_git_files(repo: Path) -> list[str]:
    return git_status_paths(repo)


def git_repo_root(path: Path) -> Path:
    resolved = path.resolve()
    roots = git_lines(resolved, "rev-parse", "--show-toplevel")
    if not roots:
        raise GateError("GIT_REPO_REQUIRED", [str(resolved)])
    return Path(roots[0]).resolve()


def filesystem_path_fingerprint(repo: Path, rel: str) -> str:
    digest = hashlib.sha256()
    full_path = repo / Path(rel)
    if full_path.is_symlink():
        digest.update(b"symlink\0")
        digest.update(str(full_path.readlink()).encode("utf-8", errors="surrogatepass"))
    elif not os.path.lexists(os.fspath(full_path)):
        digest.update(b"missing\0")
    elif stat.S_ISREG(full_path.stat().st_mode):
        digest.update(b"file\0")
        digest.update(b"executable\0" + (b"1" if full_path.stat().st_mode & 0o111 else b"0"))
        with full_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    else:
        raise GateError("GIT_UNSUPPORTED_OBJECT", [f"unsupported filesystem object: {rel}"])
    return digest.hexdigest()


def path_state_fingerprint(
    repo: Path,
    rel: str,
    index_identity: str,
    tree_identity: str,
    *,
    content_fingerprint: str | None = None,
) -> str:
    digest = hashlib.sha256()
    digest.update(b"content-index-v1\0path\0")
    digest.update(rel.encode("utf-8", errors="surrogatepass"))
    digest.update(b"\0worktree\0")
    digest.update((content_fingerprint or filesystem_path_fingerprint(repo, rel)).encode("ascii"))
    digest.update(b"\0index\0")
    digest.update(index_identity.encode("utf-8"))
    digest.update(b"\0tree\0")
    digest.update(tree_identity.encode("utf-8"))
    return digest.hexdigest()


def git_path_fingerprint(repo: Path, rel: str) -> str:
    digest = hashlib.sha256()
    digest.update(filesystem_path_fingerprint(repo, rel).encode("ascii"))
    digest.update(b"\0combined-diff\0")
    digest.update(git_bytes(repo, "diff", "--binary", "--no-renames", "HEAD", "--", literal_pathspec(rel)))
    digest.update(b"\0index-diff\0")
    digest.update(
        git_bytes(repo, "diff", "--cached", "--binary", "--no-renames", "HEAD", "--", literal_pathspec(rel))
    )
    return digest.hexdigest()


def capture_git_baseline(repo: Path) -> dict[str, Any]:
    head = git_lines(repo, "rev-parse", "HEAD")
    if not head:
        raise GateError("GIT_HEAD_REQUIRED", [str(repo)])
    dirty = actual_git_files(repo)
    staged = set(git_paths(repo, "diff", "--cached", "--name-only", "--no-renames", "-z", "HEAD"))
    candidates = sorted(set(dirty) | staged)
    snapshot = git_collection_snapshot(repo, head[0], candidates)
    return {
        "head": head[0],
        "fingerprint_format": BASELINE_FINGERPRINT_FORMAT,
        "files": {path: snapshot["state"][path] for path in dirty},
        "content_files": {path: snapshot["content"][path] for path in dirty},
        "index_files": {path: snapshot["index"].get(path, "missing") for path in staged},
    }


def ensure_baseline_fingerprint_format(state: dict[str, Any]) -> None:
    fingerprint_format = state["git_baseline"].get("fingerprint_format")
    if fingerprint_format is not None and fingerprint_format != BASELINE_FINGERPRINT_FORMAT:
        raise GateError("GIT_BASELINE_FORMAT_UNKNOWN", [str(fingerprint_format)])


def task_git_files(state: dict[str, Any], snapshot: dict[str, Any] | None = None) -> list[str]:
    repo = Path(state["repo"])
    current_head = git_lines(repo, "rev-parse", "HEAD")
    baseline = state["git_baseline"]
    if not current_head or current_head[0] != baseline["head"]:
        raise GateError("GIT_BASELINE_MOVED", ["Git HEAD changed after init; start a new run state"])
    current = set(snapshot_status_paths(repo, snapshot))
    candidates = sorted(current | set(baseline["files"]))
    fingerprint_format = baseline.get("fingerprint_format")
    if fingerprint_format is None:
        # Old v2/v3/v4 baselines remain readable through an explicit slow path;
        # never interpret their old digest as the new content-index format.
        changed = [
            path
            for path in candidates
            if git_path_fingerprint(repo, path) != baseline["files"].get(path)
        ]
        return sorted(changed)
    if fingerprint_format != BASELINE_FINGERPRINT_FORMAT:
        raise GateError("GIT_BASELINE_FORMAT_UNKNOWN", [str(fingerprint_format)])
    collection = snapshot_git_collection(repo, baseline["head"], candidates, snapshot)
    current_fingerprints = collection["state"]
    changed = [path for path in candidates if current_fingerprints[path] != baseline["files"].get(path)]
    return sorted(changed)


def git_is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(repo), "merge-base", "--is-ancestor", ancestor, descendant],
        capture_output=True,
    )
    if result.returncode not in {0, 1}:
        raise GateError("GIT_COMMAND_FAILED", [result.stderr.decode("utf-8", errors="replace").strip()])
    return result.returncode == 0


def task_delta_files(state: dict[str, Any], snapshot: dict[str, Any] | None = None) -> list[str]:
    schema_version = state.get("schema_version")
    if schema_version == 1:
        raise GateError("REVIEW_STATE_UPGRADE_REQUIRED", ["v1 state has no Git baseline for review binding"])
    repo = Path(state["repo"])
    baseline = state["git_baseline"]
    ensure_baseline_fingerprint_format(state)
    current_head_lines = git_lines(repo, "rev-parse", "HEAD")
    if not current_head_lines:
        raise GateError("GIT_HEAD_REQUIRED", [str(repo)])
    current_head = current_head_lines[0]
    if current_head == baseline["head"]:
        return task_git_files(state, snapshot)
    if schema_version == 2:
        raise GateError(
            "REVIEW_STATE_UPGRADE_REQUIRED",
            ["v2 state cannot bind review across a changed Git HEAD; rebuild the run with schema v5"],
        )
    if not git_is_ancestor(repo, baseline["head"], current_head):
        raise GateError("GIT_BASELINE_MOVED", ["baseline HEAD is not an ancestor of current HEAD"])

    baseline_content = baseline.get("content_files")
    if not isinstance(baseline_content, dict):
        raise GateError("REVIEW_STATE_UPGRADE_REQUIRED", ["baseline content fingerprints are missing; rebuild with schema v5"])
    residual = post_commit_residual_files(state, snapshot)
    return sorted(set(committed_task_files(state, current_head, snapshot)) | set(residual))


def post_commit_residual_files(state: dict[str, Any], snapshot: dict[str, Any] | None = None) -> list[str]:
    """Find new index/worktree changes after the task's reviewed commit.

    A pre-existing dirty path is exempt only when both its worktree content and
    index identity still match the init baseline.  Comparing content alone
    would miss the hidden-index regression where HEAD and worktree look equal
    while the index contains a new object.
    """
    repo = Path(state["repo"])
    baseline = state["git_baseline"]
    baseline_content = baseline.get("content_files")
    if not isinstance(baseline_content, dict):
        raise GateError("REVIEW_STATE_UPGRADE_REQUIRED", ["baseline content fingerprints are missing; rebuild with schema v5"])
    current_paths = snapshot_status_paths(repo, snapshot)
    if not current_paths:
        return []
    collection = snapshot_git_collection(repo, baseline["head"], current_paths, snapshot)
    current_index = collection["index"]
    baseline_tree = collection["tree"]
    baseline_index = baseline.get("index_files", {})
    residual: list[str] = []
    for rel in current_paths:
        content_same = rel in baseline_content and collection["content"].get(rel) == baseline_content[rel]
        expected_index = baseline_index.get(rel, baseline_tree.get(rel, "missing"))
        index_same = current_index.get(rel, "missing") == expected_index
        if not (content_same and index_same):
            residual.append(rel)
    return sorted(residual)


def git_index_identity(repo: Path, rel: str) -> str:
    return git_index_identities(repo, [rel]).get(rel, "missing")


def git_tree_identity(repo: Path, treeish: str, rel: str) -> str:
    return git_tree_identities(repo, treeish, [rel]).get(rel, "missing")


def task_staged_files(state: dict[str, Any], snapshot: dict[str, Any] | None = None) -> list[str]:
    repo = Path(state["repo"])
    baseline = state["git_baseline"]
    ensure_baseline_fingerprint_format(state)
    current_head = git_lines(repo, "rev-parse", "HEAD")
    if not current_head or current_head[0] != baseline["head"]:
        raise GateError("GIT_BASELINE_MOVED", ["staged review requires the baseline HEAD"])
    baseline_index = baseline.get("index_files", {})
    current_staged = set(git_paths(repo, "diff", "--cached", "--name-only", "--no-renames", "-z", "HEAD"))
    candidates = current_staged | set(baseline_index)
    collection = snapshot_git_collection(repo, baseline["head"], candidates, snapshot)
    current_index = collection["index"]
    baseline_tree = collection["tree"]
    changed: list[str] = []
    for rel in candidates:
        baseline_identity = (
            baseline_index[rel]
            if rel in baseline_index
            else baseline_tree.get(rel, "missing")
        )
        if current_index.get(rel, "missing") != baseline_identity:
            changed.append(rel)
    return sorted(changed)


def committed_task_files(state: dict[str, Any], current_head: str, snapshot: dict[str, Any] | None = None) -> list[str]:
    repo = Path(state["repo"])
    baseline = state["git_baseline"]
    ensure_baseline_fingerprint_format(state)
    committed = set(git_paths(repo, "diff", "--name-only", "--no-renames", "-z", baseline["head"], current_head))
    residual = set(post_commit_residual_files(state, snapshot))
    baseline_index = baseline["index_files"]
    changed_files = set(state["changed_files"])
    tree_identities = snapshot_git_collection(repo, current_head, committed, snapshot)["tree"] if committed else {}
    # Mirror the pre-commit baseline exemption without hiding modified or out-of-scope paths.
    candidates = committed | residual
    return sorted(
        rel
        for rel in candidates
        if not (
            rel not in residual
            and rel not in changed_files
            and rel in baseline_index
            and in_scope(rel, state["write_scope"])
            and tree_identities.get(rel, "missing") == baseline_index[rel]
        )
    )


def fingerprint_identities(baseline_head: str, identities: list[tuple[str, str]]) -> str:
    digest = hashlib.sha256()
    digest.update(baseline_head.encode("utf-8"))
    for rel, identity in identities:
        digest.update(b"\0path\0")
        digest.update(rel.encode("utf-8", errors="surrogatepass"))
        digest.update(b"\0identity\0")
        digest.update(identity.encode("utf-8"))
    return digest.hexdigest()


def compute_task_fingerprint(
    state: dict[str, Any], *, allow_unregistered: bool = False, snapshot: dict[str, Any] | None = None
) -> str:
    if state.get("schema_version") not in GIT_STATE_VERSIONS:
        raise GateError("REVIEW_STATE_UPGRADE_REQUIRED", ["state lacks a reviewable Git baseline"])
    repo = Path(state["repo"])
    baseline_head = state["git_baseline"]["head"]
    current_head_lines = git_lines(repo, "rev-parse", "HEAD")
    if not current_head_lines:
        raise GateError("GIT_HEAD_REQUIRED", [str(repo)])
    current_head = current_head_lines[0]

    if state.get("delivery_required"):
        if current_head == baseline_head:
            staged = task_staged_files(state, snapshot)
            expected = sorted(state.get("changed_files", []))
            if staged != expected and not allow_unregistered:
                raise GateError(
                    "REVIEW_STAGE_MISMATCH",
                    [f"staged task paths {staged!r} do not match detected task paths {expected!r}"],
                )
            if allow_unregistered:
                identities_map = snapshot_git_collection(repo, baseline_head, staged, snapshot)["index"]
                identities = [(rel, identities_map.get(rel, "missing")) for rel in staged]
                return fingerprint_identities(baseline_head, identities)
            unstaged = set(git_paths(repo, "diff", "--name-only", "--no-renames", "-z"))
            split = sorted(unstaged & set(expected))
            if split:
                raise GateError("REVIEW_STAGE_MISMATCH", ["index/worktree split: " + ", ".join(split)])
            identities_map = git_index_identities(repo, staged)
            identities = [(rel, identities_map.get(rel, "missing")) for rel in staged]
            return fingerprint_identities(baseline_head, identities)

        if state.get("schema_version") == 2:
            raise GateError(
                "REVIEW_STATE_UPGRADE_REQUIRED",
                ["v2 state cannot bind review to a final commit tree; rebuild with schema v5"],
            )
        if not git_is_ancestor(repo, baseline_head, current_head):
            raise GateError("GIT_BASELINE_MOVED", ["baseline HEAD is not an ancestor of current HEAD"])
        residual = post_commit_residual_files(state, snapshot)
        if residual:
            raise GateError("UNREVIEWED_RESIDUAL_CHANGES", residual)
        committed = committed_task_files(state, current_head, snapshot)
        tree_identities = snapshot_git_collection(repo, current_head, committed, snapshot)["tree"] if committed else {}
        identities = [(rel, tree_identities.get(rel, "missing")) for rel in committed]
        return fingerprint_identities(baseline_head, identities)

    staged = task_staged_files(state, snapshot)
    if staged:
        raise GateError(
            "REVIEW_STAGE_MISMATCH",
            ["staged task paths are not part of the reviewed worktree: " + ", ".join(staged)],
        )
    delta = task_delta_files(state, snapshot)
    if snapshot is not None and delta:
        collection = snapshot_git_collection(repo, baseline_head, delta, snapshot)
        identities = [(rel, collection["content"][rel]) for rel in delta]
    else:
        identities = [(rel, filesystem_path_fingerprint(repo, rel)) for rel in delta]
    return fingerprint_identities(baseline_head, identities)


def current_task_paths(state: dict[str, Any], snapshot: dict[str, Any] | None = None) -> list[str]:
    if state.get("schema_version") not in GIT_STATE_VERSIONS:
        return list(state.get("changed_files", []))
    repo = Path(state["repo"])
    current_head = git_lines(repo, "rev-parse", "HEAD")
    if not current_head:
        raise GateError("GIT_HEAD_REQUIRED", [str(repo)])
    if current_head[0] == state["git_baseline"]["head"]:
        return task_git_files(state, snapshot)
    return task_delta_files(state, snapshot)


def ensure_current_scope(
    state: dict[str, Any], *, require_registered: bool = True, snapshot: dict[str, Any] | None = None
) -> list[str]:
    """Recompute task paths at every release point instead of trusting old state."""
    actual = current_task_paths(state, snapshot)
    outside = [path for path in actual if not in_scope(path, state["write_scope"])]
    if outside:
        raise GateError("OUT_OF_SCOPE_CHANGE", ["out-of-scope git changes: " + ", ".join(outside)])
    if require_registered:
        registered = sorted(set(state.get("changed_files", [])))
        if sorted(actual) != registered:
            missing = sorted(set(actual) - set(registered))
            stale = sorted(set(registered) - set(actual))
            details = [
                f"registered task paths {registered!r} do not match current Git paths {sorted(actual)!r}"
            ]
            if missing:
                details.append("unregistered git changes: " + ", ".join(missing))
            if stale:
                details.append("registered paths no longer changed: " + ", ".join(stale))
            raise GateError("CHANGES_NOT_REGISTERED", details)
    return actual


def compute_evidence_fingerprint(state: dict[str, Any], snapshot: dict[str, Any] | None = None) -> str:
    """Bind evidence to worktree bytes/mode/link targets and deletion identities."""
    if state.get("schema_version") not in GIT_STATE_VERSIONS:
        raise GateError("EVIDENCE_STATE_UPGRADE_REQUIRED", ["evidence binding requires a Git baseline"])
    actual = ensure_current_scope(state, snapshot=snapshot)
    repo = Path(state["repo"])
    baseline_head = state["git_baseline"]["head"]
    if snapshot is not None and actual:
        tree_identities = snapshot_git_collection(repo, baseline_head, actual, snapshot)["tree"]
    else:
        tree_identities = git_tree_identities(repo, baseline_head, actual)
    identities = [
        (
            rel,
            "worktree=" + filesystem_path_fingerprint(repo, rel) + ";baseline=" + tree_identities.get(rel, "missing"),
        )
        for rel in actual
    ]
    return fingerprint_identities(baseline_head, identities)


def secret_findings(repo: Path, files: Iterable[str]) -> list[str]:
    findings: list[str] = []
    for rel in files:
        name = PurePosixPath(rel).name
        if any(fnmatch.fnmatchcase(name, pattern) for pattern in SECRET_NAME_PATTERNS):
            findings.append(f"sensitive filename: {rel}")
            continue
        full_path = repo / Path(rel)
        if not full_path.is_file() or full_path.stat().st_size > 2_000_000:
            continue
        try:
            content = full_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if SECRET_CONTENT.search(content):
            findings.append(f"secret-like content: {rel}")
    return findings


def status_limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("limit must be an integer from 1 to 200") from exc
    if not 1 <= parsed <= 200:
        raise argparse.ArgumentTypeError("limit must be an integer from 1 to 200")
    return parsed


def event_limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("event-limit must be an integer from 0 to 20") from exc
    if not 0 <= parsed <= MAX_EVENTS:
        raise argparse.ArgumentTypeError("event-limit must be an integer from 0 to 20")
    return parsed


def budget_limit(value: str, label: str, bounds: tuple[int, int]) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{label} must be an integer from {bounds[0]} to {bounds[1]}") from exc
    if not bounds[0] <= parsed <= bounds[1]:
        raise argparse.ArgumentTypeError(f"{label} must be an integer from {bounds[0]} to {bounds[1]}")
    return parsed


def max_attempts_arg(value: str) -> int:
    return budget_limit(value, "max-attempts", MAX_ATTEMPTS_RANGE)


def max_replans_arg(value: str) -> int:
    return budget_limit(value, "max-replans", MAX_REPLANS_RANGE)


def bounded_paths(values: Iterable[str], limit: int) -> dict[str, Any]:
    paths = list(values)
    return {
        "items": paths[:limit],
        "total": len(paths),
        "truncated": len(paths) > limit,
    }


def append_event(state: dict[str, Any], event_type: str, summary: str, source_refs: Iterable[str] = ()) -> None:
    telemetry = state["telemetry"]
    summary = bounded_text(summary, "event.summary", MAX_EVENT_CHARS)
    telemetry["event_seq"] += 1
    event = {
        "seq": telemetry["event_seq"],
        "time": utc_now(),
        "type": event_type,
        "attempt_id": (state.get("loop", {}).get("active_attempt") or {}).get("attempt_id"),
        "plan_revision": state.get("plan_revision"),
        "phase": state.get("phase"),
        "summary": summary,
        "source_refs": list(source_refs)[:8],
    }
    telemetry["events"].append(event)
    if len(telemetry["events"]) > MAX_EVENTS:
        dropped = telemetry["events"].pop(0)
        telemetry["events_dropped_before_seq"] = max(
            int(telemetry.get("events_dropped_before_seq", 0)), int(dropped.get("seq", 0))
        )


def display_line(state: dict[str, Any]) -> str:
    loop = state.get("loop", {})
    policy = loop.get("policy", {})
    attempt = loop.get("attempt_count", 0)
    max_attempts = policy.get("max_attempts", "?")
    replans = loop.get("replan_count", 0)
    max_replans = policy.get("max_replans", "?")
    result = state.get("result", "unknown")
    return (
        f"phase={state.get('phase', 'unknown')}; result={result}; "
        f"attempts={attempt}/{max_attempts}; replans={replans}/{max_replans}; "
        f"plan_revision={state.get('plan_revision', '?')}"
    )


def parse_json_value(raw: str, label: str) -> Any:
    try:
        if raw.startswith("@"):
            value = Path(raw[1:]).read_text(encoding="utf-8")
        else:
            value = raw
        return json.loads(value)
    except (OSError, json.JSONDecodeError) as exc:
        raise GateError("INVALID_JSON", [f"{label}: {exc}"]) from exc


def ensure_attempt_budget(state: dict[str, Any]) -> None:
    loop = state["loop"]
    if loop["attempt_count"] >= loop["policy"]["max_attempts"]:
        raise GateError(
            "LOOP_BUDGET_EXHAUSTED",
            ["attempts", f"{loop['attempt_count']}/{loop['policy']['max_attempts']}", "no new attempt may be started"],
        )


def failure_signatures(state: dict[str, Any]) -> set[str]:
    """Return bounded, comparable failure observations without treating them as proof of progress."""
    signatures: set[str] = set()
    for item in state.get("evidence", []):
        if not isinstance(item, dict) or item.get("result") not in {"fail", "blocked"}:
            continue
        signatures.add(
            sha256_json(
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
            sha256_json(
                {
                    "kind": "review",
                    "observed": review.get("observed"),
                    "result": review.get("result"),
                }
            )
        )
    return signatures


def historical_failure_signatures(state: dict[str, Any]) -> set[str]:
    signatures: set[str] = set()
    for archived in state.get("loop", {}).get("attempt_history", []):
        if not isinstance(archived, dict):
            continue
        historical = {
            "evidence": archived.get("evidence_snapshot", []),
            "review": archived.get("review_snapshot"),
        }
        signatures.update(failure_signatures(historical))
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
    state: dict[str, Any], kind: str, hypothesis: str, source_attempt_id: str | None = None, snapshot: dict[str, Any] | None = None
) -> dict[str, Any]:
    require_v5_write(state, "starting an attempt")
    if kind not in ATTEMPT_KINDS:
        raise GateError("INVALID_ATTEMPT_KIND", [kind])
    if state["loop"].get("active_attempt") is not None:
        raise GateError("ATTEMPT_ALREADY_ACTIVE", [state["loop"]["active_attempt"].get("attempt_id", "unknown")])
    ensure_attempt_budget(state)
    active = {
        "attempt_id": next_attempt_id(state),
        "plan_revision": state["plan_revision"],
        "kind": kind,
        "started_at": utc_now(),
        "source_attempt_id": source_attempt_id,
        "hypothesis": bounded_text(hypothesis, "hypothesis", MAX_ATTEMPT_TEXT_CHARS),
        "start_fingerprint": compute_task_fingerprint(state, allow_unregistered=True, snapshot=snapshot),
        "diagnosis": None,
        "evidence_refs": [],
        "review_ref": None,
    }
    state["loop"]["active_attempt"] = active
    state["loop"]["attempt_count"] += 1
    return active


def attempt_end_fingerprint(state: dict[str, Any]) -> str | None:
    try:
        return compute_task_fingerprint(state)
    except GateError:
        return None


def archive_active_attempt(
    state: dict[str, Any], outcome: str, closure_reason: str, snapshot: dict[str, Any] | None = None
) -> dict[str, Any]:
    active = state["loop"].get("active_attempt")
    if not isinstance(active, dict):
        raise GateError("ATTEMPT_REQUIRED", ["no active attempt to archive"])
    if outcome not in ATTEMPT_OUTCOMES:
        raise GateError("INVALID_ATTEMPT_OUTCOME", [outcome])
    end_fingerprint_error = None
    try:
        end_fingerprint = compute_task_fingerprint(state, snapshot=snapshot)
    except GateError as exc:
        end_fingerprint = None
        end_fingerprint_error = f"{exc.code}: {'; '.join(exc.details)}"
    archived = copy.deepcopy(active)
    archived.update(
        {
            "closed_at": utc_now(),
            "end_fingerprint": end_fingerprint,
            "end_fingerprint_error": end_fingerprint_error,
            "outcome": outcome,
            "closure_reason": bounded_text(closure_reason, "closure_reason", MAX_ATTEMPT_TEXT_CHARS),
            "historical_not_valid_for_gate": True,
            "result": state.get("result"),
            "gaps": copy.deepcopy(state.get("gaps", [])),
            "gap_authorization": copy.deepcopy(state.get("gap_authorization")),
            "evidence_snapshot": copy.deepcopy(state.get("evidence", [])),
            "review_snapshot": copy.deepcopy(state.get("review")),
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


def current_attempt(state: dict[str, Any]) -> dict[str, Any]:
    active = state.get("loop", {}).get("active_attempt")
    if not isinstance(active, dict):
        raise GateError("ATTEMPT_REQUIRED", ["a current active attempt is required"])
    return active


def normalize_repeat_policy(raw: Any, label: str) -> dict[str, Any]:
    return _normalize_repeat_policy(raw, label, MAX_REPEAT_SAMPLES, GateError)


def normalized_verification_definitions(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, dict):
        raw = raw.get("definitions")
    if not isinstance(raw, list):
        raise GateError("VERIFICATION_SPEC_INVALID", ["verification spec must contain a definitions array"])
    if len(raw) > MAX_VERIFICATION_DEFINITIONS:
        raise GateError("VERIFICATION_SPEC_TOO_LARGE", [str(MAX_VERIFICATION_DEFINITIONS)])
    definitions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise GateError("VERIFICATION_SPEC_INVALID", [f"definition {index} must be an object"])
        check_id = bounded_text(item.get("check_id"), "check_id", 128)
        if check_id in seen:
            raise GateError("VERIFICATION_SPEC_INVALID", [f"duplicate check_id: {check_id}"])
        seen.add(check_id)
        claim_ids = item.get("claim_ids")
        if not isinstance(claim_ids, list) or not claim_ids or any(not isinstance(value, str) or not value.strip() for value in claim_ids):
            raise GateError("VERIFICATION_SPEC_INVALID", [f"{check_id}.claim_ids must be a non-empty string array"])
        criterion_digest = bounded_text(item.get("criterion_digest"), f"{check_id}.criterion_digest", 256)
        command_spec = item.get("command_spec")
        input_spec = item.get("input_spec")
        repeat_policy = item.get("repeat_policy")
        if not isinstance(command_spec, dict) or not isinstance(input_spec, dict) or not isinstance(repeat_policy, dict):
            raise GateError("VERIFICATION_SPEC_INVALID", [f"{check_id} requires command_spec, input_spec and repeat_policy"])
        repeat_policy = normalize_repeat_policy(repeat_policy, f"{check_id}.repeat_policy")
        definitions.append(
            {
                "check_id": check_id,
                "claim_ids": list(dict.fromkeys(claim_ids)),
                "criterion_digest": criterion_digest,
                "command_spec": copy.deepcopy(command_spec),
                "input_spec": copy.deepcopy(input_spec),
                "repeat_policy": copy.deepcopy(repeat_policy),
            }
        )
    return definitions


def verification_definition(state: dict[str, Any], check_id: str) -> dict[str, Any] | None:
    registry = state.get("verification_registry") or {}
    for definition in registry.get("definitions", []):
        if definition.get("check_id") == check_id:
            return definition
    return None


def safe_identity_object(value: Any, label: str) -> Any:
    """Keep machine identities bounded without copying credentials into run state."""
    if value is None:
        return None
    if not isinstance(value, dict):
        encoded = canonical_json(value)
        if isinstance(value, (str, int, float, bool)) and len(encoded) <= 512:
            return {"value_digest": hashlib.sha256(encoded.encode("utf-8")).hexdigest()}
        raise GateError("VERIFICATION_INPUT_UNKNOWN", [f"{label} must be an object, scalar identity, or null"])
    normalized: dict[str, Any] = {}
    for key, item in sorted(value.items()):
        if not isinstance(key, str) or not key.strip() or re.search(r"secret|token|password|private|credential|api[_-]?key", key, re.I):
            raise GateError("VERIFICATION_INPUT_UNKNOWN", [f"{label} contains a sensitive or invalid key"])
        if isinstance(item, (str, int, float, bool)) or item is None:
            encoded = canonical_json(item)
            if len(encoded) > 512:
                raise GateError("VERIFICATION_INPUT_UNKNOWN", [f"{label}.{key} is too large"])
            normalized[key] = {"digest": hashlib.sha256(encoded.encode("utf-8")).hexdigest()}
        elif isinstance(item, dict) and set(item).issubset({"id", "digest", "version"}):
            normalized[key] = copy.deepcopy(item)
        else:
            raise GateError("VERIFICATION_INPUT_UNKNOWN", [f"{label}.{key} is not a supported identity value"])
    return normalized


def normalize_execution_binding(value: Any, label: str) -> dict[str, str]:
    return _normalize_execution_binding(value, label, GateError)


def verification_input_binding(
    state: dict[str, Any], definition: dict[str, Any], snapshot: dict[str, Any] | None = None
) -> dict[str, Any]:
    repo = Path(state["repo"])
    input_spec = definition.get("input_spec", {})
    paths = input_spec.get("paths", [])
    if not isinstance(paths, list) or any(not isinstance(path, str) or not path.strip() for path in paths):
        raise GateError("VERIFICATION_INPUT_UNKNOWN", ["input_spec.paths must be an explicit string array"])
    baseline_head = state["git_baseline"]["head"]
    normalized_paths = [normalize_path(raw_path) for raw_path in paths]
    if any(not rel for rel in normalized_paths):
        raise GateError("VERIFICATION_INPUT_UNKNOWN", ["empty input path"])
    try:
        collection = snapshot_git_collection(repo, baseline_head, normalized_paths, snapshot)
    except GateError as exc:
        raise GateError("VERIFICATION_INPUT_UNKNOWN", exc.details) from exc
    input_identities: list[dict[str, str]] = []
    for rel in normalized_paths:
        input_identities.append(
            {
                "path": rel,
                "worktree": collection["content"][rel],
                "index": collection["index"].get(rel, "missing"),
                "baseline": collection["tree"].get(rel, "missing"),
            }
        )
    command_spec = definition.get("command_spec", {})
    argv = command_spec.get("argv")
    if not isinstance(argv, list) or any(not isinstance(value, str) for value in argv):
        raise GateError("VERIFICATION_INPUT_UNKNOWN", ["command_spec.argv must be an ordered string array"])
    cwd = command_spec.get("cwd", "repo")
    if cwd == "repo":
        cwd = str(repo)
    elif not isinstance(cwd, str) or not cwd:
        raise GateError("VERIFICATION_INPUT_UNKNOWN", ["command_spec.cwd is unavailable"])
    coverage = input_spec.get("dependency_coverage", "unknown")
    binding = {
        "run_id": state["run_id"],
        "attempt_id": current_attempt(state)["attempt_id"],
        "plan_revision": state["plan_revision"],
        "plan_fingerprint": (state.get("plan_record") or {}).get("plan_fingerprint"),
        "check_id": definition["check_id"],
        "criterion_digest": definition["criterion_digest"],
        "definition_digest": sha256_json(definition),
        "argv": list(argv),
        "cwd": cwd,
        "runner": command_spec.get("runner"),
        "input_identities": input_identities,
        "dependency_coverage": coverage,
        "environment": safe_identity_object(input_spec.get("environment", {}), "input_spec.environment"),
        "external_state": safe_identity_object(input_spec.get("external_state"), "input_spec.external_state"),
        "repeat_policy": definition.get("repeat_policy", {}),
        "worktree_fingerprint": compute_task_fingerprint(state, snapshot=snapshot),
    }
    return {"digest": sha256_json(binding), "summary": binding}


def next_evidence_id(state: dict[str, Any]) -> str:
    used: set[str] = set()
    for item in state.get("evidence", []):
        if isinstance(item, dict) and isinstance(item.get("evidence_id"), str):
            used.add(item["evidence_id"])
    for attempt in state.get("loop", {}).get("attempt_history", []):
        for item in attempt.get("evidence_snapshot", []):
            if isinstance(item, dict) and isinstance(item.get("evidence_id"), str):
                used.add(item["evidence_id"])
    index = 1
    while f"e{index:04d}" in used:
        index += 1
    return f"e{index:04d}"


def evidence_assertion_digest(evidence: dict[str, Any]) -> str:
    return sha256_json(
        {
            "kind": evidence.get("kind"),
            "entry": evidence.get("entry"),
            "command": evidence.get("command"),
            "observed": evidence.get("observed"),
            "level": evidence.get("level"),
            "result": evidence.get("result"),
            "check_id": evidence.get("check_id"),
        }
    )


def normalize_execution_record(
    state: dict[str, Any], raw: Any, check_id: str | None, binding: dict[str, Any] | None, result: str
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise GateError("EXECUTION_RECORD_INVALID", ["execution record must be an object"])
    execution_id = bounded_text(raw.get("execution_id"), "execution_id", 128)
    source = bounded_text(raw.get("source"), "execution.source", 64)
    if source not in {"host_receipt", "adapter", "agent_report"}:
        raise GateError("EXECUTION_RECORD_INVALID", ["execution.source is unsupported"])
    observed_result = raw.get("result", result)
    if observed_result not in {"pass", "fail", "blocked"} or observed_result != result:
        raise GateError("EXECUTION_RECORD_INVALID", ["execution.result must match evidence result"])
    raw_check_id = raw.get("check_id")
    if raw_check_id is not None and raw_check_id != check_id:
        raise GateError("EXECUTION_RECORD_INVALID", ["execution.check_id does not match evidence check_id"])
    output_ref = raw.get("output_ref")
    if output_ref is not None:
        output_ref = bounded_text(output_ref, "execution.output_ref", 1024)
    normalized = {
        "execution_id": execution_id,
        "source": source,
        "started_at": _execution_timestamp(raw, "started_at", utc_now(), GateError),
        "finished_at": _execution_timestamp(raw, "finished_at", utc_now(), GateError),
        "result": observed_result,
        "output_ref": output_ref,
        "check_id": check_id,
        "binding_digest": binding["digest"] if binding else None,
        "binding": copy.deepcopy(binding["summary"]) if binding else None,
    }
    for field in ("argv", "before_binding", "after_binding"):
        if field not in raw:
            continue
        value = raw[field]
        if field == "argv":
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise GateError("EXECUTION_RECORD_INVALID", ["execution.argv must be a string array"])
            if binding and value != binding["summary"].get("argv"):
                raise GateError("EXECUTION_RECORD_INVALID", ["execution.argv does not match the registered command"])
        else:
            try:
                value = normalize_execution_binding(value, f"execution.{field}")
            except GateError as exc:
                raise GateError("EXECUTION_RECORD_INVALID", exc.details) from exc
        normalized[field] = copy.deepcopy(value)
    if raw.get("cwd") is not None:
        cwd = bounded_text(raw["cwd"], "execution.cwd", 1024)
        if binding:
            expected_cwd = binding["summary"].get("cwd")
            try:
                cwd_matches = Path(cwd).resolve(strict=False) == Path(expected_cwd).resolve(strict=False)
            except (TypeError, OSError):
                cwd_matches = cwd == expected_cwd
            if not cwd_matches:
                raise GateError("EXECUTION_RECORD_INVALID", ["execution.cwd does not match the registered command"])
        normalized["cwd"] = cwd
    if raw.get("runner") is not None:
        normalized["runner"] = bounded_text(raw["runner"], "execution.runner", 128)
        if binding and normalized["runner"] != binding["summary"].get("runner"):
            raise GateError("EXECUTION_RECORD_INVALID", ["execution.runner does not match the registered command"])
    if raw.get("sample_id") is not None:
        normalized["sample_id"] = bounded_text(raw["sample_id"], "execution.sample_id", 128)
    return normalized


def comparable_execution(existing: dict[str, Any], incoming: dict[str, Any], raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    left = copy.deepcopy(existing)
    right = copy.deepcopy(incoming)
    if not isinstance(raw.get("started_at"), str):
        left.pop("started_at", None)
        right.pop("started_at", None)
    if not isinstance(raw.get("finished_at"), str):
        left.pop("finished_at", None)
        right.pop("finished_at", None)
    return left, right


def evidence_execution(state: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any] | None:
    execution = evidence.get("execution")
    return execution if isinstance(execution, dict) else None


def current_execution_for_check(state: dict[str, Any], check_id: str) -> list[dict[str, Any]]:
    return [
        item
        for item in state.get("evidence", [])
        if isinstance(item, dict) and item.get("check_id") == check_id and isinstance(evidence_execution(state, item), dict)
    ]


def execution_binding_reason(execution: dict[str, Any], current_digest: str) -> str | None:
    return _execution_binding_reason(execution, current_digest)


def execution_sample_ids(executions: Iterable[dict[str, Any]]) -> tuple[set[str], bool]:
    return _execution_sample_ids(executions)


def verification_summary(state: dict[str, Any]) -> dict[str, Any]:
    registry = state.get("verification_registry") or {}
    evidence = state.get("evidence", [])
    return {
        "freshness": "not_revalidated",
        "definitions": [item.get("check_id") for item in registry.get("definitions", []) if isinstance(item, dict)],
        "active_refs": copy.deepcopy(registry.get("active_refs", [])),
        "current_results": [
            {
                "evidence_id": item.get("evidence_id"),
                "check_id": item.get("check_id"),
                "result": item.get("result"),
                "execution_id": (item.get("execution") or {}).get("execution_id") if isinstance(item.get("execution"), dict) else None,
            }
            for item in evidence
            if isinstance(item, dict)
        ],
        "unresolved_failures": [
            item.get("evidence_id")
            for item in evidence
            if isinstance(item, dict) and item.get("result") in {"fail", "blocked"}
        ],
    }


def command_check_verification(args: argparse.Namespace) -> None:
    state = load_state(args.state)
    snapshot: dict[str, Any] = {}
    checked_at = utc_now()
    base = {
        "ok": True,
        "checked_at": checked_at,
        "freshness": "checked_at_request_time",
        "source_evidence_id": None,
        "binding_digest": None,
        "reason_codes": [],
    }
    if not is_v5_state(state):
        base.update({"decision": "unknown", "reason_codes": ["STATE_UPGRADE_REQUIRED"]})
        emit(base)
    if state["phase"] not in {"verify", "deliver"}:
        base.update({"decision": "blocked", "reason_codes": ["WRONG_PHASE"]})
        emit(base)
    try:
        active = current_attempt(state)
    except GateError:
        base.update({"decision": "blocked", "reason_codes": ["ATTEMPT_REQUIRED"]})
        emit(base)
    plan_errors = ensure_plan_record(state) + ensure_executor_profile(state)
    if plan_errors:
        base.update({"decision": "blocked", "reason_codes": ["PLAN_OR_ROLE_GATE"], "details": plan_errors})
        emit(base)
    try:
        ensure_current_scope(state, snapshot=snapshot)
    except GateError as exc:
        base.update({"decision": "blocked", "reason_codes": ["LIVE_SCOPE_GATE", exc.code], "details": exc.details})
        emit(base)
    force_reason = None
    if args.force:
        force_reason = bounded_text(args.reason, "reason", MAX_ATTEMPT_TEXT_CHARS)
    definition = verification_definition(state, args.check_id)
    if definition is None:
        base.update({"decision": "unknown", "reason_codes": ["CHECK_NOT_REGISTERED"]})
        emit(base)
    try:
        binding = verification_input_binding(state, definition, snapshot)
    except GateError as exc:
        base.update({"decision": "unknown", "reason_codes": ["INPUT_BINDING_UNKNOWN", exc.code]})
        emit(base)
    base.update({"binding_digest": binding["digest"], "attempt_id": active["attempt_id"]})
    if binding["summary"].get("dependency_coverage") == "unknown":
        base.update({"decision": "unknown", "reason_codes": ["DEPENDENCY_COVERAGE_UNKNOWN"]})
        emit(base)
    matching_failures: list[dict[str, Any]] = []
    matching_passes: list[dict[str, Any]] = []
    for item in current_execution_for_check(state, args.check_id):
        execution = evidence_execution(state, item)
        if not isinstance(execution, dict):
            continue
        if execution.get("binding_digest") != binding["digest"]:
            continue
        base["source_evidence_id"] = item.get("evidence_id")
        if item.get("result") == "pass":
            matching_passes.append(item)
        if item.get("result") in {"fail", "blocked"}:
            matching_failures.append(item)
    if matching_failures:
        reason_codes = ["UNRESOLVED_FAILURE"]
        if args.force:
            reason_codes.append("FORCE_CANNOT_BYPASS_FAILURE")
            base["force_reason"] = force_reason
        base.update({"decision": "diagnose", "reason_codes": reason_codes, "source_evidence_id": matching_failures[-1].get("evidence_id")})
        emit(base)
    if binding["summary"].get("environment") or binding["summary"].get("external_state") is not None:
        base.update({"decision": "unknown", "reason_codes": ["CURRENT_ENVIRONMENT_UNAVAILABLE"]})
        emit(base)
    if args.force:
        base.update(
            {
                "decision": "run",
                "reason_codes": ["force", "FORCE_REQUESTED"],
                "force_reason": force_reason,
            }
        )
        emit(base)
    if matching_passes:
        trusted = [
            item
            for item in matching_passes
            if isinstance(evidence_execution(state, item), dict)
            and evidence_execution(state, item).get("source") in {"host_receipt", "adapter"}
        ]
        if not trusted:
            base.update({"decision": "unknown", "reason_codes": ["EXECUTION_SOURCE_UNVERIFIED"], "source_evidence_id": matching_passes[-1].get("evidence_id")})
            emit(base)
        stable: list[dict[str, Any]] = []
        unstable_reasons: list[str] = []
        for item in trusted:
            execution = evidence_execution(state, item)
            reason = execution_binding_reason(execution, binding["digest"])
            if reason is None:
                stable.append(item)
            elif reason not in unstable_reasons:
                unstable_reasons.append(reason)
        if not stable:
            base.update(
                {
                    "decision": "unknown",
                    "reason_codes": unstable_reasons or ["EXECUTION_BINDING_UNAVAILABLE"],
                    "source_evidence_id": trusted[-1].get("evidence_id"),
                }
            )
            emit(base)
        repeat_policy = normalize_repeat_policy(
            binding["summary"].get("repeat_policy", {}), f"{args.check_id}.repeat_policy"
        )
        if repeat_policy["mode"] == "samples":
            stable_executions = [evidence_execution(state, item) for item in stable]
            sample_ids, missing_sample_id = execution_sample_ids(stable_executions)
            if missing_sample_id:
                base.update(
                    {
                        "decision": "run",
                        "reason_codes": ["REPEAT_SAMPLE_ID_REQUIRED", "REPEAT_SAMPLES_INSUFFICIENT"],
                        "samples_observed": len(sample_ids),
                        "samples_required": repeat_policy["required_samples"],
                    }
                )
                emit(base)
            if len(sample_ids) < repeat_policy["required_samples"]:
                base.update(
                    {
                        "decision": "run",
                        "reason_codes": ["REPEAT_SAMPLES_INSUFFICIENT"],
                        "samples_observed": len(sample_ids),
                        "samples_required": repeat_policy["required_samples"],
                    }
                )
                emit(base)
        base.update({"decision": "reuse", "reason_codes": ["MATCHING_ACTIVE_EXECUTION"], "source_evidence_id": stable[-1].get("evidence_id")})
        emit(base)
        base.update({"decision": "reuse", "reason_codes": ["MATCHING_ACTIVE_EXECUTION"], "source_evidence_id": trusted[-1].get("evidence_id")})
        emit(base)
    base.update({"decision": "run", "reason_codes": ["NO_MATCHING_EXECUTION"]})
    emit(base)


def command_progress(args: argparse.Namespace) -> None:
    state = load_state(args.state)
    events = list(state.get("telemetry", {}).get("events", []))
    event_limit = args.event_limit
    limited = events[-event_limit:] if event_limit else []
    projection = {
        "ok": True,
        "snapshot": "state_only_not_a_gate",
        "state": str(args.state),
        "goal": state["goal"],
        "phase": state["phase"],
        "result": state["result"],
        "mode": state["mode"],
        "plan_revision": state.get("plan_revision"),
        "review_node": (
            "not_required"
            if not state.get("review_required")
            else "recorded"
            if isinstance(state.get("review"), dict)
            else "pending"
        ),
        "review_result": (
            state["review"].get("result")
            if isinstance(state.get("review"), dict)
            else "not_required"
            if not state.get("review_required")
            else "pending"
        ),
        "attempt": {
            "active": copy.deepcopy(state.get("loop", {}).get("active_attempt")),
            "started": state.get("loop", {}).get("attempt_count", 0),
            "max": state.get("loop", {}).get("policy", {}).get("max_attempts"),
        },
        "replans": {
            "used": state.get("loop", {}).get("replan_count", 0),
            "max": state.get("loop", {}).get("policy", {}).get("max_replans"),
        },
        "risk": copy.deepcopy(state.get("risk", {})),
        "gaps": list(state.get("gaps", [])),
        "activity": copy.deepcopy(state.get("telemetry", {}).get("activity")),
        "events": limited,
        "events_dropped_before_seq": state.get("telemetry", {}).get("events_dropped_before_seq", 0),
        "events_truncated": len(events) > len(limited),
        "verification_summary": verification_summary(state),
        "display_line": display_line(state),
    }
    if args.format == "text":
        lines = progress_text_lines(state, projection)
        print("\n".join(lines))
        raise SystemExit(0)
    emit(projection)


def command_set_activity(args: argparse.Namespace) -> None:
    state = load_state(args.state)
    require_v5_write(state, "set-activity")
    if state["phase"] == "complete":
        raise GateError("ACTIVITY_NOT_ALLOWED", ["completed states are read-only"])
    text = bounded_text(args.text, "text", MAX_ACTIVITY_CHARS)
    current = state["telemetry"].get("activity")
    active = state["loop"].get("active_attempt")
    context = {
        "text": text,
        "source": "agent_report",
        "recorded_at": utc_now(),
        "attempt_id": active.get("attempt_id") if isinstance(active, dict) else None,
        "plan_revision": state["plan_revision"],
        "phase": state["phase"],
    }
    if isinstance(current, dict) and all(current.get(key) == context.get(key) for key in ("text", "attempt_id", "plan_revision", "phase")):
        emit({"ok": True, "idempotent": True, "activity": current, "display_line": display_line(state)})
    state["telemetry"]["activity"] = context
    append_event(state, "activity_updated", text)
    validate_shape(state)
    save_state(args.state, state)
    emit({"ok": True, "activity": context, "display_line": display_line(state)})


def context_history(state: dict[str, Any], limit: int) -> dict[str, Any]:
    return compact_context_history(state, limit, MAX_CONTEXT_EVIDENCE_REFS, MAX_CONTEXT_TEXT_CHARS)


def command_context(args: argparse.Namespace) -> None:
    state = load_state(args.state)
    active = state.get("loop", {}).get("active_attempt")
    write_scope = bounded_paths(state.get("write_scope", []), args.path_limit)
    changed_files = bounded_paths(state.get("changed_files", []), args.path_limit)
    suggested_action = {
        "plan": "read the bound Plan before implementation",
        "implement": "follow the current Plan, then record the real changed scope",
        "verify": "query the registered verification checks, then obtain current evidence",
        "deliver": "recheck the live Git and delivery gates before completion",
        "complete": "read the archived attempt; no further write action is available",
    }[state["phase"]]
    context = {
        "ok": True,
        "snapshot": "state_only_not_a_gate",
        "task": state["goal"],
        "phase": state["phase"],
        "run_id": state["run_id"],
        "state": str(args.state),
        "plan": {"path": state.get("plan_file"), "revision": state.get("plan_revision"), "recorded": isinstance(state.get("plan_record"), dict)},
        "write_scope": write_scope,
        "changed_files": changed_files,
        "active_attempt": copy.deepcopy(active),
        "unresolved": {"risk": copy.deepcopy(state.get("risk", {})), "gaps": list(state.get("gaps", [])), "result": state.get("result")},
        "history": context_history(state, args.history_limit),
        "budget": copy.deepcopy(state.get("loop", {}).get("policy", {})) | {
            "attempts_started": state.get("loop", {}).get("attempt_count"),
            "replans_used": state.get("loop", {}).get("replan_count"),
        },
        "verification_summary": verification_summary(state),
        "suggested_action": suggested_action,
        "completeness": {
            "history_truncated": context_history(state, args.history_limit)["truncated"],
            "paths_truncated": write_scope["truncated"] or changed_files["truncated"],
        },
    }
    emit(context)


def command_status(args: argparse.Namespace) -> None:
    state = load_state(args.state)
    loop = state.get("loop", {})
    telemetry = state.get("telemetry", {})
    emit(
        {
            "ok": True,
            "snapshot": "state_only_not_a_gate",
            "state": str(args.state),
            "phase": state["phase"],
            "mode": state["mode"],
            "goal": state["goal"],
            "plan": {
                "file": state.get("plan_file"),
                "revision": state.get("plan_revision", 1),
            },
            "risk": state["risk"],
            "result": state["result"],
            "gaps": list(state.get("gaps", [])),
            "needs_replan": bool(state.get("replan_required", False)),
            "needs_delivery": bool(state.get("delivery_required", False) and state["phase"] != "complete"),
            "review_recorded": isinstance(state.get("review"), dict),
            "review_status": (
                "not_required"
                if not state.get("review_required")
                else "recorded"
                if isinstance(state.get("review"), dict)
                else "pending"
            ),
            "display_line": display_line(state),
            "attempt": {
                "active": copy.deepcopy(loop.get("active_attempt")),
                "started": loop.get("attempt_count", 0),
                "max": loop.get("policy", {}).get("max_attempts"),
            },
            "replans": {
                "used": loop.get("replan_count", 0),
                "max": loop.get("policy", {}).get("max_replans"),
            },
            "last_rework_reason": state.get("last_rework_reason"),
            "last_replan_reason": state.get("last_replan_reason"),
            "recent_activity": copy.deepcopy(telemetry.get("activity")),
            "verification_summary": verification_summary(state),
            "paths": {
                "write_scope": bounded_paths(state.get("write_scope", []), args.limit),
                "changed_files": bounded_paths(state.get("changed_files", []), args.limit),
            },
        }
    )


def command_init(args: argparse.Namespace) -> None:
    repo = git_repo_root(args.repo)
    state_path = args.state.absolute()
    if path_exists_including_broken_symlink(state_path):
        raise GateError("STATE_FILE_EXISTS", [str(state_path), "init never overwrites an existing state"])
    ensure_external_artifact(state_path, repo, "state_file")
    git_baseline = capture_git_baseline(repo)
    config = load_model_config()
    planner_name, _ = configured_role(config, "planner")
    executor_name, _ = configured_role(config, "executor")
    reviewer_name, _ = configured_role(config, "reviewer")
    write_scope = sorted({normalize_path(item) for item in args.write})
    review_required = bool(write_scope)
    plan_file = None
    if args.plan_file is not None:
        plan_path = ensure_external_artifact(args.plan_file, repo, "plan_file")
        ensure_not_symlink(plan_path, "plan_file")
        if not plan_path.is_file():
            raise GateError("PLAN_FILE_NOT_FOUND", [str(plan_path), "write the Plan before init"])
        if plan_path.resolve(strict=False) == state_path.resolve(strict=False):
            raise GateError("PLAN_STATE_PATH_CONFLICT", [str(plan_path), str(state_path)])
        plan_file = str(plan_path)
    state = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "run_id": args.run_id or str(uuid.uuid4()),
        "mode": args.mode,
        "phase": "plan",
        "goal": args.goal.strip(),
        "write_scope": write_scope,
        "changed_files": [],
        "evidence": [],
        "risk": {"impact": args.impact, "details": args.risk_detail},
        "result": "pending",
        "gaps": [],
        "delivery_required": args.delivery_required,
        "gaps_authorized": False,
        "gap_authorization": None,
        "delivery_audit": None,
        "rework_count": 0,
        "last_rework_reason": None,
        "rework_streak": 0,
        "replan_required": False,
        "last_replan_reason": None,
        "plan_revision": 1,
        "plan_file": plan_file,
        "repo": str(repo),
        "git_baseline": git_baseline,
        "change_detection": "git_baseline",
        "review_required": review_required,
        "planner_profile": planner_name,
        "plan_record": None,
        "executor_profile": executor_name if review_required else None,
        "reviewer_profile": reviewer_name,
        "review": None,
        "loop": {
            "format_version": 1,
            "policy": {
                "max_attempts": args.max_attempts,
                "max_replans": args.max_replans,
            },
            "attempt_count": 0,
            "replan_count": 0,
            "active_attempt": None,
            "attempt_history": [],
            "plan_history": [],
        },
        "telemetry": {
            "format_version": 1,
            "event_seq": 0,
            "events_dropped_before_seq": 0,
            "activity": None,
            "events": [],
        },
        "verification_registry": {
            "format_version": 1,
            "plan_revision": 1,
            "definitions": [],
            "active_refs": [],
        },
    }
    validate_shape(state)
    save_state(state_path, state)
    emit(
        {
            "ok": True,
            "state": str(state_path),
            "run_id": state["run_id"],
            "phase": state["phase"],
            "display_line": display_line(state),
        }
    )


def command_record_plan(args: argparse.Namespace) -> None:
    state = load_state(args.state)
    require_v5_write(state, "record-plan")
    if state["phase"] != "plan":
        raise GateError("WRONG_PHASE", ["record-plan requires plan phase"])
    if state.get("replan_required", False):
        raise GateError("REPLAN_REQUIRED", ["revise-plan is required before recording the revised Plan"])

    config = load_model_config()
    planner_name, planner = configured_role(config, "planner")
    if state.get("planner_profile") != planner_name:
        raise GateError(
            "PLAN_PROFILE_MISMATCH",
            [f"state planner profile is {state.get('planner_profile')!r}; configured planner profile is {planner_name!r}"],
        )
    supplied_profile = args.profile
    if supplied_profile and supplied_profile != planner_name:
        raise GateError("PLAN_PROFILE_MISMATCH", [f"configured planner profile is {planner_name!r}"])
    if is_session_main_profile(planner):
        if not args.model or not args.model.strip():
            raise GateError("SESSION_MAIN_MODEL_REQUIRED", ["record the current session main model with --model"])
        if not args.reasoning_effort or not args.reasoning_effort.strip():
            raise GateError(
                "SESSION_MAIN_REASONING_REQUIRED",
                ["record the current session main reasoning effort with --reasoning-effort"],
            )
        planned_model = args.model.strip()
        planned_effort = args.reasoning_effort.strip()
    else:
        if args.model and args.model != planner["model"]:
            raise GateError("PLAN_PROFILE_MISMATCH", [f"configured planner model is {planner['model']!r}"])
        if args.reasoning_effort and args.reasoning_effort != planner["reasoning_effort"]:
            raise GateError(
                "PLAN_PROFILE_MISMATCH",
                [f"configured planner reasoning effort is {planner['reasoning_effort']!r}"],
            )
        planned_model = planner["model"]
        planned_effort = planner["reasoning_effort"]

    if args.verification_spec is not None:
        definitions = normalized_verification_definitions(parse_json_value(args.verification_spec, "verification-spec"))
        state["verification_registry"]["definitions"] = definitions
        state["verification_registry"]["plan_revision"] = state["plan_revision"]
        state["verification_registry"]["active_refs"] = []
    registry_digest = sha256_json(
        {
            "plan_revision": state["verification_registry"]["plan_revision"],
            "definitions": state["verification_registry"]["definitions"],
        }
    )
    state["plan_record"] = {
        "profile": planner_name,
        "model": planned_model,
        "reasoning_effort": planned_effort,
        "recorded_at": utc_now(),
        "plan_revision": state["plan_revision"],
        "plan_fingerprint": compute_plan_fingerprint(state),
        "verification_spec_digest": registry_digest,
    }
    append_event(state, "plan_recorded", "Plan recorded for the current revision")
    validate_shape(state)
    save_state(args.state, state)
    emit({"ok": True, "plan_record": state["plan_record"], "state": str(args.state), "display_line": display_line(state)})


def command_transition(args: argparse.Namespace) -> None:
    state = load_state(args.state)
    snapshot: dict[str, Any] = {}
    require_v5_write(state, "transition")
    check_transition(state, args.to, snapshot)
    deleted_plan_file = None
    archived = None
    if args.to == "implement":
        hypothesis = args.hypothesis or state["goal"]
        archived = start_attempt(state, "implement", hypothesis, snapshot=snapshot)
    if args.to == "complete":
        ensure_plan_file_binding(state)
        archived = archive_active_attempt(state, state["result"], "complete", snapshot)
        deleted_plan_file = delete_plan_file(state)
    if args.to != state["phase"] and isinstance(state.get("telemetry"), dict):
        state["telemetry"]["activity"] = None
    state["phase"] = args.to
    if args.to in {"deliver", "complete"}:
        state["rework_streak"] = 0
    append_event(
        state,
        "completed" if args.to == "complete" else "phase_changed",
        f"phase changed to {args.to}",
        [archived["attempt_id"]] if isinstance(archived, dict) else (),
    )
    validate_shape(state)
    save_state(args.state, state)
    emit(
        {
            "ok": True,
            "phase": args.to,
            "plan_deleted": deleted_plan_file,
            "state": str(args.state),
            "display_line": display_line(state),
        }
    )


def diagnosis_source_snapshot(
    state: dict[str, Any], snapshot: dict[str, Any] | None = None
) -> tuple[dict[str, Any], str]:
    """Return the compact, non-self-referential source facts bound to a diagnosis."""
    evidence_snapshot = []
    for item in state.get("evidence", []):
        if not isinstance(item, dict):
            continue
        execution = item.get("execution") if isinstance(item.get("execution"), dict) else None
        evidence_snapshot.append(
            {
                "evidence_id": item.get("evidence_id"),
                "attempt_id": item.get("attempt_id"),
                "kind": item.get("kind"),
                "result": item.get("result"),
                "entry": item.get("entry"),
                "observed": item.get("observed"),
                "check_id": item.get("check_id"),
                "execution_id": execution.get("execution_id") if execution else None,
            }
        )
    review = state.get("review") if isinstance(state.get("review"), dict) else None
    snapshot = {
        "attempt_id": current_attempt(state)["attempt_id"],
        "plan_revision": state["plan_revision"],
        "result": state.get("result"),
        "gaps": list(state.get("gaps", [])),
        "evidence": evidence_snapshot,
        "review": (
            {
                "review_id": review.get("review_id"),
                "attempt_id": review.get("attempt_id"),
                "result": review.get("result"),
                "observed": review.get("observed"),
            }
            if review
            else None
        ),
        "worktree_fingerprint": compute_evidence_fingerprint(state, snapshot),
    }
    return snapshot, sha256_json(snapshot)


def normalized_diagnosis(
    state: dict[str, Any], raw: Any, snapshot: dict[str, Any] | None = None
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise GateError("DIAGNOSIS_INVALID", ["diagnosis input must be an object"])
    forbidden = {"phase", "result", "gaps_authorized", "authorized_by", "authorization", "max_attempts", "max_replans"}
    if forbidden.intersection(raw):
        raise GateError("DIAGNOSIS_INVALID", ["diagnosis cannot modify phase, result, budget, or authorization"])
    classification = raw.get("classification")
    if classification not in DIAGNOSIS_CLASSIFICATIONS:
        raise GateError("DIAGNOSIS_INVALID", ["classification is invalid"])
    source_refs = raw.get("source_refs")
    if not isinstance(source_refs, list) or not 1 <= len(source_refs) <= 8 or any(not isinstance(ref, str) or not ref.strip() for ref in source_refs):
        raise GateError("DIAGNOSIS_INVALID", ["source_refs must contain 1..8 non-empty IDs"])
    current_ids = {item.get("evidence_id") for item in state.get("evidence", []) if isinstance(item, dict)}
    review = state.get("review")
    if isinstance(review, dict):
        current_ids.add(review.get("review_id"))
    unknown = sorted(set(source_refs) - current_ids)
    if unknown:
        raise GateError("DIAGNOSIS_SOURCE_INVALID", unknown)
    cause_summary = bounded_text(raw.get("cause_summary"), "cause_summary", MAX_ATTEMPT_TEXT_CHARS)
    next_action = bounded_text(raw.get("next_action_summary"), "next_action_summary", MAX_ATTEMPT_TEXT_CHARS)
    hypothesis = bounded_text(raw.get("next_hypothesis"), "next_hypothesis", MAX_ATTEMPT_TEXT_CHARS)
    expected = bounded_text(raw.get("expected_observation"), "expected_observation", MAX_ATTEMPT_TEXT_CHARS)
    new_information = raw.get("new_information")
    if not isinstance(new_information, dict) or new_information.get("kind") not in DIAGNOSIS_INFORMATION_KINDS:
        raise GateError("DIAGNOSIS_INVALID", ["new_information.kind is invalid"])
    info_summary = bounded_text(new_information.get("summary"), "new_information.summary", MAX_ATTEMPT_TEXT_CHARS)
    info_refs = new_information.get("source_refs", [])
    if not isinstance(info_refs, list) or any(ref not in current_ids for ref in info_refs):
        raise GateError("DIAGNOSIS_SOURCE_INVALID", ["new_information.source_refs must reference current records"])
    review_failed = isinstance(state.get("review"), dict) and state["review"].get("result") == "fail"
    has_fail = (
        state.get("result") == "fail"
        or any(item.get("result") == "fail" for item in state.get("evidence", []))
        or review_failed
    )
    has_block = state.get("result") in {"blocked", "pass_with_gaps"} or any(item.get("result") == "blocked" for item in state.get("evidence", []))
    if classification in {"implementation", "verification_contract", "hypothesis_or_scope", "unknown"} and not has_fail:
        raise GateError("DIAGNOSIS_SOURCE_INVALID", ["this classification requires a current failure source"])
    if classification in {"environment", "input_data"} and not has_block:
        raise GateError("DIAGNOSIS_SOURCE_INVALID", ["environment/input_data requires a current blocked source"])
    active = current_attempt(state)
    source_snapshot, source_snapshot_digest = diagnosis_source_snapshot(state, snapshot)
    return {
        "diagnosis_id": f"d{uuid.uuid4().hex[:12]}",
        "attempt_id": active["attempt_id"],
        "plan_revision": state["plan_revision"],
        "recorded_at": utc_now(),
        "classification": classification,
        "cause_summary": cause_summary,
        "source_refs": list(dict.fromkeys(source_refs)),
        "next_action_summary": next_action,
        "next_hypothesis": hypothesis,
        "expected_observation": expected,
        "new_information": {"kind": new_information["kind"], "summary": info_summary, "source_refs": list(dict.fromkeys(info_refs))},
        "source_snapshot": source_snapshot,
        "source_snapshot_digest": source_snapshot_digest,
        "worktree_fingerprint": source_snapshot["worktree_fingerprint"],
    }


def command_record_diagnosis(args: argparse.Namespace) -> None:
    state = load_state(args.state)
    snapshot: dict[str, Any] = {}
    require_v5_write(state, "record-diagnosis")
    if state["phase"] != "verify":
        raise GateError("WRONG_PHASE", ["record-diagnosis requires verify phase"])
    diagnosis = normalized_diagnosis(state, parse_json_value(args.input, "diagnosis"), snapshot)
    active = current_attempt(state)
    previous = active.get("diagnosis")
    if isinstance(previous, dict):
        comparable = {key: value for key, value in previous.items() if key not in {"diagnosis_id", "recorded_at"}}
        current = {key: value for key, value in diagnosis.items() if key not in {"diagnosis_id", "recorded_at"}}
        if canonical_json(comparable) == canonical_json(current):
            emit({"ok": True, "idempotent": True, "diagnosis_id": previous.get("diagnosis_id"), "display_line": display_line(state)})
    active["diagnosis"] = diagnosis
    append_event(state, "diagnosis_recorded", "diagnosis recorded from current failure evidence", diagnosis["source_refs"])
    validate_shape(state)
    save_state(args.state, state)
    emit({"ok": True, "diagnosis": diagnosis, "display_line": display_line(state)})


def command_retry_verify(args: argparse.Namespace) -> None:
    state = load_state(args.state)
    snapshot: dict[str, Any] = {}
    require_v5_write(state, "retry-verify")
    if state["phase"] != "verify":
        raise GateError("WRONG_PHASE", ["retry-verify requires verify phase"])
    reason = bounded_text(args.reason, "reason", MAX_ATTEMPT_TEXT_CHARS)
    active = current_attempt(state)
    diagnosis = active.get("diagnosis")
    if not isinstance(diagnosis, dict):
        raise GateError("DIAGNOSIS_REQUIRED", ["record a current diagnosis before retry-verify"])
    if diagnosis.get("classification") not in {"environment", "input_data"}:
        raise GateError("RETRY_VERIFY_NOT_ALLOWED", ["retry-verify requires an environment or input_data diagnosis"])
    if state["result"] not in {"blocked", "pass_with_gaps"}:
        raise GateError("RETRY_VERIFY_NOT_ALLOWED", ["retry-verify requires blocked or pass_with_gaps result"])
    if not any(item.get("result") == "blocked" for item in state.get("evidence", [])):
        raise GateError("RETRY_VERIFY_NOT_ALLOWED", ["retry-verify requires a current blocked evidence record"])
    if state.get("result") == "fail" or any(item.get("result") == "fail" for item in state.get("evidence", [])):
        raise GateError("RETRY_VERIFY_NOT_ALLOWED", ["unresolved failure evidence must be handled by rework or revise-plan"])
    if isinstance(state.get("review"), dict) and state["review"].get("result") == "fail":
        raise GateError("RETRY_VERIFY_NOT_ALLOWED", ["a failed review must be handled by rework or revise-plan"])
    if diagnosis.get("new_information", {}).get("kind") != "external_change":
        raise GateError("EXTERNAL_OBSERVATION_REQUIRED", ["diagnosis must cite a new external_change observation"])
    if not diagnosis.get("new_information", {}).get("source_refs"):
        raise GateError("EXTERNAL_OBSERVATION_REQUIRED", ["new external information must cite a current source"])
    blocked_ids = {
        item.get("evidence_id")
        for item in state.get("evidence", [])
        if isinstance(item, dict) and item.get("result") == "blocked"
    }
    if all(ref in blocked_ids for ref in diagnosis["new_information"]["source_refs"]):
        raise GateError("EXTERNAL_OBSERVATION_REQUIRED", ["new external information must be distinct from the original blocked observation"])
    if args.observation is not None:
        bounded_text(args.observation, "observation", MAX_OBSERVED_CHARS)
    try:
        current_snapshot, current_snapshot_digest = diagnosis_source_snapshot(state, snapshot)
    except GateError as exc:
        raise GateError("RETRY_VERIFY_BLOCKED", exc.details) from exc
    if diagnosis.get("source_snapshot_digest") != current_snapshot_digest:
        raise GateError("DIAGNOSIS_STALE", ["evidence, review, result, gaps, or inputs changed after diagnosis"])
    if diagnosis.get("worktree_fingerprint") != current_snapshot.get("worktree_fingerprint"):
        raise GateError("DIAGNOSIS_STALE", ["diagnosis no longer matches the current worktree"])
    plan_errors = ensure_plan_record(state) + ensure_executor_profile(state)
    if plan_errors:
        raise GateError("RETRY_VERIFY_BLOCKED", plan_errors)
    try:
        ensure_current_scope(state, snapshot=snapshot)
    except GateError as exc:
        raise GateError("RETRY_VERIFY_BLOCKED", exc.details) from exc
    ensure_attempt_budget(state)

    archived = archive_active_attempt(state, "blocked", reason, snapshot)
    clear_current_records(state)
    state["phase"] = "verify"
    next_hypothesis = diagnosis.get("next_hypothesis") or "re-verify after the recorded external condition changed"
    start_attempt(state, "verify_only", next_hypothesis, archived["attempt_id"], snapshot)
    append_event(state, "verification_retry_started", "started a verify-only attempt after new external information", [archived["attempt_id"]])
    validate_shape(state)
    save_state(args.state, state)
    emit(
        {
            "ok": True,
            "phase": state["phase"],
            "attempt_id": state["loop"]["active_attempt"]["attempt_id"],
            "archived_attempt_id": archived["attempt_id"],
            "display_line": display_line(state),
        }
    )


def command_rework(args: argparse.Namespace) -> None:
    state = load_state(args.state)
    snapshot: dict[str, Any] = {}
    require_v5_write(state, "rework")
    if state["phase"] != "verify":
        raise GateError("WRONG_PHASE", ["rework requires verify phase"])
    reason = bounded_text(args.reason, "reason", MAX_ATTEMPT_TEXT_CHARS)
    active = current_attempt(state)
    review_failed = isinstance(state.get("review"), dict) and state["review"].get("result") == "fail"
    if state["result"] != "fail" and not any(item.get("result") == "fail" for item in state["evidence"]) and not review_failed:
        raise GateError("REWORK_NOT_JUSTIFIED", ["record failed verification evidence before rework"])
    diagnosis = active.get("diagnosis")
    if not isinstance(diagnosis, dict):
        raise GateError("DIAGNOSIS_REQUIRED", ["record a current diagnosis before rework"])
    plan_errors = ensure_plan_record(state) + ensure_executor_profile(state)
    if plan_errors:
        raise GateError("REWORK_BLOCKED", plan_errors)
    try:
        ensure_current_scope(state, snapshot=snapshot)
    except GateError as exc:
        raise GateError("REWORK_BLOCKED", exc.details) from exc
    current_fingerprint = compute_evidence_fingerprint(state, snapshot)
    if diagnosis.get("worktree_fingerprint") != current_fingerprint:
        raise GateError("DIAGNOSIS_STALE", ["diagnosis no longer matches the current evidence binding"])
    no_new_information = bool(failure_signatures(state) & historical_failure_signatures(state))
    if state["rework_streak"] + 1 < REWORK_REPLAN_AT:
        ensure_attempt_budget(state)

    archived = archive_active_attempt(state, "fail", reason, snapshot)
    state["rework_count"] = int(state.get("rework_count", 0)) + 1
    state["rework_streak"] = int(state.get("rework_streak", 0)) + 1
    state["last_rework_reason"] = reason
    clear_current_records(state)
    payload: dict[str, Any] = {
        "ok": True,
        "result": state["result"],
        "rework_count": state["rework_count"],
        "rework_streak": state["rework_streak"],
        "archived_attempt_id": archived["attempt_id"],
        "state": str(args.state),
    }
    if state["rework_streak"] >= REWORK_REPLAN_AT:
        state["phase"] = "plan"
        state["replan_required"] = True
        state["plan_record"] = None
        payload["code"] = "REPLAN_REQUIRED"
    else:
        state["phase"] = "implement"
        state["replan_required"] = False
        start_attempt(state, "implement", state["goal"], archived["attempt_id"], snapshot)
        if state["rework_streak"] >= REWORK_WARN_AT:
            payload["warning"] = "REPLAN_RECOMMENDED"
    if no_new_information:
        payload["notices"] = ["NO_NEW_INFORMATION"]
    append_event(state, "attempt_restarted", f"attempt archived after {reason}", [archived["attempt_id"]])
    payload["phase"] = state["phase"]
    validate_shape(state)
    save_state(args.state, state)
    payload["display_line"] = display_line(state)
    emit(payload)


def command_revise_plan(args: argparse.Namespace) -> None:
    state = load_state(args.state)
    snapshot: dict[str, Any] = {}
    require_v5_write(state, "revise-plan")
    if state["phase"] not in {"plan", "implement", "verify"}:
        raise GateError("REPLAN_NOT_ALLOWED", ["revise-plan is allowed only during plan, implement, or verify"])
    if state["loop"]["replan_count"] >= state["loop"]["policy"]["max_replans"]:
        raise GateError("LOOP_BUDGET_EXHAUSTED", ["replans", f"{state['loop']['replan_count']}/{state['loop']['policy']['max_replans']}"])
    reason = (args.reason or "").strip()
    if not reason and not state.get("replan_required", False):
        raise GateError("REPLAN_REASON_REQUIRED", ["--reason must be non-empty"])
    if not reason:
        reason = state.get("last_rework_reason") or "forced replan after repeated verification failure"
    reason = bounded_text(reason, "reason", MAX_ATTEMPT_TEXT_CHARS)

    revised_scope = sorted({normalize_path(item) for item in args.write})
    current_paths = current_task_paths(state, snapshot)
    outside = [path for path in current_paths if not in_scope(path, revised_scope)]
    if outside:
        raise GateError("REPLAN_SCOPE_CONFLICT", ["existing changes fall outside the revised write scope: " + ", ".join(outside)])

    old_revision = state["plan_revision"]
    old_plan_fingerprint = None
    if isinstance(state.get("plan_record"), dict):
        old_plan_fingerprint = state["plan_record"].get("plan_fingerprint")
    archived = None
    if state["loop"].get("active_attempt") is not None:
        archived = archive_active_attempt(state, "superseded", reason, snapshot)
    state["loop"]["replan_count"] += 1
    state["loop"]["plan_history"].append(
        {
            "old_revision": old_revision,
            "new_revision": old_revision + 1,
            "recorded_at": utc_now(),
            "reason": reason,
            "attempt_id": archived["attempt_id"] if archived else None,
            "old_plan_fingerprint": old_plan_fingerprint,
            "old_goal": state["goal"],
        }
    )
    state["mode"] = args.mode
    state["goal"] = bounded_text(args.goal, "goal", MAX_ATTEMPT_TEXT_CHARS)
    state["write_scope"] = revised_scope
    state["risk"] = {"impact": args.impact, "details": args.risk_detail}
    if args.delivery_required is not None:
        state["delivery_required"] = args.delivery_required
    state["changed_files"] = sorted(current_paths)
    clear_current_records(state)
    config = load_model_config()
    planner_name, _ = configured_role(config, "planner")
    executor_name, _ = configured_role(config, "executor")
    reviewer_name, _ = configured_role(config, "reviewer")
    state["planner_profile"] = planner_name
    state["plan_record"] = None
    state["review_required"] = bool(revised_scope)
    state["executor_profile"] = executor_name if revised_scope else None
    state["reviewer_profile"] = reviewer_name
    state["rework_streak"] = 0
    state["phase"] = "plan"
    state["replan_required"] = False
    state["last_replan_reason"] = reason
    state["plan_revision"] = old_revision + 1
    state["verification_registry"] = {"format_version": 1, "plan_revision": state["plan_revision"], "definitions": [], "active_refs": []}
    append_event(state, "plan_revised", f"Plan revised: {reason}", [archived["attempt_id"]] if archived else ())
    validate_shape(state)
    save_state(args.state, state)
    emit({"ok": True, "phase": state["phase"], "plan_revision": state["plan_revision"], "rework_streak": state["rework_streak"], "state": str(args.state), "display_line": display_line(state)})


def command_set_changes(args: argparse.Namespace) -> None:
    state = load_state(args.state)
    require_v5_write(state, "set-changes")
    if state["phase"] != "implement":
        raise GateError("WRONG_PHASE", ["set-changes requires implement phase"])
    if args.file:
        raise GateError("DECLARED_CHANGES_FORBIDDEN", ["v5 states derive changes from the Git baseline"])
    state["changed_files"] = task_git_files(state)
    state["review"] = None
    append_event(state, "changes_recorded", f"recorded {len(state['changed_files'])} task paths")
    validate_shape(state)
    save_state(args.state, state)
    emit({"ok": True, "changed_files": state["changed_files"], "display_line": display_line(state)})


def command_record_evidence(args: argparse.Namespace) -> None:
    state = load_state(args.state)
    snapshot: dict[str, Any] = {}
    require_v5_write(state, "record-evidence")
    if state["phase"] != "verify":
        raise GateError("WRONG_PHASE", ["record-evidence requires verify phase"])
    active = current_attempt(state)
    if len(state["evidence"]) >= MAX_ACTIVE_EVIDENCE:
        raise GateError("EVIDENCE_LIMIT_EXCEEDED", [str(MAX_ACTIVE_EVIDENCE)])
    check_id = args.check_id
    definition = None
    binding = None
    if check_id:
        check_id = bounded_text(check_id, "check_id", 128)
        definition = verification_definition(state, check_id)
        if definition is None:
            raise GateError("CHECK_NOT_REGISTERED", [check_id])
        try:
            binding = verification_input_binding(state, definition, snapshot)
        except GateError as exc:
            raise GateError("VERIFICATION_INPUT_UNKNOWN", exc.details) from exc
    raw_execution = parse_json_value(args.execution_record, "execution-record") if args.execution_record else None
    assertion = {
        "kind": args.kind,
        "entry": bounded_text(args.entry, "entry", MAX_OBSERVED_CHARS),
        "command": bounded_text(args.command, "command", MAX_COMMAND_CHARS),
        "observed": bounded_text(args.observed, "observed", MAX_OBSERVED_CHARS),
        "level": args.level,
        "result": args.result,
        "check_id": check_id,
    }
    assertion_digest = evidence_assertion_digest(assertion)
    if raw_execution is not None:
        execution_id = raw_execution.get("execution_id") if isinstance(raw_execution, dict) else None
        for existing in state["evidence"]:
            execution = evidence_execution(state, existing)
            if execution and execution.get("execution_id") == execution_id:
                incoming_execution = normalize_execution_record(state, raw_execution, check_id, binding, args.result)
                left, right = comparable_execution(execution, incoming_execution, raw_execution)
                if canonical_json(left) == canonical_json(right):
                    existing_assertion = existing.get("assertion_digest") or evidence_assertion_digest(existing)
                    if existing_assertion == assertion_digest:
                        emit({"ok": True, "idempotent": True, "evidence_id": existing.get("evidence_id"), "display_line": display_line(state)})
                else:
                    raise GateError("EXECUTION_ID_REUSE", [str(execution_id)])
    execution = normalize_execution_record(state, raw_execution, check_id, binding, args.result) if raw_execution is not None else None
    worktree_fingerprint = compute_evidence_fingerprint(state, snapshot)
    evidence = {
        "evidence_id": next_evidence_id(state),
        "attempt_id": active["attempt_id"],
        "recorded_at": utc_now(),
        "kind": args.kind,
        "entry": bounded_text(args.entry, "entry", MAX_OBSERVED_CHARS),
        "command": bounded_text(args.command, "command", MAX_COMMAND_CHARS),
        "observed": bounded_text(args.observed, "observed", MAX_OBSERVED_CHARS),
        "level": args.level,
        "result": args.result,
        "worktree_fingerprint": worktree_fingerprint,
        "assertion_digest": assertion_digest,
    }
    if check_id:
        evidence["check_id"] = check_id
    if execution is not None:
        evidence["execution"] = execution
    state["evidence"].append(evidence)
    active["evidence_refs"].append(evidence["evidence_id"])
    if check_id:
        state["verification_registry"]["active_refs"].append(
            {
                "check_id": check_id,
                "evidence_id": evidence["evidence_id"],
                "execution_id": execution.get("execution_id") if execution else None,
                "attempt_id": active["attempt_id"],
            }
        )
    state["review"] = None
    active["review_ref"] = None
    append_event(state, "evidence_recorded", f"evidence {evidence['evidence_id']} recorded", [evidence["evidence_id"]])
    validate_shape(state)
    save_state(args.state, state)
    emit(
        {
            "ok": True,
            "evidence_count": len(state["evidence"]),
            "evidence_id": evidence["evidence_id"],
            "execution_id": execution.get("execution_id") if execution else None,
            "display_line": display_line(state),
        }
    )


def command_set_result(args: argparse.Namespace) -> None:
    state = load_state(args.state)
    require_v5_write(state, "set-result")
    if state["phase"] != "verify":
        raise GateError("WRONG_PHASE", ["set-result requires verify phase"])
    if args.result == "pass_with_gaps" and not args.gap:
        raise GateError("GAPS_REQUIRED", ["pass_with_gaps requires at least one --gap"])
    state["result"] = args.result
    state["gaps"] = args.gap
    state["gaps_authorized"] = False
    state["gap_authorization"] = None
    state["review"] = None
    current_attempt(state)["review_ref"] = None
    append_event(state, "result_recorded", f"verification result recorded as {state['result']}")
    validate_shape(state)
    save_state(args.state, state)
    emit(
        {
            "ok": True,
            "result": state["result"],
            "gaps_authorized": state["gaps_authorized"],
            "display_line": display_line(state),
        }
    )


def command_record_review(args: argparse.Namespace) -> None:
    state = load_state(args.state)
    snapshot: dict[str, Any] = {}
    require_v5_write(state, "record-review")
    if state["phase"] != "verify":
        raise GateError("WRONG_PHASE", ["record-review requires verify phase"])
    if not review_is_required(state):
        raise GateError("REVIEW_NOT_REQUIRED", ["read-only tasks do not require an independent review"])
    if state["result"] not in {"pass", "pass_with_gaps"}:
        raise GateError(
            "REVIEW_NOT_READY",
            ["set verification result to pass or pass_with_gaps before recording the review"],
        )
    ensure_current_scope(state, snapshot=snapshot)
    evidence_errors = ensure_evidence(state, snapshot)
    if evidence_errors:
        raise GateError("REVIEW_NOT_READY", evidence_errors)

    config = load_model_config()
    executor_name, _ = configured_role(config, "executor")
    reviewer_name, reviewer = configured_role(config, "reviewer")
    supplied_profile = args.profile
    if supplied_profile and supplied_profile != reviewer_name:
        raise GateError("REVIEW_PROFILE_MISMATCH", [f"configured reviewer profile is {reviewer_name!r}"])
    if is_session_main_profile(reviewer):
        if not args.model or not args.model.strip():
            raise GateError("SESSION_MAIN_MODEL_REQUIRED", ["record the current session main model with --model"])
        if not args.reasoning_effort or not args.reasoning_effort.strip():
            raise GateError(
                "SESSION_MAIN_REASONING_REQUIRED",
                ["record the current session main reasoning effort with --reasoning-effort"],
            )
        reviewed_model = args.model.strip()
        reviewed_effort = args.reasoning_effort.strip()
    else:
        if args.model and args.model != reviewer["model"]:
            raise GateError("REVIEW_PROFILE_MISMATCH", [f"configured reviewer model is {reviewer['model']!r}"])
        if args.reasoning_effort and args.reasoning_effort != reviewer["reasoning_effort"]:
            raise GateError(
                "REVIEW_PROFILE_MISMATCH",
                [f"configured reviewer reasoning effort is {reviewer['reasoning_effort']!r}"],
            )
        reviewed_model = reviewer["model"]
        reviewed_effort = reviewer["reasoning_effort"]
    observed = bounded_text(args.observed, "observed", MAX_OBSERVED_CHARS)
    reviewed_at = utc_now()
    attempt = current_attempt(state)
    state["review"] = {
        "review_id": f"r{uuid.uuid4().hex[:12]}",
        "attempt_id": attempt["attempt_id"],
        "profile": reviewer_name,
        "model": reviewed_model,
        "reasoning_effort": reviewed_effort,
        "result": args.result,
        "observed": observed,
        "reviewed_at": reviewed_at,
        "task_fingerprint": compute_task_fingerprint(state, snapshot=snapshot),
    }
    attempt["review_ref"] = state["review"]["review_id"]
    append_event(state, "review_recorded", "review recorded for the current attempt", [state["review"]["review_id"]])
    validate_shape(state)
    save_state(args.state, state)
    emit({"ok": True, "review": state["review"], "state": str(args.state), "display_line": display_line(state)})


def command_authorize_gaps(args: argparse.Namespace) -> None:
    state = load_state(args.state)
    require_v5_write(state, "authorize-gaps")
    if state["phase"] != "verify":
        raise GateError("WRONG_PHASE", ["authorize-gaps requires verify phase"])
    if state["result"] != "pass_with_gaps" or not state["gaps"]:
        raise GateError("GAPS_NOT_PENDING", ["set pass_with_gaps and list gaps before authorization"])
    authorized_by = bounded_text(args.authorized_by, "authorized-by", 256)
    if not re.fullmatch(r"(?:user|host):[^\s].*", authorized_by):
        raise GateError("INVALID_AUTHORIZER", ["authorized_by must start with user: or host:"])
    reason = bounded_text(args.reason, "reason", MAX_OBSERVED_CHARS)
    authorization = {
        "authorization_id": str(uuid.uuid4()),
        "authorized_by": authorized_by,
        "authorized_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
    }
    state["gaps_authorized"] = True
    state["gap_authorization"] = authorization
    append_event(state, "gap_authorized", "verification gap authorization recorded")
    validate_shape(state)
    save_state(args.state, state)
    emit({"ok": True, "gap_authorization": authorization, "state": str(args.state), "display_line": display_line(state)})


def command_audit(args: argparse.Namespace) -> None:
    state = load_state(args.state)
    snapshot: dict[str, Any] = {}
    require_v5_write(state, "audit")
    if state["phase"] != "deliver":
        raise GateError("WRONG_PHASE", ["audit requires deliver phase"])
    repo = args.repo.resolve()
    if state["schema_version"] in GIT_STATE_VERSIONS:
        if repo != Path(state["repo"]).resolve():
            raise GateError("AUDIT_REPO_MISMATCH", [str(repo), state["repo"]])
        actual = current_task_paths(state, snapshot)
    else:
        actual = actual_git_files(repo)
    errors: list[str] = []
    undeclared = [path for path in actual if path not in state["changed_files"]]
    outside = [path for path in actual if not in_scope(path, state["write_scope"])]
    if undeclared:
        errors.append("undeclared git changes: " + ", ".join(undeclared))
    if outside:
        errors.append("out-of-scope git changes: " + ", ".join(outside))
    errors.extend(secret_findings(repo, actual))
    task_fingerprint = (
        compute_task_fingerprint(state, snapshot=snapshot) if state.get("schema_version") in GIT_STATE_VERSIONS else "legacy-state"
    )
    if errors:
        state["delivery_audit"] = {
            "passed": False,
            "repo": str(repo),
            "checked_files": actual,
            "task_fingerprint": task_fingerprint,
        }
        save_state(args.state, state)
        raise GateError("DELIVERY_AUDIT_FAILED", errors)
    state["delivery_audit"] = {
        "passed": True,
        "repo": str(repo),
        "checked_files": actual,
        "task_fingerprint": task_fingerprint,
    }
    save_state(args.state, state)
    emit({"ok": True, "checked_files": actual, "state": str(args.state)})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    def add_markdown_content_source(command: argparse.ArgumentParser) -> None:
        source = command.add_mutually_exclusive_group(required=True)
        source.add_argument("--content-file", type=Path)
        source.add_argument("--stdin", action="store_true")

    write_plan = subparsers.add_parser("write-plan", help="create the canonical Plan Markdown file")
    write_plan.add_argument("--file", type=Path, required=True)
    add_markdown_content_source(write_plan)
    write_plan.set_defaults(handler=command_write_plan)

    prepend_requirement = subparsers.add_parser(
        "prepend-requirement", help="place a new requirement above the existing Plan Markdown"
    )
    prepend_requirement.add_argument("--file", type=Path, required=True)
    add_markdown_content_source(prepend_requirement)
    prepend_requirement.set_defaults(handler=command_prepend_requirement)

    status = subparsers.add_parser("status", help="show a bounded state summary; never a release gate")
    status.add_argument("--state", type=Path, required=True)
    status.add_argument("--limit", type=status_limit, default=20)
    status.set_defaults(handler=command_status)

    progress = subparsers.add_parser("progress", help="show a bounded user-visible progress projection")
    progress.add_argument("--state", type=Path, required=True)
    progress.add_argument("--format", choices=("text", "json"), default="text")
    progress.add_argument("--event-limit", type=event_limit, default=5)
    progress.set_defaults(handler=command_progress)

    activity = subparsers.add_parser("set-activity", help="record a short non-gating activity notice")
    activity.add_argument("--state", type=Path, required=True)
    activity.add_argument("--text", required=True)
    activity.set_defaults(handler=command_set_activity)

    context = subparsers.add_parser("context", help="show deterministic read-only task context")
    context.add_argument("--state", type=Path, required=True)
    context.add_argument("--history-limit", type=status_limit, default=3)
    context.add_argument("--path-limit", type=status_limit, default=20)
    context.set_defaults(handler=command_context)

    init = subparsers.add_parser("init", help="create a run state")
    init.add_argument("--state", type=Path, required=True)
    init.add_argument("--repo", type=Path, required=True)
    init.add_argument("--run-id")
    init.add_argument("--mode", choices=("FAST", "FULL"), required=True)
    init.add_argument("--goal", required=True)
    init.add_argument("--write", action="append", default=[])
    init.add_argument("--impact", choices=sorted(IMPACTS), required=True)
    init.add_argument("--risk-detail", action="append", default=[])
    init.add_argument("--plan-file", type=Path)
    init.add_argument("--delivery-required", action="store_true")
    init.add_argument("--max-attempts", type=max_attempts_arg, default=DEFAULT_MAX_ATTEMPTS)
    init.add_argument("--max-replans", type=max_replans_arg, default=DEFAULT_MAX_REPLANS)
    init.set_defaults(handler=command_init)

    plan = subparsers.add_parser("record-plan", help="record the configured planner's current Plan")
    plan.add_argument("--state", type=Path, required=True)
    plan.add_argument("--profile")
    plan.add_argument("--model")
    plan.add_argument("--reasoning-effort")
    plan.add_argument("--verification-spec")
    plan.set_defaults(handler=command_record_plan)

    transition = subparsers.add_parser("transition", help="validate and move to a phase")
    transition.add_argument("--state", type=Path, required=True)
    transition.add_argument("--to", choices=("implement", "verify", "deliver", "complete"), required=True)
    transition.add_argument("--hypothesis")
    transition.set_defaults(handler=command_transition)

    rework = subparsers.add_parser("rework", help="return failed verification to implementation")
    rework.add_argument("--state", type=Path, required=True)
    rework.add_argument("--reason", required=True)
    rework.set_defaults(handler=command_rework)

    diagnosis = subparsers.add_parser("record-diagnosis", help="record a bounded diagnosis from current evidence")
    diagnosis.add_argument("--state", type=Path, required=True)
    diagnosis.add_argument("--input", required=True)
    diagnosis.set_defaults(handler=command_record_diagnosis)

    retry = subparsers.add_parser("retry-verify", help="start a verify-only attempt after an external condition changes")
    retry.add_argument("--state", type=Path, required=True)
    retry.add_argument("--reason", required=True)
    retry.add_argument("--observation")
    retry.set_defaults(handler=command_retry_verify)

    revise_plan = subparsers.add_parser("revise-plan", help="replace a plan after the rework limit")
    revise_plan.add_argument("--state", type=Path, required=True)
    revise_plan.add_argument("--mode", choices=("FAST", "FULL"), required=True)
    revise_plan.add_argument("--goal", required=True)
    revise_plan.add_argument("--write", action="append", default=[])
    revise_plan.add_argument("--impact", choices=sorted(IMPACTS), required=True)
    revise_plan.add_argument("--risk-detail", action="append", default=[])
    revise_plan.add_argument("--reason")
    delivery = revise_plan.add_mutually_exclusive_group()
    delivery.add_argument("--delivery-required", dest="delivery_required", action="store_true")
    delivery.add_argument("--no-delivery-required", dest="delivery_required", action="store_false")
    revise_plan.set_defaults(delivery_required=None)
    revise_plan.set_defaults(handler=command_revise_plan)

    changes = subparsers.add_parser("set-changes", help="record changed files")
    changes.add_argument("--state", type=Path, required=True)
    changes.add_argument("--file", action="append", default=[])
    changes.set_defaults(handler=command_set_changes)

    evidence = subparsers.add_parser("record-evidence", help="append an evidence record")
    evidence.add_argument("--state", type=Path, required=True)
    evidence.add_argument("--kind", choices=("success", "boundary"), required=True)
    evidence.add_argument("--entry", required=True)
    evidence.add_argument("--command", required=True)
    evidence.add_argument("--observed", required=True)
    evidence.add_argument("--level", choices=sorted(LEVELS), required=True)
    evidence.add_argument("--result", choices=("pass", "fail", "blocked"), required=True)
    evidence.add_argument("--check-id")
    evidence.add_argument("--execution-record")
    evidence.set_defaults(handler=command_record_evidence)

    result = subparsers.add_parser("set-result", help="record verification result")
    result.add_argument("--state", type=Path, required=True)
    result.add_argument("--result", choices=("pass", "pass_with_gaps", "blocked", "fail"), required=True)
    result.add_argument("--gap", action="append", default=[])
    result.set_defaults(handler=command_set_result)

    review = subparsers.add_parser("record-review", help="record the configured independent reviewer check")
    review.add_argument("--state", type=Path, required=True)
    review.add_argument("--result", choices=sorted(REVIEW_RESULTS), required=True)
    review.add_argument("--observed", required=True)
    review.add_argument("--profile")
    review.add_argument("--model")
    review.add_argument("--reasoning-effort")
    review.set_defaults(handler=command_record_review)

    authorize = subparsers.add_parser("authorize-gaps", help="record external authorization for verification gaps")
    authorize.add_argument("--state", type=Path, required=True)
    authorize.add_argument("--authorized-by", required=True)
    authorize.add_argument("--reason", required=True)
    authorize.set_defaults(handler=command_authorize_gaps)

    audit = subparsers.add_parser("audit", help="audit Git changes before delivery")
    audit.add_argument("--state", type=Path, required=True)
    audit.add_argument("--repo", type=Path, required=True)
    audit.set_defaults(handler=command_audit)

    check = subparsers.add_parser("check-verification", help="read-only live verification reuse query")
    check.add_argument("--state", type=Path, required=True)
    check.add_argument("--check-id", required=True)
    check.add_argument("--force", action="store_true")
    check.add_argument("--reason")
    check.set_defaults(handler=command_check_verification)
    return parser


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    try:
        args = build_parser().parse_args()
        args.handler(args)
    except GateError as exc:
        emit({"ok": False, "code": exc.code, "details": exc.details}, 2)


if __name__ == "__main__":
    main()
