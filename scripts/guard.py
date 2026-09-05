#!/usr/bin/env python3
"""Deterministic phase gates for Coding Rules Guard."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


PHASES = {"plan", "implement", "verify", "deliver", "complete"}
IMPACTS = {"no_known_impact", "known_impact", "unverified"}
RESULTS = {"pending", "pass", "pass_with_gaps", "blocked", "fail"}
LEVELS = {"source", "test", "browser", "installed", "host", "production"}
REVIEW_RESULTS = {"pass", "fail", "blocked"}
CURRENT_SCHEMA_VERSION = 4
SUPPORTED_SCHEMA_VERSIONS = {1, 2, 3, CURRENT_SCHEMA_VERSION}
REWORK_WARN_AT = 2
REWORK_REPLAN_AT = 3
PLAN_RECORD_MAX_AGE = timedelta(hours=24)
MODEL_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "model-profiles.json"
SECRET_NAME_PATTERNS = (".env", "*.pem", "*.p12", "*.pfx", "id_rsa", "id_ed25519")
SECRET_CONTENT = re.compile(
    r"(?:-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b|\bsk-[A-Za-z0-9]{20,}\b|\bAKIA[0-9A-Z]{16}\b)"
)


class GateError(Exception):
    def __init__(self, code: str, details: Iterable[str]):
        super().__init__(code)
        self.code = code
        self.details = list(details)


def emit(payload: dict[str, Any], exit_code: int = 0) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    raise SystemExit(exit_code)


def normalize_path(value: str) -> str:
    normalized = value.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.rstrip("/")


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
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
        raise GateError("PLAN_STATE_UPGRADE_REQUIRED", ["plan fingerprints require schema v4"])
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
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def ensure_plan_record(state: dict[str, Any]) -> list[str]:
    """Require a current, configured, non-expired planner record before WRITE."""
    errors: list[str] = []
    if state.get("schema_version") != CURRENT_SCHEMA_VERSION:
        return ["PLAN_STATE_UPGRADE_REQUIRED: plan -> implement requires schema v4"]
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


def ensure_review(state: dict[str, Any]) -> list[str]:
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
        current_task_fingerprint = compute_task_fingerprint(state)
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


def validate_shape(state: dict[str, Any]) -> None:
    if not isinstance(state, dict):
        raise GateError("STATE_SCHEMA_INVALID", ["state must be an object"])
    errors: list[str] = []
    schema_version = state.get("schema_version")
    if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        errors.append("schema_version must be 1, 2, 3, or 4")
    if state.get("mode") not in {"FAST", "FULL"}:
        errors.append("mode must be FAST or FULL")
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
    if schema_version in {2, 3, 4}:
        repo = state.get("repo")
        baseline = state.get("git_baseline")
        if not isinstance(repo, str) or not Path(repo).is_absolute():
            errors.append("repo must be an absolute path")
        if not isinstance(baseline, dict) or not isinstance(baseline.get("head"), str):
            errors.append("git_baseline.head is required")
        elif not isinstance(baseline.get("files"), dict):
            errors.append("git_baseline.files must be an object")
        elif schema_version in {3, 4} and not isinstance(baseline.get("content_files"), dict):
            errors.append("git_baseline.content_files must be an object")
        elif schema_version in {3, 4} and not isinstance(baseline.get("index_files"), dict):
            errors.append("git_baseline.index_files must be an object")
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
    if schema_version in {3, 4}:
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
            if item["kind"] not in {"success", "boundary"} or item["level"] not in LEVELS:
                errors.append(f"evidence[{index}] has an invalid kind or level")
            if item["result"] not in {"pass", "fail", "blocked"}:
                errors.append(f"evidence[{index}].result is invalid")
            if any(not isinstance(item[field], str) or not item[field].strip() for field in ("entry", "command", "observed")):
                errors.append(f"evidence[{index}] text fields must be non-empty")
    if errors:
        raise GateError("STATE_SCHEMA_INVALID", errors)


def in_scope(path: str, scopes: Iterable[str]) -> bool:
    candidate = normalize_path(path)
    for raw_scope in scopes:
        scope = normalize_path(raw_scope)
        if not scope:
            continue
        if any(mark in scope for mark in "*?["):
            if fnmatch.fnmatchcase(candidate, scope) or PurePosixPath(candidate).match(scope):
                return True
        elif candidate == scope or candidate.startswith(scope + "/"):
            return True
    return False


def ensure_evidence(state: dict[str, Any]) -> list[str]:
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
    return errors


def check_transition(state: dict[str, Any], target: str) -> None:
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
    elif target in {"deliver", "complete"}:
        errors.extend(ensure_evidence(state))
        errors.extend(ensure_review(state))
        if state["result"] not in {"pass", "pass_with_gaps"}:
            errors.append("verification result must be pass or pass_with_gaps")
        if state["result"] == "pass_with_gaps" and (not state["gaps"] or not state["gaps_authorized"]):
            errors.append("pass_with_gaps requires listed and explicitly authorized gaps")
        if state.get("schema_version") in {2, 3, 4} and state["result"] == "pass_with_gaps":
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
        ["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if result.returncode != 0:
        raise GateError("GIT_COMMAND_FAILED", [result.stderr.strip() or "git command failed"])
    return [normalize_path(line) for line in result.stdout.splitlines() if line.strip()]


def git_bytes(repo: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if result.returncode != 0:
        details = result.stderr.decode("utf-8", errors="replace").strip() or "git command failed"
        raise GateError("GIT_COMMAND_FAILED", [details])
    return result.stdout


def actual_git_files(repo: Path) -> list[str]:
    files = set(git_lines(repo, "diff", "--name-only", "--no-renames", "HEAD"))
    files.update(git_lines(repo, "ls-files", "--others", "--exclude-standard"))
    return sorted(files)


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
    elif full_path.is_file():
        digest.update(b"file\0")
        digest.update(b"executable\0" + (b"1" if full_path.stat().st_mode & 0o111 else b"0"))
        with full_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    elif full_path.is_dir():
        digest.update(b"directory\0")
    else:
        digest.update(b"missing\0")
    return digest.hexdigest()


def git_path_fingerprint(repo: Path, rel: str) -> str:
    digest = hashlib.sha256()
    digest.update(filesystem_path_fingerprint(repo, rel).encode("ascii"))
    digest.update(b"\0combined-diff\0")
    digest.update(git_bytes(repo, "diff", "--binary", "--no-renames", "HEAD", "--", rel))
    digest.update(b"\0index-diff\0")
    digest.update(git_bytes(repo, "diff", "--cached", "--binary", "--no-renames", "HEAD", "--", rel))
    return digest.hexdigest()


def capture_git_baseline(repo: Path) -> dict[str, Any]:
    head = git_lines(repo, "rev-parse", "HEAD")
    if not head:
        raise GateError("GIT_HEAD_REQUIRED", [str(repo)])
    dirty = actual_git_files(repo)
    staged = set(git_lines(repo, "diff", "--cached", "--name-only", "--no-renames", "HEAD"))
    return {
        "head": head[0],
        "files": {path: git_path_fingerprint(repo, path) for path in dirty},
        "content_files": {path: filesystem_path_fingerprint(repo, path) for path in dirty},
        "index_files": {path: git_index_identity(repo, path) for path in staged},
    }


def task_git_files(state: dict[str, Any]) -> list[str]:
    repo = Path(state["repo"])
    current_head = git_lines(repo, "rev-parse", "HEAD")
    baseline = state["git_baseline"]
    if not current_head or current_head[0] != baseline["head"]:
        raise GateError("GIT_BASELINE_MOVED", ["Git HEAD changed after init; start a new run state"])
    current = set(actual_git_files(repo))
    candidates = current | set(baseline["files"])
    changed = [
        path
        for path in candidates
        if git_path_fingerprint(repo, path) != baseline["files"].get(path)
    ]
    return sorted(changed)


def git_is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", ancestor, descendant],
        capture_output=True,
    )
    if result.returncode not in {0, 1}:
        raise GateError("GIT_COMMAND_FAILED", [result.stderr.decode("utf-8", errors="replace").strip()])
    return result.returncode == 0


def task_delta_files(state: dict[str, Any]) -> list[str]:
    schema_version = state.get("schema_version")
    if schema_version == 1:
        raise GateError("REVIEW_STATE_UPGRADE_REQUIRED", ["v1 state has no Git baseline for review binding"])
    repo = Path(state["repo"])
    baseline = state["git_baseline"]
    current_head_lines = git_lines(repo, "rev-parse", "HEAD")
    if not current_head_lines:
        raise GateError("GIT_HEAD_REQUIRED", [str(repo)])
    current_head = current_head_lines[0]
    if current_head == baseline["head"]:
        return task_git_files(state)
    if schema_version == 2:
        raise GateError(
            "REVIEW_STATE_UPGRADE_REQUIRED",
            ["v2 state cannot bind review across a changed Git HEAD; rebuild the run with schema v4"],
        )
    if not git_is_ancestor(repo, baseline["head"], current_head):
        raise GateError("GIT_BASELINE_MOVED", ["baseline HEAD is not an ancestor of current HEAD"])

    committed = set(git_lines(repo, "diff", "--name-only", "--no-renames", baseline["head"], current_head))
    baseline_content = baseline.get("content_files")
    if not isinstance(baseline_content, dict):
        raise GateError("REVIEW_STATE_UPGRADE_REQUIRED", ["baseline content fingerprints are missing; rebuild with schema v4"])
    residual: set[str] = set()
    for rel in actual_git_files(repo):
        if rel in baseline_content and filesystem_path_fingerprint(repo, rel) == baseline_content[rel]:
            continue
        residual.add(rel)
    return sorted(committed | residual)


def git_index_identity(repo: Path, rel: str) -> str:
    lines = git_lines(repo, "ls-files", "-s", "--", rel)
    if not lines:
        return "missing"
    fields = lines[0].split()
    if len(fields) < 2:
        raise GateError("GIT_INDEX_INVALID", [rel, lines[0]])
    return f"{fields[0]}:{fields[1]}"


def git_tree_identity(repo: Path, treeish: str, rel: str) -> str:
    lines = git_lines(repo, "ls-tree", treeish, "--", rel)
    if not lines:
        return "missing"
    fields = lines[0].split()
    if len(fields) < 3:
        raise GateError("GIT_TREE_INVALID", [rel, lines[0]])
    return f"{fields[0]}:{fields[2]}"


def task_staged_files(state: dict[str, Any]) -> list[str]:
    repo = Path(state["repo"])
    baseline = state["git_baseline"]
    current_head = git_lines(repo, "rev-parse", "HEAD")
    if not current_head or current_head[0] != baseline["head"]:
        raise GateError("GIT_BASELINE_MOVED", ["staged review requires the baseline HEAD"])
    baseline_index = baseline.get("index_files", {})
    current_staged = set(git_lines(repo, "diff", "--cached", "--name-only", "--no-renames", "HEAD"))
    candidates = current_staged | set(baseline_index)
    changed: list[str] = []
    for rel in candidates:
        baseline_identity = (
            baseline_index[rel]
            if rel in baseline_index
            else git_tree_identity(repo, baseline["head"], rel)
        )
        if git_index_identity(repo, rel) != baseline_identity:
            changed.append(rel)
    return sorted(changed)


def fingerprint_identities(baseline_head: str, identities: list[tuple[str, str]]) -> str:
    digest = hashlib.sha256()
    digest.update(baseline_head.encode("utf-8"))
    for rel, identity in identities:
        digest.update(b"\0path\0")
        digest.update(rel.encode("utf-8", errors="surrogatepass"))
        digest.update(b"\0identity\0")
        digest.update(identity.encode("utf-8"))
    return digest.hexdigest()


def compute_task_fingerprint(state: dict[str, Any]) -> str:
    if state.get("schema_version") not in {2, 3, 4}:
        raise GateError("REVIEW_STATE_UPGRADE_REQUIRED", ["state lacks a reviewable Git baseline"])
    repo = Path(state["repo"])
    baseline_head = state["git_baseline"]["head"]
    current_head_lines = git_lines(repo, "rev-parse", "HEAD")
    if not current_head_lines:
        raise GateError("GIT_HEAD_REQUIRED", [str(repo)])
    current_head = current_head_lines[0]

    if state.get("delivery_required"):
        if current_head == baseline_head:
            staged = task_staged_files(state)
            expected = sorted(state.get("changed_files", []))
            if staged != expected:
                raise GateError(
                    "REVIEW_STAGE_MISMATCH",
                    [f"staged task paths {staged!r} do not match detected task paths {expected!r}"],
                )
            unstaged = set(git_lines(repo, "diff", "--name-only", "--no-renames"))
            split = sorted(unstaged & set(expected))
            if split:
                raise GateError("REVIEW_STAGE_MISMATCH", ["index/worktree split: " + ", ".join(split)])
            identities = [(rel, git_index_identity(repo, rel)) for rel in staged]
            return fingerprint_identities(baseline_head, identities)

        if state.get("schema_version") == 2:
            raise GateError(
                "REVIEW_STATE_UPGRADE_REQUIRED",
                ["v2 state cannot bind review to a final commit tree; rebuild with schema v4"],
            )
        if not git_is_ancestor(repo, baseline_head, current_head):
            raise GateError("GIT_BASELINE_MOVED", ["baseline HEAD is not an ancestor of current HEAD"])
        committed = sorted(
            set(git_lines(repo, "diff", "--name-only", "--no-renames", baseline_head, current_head))
        )
        identities = [(rel, git_tree_identity(repo, current_head, rel)) for rel in committed]
        return fingerprint_identities(baseline_head, identities)

    staged = task_staged_files(state)
    if staged:
        raise GateError(
            "REVIEW_STAGE_MISMATCH",
            ["staged task paths are not part of the reviewed worktree: " + ", ".join(staged)],
        )
    identities = [(rel, filesystem_path_fingerprint(repo, rel)) for rel in task_delta_files(state)]
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


def command_init(args: argparse.Namespace) -> None:
    repo = git_repo_root(args.repo)
    git_baseline = capture_git_baseline(repo)
    config = load_model_config()
    planner_name, _ = configured_role(config, "planner")
    executor_name, _ = configured_role(config, "executor")
    reviewer_name, _ = configured_role(config, "reviewer")
    write_scope = sorted({normalize_path(item) for item in args.write})
    review_required = bool(write_scope)
    plan_file = None
    if args.plan_file is not None:
        plan_path = args.plan_file.absolute()
        if not plan_path.is_file():
            raise GateError("PLAN_FILE_NOT_FOUND", [str(plan_path), "write the Plan before init"])
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
    }
    validate_shape(state)
    save_state(args.state, state)
    emit({"ok": True, "state": str(args.state), "run_id": state["run_id"], "phase": state["phase"]})


def command_record_plan(args: argparse.Namespace) -> None:
    state = load_state(args.state)
    if state["phase"] != "plan":
        raise GateError("WRONG_PHASE", ["record-plan requires plan phase"])
    if state.get("schema_version") != CURRENT_SCHEMA_VERSION:
        raise GateError("PLAN_STATE_UPGRADE_REQUIRED", ["record-plan requires schema v4"])
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

    state["plan_record"] = {
        "profile": planner_name,
        "model": planned_model,
        "reasoning_effort": planned_effort,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "plan_revision": state["plan_revision"],
        "plan_fingerprint": compute_plan_fingerprint(state),
    }
    validate_shape(state)
    save_state(args.state, state)
    emit({"ok": True, "plan_record": state["plan_record"], "state": str(args.state)})


def command_transition(args: argparse.Namespace) -> None:
    state = load_state(args.state)
    check_transition(state, args.to)
    deleted_plan_file = None
    if args.to == "complete":
        ensure_plan_file_binding(state)
        deleted_plan_file = delete_plan_file(state)
    state["phase"] = args.to
    if args.to in {"deliver", "complete"}:
        state["rework_streak"] = 0
    save_state(args.state, state)
    emit({"ok": True, "phase": args.to, "plan_deleted": deleted_plan_file, "state": str(args.state)})


def command_rework(args: argparse.Namespace) -> None:
    state = load_state(args.state)
    if state["phase"] != "verify":
        raise GateError("WRONG_PHASE", ["rework requires verify phase"])
    review_failed = isinstance(state.get("review"), dict) and state["review"].get("result") == "fail"
    if (
        state["result"] != "fail"
        and not any(item.get("result") == "fail" for item in state["evidence"])
        and not review_failed
    ):
        raise GateError("REWORK_NOT_JUSTIFIED", ["record failed verification evidence before rework"])
    reason = args.reason.strip()
    if not reason:
        raise GateError("REWORK_REASON_REQUIRED", ["--reason must be non-empty"])

    state["result"] = "pending"
    state["evidence"] = []
    state["gaps"] = []
    state["gaps_authorized"] = False
    state["gap_authorization"] = None
    state["delivery_audit"] = None
    if "review" in state or state.get("schema_version") == CURRENT_SCHEMA_VERSION:
        state["review"] = None
    state["rework_count"] = int(state.get("rework_count", 0)) + 1
    state["rework_streak"] = int(state.get("rework_streak", 0)) + 1
    state["last_rework_reason"] = reason
    payload: dict[str, Any] = {
        "ok": True,
        "result": state["result"],
        "rework_count": state["rework_count"],
        "rework_streak": state["rework_streak"],
        "state": str(args.state),
    }
    if state["rework_streak"] >= REWORK_REPLAN_AT:
        state["phase"] = "plan"
        state["replan_required"] = True
        if state.get("schema_version") == CURRENT_SCHEMA_VERSION:
            state["plan_record"] = None
        payload["code"] = "REPLAN_REQUIRED"
    else:
        state["phase"] = "implement"
        state["replan_required"] = False
        if state["rework_streak"] >= REWORK_WARN_AT:
            payload["warning"] = "REPLAN_RECOMMENDED"
    payload["phase"] = state["phase"]
    validate_shape(state)
    save_state(args.state, state)
    emit(payload)


def command_revise_plan(args: argparse.Namespace) -> None:
    state = load_state(args.state)
    if state["phase"] != "plan" or not state.get("replan_required", False):
        raise GateError("REPLAN_NOT_REQUIRED", ["revise-plan requires a forced replan state"])

    revised_scope = sorted({normalize_path(item) for item in args.write})
    outside = [path for path in state["changed_files"] if not in_scope(path, revised_scope)]
    if outside:
        raise GateError(
            "REPLAN_SCOPE_CONFLICT",
            ["existing changes fall outside the revised write scope: " + ", ".join(outside)],
        )

    state["mode"] = args.mode
    state["goal"] = args.goal.strip()
    state["write_scope"] = revised_scope
    state["risk"] = {"impact": args.impact, "details": args.risk_detail}
    state["delivery_required"] = args.delivery_required
    state["result"] = "pending"
    state["evidence"] = []
    state["gaps"] = []
    state["gaps_authorized"] = False
    state["gap_authorization"] = None
    state["delivery_audit"] = None
    if state.get("schema_version") == CURRENT_SCHEMA_VERSION:
        config = load_model_config()
        planner_name, _ = configured_role(config, "planner")
        executor_name, _ = configured_role(config, "executor")
        reviewer_name, _ = configured_role(config, "reviewer")
        state["planner_profile"] = planner_name
        state["plan_record"] = None
        state["review_required"] = bool(revised_scope)
        state["executor_profile"] = executor_name if revised_scope else None
        state["reviewer_profile"] = reviewer_name
        state["review"] = None
    state["rework_streak"] = 0
    state["replan_required"] = False
    state["plan_revision"] = int(state.get("plan_revision", 1)) + 1
    validate_shape(state)
    save_state(args.state, state)
    emit(
        {
            "ok": True,
            "phase": state["phase"],
            "plan_revision": state["plan_revision"],
            "rework_streak": state["rework_streak"],
            "state": str(args.state),
        }
    )


def command_set_changes(args: argparse.Namespace) -> None:
    state = load_state(args.state)
    if state["phase"] != "implement":
        raise GateError("WRONG_PHASE", ["set-changes requires implement phase"])
    if state["schema_version"] in {2, 3, 4}:
        if args.file:
            raise GateError("DECLARED_CHANGES_FORBIDDEN", ["v2 states derive changes from the Git baseline"])
        state["changed_files"] = task_git_files(state)
    else:
        if not args.file:
            raise GateError("DECLARED_CHANGES_REQUIRED", ["legacy v1 states require --file"])
        state["changed_files"] = sorted({normalize_path(item) for item in args.file})
    if "review" in state or state.get("schema_version") == CURRENT_SCHEMA_VERSION:
        state["review"] = None
    validate_shape(state)
    save_state(args.state, state)
    emit({"ok": True, "changed_files": state["changed_files"]})


def command_record_evidence(args: argparse.Namespace) -> None:
    state = load_state(args.state)
    if state["phase"] != "verify":
        raise GateError("WRONG_PHASE", ["record-evidence requires verify phase"])
    state["evidence"].append(
        {
            "kind": args.kind,
            "entry": args.entry,
            "command": args.command,
            "observed": args.observed,
            "level": args.level,
            "result": args.result,
        }
    )
    if "review" in state or state.get("schema_version") == CURRENT_SCHEMA_VERSION:
        state["review"] = None
    validate_shape(state)
    save_state(args.state, state)
    emit({"ok": True, "evidence_count": len(state["evidence"])})


def command_set_result(args: argparse.Namespace) -> None:
    state = load_state(args.state)
    if state["phase"] != "verify":
        raise GateError("WRONG_PHASE", ["set-result requires verify phase"])
    if args.result == "pass_with_gaps" and not args.gap:
        raise GateError("GAPS_REQUIRED", ["pass_with_gaps requires at least one --gap"])
    state["result"] = args.result
    state["gaps"] = args.gap
    state["gaps_authorized"] = False
    state["gap_authorization"] = None
    if "review" in state or state.get("schema_version") == CURRENT_SCHEMA_VERSION:
        state["review"] = None
    validate_shape(state)
    save_state(args.state, state)
    emit({"ok": True, "result": state["result"], "gaps_authorized": state["gaps_authorized"]})


def command_record_review(args: argparse.Namespace) -> None:
    state = load_state(args.state)
    if state["phase"] != "verify":
        raise GateError("WRONG_PHASE", ["record-review requires verify phase"])
    if not review_is_required(state):
        raise GateError("REVIEW_NOT_REQUIRED", ["read-only tasks do not require an independent review"])
    if state["result"] not in {"pass", "pass_with_gaps"}:
        raise GateError(
            "REVIEW_NOT_READY",
            ["set verification result to pass or pass_with_gaps before recording the review"],
        )

    config = load_model_config()
    executor_name, _ = configured_role(config, "executor")
    reviewer_name, reviewer = configured_role(config, "reviewer")
    if state.get("schema_version") in {1, 2}:
        state["review_required"] = bool(state.get("write_scope"))
        state["executor_profile"] = executor_name if state["review_required"] else None
        state["reviewer_profile"] = reviewer_name
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
    observed = args.observed.strip()
    if not observed:
        raise GateError("REVIEW_OBSERVED_REQUIRED", ["--observed must be non-empty"])
    reviewed_at = datetime.now(timezone.utc).isoformat()
    state["review"] = {
        "profile": reviewer_name,
        "model": reviewed_model,
        "reasoning_effort": reviewed_effort,
        "result": args.result,
        "observed": observed,
        "reviewed_at": reviewed_at,
        "task_fingerprint": compute_task_fingerprint(state),
    }
    validate_shape(state)
    save_state(args.state, state)
    emit({"ok": True, "review": state["review"], "state": str(args.state)})


def command_authorize_gaps(args: argparse.Namespace) -> None:
    state = load_state(args.state)
    if state["phase"] != "verify":
        raise GateError("WRONG_PHASE", ["authorize-gaps requires verify phase"])
    if state["result"] != "pass_with_gaps" or not state["gaps"]:
        raise GateError("GAPS_NOT_PENDING", ["set pass_with_gaps and list gaps before authorization"])
    authorized_by = args.authorized_by.strip()
    if not re.fullmatch(r"(?:user|host):[^\s].*", authorized_by):
        raise GateError("INVALID_AUTHORIZER", ["authorized_by must start with user: or host:"])
    reason = args.reason.strip()
    if not reason:
        raise GateError("AUTHORIZATION_REASON_REQUIRED", ["--reason must be non-empty"])
    authorization = {
        "authorization_id": str(uuid.uuid4()),
        "authorized_by": authorized_by,
        "authorized_at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
    }
    state["gaps_authorized"] = True
    state["gap_authorization"] = authorization
    validate_shape(state)
    save_state(args.state, state)
    emit({"ok": True, "gap_authorization": authorization, "state": str(args.state)})


def command_audit(args: argparse.Namespace) -> None:
    state = load_state(args.state)
    if state["phase"] != "deliver":
        raise GateError("WRONG_PHASE", ["audit requires deliver phase"])
    repo = args.repo.resolve()
    if state["schema_version"] in {2, 3, 4}:
        if repo != Path(state["repo"]).resolve():
            raise GateError("AUDIT_REPO_MISMATCH", [str(repo), state["repo"]])
        actual = task_git_files(state)
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
        compute_task_fingerprint(state) if state.get("schema_version") in {2, 3, 4} else "legacy-state"
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
    init.set_defaults(handler=command_init)

    plan = subparsers.add_parser("record-plan", help="record the configured planner's current Plan")
    plan.add_argument("--state", type=Path, required=True)
    plan.add_argument("--profile")
    plan.add_argument("--model")
    plan.add_argument("--reasoning-effort")
    plan.set_defaults(handler=command_record_plan)

    transition = subparsers.add_parser("transition", help="validate and move to a phase")
    transition.add_argument("--state", type=Path, required=True)
    transition.add_argument("--to", choices=("implement", "verify", "deliver", "complete"), required=True)
    transition.set_defaults(handler=command_transition)

    rework = subparsers.add_parser("rework", help="return failed verification to implementation")
    rework.add_argument("--state", type=Path, required=True)
    rework.add_argument("--reason", required=True)
    rework.set_defaults(handler=command_rework)

    revise_plan = subparsers.add_parser("revise-plan", help="replace a plan after the rework limit")
    revise_plan.add_argument("--state", type=Path, required=True)
    revise_plan.add_argument("--mode", choices=("FAST", "FULL"), required=True)
    revise_plan.add_argument("--goal", required=True)
    revise_plan.add_argument("--write", action="append", default=[])
    revise_plan.add_argument("--impact", choices=sorted(IMPACTS), required=True)
    revise_plan.add_argument("--risk-detail", action="append", default=[])
    revise_plan.add_argument("--delivery-required", action="store_true")
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
    return parser


def main() -> None:
    try:
        args = build_parser().parse_args()
        args.handler(args)
    except GateError as exc:
        emit({"ok": False, "code": exc.code, "details": exc.details}, 2)


if __name__ == "__main__":
    main()
