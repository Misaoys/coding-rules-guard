import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


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
        guard.check_transition(state, "complete")

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


class GuardCliTests(unittest.TestCase):
    def run_guard(self, *args):
        return subprocess.run(
            [sys.executable, str(GUARD_PATH), *args], capture_output=True, text=True, encoding="utf-8"
        )

    def git(self, repo, *args):
        result = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, encoding="utf-8"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_cli_happy_path_without_delivery(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = Path(temp_dir) / "state.json"
            commands = [
                ("init", "--state", str(state), "--mode", "FAST", "--goal", "bounded", "--write", "src/a.py", "--impact", "no_known_impact"),
                ("transition", "--state", str(state), "--to", "implement"),
                ("set-changes", "--state", str(state), "--file", "src/a.py"),
                ("transition", "--state", str(state), "--to", "verify"),
                ("record-evidence", "--state", str(state), "--kind", "success", "--entry", "unit", "--command", "test", "--observed", "passed", "--level", "test", "--result", "pass"),
                ("record-evidence", "--state", str(state), "--kind", "boundary", "--entry", "edge", "--command", "test edge", "--observed", "passed", "--level", "test", "--result", "pass"),
                ("set-result", "--state", str(state), "--result", "pass"),
                ("transition", "--state", str(state), "--to", "complete"),
            ]
            for command in commands:
                result = self.run_guard(*command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(payload["phase"], "complete")

    def test_cli_returns_machine_readable_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = Path(temp_dir) / "state.json"
            result = self.run_guard(
                "init", "--state", str(state), "--mode", "FAST", "--goal", "bounded",
                "--write", "src/a.py", "--impact", "unverified"
            )
            self.assertEqual(result.returncode, 0)
            result = self.run_guard("transition", "--state", str(state), "--to", "implement")
            self.assertEqual(result.returncode, 2)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["code"], "TRANSITION_BLOCKED")

    def test_rework_returns_failed_verify_to_implement_and_resets_stale_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = Path(temp_dir) / "state.json"
            commands = [
                ("init", "--state", str(state), "--mode", "FAST", "--goal", "repair", "--write", "src/a.py", "--impact", "no_known_impact"),
                ("transition", "--state", str(state), "--to", "implement"),
                ("set-changes", "--state", str(state), "--file", "src/a.py"),
                ("transition", "--state", str(state), "--to", "verify"),
                ("record-evidence", "--state", str(state), "--kind", "success", "--entry", "runtime", "--command", "run", "--observed", "bug reproduced", "--level", "test", "--result", "fail"),
                ("set-result", "--state", str(state), "--result", "fail", "--gap", "stale gap"),
            ]
            for command in commands:
                result = self.run_guard(*command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            stale = json.loads(state.read_text(encoding="utf-8"))
            stale["gaps_authorized"] = True
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
            self.assertIsNone(payload["delivery_audit"])
            self.assertEqual(payload["rework_count"], 1)
            self.assertEqual(payload["last_rework_reason"], "implementation defect confirmed")

            result = self.run_guard("set-changes", "--state", str(state), "--file", "src/a.py")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = self.run_guard("transition", "--state", str(state), "--to", "verify")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_rework_requires_failed_evidence_and_direct_back_transition_stays_blocked(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = Path(temp_dir) / "state.json"
            commands = [
                ("init", "--state", str(state), "--mode", "FAST", "--goal", "repair", "--write", "src/a.py", "--impact", "no_known_impact"),
                ("transition", "--state", str(state), "--to", "implement"),
                ("set-changes", "--state", str(state), "--file", "src/a.py"),
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
            state = Path(temp_dir) / "state.json"
            result = self.run_guard(
                "init", "--state", str(state), "--mode", "FAST", "--goal", "first hypothesis",
                "--write", "src/a.py", "--impact", "no_known_impact"
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = self.run_guard("transition", "--state", str(state), "--to", "implement")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            rework_outputs = []
            for attempt in range(1, 4):
                commands = [
                    ("set-changes", "--state", str(state), "--file", "src/a.py"),
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

            result = self.run_guard("transition", "--state", str(state), "--to", "implement")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_successful_verification_resets_only_the_rework_streak(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = Path(temp_dir) / "state.json"
            commands = [
                ("init", "--state", str(state), "--mode", "FAST", "--goal", "repair", "--write", "src/a.py", "--impact", "no_known_impact"),
                ("transition", "--state", str(state), "--to", "implement"),
                ("set-changes", "--state", str(state), "--file", "src/a.py"),
                ("transition", "--state", str(state), "--to", "verify"),
                ("record-evidence", "--state", str(state), "--kind", "success", "--entry", "runtime", "--command", "run", "--observed", "failed", "--level", "test", "--result", "fail"),
                ("set-result", "--state", str(state), "--result", "fail"),
                ("rework", "--state", str(state), "--reason", "implementation defect"),
                ("set-changes", "--state", str(state), "--file", "src/a.py"),
                ("transition", "--state", str(state), "--to", "verify"),
                ("record-evidence", "--state", str(state), "--kind", "success", "--entry", "runtime", "--command", "run", "--observed", "passed", "--level", "test", "--result", "pass"),
                ("record-evidence", "--state", str(state), "--kind", "boundary", "--entry", "edge", "--command", "run edge", "--observed", "passed", "--level", "test", "--result", "pass"),
                ("set-result", "--state", str(state), "--result", "pass"),
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
            repo = root / "repo"
            repo.mkdir()
            self.git(repo, "init", "-b", "main")
            self.git(repo, "config", "user.name", "Test User")
            self.git(repo, "config", "user.email", "test@example.invalid")
            allowed = repo / "src" / "allowed.py"
            allowed.parent.mkdir()
            allowed.write_text("before = True\n", encoding="utf-8")
            self.git(repo, "add", "--", "src/allowed.py")
            self.git(repo, "commit", "-m", "initial")
            allowed.write_text("after = True\n", encoding="utf-8")

            state = root / "state.json"
            commands = [
                ("init", "--state", str(state), "--mode", "FULL", "--goal", "deliver", "--write", "src/allowed.py", "--impact", "no_known_impact", "--delivery-required"),
                ("transition", "--state", str(state), "--to", "implement"),
                ("set-changes", "--state", str(state), "--file", "src/allowed.py"),
                ("transition", "--state", str(state), "--to", "verify"),
                ("record-evidence", "--state", str(state), "--kind", "success", "--entry", "unit", "--command", "test", "--observed", "passed", "--level", "test", "--result", "pass"),
                ("record-evidence", "--state", str(state), "--kind", "boundary", "--entry", "edge", "--command", "test edge", "--observed", "passed", "--level", "test", "--result", "pass"),
                ("set-result", "--state", str(state), "--result", "pass"),
                ("transition", "--state", str(state), "--to", "deliver"),
                ("audit", "--state", str(state), "--repo", str(repo)),
            ]
            for command in commands:
                result = self.run_guard(*command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(state.read_text(encoding="utf-8"))
            self.assertTrue(payload["delivery_audit"]["passed"])
            self.assertEqual(payload["delivery_audit"]["checked_files"], ["src/allowed.py"])


if __name__ == "__main__":
    unittest.main()
