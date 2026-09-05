import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
GUARD_PATH = ROOT / "scripts" / "guard.py"
SPEC = importlib.util.spec_from_file_location("guard", GUARD_PATH)
assert SPEC and SPEC.loader
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


class GuardUnitTests(unittest.TestCase):
    def base_state(self):
        return {
            "schema_version": 1,
            "run_id": "test-run",
            "mode": "FAST",
            "phase": "plan",
            "goal": "make a bounded change",
            "write_scope": ["src/allowed"],
            "changed_files": [],
            "evidence": [],
            "risk": {"impact": "no_known_impact", "details": []},
            "result": "pending",
            "gaps": [],
            "delivery_required": False,
            "gaps_authorized": False,
            "delivery_audit": None,
        }

    def test_path_scope_accepts_directory_and_glob(self):
        self.assertTrue(guard.in_scope("src/allowed/file.py", ["src/allowed"]))
        self.assertTrue(guard.in_scope("tests/test_one.py", ["tests/*.py"]))
        self.assertFalse(guard.in_scope("src/other.py", ["src/allowed"]))

    def test_implement_requires_assessed_risk(self):
        state = self.base_state()
        state["risk"]["impact"] = "unverified"
        with self.assertRaises(guard.GateError) as raised:
            guard.check_transition(state, "implement")
        self.assertEqual(raised.exception.code, "TRANSITION_BLOCKED")

    def test_state_rejects_empty_write_scope_entry(self):
        state = self.base_state()
        state["write_scope"] = [""]
        with self.assertRaises(guard.GateError) as raised:
            guard.validate_shape(state)
        self.assertEqual(raised.exception.code, "STATE_SCHEMA_INVALID")

    def test_verify_blocks_out_of_scope_change(self):
        state = self.base_state()
        state["phase"] = "implement"
        state["changed_files"] = ["src/other.py"]
        with self.assertRaises(guard.GateError) as raised:
            guard.check_transition(state, "verify")
        self.assertIn("out-of-scope", " ".join(raised.exception.details))

    def test_complete_requires_success_and_boundary_evidence(self):
        state = self.base_state()
        state["phase"] = "verify"
        state["result"] = "pass"
        state["evidence"] = [
            {"kind": "success", "entry": "run", "command": "test", "observed": "ok", "level": "test", "result": "pass"}
        ]
        with self.assertRaises(guard.GateError):
            guard.check_transition(state, "complete")

    def test_pass_with_gaps_requires_authorization(self):
        state = self.base_state()
        state["phase"] = "verify"
        state["result"] = "pass_with_gaps"
        state["gaps"] = ["host unavailable"]
        state["evidence"] = [
            {"kind": "success", "entry": "run", "command": "test", "observed": "ok", "level": "test", "result": "pass"},
            {"kind": "boundary", "entry": "host", "command": "host test", "observed": "host unavailable", "level": "host", "result": "blocked"},
        ]
        with self.assertRaises(guard.GateError):
            guard.check_transition(state, "complete")
        state["gaps_authorized"] = True
        state["review_required"] = True
        state["reviewer_profile"] = "session_main"
        state["review"] = {
            "profile": "session_main",
            "model": "gpt-test-main",
            "reasoning_effort": "high",
            "result": "pass",
            "observed": "legacy state independently reviewed",
            "reviewed_at": "2026-08-29T00:00:00+00:00",
            "task_fingerprint": "legacy-state",
        }
        with self.assertRaises(guard.GateError) as raised:
            guard.check_transition(state, "complete")
        self.assertIn("REVIEW_STATE_UPGRADE_REQUIRED", " ".join(raised.exception.details))

    def test_legacy_write_state_cannot_complete_without_review(self):
        state = self.base_state()
        state["phase"] = "verify"
        state["result"] = "pass"
        state["evidence"] = [
            {"kind": "success", "entry": "run", "command": "test", "observed": "ok", "level": "test", "result": "pass"},
            {"kind": "boundary", "entry": "edge", "command": "test edge", "observed": "ok", "level": "test", "result": "pass"},
        ]
        with self.assertRaises(guard.GateError) as raised:
            guard.check_transition(state, "complete")
        self.assertIn("reviewer record is required", " ".join(raised.exception.details))

    def test_failed_evidence_cannot_be_reported_as_pass(self):
        state = self.base_state()
        state["phase"] = "verify"
        state["result"] = "pass"
        state["evidence"] = [
            {"kind": "success", "entry": "run", "command": "test", "observed": "ok", "level": "test", "result": "pass"},
            {"kind": "boundary", "entry": "edge", "command": "test edge", "observed": "failed", "level": "test", "result": "fail"},
        ]
        with self.assertRaises(guard.GateError) as raised:
            guard.check_transition(state, "complete")
        self.assertIn("failed evidence", " ".join(raised.exception.details))

    def test_missing_planner_configuration_blocks_without_fallback(self):
        config = json.loads(guard.MODEL_CONFIG_PATH.read_text(encoding="utf-8"))
        del config["delegation"]["planner_profile"]
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "model-profiles.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with patch.object(guard, "MODEL_CONFIG_PATH", config_path):
                with self.assertRaises(guard.GateError) as raised:
                    guard.load_model_config()
        self.assertEqual(raised.exception.code, "MODEL_CONFIG_INVALID")
        self.assertIn("planner_profile", " ".join(raised.exception.details))


class GuardCliTests(unittest.TestCase):
    def run_guard(self, *args):
        command = list(args)
        if command and command[0] in {"record-plan", "record-review"}:
            if "--profile" not in command:
                command.extend(("--profile", "session_main"))
            if "--model" not in command:
                command.extend(("--model", "gpt-test-main"))
            if "--reasoning-effort" not in command:
                command.extend(("--reasoning-effort", "high"))
        return subprocess.run(
            [sys.executable, str(GUARD_PATH), *command], capture_output=True, text=True, encoding="utf-8"
        )

    def git(self, repo, *args):
        result = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def create_repo(self, root, rel="src/a.py"):
        repo = root / "repo"
        repo.mkdir()
        self.git(repo, "init", "-b", "main")
        self.git(repo, "config", "user.name", "Test User")
        self.git(repo, "config", "user.email", "test@example.invalid")
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("value = 1\n", encoding="utf-8")
        self.git(repo, "add", "--", rel)
        self.git(repo, "commit", "-m", "initial")
        return repo, target

    def create_planned_run(self, root, goal="plan gate"):
        repo, target = self.create_repo(root)
        state = root / "state.json"
        result = self.run_guard(
            "init", "--state", str(state), "--repo", str(repo), "--mode", "FAST",
            "--goal", goal, "--write", "src/a.py", "--impact", "no_known_impact"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return repo, target, state

    def create_verified_run(self, root):
        repo, target = self.create_repo(root)
        state = root / "state.json"
        result = self.run_guard(
            "init", "--state", str(state), "--repo", str(repo), "--mode", "FAST",
            "--goal", "review gate", "--write", "src/a.py", "--impact", "no_known_impact"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        initialized = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual(initialized["schema_version"], 4)
        self.assertTrue(initialized["review_required"])
        self.assertEqual(initialized["planner_profile"], "session_main")
        self.assertIsNone(initialized["plan_record"])
        self.assertEqual(initialized["executor_profile"], "executor_default")
        self.assertEqual(initialized["reviewer_profile"], "session_main")
        self.assertIsNone(initialized["review"])
        target.write_text("value = 2\n", encoding="utf-8")
        commands = [
            ("record-plan", "--state", str(state)),
            ("transition", "--state", str(state), "--to", "implement"),
            ("set-changes", "--state", str(state)),
            ("transition", "--state", str(state), "--to", "verify"),
            ("record-evidence", "--state", str(state), "--kind", "success", "--entry", "unit", "--command", "test", "--observed", "passed", "--level", "test", "--result", "pass"),
            ("record-evidence", "--state", str(state), "--kind", "boundary", "--entry", "edge", "--command", "test edge", "--observed", "passed", "--level", "test", "--result", "pass"),
            ("set-result", "--state", str(state), "--result", "pass"),
        ]
        for command in commands:
            result = self.run_guard(*command)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return repo, state

    def test_plan_record_is_required_then_unlocks_implementation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, target, state = self.create_planned_run(root)
            target.write_text("value = 2\n", encoding="utf-8")
            result = self.run_guard("transition", "--state", str(state), "--to", "implement")
            self.assertEqual(result.returncode, 2)
            self.assertIn("planner record", " ".join(json.loads(result.stdout)["details"]))

            result = self.run_guard(
                "record-plan", "--state", str(state), "--profile", "session_main",
                "--model", "gpt-main-under-test", "--reasoning-effort", "high"
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            record = json.loads(state.read_text(encoding="utf-8"))["plan_record"]
            self.assertEqual(record["profile"], "session_main")
            self.assertEqual(record["model"], "gpt-main-under-test")
            self.assertEqual(record["reasoning_effort"], "high")
            self.assertEqual(record["plan_revision"], 1)
            self.assertTrue(record["plan_fingerprint"])
            self.assertTrue(record["recorded_at"].endswith("+00:00"))

            result = self.run_guard("transition", "--state", str(state), "--to", "implement")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_record_plan_rejects_wrong_profile_model_and_effort(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, _, state = self.create_planned_run(root)
            result = self.run_guard("record-plan", "--state", str(state), "--profile", "wrong-planner")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["code"], "PLAN_PROFILE_MISMATCH")
            self.assertIsNone(json.loads(state.read_text(encoding="utf-8"))["plan_record"])

    def test_session_main_plan_requires_model_and_reasoning(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, _, state = self.create_planned_run(root)
            result = subprocess.run(
                [sys.executable, str(GUARD_PATH), "record-plan", "--state", str(state)],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["code"], "SESSION_MAIN_MODEL_REQUIRED")

    def test_plan_record_blocks_tampered_fields_and_expired_revision(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, _, state = self.create_planned_run(root)
            result = self.run_guard("record-plan", "--state", str(state))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(state.read_text(encoding="utf-8"))

            payload["goal"] = "tampered plan"
            state.write_text(json.dumps(payload), encoding="utf-8")
            result = self.run_guard("transition", "--state", str(state), "--to", "implement")
            self.assertEqual(result.returncode, 2)
            self.assertIn("PLAN_STALE", " ".join(json.loads(result.stdout)["details"]))

            payload["goal"] = "plan gate"
            payload["plan_revision"] = 2
            state.write_text(json.dumps(payload), encoding="utf-8")
            result = self.run_guard("transition", "--state", str(state), "--to", "implement")
            self.assertEqual(result.returncode, 2)
            self.assertIn("PLAN_RECORD_EXPIRED", " ".join(json.loads(result.stdout)["details"]))

    def test_legacy_plan_states_fail_closed_for_implementation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, _, state = self.create_planned_run(root)
            result = self.run_guard("record-plan", "--state", str(state))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(state.read_text(encoding="utf-8"))
            for schema_version in (1, 2, 3):
                payload["schema_version"] = schema_version
                payload["phase"] = "plan"
                state.write_text(json.dumps(payload), encoding="utf-8")
                result = self.run_guard("transition", "--state", str(state), "--to", "implement")
                self.assertEqual(result.returncode, 2)
                self.assertIn("PLAN_STATE_UPGRADE_REQUIRED", " ".join(json.loads(result.stdout)["details"]))

    def test_planner_configuration_drift_blocks_implementation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, _, state_path = self.create_planned_run(root)
            result = self.run_guard("record-plan", "--state", str(state_path))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            state = guard.load_state(state_path)
            config = json.loads(guard.MODEL_CONFIG_PATH.read_text(encoding="utf-8"))
            config["profiles"]["session_main"]["source"] = "invalid-source"
            config_path = root / "model-profiles.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with patch.object(guard, "MODEL_CONFIG_PATH", config_path):
                with self.assertRaises(guard.GateError) as raised:
                    guard.check_transition(state, "implement")
            self.assertIn("planner configuration unavailable", " ".join(raised.exception.details))

    def test_new_write_state_requires_an_independent_review(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, state = self.create_verified_run(root)
            result = self.run_guard("transition", "--state", str(state), "--to", "complete")
            self.assertEqual(result.returncode, 2)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["code"], "TRANSITION_BLOCKED")
            self.assertIn("reviewer record is required", " ".join(payload["details"]))

    def test_executor_profile_mismatch_blocks_implementation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo, _ = self.create_repo(root)
            state = root / "state.json"
            result = self.run_guard(
                "init", "--state", str(state), "--repo", str(repo), "--mode", "FAST",
                "--goal", "executor gate", "--write", "src/a.py", "--impact", "no_known_impact"
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = self.run_guard("record-plan", "--state", str(state))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(state.read_text(encoding="utf-8"))
            payload["executor_profile"] = "wrong-executor"
            state.write_text(json.dumps(payload), encoding="utf-8")
            result = self.run_guard("transition", "--state", str(state), "--to", "implement")
            self.assertEqual(result.returncode, 2)
            self.assertIn("executor profile mismatch", " ".join(json.loads(result.stdout)["details"]))

    def test_failed_independent_review_blocks_completion(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, state = self.create_verified_run(root)
            result = self.run_guard(
                "record-review", "--state", str(state), "--result", "fail",
                "--observed", "Sol xhigh found an implementation issue"
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = self.run_guard("transition", "--state", str(state), "--to", "complete")
            self.assertEqual(result.returncode, 2)
            self.assertIn("reviewer result must be pass", " ".join(json.loads(result.stdout)["details"]))
            result = self.run_guard(
                "rework", "--state", str(state), "--reason", "Sol review found an implementation defect"
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(state.read_text(encoding="utf-8"))["phase"], "implement")

    def test_session_main_review_allows_completion(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, state = self.create_verified_run(root)
            result = self.run_guard(
                "record-review", "--state", str(state), "--result", "pass",
                "--observed", "current session main model checked the passing evidence",
                "--profile", "session_main", "--model", "gpt-main-under-test", "--reasoning-effort", "high"
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            review = json.loads(state.read_text(encoding="utf-8"))["review"]
            self.assertEqual(review["profile"], "session_main")
            self.assertEqual(review["model"], "gpt-main-under-test")
            self.assertEqual(review["reasoning_effort"], "high")
            self.assertTrue(review["observed"])
            self.assertTrue(review["reviewed_at"].endswith("+00:00"))
            self.assertTrue(review["task_fingerprint"])
            result = self.run_guard("transition", "--state", str(state), "--to", "complete")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_session_main_review_requires_model_and_reasoning(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, state = self.create_verified_run(root)
            result = subprocess.run(
                [
                    sys.executable, str(GUARD_PATH), "record-review", "--state", str(state),
                    "--result", "pass", "--observed", "review without session model",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["code"], "SESSION_MAIN_MODEL_REQUIRED")

    def test_review_becomes_stale_after_same_file_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo, state = self.create_verified_run(root)
            result = self.run_guard(
                "record-review", "--state", str(state), "--result", "pass", "--observed", "independent pass"
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            (repo / "src" / "a.py").write_text("value = 3\n", encoding="utf-8")
            result = self.run_guard("transition", "--state", str(state), "--to", "complete")
            self.assertEqual(result.returncode, 2)
            self.assertIn("REVIEW_STALE", " ".join(json.loads(result.stdout)["details"]))

    def test_v2_review_uses_real_fingerprint_and_becomes_stale(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo, state = self.create_verified_run(root)
            payload = json.loads(state.read_text(encoding="utf-8"))
            payload["schema_version"] = 2
            state.write_text(json.dumps(payload), encoding="utf-8")
            result = self.run_guard(
                "record-review", "--state", str(state), "--result", "pass", "--observed", "legacy v2 reviewed"
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            (repo / "src" / "a.py").write_text("value = 4\n", encoding="utf-8")
            result = self.run_guard("transition", "--state", str(state), "--to", "complete")
            self.assertEqual(result.returncode, 2)
            self.assertIn("REVIEW_STALE", " ".join(json.loads(result.stdout)["details"]))

    def test_set_result_invalidates_an_existing_review(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, state = self.create_verified_run(root)
            commands = [
                ("record-review", "--state", str(state), "--result", "pass", "--observed", "independent pass"),
                ("set-result", "--state", str(state), "--result", "pass"),
            ]
            for command in commands:
                result = self.run_guard(*command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIsNone(json.loads(state.read_text(encoding="utf-8"))["review"])
            result = self.run_guard("transition", "--state", str(state), "--to", "complete")
            self.assertEqual(result.returncode, 2)
            self.assertIn("reviewer record is required", " ".join(json.loads(result.stdout)["details"]))

    def test_cli_happy_path_without_delivery(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo, target = self.create_repo(root)
            state = root / "state.json"
            plan = root / "work" / "plan.md"
            plan.parent.mkdir()
            plan.write_text("### MODE\n\n`FAST`\n", encoding="utf-8")
            result = self.run_guard(
                "init", "--state", str(state), "--repo", str(repo), "--mode", "FAST",
                "--goal", "bounded", "--write", "src/a.py", "--impact", "no_known_impact",
                "--plan-file", str(plan)
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            target.write_text("value = 2\n", encoding="utf-8")
            commands = [
                ("record-plan", "--state", str(state)),
                ("transition", "--state", str(state), "--to", "implement"),
                ("set-changes", "--state", str(state)),
                ("transition", "--state", str(state), "--to", "verify"),
                ("record-evidence", "--state", str(state), "--kind", "success", "--entry", "unit", "--command", "test", "--observed", "passed", "--level", "test", "--result", "pass"),
                ("record-evidence", "--state", str(state), "--kind", "boundary", "--entry", "edge", "--command", "test edge", "--observed", "passed", "--level", "test", "--result", "pass"),
                ("set-result", "--state", str(state), "--result", "pass"),
                ("record-review", "--state", str(state), "--result", "pass", "--observed", "Sol xhigh independent review passed"),
                ("transition", "--state", str(state), "--to", "complete"),
            ]
            for command in commands:
                result = self.run_guard(*command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["plan_deleted"], str(plan.absolute()))
            self.assertFalse(plan.exists())
            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(payload["phase"], "complete")

    def test_cli_returns_machine_readable_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo, _ = self.create_repo(root)
            state = root / "state.json"
            result = self.run_guard(
                "init", "--state", str(state), "--repo", str(repo), "--mode", "FAST", "--goal", "bounded",
                "--write", "src/a.py", "--impact", "unverified"
            )
            self.assertEqual(result.returncode, 0)
            result = self.run_guard("transition", "--state", str(state), "--to", "implement")
            self.assertEqual(result.returncode, 2)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["code"], "TRANSITION_BLOCKED")

    def test_blocked_completion_keeps_the_attached_plan_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo, _ = self.create_repo(root)
            state = root / "state.json"
            plan = root / "work" / "plan.md"
            plan.parent.mkdir()
            plan.write_text("### MODE\n\n`FAST`\n", encoding="utf-8")
            result = self.run_guard(
                "init", "--state", str(state), "--repo", str(repo), "--mode", "FAST",
                "--goal", "bounded", "--write", "src/a.py", "--impact", "no_known_impact",
                "--plan-file", str(plan)
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            result = self.run_guard("transition", "--state", str(state), "--to", "complete")
            self.assertEqual(result.returncode, 2)
            self.assertTrue(plan.exists())

    def test_rework_returns_failed_verify_to_implement_and_resets_stale_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo, target = self.create_repo(root)
            state = root / "state.json"
            result = self.run_guard(
                "init", "--state", str(state), "--repo", str(repo), "--mode", "FAST",
                "--goal", "repair", "--write", "src/a.py", "--impact", "no_known_impact"
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            target.write_text("value = 2\n", encoding="utf-8")
            commands = [
                ("record-plan", "--state", str(state)),
                ("transition", "--state", str(state), "--to", "implement"),
                ("set-changes", "--state", str(state)),
                ("transition", "--state", str(state), "--to", "verify"),
                ("record-evidence", "--state", str(state), "--kind", "success", "--entry", "runtime", "--command", "run", "--observed", "bug reproduced", "--level", "test", "--result", "fail"),
                ("set-result", "--state", str(state), "--result", "fail", "--gap", "stale gap"),
            ]
            for command in commands:
                result = self.run_guard(*command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            stale = json.loads(state.read_text(encoding="utf-8"))
            stale["gaps_authorized"] = True
            stale["gap_authorization"] = {
                "authorization_id": "stale-authorization",
                "authorized_by": "user:test",
                "authorized_at": "2026-08-28T00:00:00+00:00",
                "reason": "stale authorization must be cleared",
            }
            stale["delivery_audit"] = {"passed": True, "repo": "stale", "checked_files": ["src/a.py"]}
            state.write_text(json.dumps(stale), encoding="utf-8")
            result = self.run_guard(
                "rework", "--state", str(state), "--reason", "implementation defect confirmed"
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(payload["phase"], "implement")
            self.assertEqual(payload["result"], "pending")
            self.assertEqual(payload["evidence"], [])
            self.assertEqual(payload["gaps"], [])
            self.assertFalse(payload["gaps_authorized"])
            self.assertIsNone(payload["gap_authorization"])
            self.assertIsNone(payload["delivery_audit"])
            self.assertEqual(payload["rework_count"], 1)
            self.assertEqual(payload["last_rework_reason"], "implementation defect confirmed")

            result = self.run_guard("set-changes", "--state", str(state))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = self.run_guard("transition", "--state", str(state), "--to", "verify")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_rework_requires_failed_evidence_and_direct_back_transition_stays_blocked(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo, target = self.create_repo(root)
            state = root / "state.json"
            result = self.run_guard(
                "init", "--state", str(state), "--repo", str(repo), "--mode", "FAST",
                "--goal", "repair", "--write", "src/a.py", "--impact", "no_known_impact"
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            target.write_text("value = 2\n", encoding="utf-8")
            commands = [
                ("record-plan", "--state", str(state)),
                ("transition", "--state", str(state), "--to", "implement"),
                ("set-changes", "--state", str(state)),
                ("transition", "--state", str(state), "--to", "verify"),
            ]
            for command in commands:
                result = self.run_guard(*command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            result = self.run_guard("rework", "--state", str(state), "--reason", "no failure recorded")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["code"], "REWORK_NOT_JUSTIFIED")

            result = self.run_guard("transition", "--state", str(state), "--to", "implement")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["code"], "INVALID_TRANSITION")

    def test_rework_streak_warns_then_requires_revised_plan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo, target = self.create_repo(root)
            state = root / "state.json"
            result = self.run_guard(
                "init", "--state", str(state), "--repo", str(repo), "--mode", "FAST", "--goal", "first hypothesis",
                "--write", "src/a.py", "--impact", "no_known_impact"
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            target.write_text("value = 2\n", encoding="utf-8")
            result = self.run_guard("record-plan", "--state", str(state))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = self.run_guard("transition", "--state", str(state), "--to", "implement")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            rework_outputs = []
            for attempt in range(1, 4):
                commands = [
                    ("set-changes", "--state", str(state)),
                    ("transition", "--state", str(state), "--to", "verify"),
                    ("record-evidence", "--state", str(state), "--kind", "success", "--entry", "runtime", "--command", "run", "--observed", f"attempt {attempt} failed", "--level", "test", "--result", "fail"),
                    ("set-result", "--state", str(state), "--result", "fail"),
                    ("rework", "--state", str(state), "--reason", f"failed hypothesis {attempt}"),
                ]
                for command in commands:
                    result = self.run_guard(*command)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                rework_outputs.append(json.loads(result.stdout))

            self.assertNotIn("warning", rework_outputs[0])
            self.assertEqual(rework_outputs[1]["warning"], "REPLAN_RECOMMENDED")
            self.assertEqual(rework_outputs[2]["code"], "REPLAN_REQUIRED")

            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(payload["phase"], "plan")
            self.assertEqual(payload["rework_count"], 3)
            self.assertEqual(payload["rework_streak"], 3)
            self.assertTrue(payload["replan_required"])
            self.assertIsNone(payload["plan_record"])

            result = self.run_guard("transition", "--state", str(state), "--to", "implement")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["code"], "TRANSITION_BLOCKED")

            result = self.run_guard(
                "revise-plan", "--state", str(state), "--mode", "FULL", "--goal", "wrong scope",
                "--write", "src/b.py", "--impact", "known_impact"
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["code"], "REPLAN_SCOPE_CONFLICT")

            result = self.run_guard(
                "revise-plan", "--state", str(state), "--mode", "FULL", "--goal", "revised hypothesis",
                "--write", "src/a.py", "--impact", "known_impact", "--risk-detail", "hypothesis changed"
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(payload["phase"], "plan")
            self.assertEqual(payload["plan_revision"], 2)
            self.assertEqual(payload["rework_count"], 3)
            self.assertEqual(payload["rework_streak"], 0)
            self.assertFalse(payload["replan_required"])
            self.assertEqual(payload["goal"], "revised hypothesis")

            result = self.run_guard("record-plan", "--state", str(state))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = self.run_guard("transition", "--state", str(state), "--to", "implement")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_successful_verification_resets_only_the_rework_streak(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo, target = self.create_repo(root)
            state = root / "state.json"
            result = self.run_guard(
                "init", "--state", str(state), "--repo", str(repo), "--mode", "FAST",
                "--goal", "repair", "--write", "src/a.py", "--impact", "no_known_impact"
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            target.write_text("value = 2\n", encoding="utf-8")
            commands = [
                ("record-plan", "--state", str(state)),
                ("transition", "--state", str(state), "--to", "implement"),
                ("set-changes", "--state", str(state)),
                ("transition", "--state", str(state), "--to", "verify"),
                ("record-evidence", "--state", str(state), "--kind", "success", "--entry", "runtime", "--command", "run", "--observed", "failed", "--level", "test", "--result", "fail"),
                ("set-result", "--state", str(state), "--result", "fail"),
                ("rework", "--state", str(state), "--reason", "implementation defect"),
                ("set-changes", "--state", str(state)),
                ("transition", "--state", str(state), "--to", "verify"),
                ("record-evidence", "--state", str(state), "--kind", "success", "--entry", "runtime", "--command", "run", "--observed", "passed", "--level", "test", "--result", "pass"),
                ("record-evidence", "--state", str(state), "--kind", "boundary", "--entry", "edge", "--command", "run edge", "--observed", "passed", "--level", "test", "--result", "pass"),
                ("set-result", "--state", str(state), "--result", "pass"),
                ("record-review", "--state", str(state), "--result", "pass", "--observed", "Sol xhigh independent review passed"),
                ("transition", "--state", str(state), "--to", "complete"),
            ]
            for command in commands:
                result = self.run_guard(*command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(payload["rework_streak"], 0)
            self.assertEqual(payload["rework_count"], 1)

    def test_delivery_audit_checks_real_git_scope(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo, allowed = self.create_repo(root, "src/allowed.py")
            state = root / "state.json"
            result = self.run_guard(
                "init", "--state", str(state), "--repo", str(repo), "--mode", "FULL",
                "--goal", "deliver", "--write", "src/allowed.py", "--impact", "no_known_impact",
                "--delivery-required"
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            allowed.write_text("after = True\n", encoding="utf-8")
            self.git(repo, "add", "--", "src/allowed.py")

            commands = [
                ("record-plan", "--state", str(state)),
                ("transition", "--state", str(state), "--to", "implement"),
                ("set-changes", "--state", str(state)),
                ("transition", "--state", str(state), "--to", "verify"),
                ("record-evidence", "--state", str(state), "--kind", "success", "--entry", "unit", "--command", "test", "--observed", "passed", "--level", "test", "--result", "pass"),
                ("record-evidence", "--state", str(state), "--kind", "boundary", "--entry", "edge", "--command", "test edge", "--observed", "passed", "--level", "test", "--result", "pass"),
                ("set-result", "--state", str(state), "--result", "pass"),
                ("record-review", "--state", str(state), "--result", "pass", "--observed", "Sol xhigh independent review passed"),
                ("transition", "--state", str(state), "--to", "deliver"),
                ("audit", "--state", str(state), "--repo", str(repo)),
            ]
            for command in commands:
                result = self.run_guard(*command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertTrue(payload["delivery_audit"]["passed"])
            self.assertEqual(payload["delivery_audit"]["checked_files"], ["src/allowed.py"])
            self.assertEqual(
                payload["delivery_audit"]["task_fingerprint"], payload["review"]["task_fingerprint"]
            )
            self.git(repo, "add", "--", "src/allowed.py")
            self.git(repo, "commit", "-m", "deliver reviewed change")
            result = self.run_guard("transition", "--state", str(state), "--to", "complete")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_post_audit_extra_committed_file_blocks_completion(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo, allowed = self.create_repo(root, "src/allowed.py")
            state = root / "state.json"
            result = self.run_guard(
                "init", "--state", str(state), "--repo", str(repo), "--mode", "FULL",
                "--goal", "reject extra commit", "--write", "src/allowed.py", "--impact", "no_known_impact",
                "--delivery-required"
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            allowed.write_text("after = True\n", encoding="utf-8")
            self.git(repo, "add", "--", "src/allowed.py")
            commands = [
                ("record-plan", "--state", str(state)),
                ("transition", "--state", str(state), "--to", "implement"),
                ("set-changes", "--state", str(state)),
                ("transition", "--state", str(state), "--to", "verify"),
                ("record-evidence", "--state", str(state), "--kind", "success", "--entry", "unit", "--command", "test", "--observed", "passed", "--level", "test", "--result", "pass"),
                ("record-evidence", "--state", str(state), "--kind", "boundary", "--entry", "edge", "--command", "edge", "--observed", "passed", "--level", "test", "--result", "pass"),
                ("set-result", "--state", str(state), "--result", "pass"),
                ("record-review", "--state", str(state), "--result", "pass", "--observed", "reviewed allowed file"),
                ("transition", "--state", str(state), "--to", "deliver"),
                ("audit", "--state", str(state), "--repo", str(repo)),
            ]
            for command in commands:
                result = self.run_guard(*command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            extra = repo / "src" / "extra.py"
            extra.write_text("unreviewed = True\n", encoding="utf-8")
            self.git(repo, "add", "--", "src/allowed.py", "src/extra.py")
            self.git(repo, "commit", "-m", "commit reviewed and unreviewed files")
            result = self.run_guard("transition", "--state", str(state), "--to", "complete")
            self.assertEqual(result.returncode, 2)
            self.assertIn("REVIEW_STALE", " ".join(json.loads(result.stdout)["details"]))

    def test_v2_delivery_audit_uses_the_real_review_fingerprint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo, target = self.create_repo(root)
            state = root / "state.json"
            result = self.run_guard(
                "init", "--state", str(state), "--repo", str(repo), "--mode", "FULL",
                "--goal", "v2 audit", "--write", "src/a.py", "--impact", "no_known_impact",
                "--delivery-required"
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(state.read_text(encoding="utf-8"))
            target.write_text("value = 2\n", encoding="utf-8")
            self.git(repo, "add", "--", "src/a.py")
            commands = [
                ("record-plan", "--state", str(state)),
                ("transition", "--state", str(state), "--to", "implement"),
            ]
            for command in commands:
                result = self.run_guard(*command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(state.read_text(encoding="utf-8"))
            payload["schema_version"] = 2
            state.write_text(json.dumps(payload), encoding="utf-8")
            commands = [
                ("set-changes", "--state", str(state)),
                ("transition", "--state", str(state), "--to", "verify"),
                ("record-evidence", "--state", str(state), "--kind", "success", "--entry", "unit", "--command", "test", "--observed", "passed", "--level", "test", "--result", "pass"),
                ("record-evidence", "--state", str(state), "--kind", "boundary", "--entry", "edge", "--command", "edge", "--observed", "passed", "--level", "test", "--result", "pass"),
                ("set-result", "--state", str(state), "--result", "pass"),
                ("record-review", "--state", str(state), "--result", "pass", "--observed", "v2 reviewed"),
                ("transition", "--state", str(state), "--to", "deliver"),
                ("audit", "--state", str(state), "--repo", str(repo)),
            ]
            for command in commands:
                result = self.run_guard(*command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(payload["delivery_audit"]["task_fingerprint"], payload["review"]["task_fingerprint"])

    def test_delivery_review_rejects_index_worktree_split(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo, target = self.create_repo(root)
            state = root / "state.json"
            result = self.run_guard(
                "init", "--state", str(state), "--repo", str(repo), "--mode", "FULL",
                "--goal", "reject hidden index content", "--write", "src/a.py", "--impact", "no_known_impact",
                "--delivery-required"
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            target.write_text("value = 999\n", encoding="utf-8")
            self.git(repo, "add", "--", "src/a.py")
            target.write_text("value = 2\n", encoding="utf-8")
            commands = [
                ("record-plan", "--state", str(state)),
                ("transition", "--state", str(state), "--to", "implement"),
                ("set-changes", "--state", str(state)),
                ("transition", "--state", str(state), "--to", "verify"),
                ("record-evidence", "--state", str(state), "--kind", "success", "--entry", "unit", "--command", "test", "--observed", "passed", "--level", "test", "--result", "pass"),
                ("record-evidence", "--state", str(state), "--kind", "boundary", "--entry", "edge", "--command", "edge", "--observed", "passed", "--level", "test", "--result", "pass"),
                ("set-result", "--state", str(state), "--result", "pass"),
            ]
            for command in commands:
                result = self.run_guard(*command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = self.run_guard(
                "record-review", "--state", str(state), "--result", "pass", "--observed", "must not pass"
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["code"], "REVIEW_STAGE_MISMATCH")

    def test_non_delivery_review_rejects_index_worktree_split(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo, state = self.create_verified_run(root)
            target = repo / "src" / "a.py"
            target.write_text("value = 999\n", encoding="utf-8")
            self.git(repo, "add", "--", "src/a.py")
            target.write_text("value = 2\n", encoding="utf-8")

            result = self.run_guard(
                "record-review", "--state", str(state), "--result", "pass", "--observed", "must not pass"
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["code"], "REVIEW_STAGE_MISMATCH")

    def test_delivery_deleted_file_can_be_reviewed_audited_and_completed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo, target = self.create_repo(root)
            state = root / "state.json"
            result = self.run_guard(
                "init", "--state", str(state), "--repo", str(repo), "--mode", "FULL",
                "--goal", "deliver deleted file", "--write", "src/a.py", "--impact", "no_known_impact",
                "--delivery-required"
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            target.unlink()
            self.git(repo, "add", "--", "src/a.py")

            commands = [
                ("record-plan", "--state", str(state)),
                ("transition", "--state", str(state), "--to", "implement"),
                ("set-changes", "--state", str(state)),
                ("transition", "--state", str(state), "--to", "verify"),
                ("record-evidence", "--state", str(state), "--kind", "success", "--entry", "unit", "--command", "test", "--observed", "passed", "--level", "test", "--result", "pass"),
                ("record-evidence", "--state", str(state), "--kind", "boundary", "--entry", "edge", "--command", "test edge", "--observed", "passed", "--level", "test", "--result", "pass"),
                ("set-result", "--state", str(state), "--result", "pass"),
                ("record-review", "--state", str(state), "--result", "pass", "--observed", "Sol xhigh reviewed deletion"),
                ("transition", "--state", str(state), "--to", "deliver"),
                ("audit", "--state", str(state), "--repo", str(repo)),
            ]
            for command in commands:
                result = self.run_guard(*command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(payload["changed_files"], ["src/a.py"])
            self.assertTrue(payload["delivery_audit"]["passed"])
            self.git(repo, "commit", "-m", "deliver deleted file")
            result = self.run_guard("transition", "--state", str(state), "--to", "complete")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_git_baseline_excludes_untouched_dirty_files_and_forbids_declared_changes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            self.git(repo, "init", "-b", "main")
            self.git(repo, "config", "user.name", "Test User")
            self.git(repo, "config", "user.email", "test@example.invalid")
            allowed = repo / "src" / "allowed.py"
            preexisting = repo / "src" / "preexisting.py"
            allowed.parent.mkdir()
            allowed.write_text("value = 1\n", encoding="utf-8")
            preexisting.write_text("value = 1\n", encoding="utf-8")
            self.git(repo, "add", "--", "src/allowed.py", "src/preexisting.py")
            self.git(repo, "commit", "-m", "initial")
            preexisting.write_text("user_change = True\n", encoding="utf-8")

            state = root / "state.json"
            result = self.run_guard(
                "init", "--state", str(state), "--repo", str(repo), "--mode", "FAST",
                "--goal", "machine detected changes", "--write", "src/allowed.py", "--impact", "no_known_impact"
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            allowed.write_text("task_change = True\n", encoding="utf-8")
            result = self.run_guard("record-plan", "--state", str(state))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = self.run_guard("transition", "--state", str(state), "--to", "implement")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            result = self.run_guard("set-changes", "--state", str(state))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(payload["changed_files"], ["src/allowed.py"])
            self.assertEqual(payload["change_detection"], "git_baseline")

            result = self.run_guard("set-changes", "--state", str(state), "--file", "src/allowed.py")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["code"], "DECLARED_CHANGES_FORBIDDEN")

            preexisting.write_text("task_modified_user_change = True\n", encoding="utf-8")
            result = self.run_guard("set-changes", "--state", str(state))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(payload["changed_files"], ["src/allowed.py", "src/preexisting.py"])
            result = self.run_guard("transition", "--state", str(state), "--to", "verify")
            self.assertEqual(result.returncode, 2)
            self.assertIn("out-of-scope changes", " ".join(json.loads(result.stdout)["details"]))

    def test_gap_authorization_is_a_separate_audited_command(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo = root / "repo"
            repo.mkdir()
            self.git(repo, "init", "-b", "main")
            self.git(repo, "config", "user.name", "Test User")
            self.git(repo, "config", "user.email", "test@example.invalid")
            target = repo / "src" / "a.py"
            target.parent.mkdir()
            target.write_text("value = 1\n", encoding="utf-8")
            self.git(repo, "add", "--", "src/a.py")
            self.git(repo, "commit", "-m", "initial")

            state = root / "state.json"
            result = self.run_guard(
                "init", "--state", str(state), "--repo", str(repo), "--mode", "FAST",
                "--goal", "authorize a real gap", "--write", "src/a.py", "--impact", "no_known_impact"
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            target.write_text("value = 2\n", encoding="utf-8")
            commands = [
                ("record-plan", "--state", str(state)),
                ("transition", "--state", str(state), "--to", "implement"),
                ("set-changes", "--state", str(state)),
                ("transition", "--state", str(state), "--to", "verify"),
                ("record-evidence", "--state", str(state), "--kind", "success", "--entry", "unit", "--command", "test", "--observed", "passed", "--level", "test", "--result", "pass"),
                ("record-evidence", "--state", str(state), "--kind", "boundary", "--entry", "host", "--command", "host test", "--observed", "host unavailable", "--level", "host", "--result", "blocked"),
                ("set-result", "--state", str(state), "--result", "pass_with_gaps", "--gap", "host unavailable"),
                ("record-review", "--state", str(state), "--result", "pass", "--observed", "Sol xhigh independent review passed"),
            ]
            for command in commands:
                result = self.run_guard(*command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            result = self.run_guard("transition", "--state", str(state), "--to", "complete")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["code"], "TRANSITION_BLOCKED")

            result = self.run_guard(
                "authorize-gaps", "--state", str(state), "--authorized-by", "agent:codex", "--reason", "self approval"
            )
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["code"], "INVALID_AUTHORIZER")

            result = self.run_guard(
                "authorize-gaps", "--state", str(state), "--authorized-by", "user:viola", "--reason", "accepted host gap"
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(payload["gap_authorization"]["authorized_by"], "user:viola")
            self.assertEqual(payload["gap_authorization"]["reason"], "accepted host gap")
            self.assertTrue(payload["gap_authorization"]["authorized_at"].endswith("+00:00"))

            valid_payload = payload
            tampered = json.loads(json.dumps(payload))
            tampered["gap_authorization"]["authorized_by"] = "agent:codex"
            state.write_text(json.dumps(tampered), encoding="utf-8")
            result = self.run_guard("transition", "--state", str(state), "--to", "complete")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["code"], "STATE_SCHEMA_INVALID")
            state.write_text(json.dumps(valid_payload), encoding="utf-8")

            result = self.run_guard("transition", "--state", str(state), "--to", "complete")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_git_head_change_invalidates_the_baseline(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo, target = self.create_repo(root)
            state = root / "state.json"
            result = self.run_guard(
                "init", "--state", str(state), "--repo", str(repo), "--mode", "FAST",
                "--goal", "detect moved baseline", "--write", "src/a.py", "--impact", "no_known_impact"
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = self.run_guard("record-plan", "--state", str(state))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            target.write_text("value = 2\n", encoding="utf-8")
            self.git(repo, "add", "--", "src/a.py")
            self.git(repo, "commit", "-m", "move head")
            result = self.run_guard("transition", "--state", str(state), "--to", "implement")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = self.run_guard("set-changes", "--state", str(state))
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["code"], "GIT_BASELINE_MOVED")

    def test_write_plan_creates_markdown_and_refuses_to_replace_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            content = root / "initial-plan.md"
            plan = root / "plan.md"
            content.write_text("### MODE\n\n`FAST`\n", encoding="utf-8")

            result = self.run_guard("write-plan", "--file", str(plan), "--content-file", str(content))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(plan.read_text(encoding="utf-8"), "### MODE\n\n`FAST`\n")

            result = self.run_guard("write-plan", "--file", str(plan), "--content-file", str(content))
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["code"], "PLAN_FILE_EXISTS")

    def test_prepend_requirement_places_new_markdown_above_existing_plan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            plan = root / "plan.md"
            requirement = root / "requirement.md"
            plan.write_text("### GOAL\n\nExisting Plan\n", encoding="utf-8")
            requirement.write_text("## 新增需求\n\n保存 Plan 到 Markdown。\n", encoding="utf-8")

            result = self.run_guard(
                "prepend-requirement", "--file", str(plan), "--content-file", str(requirement)
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(
                plan.read_text(encoding="utf-8"),
                "## 新增需求\n\n保存 Plan 到 Markdown。\n\n---\n\n### GOAL\n\nExisting Plan\n",
            )


if __name__ == "__main__":
    unittest.main()
