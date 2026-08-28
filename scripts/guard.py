#!/usr/bin/env python3
"""Deterministic phase gates for Coding Rules Guard."""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


PHASES = {"plan", "implement", "verify", "deliver", "complete"}
IMPACTS = {"no_known_impact", "known_impact", "unverified"}
RESULTS = {"pending", "pass", "pass_with_gaps", "blocked", "fail"}
LEVELS = {"source", "test", "browser", "installed", "host", "production"}
REWORK_WARN_AT = 2
REWORK_REPLAN_AT = 3
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


def validate_shape(state: dict[str, Any]) -> None:
    if not isinstance(state, dict):
        raise GateError("STATE_SCHEMA_INVALID", ["state must be an object"])
    errors: list[str] = []
    if state.get("schema_version") != 1:
        errors.append("schema_version must be 1")
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
    if not isinstance(state.get("replan_required", False), bool):
        errors.append("replan_required must be a boolean")
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
    elif target == "verify":
        if not state["changed_files"]:
            errors.append("changed_files is empty")
        outside = [path for path in state["changed_files"] if not in_scope(path, state["write_scope"])]
        if outside:
            errors.append("out-of-scope changes: " + ", ".join(outside))
    elif target in {"deliver", "complete"}:
        errors.extend(ensure_evidence(state))
        if state["result"] not in {"pass", "pass_with_gaps"}:
            errors.append("verification result must be pass or pass_with_gaps")
        if state["result"] == "pass_with_gaps" and (not state["gaps"] or not state["gaps_authorized"]):
            errors.append("pass_with_gaps requires listed and explicitly authorized gaps")
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


def actual_git_files(repo: Path) -> list[str]:
    files = set(git_lines(repo, "diff", "--name-only", "HEAD"))
    files.update(git_lines(repo, "ls-files", "--others", "--exclude-standard"))
    return sorted(files)


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
    state = {
        "schema_version": 1,
        "run_id": args.run_id or str(uuid.uuid4()),
        "mode": args.mode,
        "phase": "plan",
        "goal": args.goal.strip(),
        "write_scope": sorted({normalize_path(item) for item in args.write}),
        "changed_files": [],
        "evidence": [],
        "risk": {"impact": args.impact, "details": args.risk_detail},
        "result": "pending",
        "gaps": [],
        "delivery_required": args.delivery_required,
        "gaps_authorized": False,
        "delivery_audit": None,
        "rework_count": 0,
        "last_rework_reason": None,
        "rework_streak": 0,
        "replan_required": False,
        "plan_revision": 1,
    }
    validate_shape(state)
    save_state(args.state, state)
    emit({"ok": True, "state": str(args.state), "run_id": state["run_id"], "phase": state["phase"]})


def command_transition(args: argparse.Namespace) -> None:
    state = load_state(args.state)
    check_transition(state, args.to)
    state["phase"] = args.to
    if args.to in {"deliver", "complete"}:
        state["rework_streak"] = 0
    save_state(args.state, state)
    emit({"ok": True, "phase": args.to, "state": str(args.state)})


def command_rework(args: argparse.Namespace) -> None:
    state = load_state(args.state)
    if state["phase"] != "verify":
        raise GateError("WRONG_PHASE", ["rework requires verify phase"])
    if state["result"] != "fail" and not any(item.get("result") == "fail" for item in state["evidence"]):
        raise GateError("REWORK_NOT_JUSTIFIED", ["record failed verification evidence before rework"])
    reason = args.reason.strip()
    if not reason:
        raise GateError("REWORK_REASON_REQUIRED", ["--reason must be non-empty"])

    state["result"] = "pending"
    state["evidence"] = []
    state["gaps"] = []
    state["gaps_authorized"] = False
    state["delivery_audit"] = None
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
    state["delivery_audit"] = None
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
    state["changed_files"] = sorted({normalize_path(item) for item in args.file})
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
    state["gaps_authorized"] = bool(args.authorize_gaps)
    save_state(args.state, state)
    emit({"ok": True, "result": state["result"], "gaps_authorized": state["gaps_authorized"]})


def command_audit(args: argparse.Namespace) -> None:
    state = load_state(args.state)
    if state["phase"] != "deliver":
        raise GateError("WRONG_PHASE", ["audit requires deliver phase"])
    repo = args.repo.resolve()
    actual = actual_git_files(repo)
    errors: list[str] = []
    undeclared = [path for path in actual if path not in state["changed_files"]]
    outside = [path for path in actual if not in_scope(path, state["write_scope"])]
    if undeclared:
        errors.append("undeclared git changes: " + ", ".join(undeclared))
    if outside:
        errors.append("out-of-scope git changes: " + ", ".join(outside))
    errors.extend(secret_findings(repo, actual))
    if errors:
        state["delivery_audit"] = {"passed": False, "repo": str(repo), "checked_files": actual}
        save_state(args.state, state)
        raise GateError("DELIVERY_AUDIT_FAILED", errors)
    state["delivery_audit"] = {"passed": True, "repo": str(repo), "checked_files": actual}
    save_state(args.state, state)
    emit({"ok": True, "checked_files": actual, "state": str(args.state)})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    init = subparsers.add_parser("init", help="create a run state")
    init.add_argument("--state", type=Path, required=True)
    init.add_argument("--run-id")
    init.add_argument("--mode", choices=("FAST", "FULL"), required=True)
    init.add_argument("--goal", required=True)
    init.add_argument("--write", action="append", default=[], required=True)
    init.add_argument("--impact", choices=sorted(IMPACTS), required=True)
    init.add_argument("--risk-detail", action="append", default=[])
    init.add_argument("--delivery-required", action="store_true")
    init.set_defaults(handler=command_init)

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
    revise_plan.add_argument("--write", action="append", default=[], required=True)
    revise_plan.add_argument("--impact", choices=sorted(IMPACTS), required=True)
    revise_plan.add_argument("--risk-detail", action="append", default=[])
    revise_plan.add_argument("--delivery-required", action="store_true")
    revise_plan.set_defaults(handler=command_revise_plan)

    changes = subparsers.add_parser("set-changes", help="record changed files")
    changes.add_argument("--state", type=Path, required=True)
    changes.add_argument("--file", action="append", default=[], required=True)
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
    result.add_argument("--authorize-gaps", action="store_true")
    result.set_defaults(handler=command_set_result)

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
