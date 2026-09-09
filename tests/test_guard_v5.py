import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GUARD_PATH = ROOT / "scripts" / "guard.py"


class GuardV5Tests(unittest.TestCase):
    def run_guard(self, *args):
        return subprocess.run(
            [sys.executable, str(GUARD_PATH), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def git(self, repo, *args):
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def create_repo(self, root):
        repo = root / "repo"
        repo.mkdir()
        self.git(repo, "init", "-b", "main")
        self.git(repo, "config", "user.name", "Test User")
        self.git(repo, "config", "user.email", "test@example.invalid")
        target = repo / "src" / "a.py"
        target.parent.mkdir(parents=True)
        target.write_text("value = 1\n", encoding="utf-8")
        self.git(repo, "add", "--", "src/a.py")
        self.git(repo, "commit", "-m", "initial")
        return repo, target

    def init_run(self, root, *, delivery=False, max_attempts="3", max_replans="2"):
        repo, target = self.create_repo(root)
        state = root / "state.json"
        args = [
            "init",
            "--state",
            str(state),
            "--repo",
            str(repo),
            "--mode",
            "FAST",
            "--goal",
            "v5 behavior",
            "--write",
            "src/a.py",
            "--impact",
            "no_known_impact",
            "--max-attempts",
            max_attempts,
            "--max-replans",
            max_replans,
        ]
        if delivery:
            args.append("--delivery-required")
        result = self.run_guard(*args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return repo, target, state

    def start_verify(self, root, verification_spec=None):
        repo, target, state = self.init_run(root)
        target.write_text("value = 2\n", encoding="utf-8")
        command = ["record-plan", "--state", str(state)]
        if verification_spec is not None:
            command.extend(["--verification-spec", json.dumps(verification_spec)])
        result = self.run_guard(*command, "--profile", "session_main", "--model", "gpt-test-main", "--reasoning-effort", "high")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for command in (
            ("transition", "--state", str(state), "--to", "implement", "--hypothesis", "the change is bounded"),
            ("set-changes", "--state", str(state)),
            ("transition", "--state", str(state), "--to", "verify"),
        ):
            result = self.run_guard(*command)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return repo, target, state

    def test_init_creates_v5_loop_telemetry_and_registry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, _, state_path = self.init_run(Path(temp_dir))
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["schema_version"], 5)
            self.assertEqual(state["loop"]["policy"], {"max_attempts": 3, "max_replans": 2})
            self.assertEqual(state["loop"]["attempt_count"], 0)
            self.assertEqual(state["loop"]["replan_count"], 0)
            self.assertIsNone(state["loop"]["active_attempt"])
            self.assertEqual(state["loop"]["attempt_history"], [])
            self.assertEqual(state["loop"]["plan_history"], [])
            self.assertEqual(state["telemetry"]["format_version"], 1)
            self.assertEqual(state["telemetry"]["event_seq"], 0)
            self.assertEqual(state["telemetry"]["events"], [])
            self.assertEqual(state["verification_registry"]["format_version"], 1)
            self.assertEqual(state["verification_registry"]["definitions"], [])

    def test_hidden_index_is_part_of_actual_git_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo, target = self.create_repo(root)
            target.write_text("value = 2\n", encoding="utf-8")
            self.git(repo, "add", "--", "src/a.py")
            target.write_text("value = 1\n", encoding="utf-8")
            sys.path.insert(0, str(ROOT / "scripts"))
            try:
                import guard

                self.assertEqual(guard.actual_git_files(repo), ["src/a.py"])
            finally:
                sys.path.pop(0)

    def test_unsupported_directory_fingerprint_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo, _ = self.create_repo(root)
            sys.path.insert(0, str(ROOT / "scripts"))
            try:
                import guard

                with self.assertRaises(guard.GateError) as raised:
                    guard.filesystem_path_fingerprint(repo, "src")
                self.assertEqual(raised.exception.code, "GIT_UNSUPPORTED_OBJECT")
            finally:
                sys.path.pop(0)

    def test_v4_write_commands_are_read_only_and_require_upgrade(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, _, state_path = self.init_run(root)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["schema_version"] = 4
            state_path.write_text(json.dumps(state), encoding="utf-8")
            result = self.run_guard("record-plan", "--state", str(state_path))
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["code"], "STATE_UPGRADE_REQUIRED")

    def test_transition_starts_attempt_and_complete_archives_it(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            repo, target, state_path = self.start_verify(Path(temp_dir))
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "verify")
            self.assertEqual(state["loop"]["attempt_count"], 1)
            self.assertEqual(state["loop"]["active_attempt"]["kind"], "implement")
            self.assertEqual(state["loop"]["active_attempt"]["plan_revision"], 1)

            evidence_args = [
                "record-evidence",
                "--state",
                str(state_path),
                "--kind",
                "success",
                "--entry",
                "unit",
                "--command",
                "python -m unittest",
                "--observed",
                "passed",
                "--level",
                "test",
                "--result",
                "pass",
                "--execution-record",
                json.dumps({
                    "execution_id": "x001",
                    "source": "host_receipt",
                    "argv": ["python", "-m", "unittest"],
                    "cwd": str(repo),
                    "result": "pass",
                    "output_ref": "test-output.txt",
                }),
            ]
            result = self.run_guard(*evidence_args)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            boundary = evidence_args.copy()
            boundary[boundary.index("--kind") + 1] = "boundary"
            boundary[boundary.index("--entry") + 1] = "edge"
            boundary[boundary.index("--execution-record") + 1] = json.dumps({
                "execution_id": "x002",
                "source": "host_receipt",
                "argv": ["python", "-m", "unittest", "edge"],
                "cwd": str(repo),
                "result": "pass",
                "output_ref": "test-edge.txt",
            })
            result = self.run_guard(*boundary)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for command in (
                ("set-result", "--state", str(state_path), "--result", "pass"),
                ("record-review", "--state", str(state_path), "--result", "pass", "--observed", "reviewed", "--profile", "session_main", "--model", "gpt-test-main", "--reasoning-effort", "high"),
                ("transition", "--state", str(state_path), "--to", "complete"),
            ):
                result = self.run_guard(*command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "complete")
            self.assertIsNone(state["loop"]["active_attempt"])
            self.assertEqual(state["loop"]["attempt_history"][-1]["outcome"], "pass")
            self.assertEqual(state["loop"]["attempt_history"][-1]["attempt_id"], "a0001")

    def test_rework_requires_diagnosis_and_archives_failed_attempt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, _, state_path = self.start_verify(Path(temp_dir))
            failure = self.run_guard(
                "record-evidence", "--state", str(state_path), "--kind", "boundary", "--entry", "failure",
                "--command", "python -m unittest", "--observed", "failed", "--level", "test", "--result", "fail",
            )
            self.assertEqual(failure.returncode, 0, failure.stdout + failure.stderr)
            result = self.run_guard("set-result", "--state", str(state_path), "--result", "fail")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = self.run_guard("rework", "--state", str(state_path), "--reason", "implementation defect")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["code"], "DIAGNOSIS_REQUIRED")

            diagnosis = {
                "classification": "implementation",
                "cause_summary": "the boundary behavior is wrong",
                "source_refs": ["e0001"],
                "next_action_summary": "fix the guarded path",
                "next_hypothesis": "the corrected path will pass the boundary check",
                "expected_observation": "the same boundary passes",
                "new_information": {"kind": "code_change_planned", "summary": "a minimal fix is planned", "source_refs": ["e0001"]},
            }
            result = self.run_guard("record-diagnosis", "--state", str(state_path), "--input", json.dumps(diagnosis))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = self.run_guard("rework", "--state", str(state_path), "--reason", "implementation defect")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["phase"], "implement")
            self.assertEqual(state["loop"]["attempt_count"], 2)
            self.assertEqual(len(state["loop"]["attempt_history"]), 1)
            self.assertEqual(state["loop"]["attempt_history"][0]["outcome"], "fail")
            self.assertEqual(state["evidence"], [])

    def test_attempt_budget_rejects_rework_without_mutating_failed_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, target, state_path = self.init_run(root, max_attempts="1")
            target.write_text("value = 2\n", encoding="utf-8")
            for command in (
                ("record-plan", "--state", str(state_path), "--profile", "session_main", "--model", "gpt-test-main", "--reasoning-effort", "high"),
                ("transition", "--state", str(state_path), "--to", "implement"),
                ("set-changes", "--state", str(state_path)),
                ("transition", "--state", str(state_path), "--to", "verify"),
                ("record-evidence", "--state", str(state_path), "--kind", "success", "--entry", "unit", "--command", "test", "--observed", "failed", "--level", "test", "--result", "fail"),
                ("set-result", "--state", str(state_path), "--result", "fail"),
            ):
                result = self.run_guard(*command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            diagnosis = {
                "classification": "implementation",
                "cause_summary": "the implementation still fails",
                "source_refs": ["e0001"],
                "next_action_summary": "request a new attempt",
                "next_hypothesis": "a new implementation would pass",
                "expected_observation": "the test passes",
                "new_information": {"kind": "code_change_planned", "summary": "no budget remains", "source_refs": ["e0001"]},
            }
            result = self.run_guard("record-diagnosis", "--state", str(state_path), "--input", json.dumps(diagnosis))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            before = state_path.read_bytes()
            result = self.run_guard("rework", "--state", str(state_path), "--reason", "try the next implementation")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["code"], "LOOP_BUDGET_EXHAUSTED")
            self.assertEqual(state_path.read_bytes(), before)

    def test_repeated_failure_emits_no_new_information_notice(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            _, _, state_path = self.start_verify(root)
            failure_commands = (
                ("record-evidence", "--state", str(state_path), "--kind", "boundary", "--entry", "same-check", "--command", "python -m unittest", "--observed", "same failure", "--level", "test", "--result", "fail"),
                ("set-result", "--state", str(state_path), "--result", "fail"),
            )
            for command in failure_commands:
                result = self.run_guard(*command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            diagnosis = {
                "classification": "implementation",
                "cause_summary": "the same check still fails",
                "source_refs": ["e0001"],
                "next_action_summary": "make a bounded correction",
                "next_hypothesis": "the correction will remove the failure",
                "expected_observation": "the same check passes",
                "new_information": {"kind": "code_change_planned", "summary": "a correction is planned", "source_refs": ["e0001"]},
            }
            result = self.run_guard("record-diagnosis", "--state", str(state_path), "--input", json.dumps(diagnosis))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = self.run_guard("rework", "--state", str(state_path), "--reason", "bounded correction")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for command in (
                ("set-changes", "--state", str(state_path)),
                ("transition", "--state", str(state_path), "--to", "verify"),
                *failure_commands,
            ):
                result = self.run_guard(*command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            diagnosis["source_refs"] = ["e0002"]
            diagnosis["new_information"]["source_refs"] = ["e0002"]
            result = self.run_guard("record-diagnosis", "--state", str(state_path), "--input", json.dumps(diagnosis))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = self.run_guard("rework", "--state", str(state_path), "--reason", "repeat bounded correction")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("NO_NEW_INFORMATION", json.loads(result.stdout).get("notices", []))

    def test_progress_activity_and_context_are_read_only_projections(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, _, state_path = self.init_run(Path(temp_dir))
            before = state_path.read_bytes()
            result = self.run_guard("progress", "--state", str(state_path), "--format", "json", "--event-limit", "5")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            projection = json.loads(result.stdout)
            self.assertEqual(projection["snapshot"], "state_only_not_a_gate")
            self.assertEqual(projection["phase"], "plan")
            result = self.run_guard("context", "--state", str(state_path), "--history-limit", "3", "--path-limit", "20")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            context = json.loads(result.stdout)
            self.assertEqual(context["snapshot"], "state_only_not_a_gate")
            self.assertIn("suggested_action", context)
            self.assertEqual(state_path.read_bytes(), before)

            result = self.run_guard("set-activity", "--state", str(state_path), "--text", "读取验证入口")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["telemetry"]["activity"]["text"], "读取验证入口")
            sequence = state["telemetry"]["event_seq"]
            result = self.run_guard("set-activity", "--state", str(state_path), "--text", "读取验证入口")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["telemetry"]["event_seq"], sequence)

    def test_verification_registry_returns_reuse_then_run_and_force(self):
        spec = {
            "definitions": [
                {
                    "check_id": "unit",
                    "claim_ids": ["claim.unit"],
                    "criterion_digest": "criterion-v1",
                    "command_spec": {"argv": ["python", "-m", "unittest"], "cwd": "repo", "runner": "local"},
                    "input_spec": {"paths": ["src/a.py"], "dependency_coverage": "declared"},
                    "repeat_policy": {"mode": "once"},
                }
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            repo, target, state_path = self.start_verify(Path(temp_dir), spec)
            sys.path.insert(0, str(ROOT / "scripts"))
            try:
                import guard

                state = json.loads(state_path.read_text(encoding="utf-8"))
                definition = guard.verification_definition(state, "unit")
                digest = guard.verification_input_binding(state, definition, {})["digest"]
            finally:
                sys.path.pop(0)
            execution = {
                "execution_id": "x001",
                "source": "host_receipt",
                "argv": ["python", "-m", "unittest"],
                "cwd": str(repo),
                "result": "pass",
                "output_ref": "test-output.txt",
                "check_id": "unit",
                "before_binding": {"binding_digest": digest},
                "after_binding": {"binding_digest": digest},
            }
            result = self.run_guard(
                "record-evidence", "--state", str(state_path), "--kind", "success", "--entry", "unit",
                "--command", "python -m unittest", "--observed", "passed", "--level", "test", "--result", "pass",
                "--check-id", "unit", "--execution-record", json.dumps(execution),
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = self.run_guard("check-verification", "--state", str(state_path), "--check-id", "unit")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["decision"], "reuse")
            target.write_text("value = 3\n", encoding="utf-8")
            result = self.run_guard("check-verification", "--state", str(state_path), "--check-id", "unit")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["decision"], "run")
            result = self.run_guard(
                "check-verification", "--state", str(state_path), "--check-id", "unit", "--force", "--reason", "user requested a fresh sample"
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["decision"], "run")
            self.assertIn("force", payload["reason_codes"])

    def test_verification_reuse_rejects_changed_execution_bindings(self):
        spec = {
            "definitions": [
                {
                    "check_id": "unit",
                    "claim_ids": ["claim.unit"],
                    "criterion_digest": "criterion-v1",
                    "command_spec": {"argv": ["python", "-m", "unittest"], "cwd": "repo", "runner": "local"},
                    "input_spec": {"paths": ["src/a.py"], "dependency_coverage": "declared"},
                    "repeat_policy": {"mode": "once"},
                }
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            repo, target, state_path = self.start_verify(Path(temp_dir), spec)
            sys.path.insert(0, str(ROOT / "scripts"))
            try:
                import guard

                state = json.loads(state_path.read_text(encoding="utf-8"))
                definition = guard.verification_definition(state, "unit")
                before_digest = guard.verification_input_binding(state, definition, {})["digest"]
            finally:
                sys.path.pop(0)
            target.write_text("value = 3\n", encoding="utf-8")
            sys.path.insert(0, str(ROOT / "scripts"))
            try:
                import guard

                state = json.loads(state_path.read_text(encoding="utf-8"))
                definition = guard.verification_definition(state, "unit")
                after_digest = guard.verification_input_binding(state, definition, {})["digest"]
            finally:
                sys.path.pop(0)
            execution = {
                "execution_id": "x-binding-change",
                "source": "host_receipt",
                "argv": ["python", "-m", "unittest"],
                "cwd": str(repo),
                "result": "pass",
                "before_binding": {"binding_digest": before_digest},
                "after_binding": {"binding_digest": after_digest},
            }
            result = self.run_guard(
                "record-evidence", "--state", str(state_path), "--kind", "success", "--entry", "unit",
                "--command", "python -m unittest", "--observed", "passed", "--level", "test", "--result", "pass",
                "--check-id", "unit", "--execution-record", json.dumps(execution),
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = self.run_guard("check-verification", "--state", str(state_path), "--check-id", "unit")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["decision"], "unknown")
            self.assertIn("EXECUTION_BINDING_CHANGED", payload["reason_codes"])

    def test_repeat_policy_requires_distinct_trusted_samples_before_reuse(self):
        spec = {
            "definitions": [
                {
                    "check_id": "unit",
                    "claim_ids": ["claim.unit"],
                    "criterion_digest": "criterion-v1",
                    "command_spec": {"argv": ["python", "-m", "unittest"], "cwd": "repo", "runner": "local"},
                    "input_spec": {"paths": ["src/a.py"], "dependency_coverage": "declared"},
                    "repeat_policy": {"mode": "samples", "required_samples": 2},
                }
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            repo, _, state_path = self.start_verify(Path(temp_dir), spec)
            sys.path.insert(0, str(ROOT / "scripts"))
            try:
                import guard

                state = json.loads(state_path.read_text(encoding="utf-8"))
                definition = guard.verification_definition(state, "unit")
                digest = guard.verification_input_binding(state, definition, {})["digest"]
            finally:
                sys.path.pop(0)

            def record(sample_id, execution_id):
                execution = {
                    "execution_id": execution_id,
                    "sample_id": sample_id,
                    "source": "host_receipt",
                    "argv": ["python", "-m", "unittest"],
                    "cwd": str(repo),
                    "result": "pass",
                    "before_binding": {"binding_digest": digest},
                    "after_binding": {"binding_digest": digest},
                }
                result = self.run_guard(
                    "record-evidence", "--state", str(state_path), "--kind", "success", "--entry", sample_id,
                    "--command", "python -m unittest", "--observed", "passed", "--level", "test", "--result", "pass",
                    "--check-id", "unit", "--execution-record", json.dumps(execution),
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            record("sample-1", "x-sample-1")
            result = self.run_guard("check-verification", "--state", str(state_path), "--check-id", "unit")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["decision"], "run")
            self.assertIn("REPEAT_SAMPLES_INSUFFICIENT", payload["reason_codes"])
            record("sample-2", "x-sample-2")
            result = self.run_guard("check-verification", "--state", str(state_path), "--check-id", "unit")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(result.stdout)["decision"], "reuse")

    def test_force_does_not_bypass_matching_failure(self):
        spec = {
            "definitions": [
                {
                    "check_id": "unit",
                    "claim_ids": ["claim.unit"],
                    "criterion_digest": "criterion-v1",
                    "command_spec": {"argv": ["python", "-m", "unittest"], "cwd": "repo", "runner": "local"},
                    "input_spec": {"paths": ["src/a.py"], "dependency_coverage": "declared"},
                    "repeat_policy": {"mode": "once"},
                }
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            repo, _, state_path = self.start_verify(Path(temp_dir), spec)
            sys.path.insert(0, str(ROOT / "scripts"))
            try:
                import guard

                state = json.loads(state_path.read_text(encoding="utf-8"))
                definition = guard.verification_definition(state, "unit")
                digest = guard.verification_input_binding(state, definition, {})["digest"]
            finally:
                sys.path.pop(0)
            execution = {
                "execution_id": "x-failed",
                "source": "host_receipt",
                "argv": ["python", "-m", "unittest"],
                "cwd": str(repo),
                "result": "fail",
                "before_binding": {"binding_digest": digest},
                "after_binding": {"binding_digest": digest},
            }
            result = self.run_guard(
                "record-evidence", "--state", str(state_path), "--kind", "boundary", "--entry", "unit",
                "--command", "python -m unittest", "--observed", "failed", "--level", "test", "--result", "fail",
                "--check-id", "unit", "--execution-record", json.dumps(execution),
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = self.run_guard(
                "check-verification", "--state", str(state_path), "--check-id", "unit", "--force", "--reason", "fresh sample requested"
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["decision"], "diagnose")
            self.assertIn("FORCE_CANNOT_BYPASS_FAILURE", payload["reason_codes"])

    def test_same_execution_can_back_multiple_evidence_assertions_but_same_assertion_is_idempotent(self):
        spec = {
            "definitions": [
                {
                    "check_id": "unit",
                    "claim_ids": ["claim.unit"],
                    "criterion_digest": "criterion-v1",
                    "command_spec": {"argv": ["python", "-m", "unittest"], "cwd": "repo", "runner": "local"},
                    "input_spec": {"paths": ["src/a.py"], "dependency_coverage": "declared"},
                    "repeat_policy": {"mode": "once"},
                }
            ]
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            repo, _, state_path = self.start_verify(Path(temp_dir), spec)
            sys.path.insert(0, str(ROOT / "scripts"))
            try:
                import guard

                state = json.loads(state_path.read_text(encoding="utf-8"))
                definition = guard.verification_definition(state, "unit")
                digest = guard.verification_input_binding(state, definition, {})["digest"]
            finally:
                sys.path.pop(0)
            execution = {
                "execution_id": "x-shared",
                "source": "host_receipt",
                "argv": ["python", "-m", "unittest"],
                "cwd": str(repo),
                "result": "pass",
                "before_binding": {"binding_digest": digest},
                "after_binding": {"binding_digest": digest},
            }
            first = (
                "record-evidence", "--state", str(state_path), "--kind", "success", "--entry", "success path",
                "--command", "python -m unittest", "--observed", "passed", "--level", "test", "--result", "pass",
                "--check-id", "unit", "--execution-record", json.dumps(execution),
            )
            result = self.run_guard(*first)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            second = list(first)
            second[second.index("--kind") + 1] = "boundary"
            second[second.index("--entry") + 1] = "boundary path"
            result = self.run_guard(*second)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            payload = json.loads(result.stdout)
            self.assertNotIn("idempotent", payload)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(len(state["evidence"]), 2)
            self.assertEqual(state["evidence"][0]["execution"]["execution_id"], "x-shared")
            self.assertEqual(state["evidence"][1]["execution"]["execution_id"], "x-shared")
            result = self.run_guard(*first)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(json.loads(result.stdout)["idempotent"])
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(len(state["evidence"]), 2)

    def test_context_history_is_a_compact_handoff_projection(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            import guard

            evidence = [
                {
                    "evidence_id": f"e{index:04d}",
                    "kind": "boundary",
                    "command": "c" * 2048,
                    "observed": "o" * 2048,
                    "result": "fail",
                    "output_ref": "output.txt",
                }
                for index in range(32)
            ]
            state = {
                "loop": {
                    "attempt_history": [
                        {
                            "attempt_id": "a0001",
                            "plan_revision": 1,
                            "kind": "implement",
                            "hypothesis": "h" * 512,
                            "outcome": "fail",
                            "result": "fail",
                            "closure_reason": "c" * 512,
                            "evidence_snapshot": evidence,
                            "review_snapshot": None,
                            "diagnosis": {
                                "classification": "implementation",
                                "cause_summary": "the same boundary failed",
                                "next_action_summary": "make a bounded correction",
                                "source_refs": ["e0001"],
                            },
                        }
                    ]
                }
            }
            projection = guard.context_history(state, 3)
            self.assertLess(len(json.dumps(projection, ensure_ascii=False).encode("utf-8")), 12000)
            item = projection["items"][0]
            self.assertNotIn("evidence_snapshot", item)
            self.assertNotIn("command", item)
            self.assertNotIn("observed", item)
            self.assertEqual(item["evidence_refs"], [f"e{index:04d}" for index in range(8)])
            self.assertEqual(item["failure_classification"], "implementation")
            self.assertEqual(item["next_change"], "make a bounded correction")
        finally:
            sys.path.pop(0)

    def test_progress_text_includes_risk_details_and_gaps(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, _, state_path = self.init_run(Path(temp_dir))
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["risk"]["details"] = ["宿主环境仍未核验"]
            state["gaps"] = ["需要真实宿主收据"]
            state_path.write_text(json.dumps(state), encoding="utf-8")
            result = self.run_guard("progress", "--state", str(state_path), "--format", "text")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("risk.details=宿主环境仍未核验", result.stdout)
            self.assertIn("gaps=需要真实宿主收据", result.stdout)

    def test_plan_skill_defines_distinct_fast_and_full_expansions(self):
        content = (ROOT / "skills" / "coding-rules-plan" / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("FAST 展开", content)
        self.assertIn("FULL 展开", content)
        self.assertIn("七节骨架", content)

    def test_guard_entrypoint_uses_split_guardlib_modules(self):
        package = ROOT / "scripts" / "guardlib"
        self.assertTrue((package / "__init__.py").is_file())
        self.assertTrue((package / "verification.py").is_file())
        self.assertTrue((package / "projections.py").is_file())
        entrypoint = (ROOT / "scripts" / "guard.py").read_text(encoding="utf-8")
        self.assertIn("from guardlib.guard import main", entrypoint)

    def test_retry_verify_requires_new_external_observation_and_archives_attempt(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, target, state_path = self.start_verify(Path(temp_dir))
            target.write_text("value = 3\n", encoding="utf-8")
            result = self.run_guard(
                "record-evidence", "--state", str(state_path), "--kind", "boundary", "--entry", "host",
                "--command", "host check", "--observed", "host unavailable", "--level", "host", "--result", "blocked",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = self.run_guard("set-result", "--state", str(state_path), "--result", "blocked")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = self.run_guard(
                "record-evidence", "--state", str(state_path), "--kind", "boundary", "--entry", "host-recovery",
                "--command", "host check", "--observed", "host receipt is available", "--level", "host", "--result", "pass",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            diagnosis = {
                "classification": "environment",
                "cause_summary": "the host was unavailable",
                "source_refs": ["e0001", "e0002"],
                "next_action_summary": "retry the verification after host recovery",
                "next_hypothesis": "the host check will become available",
                "expected_observation": "the host check returns a real receipt",
                "new_information": {"kind": "external_change", "summary": "host recovery was observed", "source_refs": ["e0002"]},
            }
            result = self.run_guard("record-diagnosis", "--state", str(state_path), "--input", json.dumps(diagnosis))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = self.run_guard("retry-verify", "--state", str(state_path), "--reason", "host recovery observed")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["loop"]["attempt_count"], 2)
            self.assertEqual(state["loop"]["attempt_history"][-1]["outcome"], "blocked")
            self.assertEqual(state["loop"]["active_attempt"]["kind"], "verify_only")
            self.assertEqual(state["evidence"], [])
            self.assertEqual(state["result"], "pending")

    def test_retry_verify_rejects_without_new_external_information(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _, _, state_path = self.start_verify(Path(temp_dir))
            result = self.run_guard(
                "record-evidence", "--state", str(state_path), "--kind", "boundary", "--entry", "host",
                "--command", "host check", "--observed", "host unavailable", "--level", "host", "--result", "blocked",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = self.run_guard("set-result", "--state", str(state_path), "--result", "blocked")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            diagnosis = {
                "classification": "environment",
                "cause_summary": "the host is unavailable",
                "source_refs": ["e0001"],
                "next_action_summary": "wait for a real host observation",
                "next_hypothesis": "the host will become available",
                "expected_observation": "the host check returns",
                "new_information": {"kind": "none", "summary": "no new observation", "source_refs": []},
            }
            result = self.run_guard("record-diagnosis", "--state", str(state_path), "--input", json.dumps(diagnosis))
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            before = state_path.read_bytes()
            result = self.run_guard("retry-verify", "--state", str(state_path), "--reason", "try again")
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(result.stdout)["code"], "EXTERNAL_OBSERVATION_REQUIRED")
            self.assertEqual(state_path.read_bytes(), before)

    def test_post_commit_residual_worktree_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            repo, target, state_path = self.init_run(root, delivery=True)
            target.write_text("value = 2\n", encoding="utf-8")
            self.git(repo, "add", "--", "src/a.py")
            for command in (
                ("record-plan", "--state", str(state_path), "--profile", "session_main", "--model", "gpt-test-main", "--reasoning-effort", "high"),
                ("transition", "--state", str(state_path), "--to", "implement"),
                ("set-changes", "--state", str(state_path)),
                ("transition", "--state", str(state_path), "--to", "verify"),
                ("record-evidence", "--state", str(state_path), "--kind", "success", "--entry", "unit", "--command", "test", "--observed", "passed", "--level", "test", "--result", "pass"),
                ("record-evidence", "--state", str(state_path), "--kind", "boundary", "--entry", "edge", "--command", "edge", "--observed", "passed", "--level", "test", "--result", "pass"),
                ("set-result", "--state", str(state_path), "--result", "pass"),
                ("record-review", "--state", str(state_path), "--result", "pass", "--observed", "reviewed", "--profile", "session_main", "--model", "gpt-test-main", "--reasoning-effort", "high"),
                ("transition", "--state", str(state_path), "--to", "deliver"),
                ("audit", "--state", str(state_path), "--repo", str(repo)),
            ):
                result = self.run_guard(*command)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.git(repo, "commit", "-m", "reviewed change")
            target.write_text("value = 3\n", encoding="utf-8")
            result = self.run_guard("transition", "--state", str(state_path), "--to", "complete")
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn("UNREVIEWED_RESIDUAL_CHANGES", result.stdout)


if __name__ == "__main__":
    unittest.main()
