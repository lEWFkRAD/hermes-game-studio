"""Offline transport/recovery checks. These never contact the live art service."""
import contextlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import uuid

import neongrid_photo_mesh as driver
driver.EXPECTED_BACKEND = "fixture-backend"

JOB = "8" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"fixture"
GLB = b"glTF" + struct.pack("<II", 2, 28) + b"0" * 16


class SaveJsonRetryTests(unittest.TestCase):
    def setUp(self):
        # Explicit workspace fixtures avoid system-temp ACL peculiarities on
        # Windows. Only this test's unique files are removed during cleanup.
        self.root = Path(__file__).resolve().parent / ("_save_json_test_" + uuid.uuid4().hex)
        self.root.mkdir()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for path in self.root.iterdir():
            path.unlink()
        self.root.rmdir()

    @staticmethod
    def windows_error(code):
        error = PermissionError("Simulated Windows replacement conflict")
        error.winerror = code
        return error

    def test_transient_windows_conflict_retries_same_pending_then_replaces(self):
        real_replace = Path.replace
        for code in (5, 32, 33):
            with self.subTest(winerror=code):
                target = self.root / ("state-" + str(code) + ".json")
                original = b'{"old":true}\n'
                target.write_bytes(original)
                pending_paths = []
                def replacement(pending, destination):
                    pending_paths.append(pending)
                    self.assertEqual(target.read_bytes(), original)
                    self.assertEqual(json.loads(pending.read_text()), {"new": True})
                    if len(pending_paths) == 1:
                        raise self.windows_error(code)
                    return real_replace(pending, destination)
                with patch.object(Path, "replace", new=replacement), patch.object(driver.time, "sleep") as sleep:
                    driver.save_json(target, {"new": True})
                self.assertEqual(len(pending_paths), 2)
                self.assertEqual(pending_paths[0], pending_paths[1])
                self.assertEqual(json.loads(target.read_text()), {"new": True})
                self.assertFalse(pending_paths[0].exists())
                sleep.assert_called_once_with(0.05)

    def test_persistent_conflict_is_bounded_and_preserves_old_and_pending(self):
        target = self.root / "state.json"
        original = b'{"complete":137}\n'
        target.write_bytes(original)
        error = self.windows_error(5)
        with patch.object(Path, "replace", side_effect=error) as replace, patch.object(driver.time, "sleep") as sleep:
            with self.assertRaises(PermissionError) as raised:
                driver.save_json(target, {"complete": 138})
        self.assertIs(raised.exception, error)
        self.assertEqual(replace.call_count, 7)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.05, 0.1, 0.2, 0.4, 0.8, 1.6])
        self.assertEqual(target.read_bytes(), original)
        pending = list(self.root.glob("state.json.*.tmp"))
        self.assertEqual(len(pending), 1)
        self.assertEqual(json.loads(pending[0].read_text()), {"complete": 138})

    def test_other_filesystem_errors_propagate_immediately(self):
        for index, error in enumerate((PermissionError("No Windows error code"), self.windows_error(123))):
            with self.subTest(error=error):
                target = self.root / ("state-" + str(index) + ".json")
                target.write_bytes(b'{"old":true}\n')
                with patch.object(Path, "replace", side_effect=error) as replace, patch.object(driver.time, "sleep") as sleep:
                    with self.assertRaises(PermissionError) as raised:
                        driver.save_json(target, {"new": True})
                self.assertIs(raised.exception, error)
                replace.assert_called_once()
                sleep.assert_not_called()
                self.assertEqual(json.loads(target.read_text()), {"old": True})

    def test_exclusive_attempt_marker_is_not_replaced_or_retried(self):
        target = self.root / "submission-attempt.json"
        target.write_bytes(b'{"request_key":"original"}\n')
        with patch.object(Path, "replace") as replace, patch.object(driver.time, "sleep") as sleep:
            with self.assertRaises(FileExistsError):
                driver.save_json(target, {"request_key": "replacement"}, exclusive=True)
        replace.assert_not_called()
        sleep.assert_not_called()
        self.assertEqual(json.loads(target.read_text()), {"request_key": "original"})
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_generation_transport_error_still_issues_only_one_post(self):
        api = driver.Api()
        error = self.windows_error(32)
        manifest = dict(request_key="fixture", preset="fast", label="test", input_file="input.png", mime="image/png")
        with patch.object(api.opener, "open", side_effect=error) as transport, patch.object(driver.time, "sleep") as sleep:
            with self.assertRaises(PermissionError) as raised:
                api.submit(manifest, PNG)
        self.assertIs(raised.exception, error)
        transport.assert_called_once()
        self.assertEqual(transport.call_args.args[0].method, "POST")
        sleep.assert_not_called()


class FakeApi:
    def __init__(self, health=None, jobs=None):
        self.health = health or {"generation_backend": "fixture-backend"}
        self.jobs = jobs or []
        self.calls = []
        self.fail_post = False
        self.before_submit = None
        self.state = {"id": JOB, "status": "success", "stage": "Ready", "preset": "detailed",
                      "photo_url": "/photo", "metrics": {"bytes": len(GLB)},
                      "checkpoints": {"cutout": {"bytes": len(PNG)}, "mesh": {"bytes": len(GLB)}, "wire": {"bytes": len(GLB)}}}
        self.damage_final = False

    def get(self, path):
        self.calls.append(("GET", path))
        if path == "/health":
            return self.health
        if path == "/api/jobs":
            return {"jobs": self.jobs}
        if path == "/api/jobs/" + JOB:
            return self.state
        raise AssertionError(path)

    def submit(self, manifest, image):
        self.calls.append(("POST", "/generate3d"))
        if self.before_submit:
            self.before_submit(manifest, image)
        if self.fail_post:
            raise TimeoutError("Response lost after server acceptance")
        return {"backend": "fixture-backend", "job_id": JOB}

    def binary(self, path, limit):
        self.calls.append(("GET", path))
        if path.endswith("/photo") or path.endswith("/cutout"):
            return PNG
        return GLB[:-1] if self.damage_final and path.startswith("/local-models/") else GLB


class DriverRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="neongrid-photo-mesh-")
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.png"
        self.source.write_bytes(PNG)
        self.console = contextlib.redirect_stdout(io.StringIO())
        self.console.__enter__()

    def tearDown(self):
        self.console.__exit__(None, None, None)
        self.temporary.cleanup()

    def prepared(self):
        return driver.prepare(self.source, "NeonGrid crown form only", "crown-v01", self.root / "runs")

    def receipt(self, run):
        manifest = driver.load_json(run / "input-manifest.json")
        records = self.root / "records"
        records.mkdir(exist_ok=True)
        driver.save_json(records / (JOB + ".json"), {"id": JOB, "preset": manifest["preset"], "request_key": manifest["request_key"],
                                                   "input_hash": manifest["input_sha256"], "status": "processing"})
        return records

    def test_busy_keeps_input_and_does_not_submit_or_stop(self):
        run = self.prepared()
        api = FakeApi(jobs=[{"id": JOB, "status": "processing", "stage": "Generating textures"}])
        self.assertIsNone(driver.submit_once(run, api))
        self.assertEqual((run / "input.png").read_bytes(), PNG)
        self.assertFalse((run / "submission-attempt.json").exists())
        self.assertTrue(all(method == "GET" for method, _ in api.calls))

    def test_no_backend_selection_cannot_contact_service(self):
        run = self.prepared()
        api = FakeApi()
        with patch.object(driver, 'EXPECTED_BACKEND', None):
            with self.assertRaisesRegex(RuntimeError, 'Set PHOTO_MESH_BACKEND'):
                driver.submit_once(run, api)
        self.assertEqual([], api.calls)
        self.assertFalse((run / 'submission-attempt.json').exists())

    def test_wrong_live_backend_is_rejected_before_post(self):
        run = self.prepared()
        api = FakeApi(health={"generation_backend": "tripo"})
        with self.assertRaisesRegex(RuntimeError, "not using the configured backend"):
            driver.submit_once(run, api)
        self.assertFalse(any(method == "POST" for method, _ in api.calls))

    def test_lost_response_reconciles_exact_record_without_second_post(self):
        run = self.prepared()
        api = FakeApi()
        api.fail_post = True
        def persist_server_job(manifest, image):
            self.assertTrue((run / "submission-attempt.json").is_file())
            self.assertEqual(image, (run / manifest["input_file"]).read_bytes())
            self.receipt(run)
        api.before_submit = persist_server_job
        with self.assertRaisesRegex(RuntimeError, "uncertain"):
            driver.submit_once(run, api)
        with self.assertRaises(FileExistsError):
            driver.submit_once(run, api)
        result = driver.collect(run, api, wait=0, records=self.root / "records")
        self.assertEqual(result["phase"], "collected")
        self.assertEqual(driver.load_json(run / "job.json")["job_id"], JOB)
        self.assertEqual(sum(method == "POST" for method, _ in api.calls), 1)
        self.assertFalse(any("isolate" in path or "retry" in path or "stop" in path or "import" in path for _, path in api.calls))

    def test_unknown_submission_remains_unresolved_without_network_mutation(self):
        run = self.prepared()
        api = FakeApi()
        with self.assertRaisesRegex(RuntimeError, "unresolved"):
            driver.collect(run, api, wait=0, records=self.root / "missing")
        self.assertEqual(api.calls, [])

    def test_final_download_can_resume_after_truncation_without_inference(self):
        run = self.prepared()
        driver.bind_job(run, JOB, "fixture")
        api = FakeApi()
        api.damage_final = True
        with self.assertRaisesRegex(ValueError, "Incomplete artifact"):
            driver.collect(run, api, wait=0)
        self.assertFalse((run / "final.glb").exists())
        self.assertEqual((run / "surface.glb").read_bytes(), GLB)
        api.damage_final = False
        result = driver.collect(run, api, wait=0)
        self.assertEqual(result["phase"], "collected")
        self.assertEqual(len(result["files"]), 5)
        self.assertTrue(all(method == "GET" for method, _ in api.calls))
        hashes = driver.load_json(run / "artifacts.json")
        self.assertEqual(hashes["final.glb"]["sha256"], driver.sha256(GLB))

    def test_existing_run_and_changed_input_cannot_submit(self):
        run = self.prepared()
        with self.assertRaises(FileExistsError):
            self.prepared()
        (run / "input.png").write_bytes(PNG + b"changed")
        api = FakeApi()
        with self.assertRaisesRegex(ValueError, "Saved input changed"):
            driver.submit_once(run, api)
        self.assertEqual(api.calls, [])

    def test_fast_multipart_uses_explicit_preset_and_saved_receipt(self):
        run = driver.prepare(self.source, "Face Fast benchmark", "fast-face", self.root / "runs", preset="fast")
        api = driver.Api()
        captured = []
        class LocalTransport:
            def open(self, request, timeout):
                captured.append(request)
                return contextlib.closing(io.BytesIO(json.dumps({"job_id": JOB, "backend": "fixture-backend"}).encode()))
        api.opener = LocalTransport()
        manifest = driver.load_json(run / "input-manifest.json")
        result = api.submit(manifest, PNG)
        self.assertEqual(result["job_id"], JOB)
        self.assertEqual(manifest["preset"], "fast")
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].method, "POST")
        self.assertIn(b'name="preset"\r\n\r\nfast\r\n', captured[0].data)
        self.assertNotIn(b'\r\n\r\ndetailed\r\n', captured[0].data)

    def test_fast_lost_response_matches_fast_record_not_detailed(self):
        run = driver.prepare(self.source, "Face Fast benchmark", "fast-face", self.root / "runs", preset="fast")
        api = FakeApi()
        api.state["preset"] = "fast"
        api.fail_post = True
        def persist_server_job(manifest, image):
            records = self.receipt(run)
            wrong = {"id": "9" * 32, "preset": "detailed", "request_key": manifest["request_key"],
                     "input_hash": manifest["input_sha256"], "status": "failed"}
            driver.save_json(records / (wrong["id"] + ".json"), wrong)
        api.before_submit = persist_server_job
        with self.assertRaisesRegex(RuntimeError, "uncertain"):
            driver.submit_once(run, api)
        self.assertEqual(driver.load_json(run / "submission-attempt.json")["preset"], "fast")
        result = driver.collect(run, api, wait=0, records=self.root / "records")
        self.assertEqual(result["job_id"], JOB)
        self.assertEqual(result["preset"], "fast")
        self.assertEqual(sum(method == "POST" for method, _ in api.calls), 1)
        with self.assertRaisesRegex(RuntimeError, "unresolved"):
            driver.recover_binding(run, self.root / "records", explicit_job="9" * 32)


if __name__ == "__main__":
    unittest.main()
