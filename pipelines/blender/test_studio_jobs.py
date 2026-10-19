"""Failure/recovery contract tests; actual Blender/MCP acceptance is separate."""
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from studio_jobs import StudioJobs, digest, run_job, save_json


class ReviewRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source.blend'
        self.source.write_bytes(b'unchanged source fixture')
        self.blender = self.root / 'fake-blender.exe'
        self.blender.write_bytes(b'fixture')
        self.jobs = StudioJobs(self.root, self.blender)
        self.job_id = 'a' * 32
        self.output = self.jobs.job_dir(self.job_id)
        self.output.mkdir()
        self.receipt = dict(id=self.job_id, status='running', source=str(self.source),
                            source_sha256=digest(self.source), blender=str(self.blender),
                            samples=2, size=128, audit_only=True,
                            created_at=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat())
        save_json(self.output / 'job.json', self.receipt)

    def tearDown(self):
        self.temporary.cleanup()

    def test_dead_runner_does_not_auto_resume(self):
        before = list(self.jobs.jobs.iterdir())
        with patch('studio_jobs.owner_state', return_value='dead'):
            result = self.jobs.status(self.job_id)
        self.assertEqual(result['status'], 'interrupted')
        self.assertTrue(result['can_resume'])
        self.assertEqual(list(self.jobs.jobs.iterdir()), before)

    def test_dead_runner_live_child_blocks_resume(self):
        with patch('studio_jobs.owner_state', side_effect=lambda path: 'alive' if path.name == 'blender-owner.json' else 'dead'):
            self.assertEqual(self.jobs.status(self.job_id)['status'], 'interrupted_child_running')
            with self.assertRaises(ValueError):
                self.jobs.resume(self.job_id)

    def test_failed_receipt_with_live_child_blocks_resume(self):
        save_json(self.output / 'job.json', dict(self.receipt, status='failed'))
        with patch('studio_jobs.owner_state', return_value='alive'):
            with self.assertRaises(ValueError):
                self.jobs.resume(self.job_id)

    def test_nonzero_blender_is_failed_and_preserves_source(self):
        class FailedChild:
            pid = 123
            def wait(self, timeout):
                return 7
        before = self.source.read_bytes()
        with patch('studio_jobs.subprocess.Popen', return_value=FailedChild()), patch('studio_jobs.process_identity', return_value=None):
            run_job(self.output)
        result = self.jobs.status(self.job_id)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['exit_code'], 7)
        self.assertTrue(result['source_unchanged'])
        self.assertEqual(self.source.read_bytes(), before)

    def test_changed_source_cannot_resume(self):
        self.source.write_bytes(b'new user revision')
        with patch('studio_jobs.owner_state', return_value='dead'):
            with self.assertRaisesRegex(ValueError, 'Source changed'):
                self.jobs.resume(self.job_id)

    def test_repeat_resume_returns_existing_replacement(self):
        replacement_id = 'b' * 32
        replacement = self.jobs.job_dir(replacement_id)
        replacement.mkdir()
        save_json(replacement / 'job.json', dict(self.receipt, id=replacement_id, status='complete', resume_of=self.job_id))
        save_json(self.output / 'resume-link.json', {'job_id': replacement_id})
        with patch('studio_jobs.owner_state', return_value='dead'):
            result = self.jobs.resume(self.job_id)
        self.assertEqual(result['job_id'], replacement_id)
        self.assertEqual(len(list(self.jobs.jobs.iterdir())), 2)

    def test_concurrent_resume_claim_never_starts_duplicate(self):
        (self.output / 'resume-link.json').write_text('', encoding='utf-8')
        with patch('studio_jobs.owner_state', return_value='dead'):
            result = self.jobs.resume(self.job_id)
        self.assertEqual(result['status'], 'resume_starting')
        self.assertEqual(len(list(self.jobs.jobs.iterdir())), 1)


if __name__ == '__main__':
    unittest.main()
