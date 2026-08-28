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
