"""Pure local fixtures: no Mac, network, subprocesses, model weights or inference."""
import concurrent.futures
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zlib

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
EVIDENCE = ROOT / 'artifacts/native-quality-tests'
EVIDENCE.mkdir(parents=True, exist_ok=True)
RUN = Path(tempfile.mkdtemp(prefix='fixture-tests-', dir=EVIDENCE))
spec = importlib.util.spec_from_file_location('quality_job', HERE / 'quality-job.py')
job = importlib.util.module_from_spec(spec)
spec.loader.exec_module(job)


def chunk(kind, data):
    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)


def png():
    # A tiny, explicitly synthetic test input; never offered as a game asset.
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 8, 8, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress((b'\x00' + b'\x40\x80\xc0\xff' * 8) * 8)) + chunk(b'IEND', b''))


class Fixtures(unittest.TestCase):
    def setUp(self):
        self.base = RUN / self._testMethodName
        self.base.mkdir()
        self.input = self.base / 'fixture.png'
        self.input.write_bytes(png())
        self.input_sha = job.sha(self.input)
        self.run = self.base / 'prepared'
        # Any accidental process/network access fails a local test immediately.
        self.popen = patch.object(job.subprocess, 'Popen', side_effect=AssertionError('No processes in fixtures'))
        self.run_process = patch.object(job.subprocess, 'run', side_effect=AssertionError('No commands in fixtures'))
        self.popen.start(); self.run_process.start()
        self.addCleanup(self.popen.stop); self.addCleanup(self.run_process.stop)

    def prepare(self):
        return job.prepare(self.run, 'fixture-asset', self.input, self.input_sha, 42)

    def test_profile_has_ten_evidence_bound_weights_and_sources(self):
        profile = job.load_profile()
        reference = json.loads((HERE / 'reference-pins.json').read_text())
        self.assertEqual(len(reference['weights']), 10)
        self.assertEqual(sorted(profile['weights'],key=lambda r:r['name']), sorted(reference['weights'],key=lambda r:r['name']))
        self.assertEqual(sorted(profile['runtimeLibraries'],key=lambda r:r['name']), sorted(reference['runtimeLibraries'],key=lambda r:r['name']))
        self.assertEqual(profile['sourceCommit'], reference['sourceCommit'])
        for name, row in profile['sources'].items():
            self.assertEqual(job.sha(HERE / 'pinned-sources' / name), row['sha256'])

    def test_invalid_json_receipts_are_bounded_objects(self):
        self.prepare()
        marker = self.run / 'prepared.json'
        marker.chmod(0o666)
        for raw in [b'null', b'[]', b' ' * (2 * 1024 * 1024 + 100)]:
            marker.write_bytes(raw)
            result = job.status(self.run)
            self.assertEqual(result['status'], 'incomplete-or-invalid-package-report-only')
            self.assertFalse(result['launchAllowed'])

    def test_foreign_receipt_never_echoes_arbitrary_payload(self):
        prepared = self.prepare(); job.claim_launch(self.run, prepared['planSha256'])
        receipt = self.run / 'launch.json'
        for binding in ['wrong-plan', prepared['planSha256']]:
            receipt.write_text(json.dumps({'planSha256': binding, 'status': 'dispatched',
                                          'api_key': 'SYNTHETIC_SENTINEL'}))
            result = job.status(self.run)
            self.assertNotIn('SYNTHETIC_SENTINEL', json.dumps(result))
            self.assertFalse(result['launchAllowed'])

    def test_symlink_receipt_is_report_only(self):
        prepared = self.prepare(); job.claim_launch(self.run, prepared['planSha256'])
        target = self.base / 'foreign.json'; target.write_text('{"api_key":"SYNTHETIC_SENTINEL"}')
        link = self.run / 'launch.json'
        # Model the lstat result so this pure fixture needs no Windows symlink privilege.
        original = Path.is_symlink
        with patch.object(Path, 'is_symlink', lambda path: path == link or original(path)):
            result = job.status(self.run)
        self.assertEqual(result['status'], 'receipt-inconsistent-report-only')
        self.assertNotIn('SYNTHETIC_SENTINEL', json.dumps(result))

    def test_loader_and_backend_overrides_removed(self):
        result = job.clean_environment({'PATH': '/fixture', 'DYLD_LIBRARY_PATH': 'untrusted',
                    'DYLD_INSERT_LIBRARIES': 'untrusted', 'GGML_BACKEND_PATH': 'untrusted',
                    'GGML_METAL_PATH_RESOURCES': 'untrusted', 'PYTHONPATH': 'untrusted', 'PYTHONHOME': 'untrusted'})
        self.assertEqual(result, {'PATH': '/fixture'})

    def test_runtime_alias_closure_detects_mutated_alias(self):
        build = self.base / 'build'; build.mkdir()
        binary = build / 'trellis-cli'; binary.write_bytes(b'fixture executable, never executed')
        rows, aliases = [], []
        for stem in ['libggml', 'libggml-base', 'libggml-blas', 'libggml-cpu', 'libggml-metal']:
            name, alias = stem + '.0.24.0.dylib', stem + '.0.dylib'
            data = ('fixture library ' + stem).encode()
            (build / name).write_bytes(data); (build / alias).write_bytes(data)
            rows.append({'name': name, 'sha256': hashlib.sha256(data).hexdigest()}); aliases.append(alias)
        profile = {'runtimeLibraries': rows}
        def otool_fixture(argv, **kwargs):
            owner = Path(argv[2])
            if argv[1] == '-l':
                return SimpleNamespace(stdout=('Load command 7\n cmd LC_RPATH\n cmdsize 64\n path '
                    + str(build) + ' (offset 12)\n') if owner == binary else '')
            refs = aliases if owner == binary else [owner.name]
            data = str(owner) + ':\n' + ''.join('\t@rpath/' + ref +
                ' (compatibility version 0.0.0, current version 0.24.0)\n' for ref in refs)
            return SimpleNamespace(stdout=data + '\t/usr/lib/libSystem.B.dylib (compatibility version 1.0.0, current version 1336.0.0)\n')
        with patch.object(job.subprocess, 'run', side_effect=otool_fixture):
            result = job.verify_libraries(binary, profile, self.base, {})
            self.assertEqual(len(result['verifiedLibraries']), 5)
            self.assertEqual({row['qualifiedName'] for row in result['resolvedDependencies']}, {r['name'] for r in rows})
            (build / 'libggml-cpu.0.dylib').write_bytes(b'mutated alias, original versioned file unchanged')
            with self.assertRaisesRegex(ValueError, 'Unqualified dynamic dependency'):
                job.verify_libraries(binary, profile, self.base, {})

    def test_prepare_immutable_input_and_snapshot_commands(self):
        result = self.prepare()
        plan, marker, profile = job.verify_package(self.run)
        self.assertEqual(result['status'], 'prepared')
        self.assertTrue(result['launchAllowed'])
        self.assertEqual((self.run / 'input.png').read_bytes(), self.input.read_bytes())
        for key, filename in [('runner', 'mac_runner.py'), ('guard', 'guard-model.py'), ('lease', 'manual-queue-lease.py')]:
            self.assertEqual(plan['commands'][key][1], plan['target']['runDir'] + '/source/' + filename)
        command = plan['commands']['cli']
        for flag, value in [('--res', '1024'), ('--tex-res', '1024'), ('--steps', '12'), ('--threads', '4'), ('--seed', '42')]:
            self.assertEqual(command[command.index(flag) + 1], value)
        self.assertEqual(result['planSha256'], marker['planSha256'])

    def test_existing_prepare_is_report_only_even_with_different_arguments(self):
        first = self.prepare()
        original = (self.run / 'plan.json').read_bytes()
        again = job.prepare(self.run, 'different', Path('missing.png'), 'wrong', -1)
        self.assertEqual(again['planSha256'], first['planSha256'])
        self.assertEqual((self.run / 'plan.json').read_bytes(), original)

    def test_partial_directory_is_never_resumed(self):
        self.run.mkdir(); (self.run / 'partial.txt').write_text('fixture interrupted preparation')
        result = self.prepare()
        self.assertFalse(result['launchAllowed'])
        self.assertFalse((self.run / 'plan.json').exists())

    def test_input_sha_and_png_crc_are_enforced_before_creating_run(self):
        with self.assertRaises(ValueError):
            job.prepare(self.run, 'fixture', self.input, '0' * 64, 42)
        self.assertFalse(self.run.exists())
        corrupt = bytearray(png()); corrupt[30] ^= 1
        with self.assertRaises(ValueError):
            job.validate_png(corrupt)

    def test_invalid_name_seed_and_target_path_are_refused(self):
        for name in ['../outside', 'UPPER', 'space here', 'aux', 'a' * 65]:
            with self.assertRaises(ValueError):
                job.prepare(self.run, name, self.input, self.input_sha, 42)
        for seed in [-1, True, 2147483648]:
            with self.assertRaises(ValueError):
                job.prepare(self.run, 'fixture', self.input, self.input_sha, seed)
        with self.assertRaises(ValueError):
            job.prepare(self.run, 'fixture', self.input, self.input_sha, 42, {'macRoot': '/tmp/../escape'})
        self.assertFalse(self.run.exists())

    def test_changed_input_disables_launch(self):
        self.prepare(); target = self.run / 'input.png'; target.chmod(0o666); target.write_bytes(png() + b'x')
        self.assertFalse(job.status(self.run)['launchAllowed'])
        with self.assertRaises(ValueError):
            job.verify_package(self.run)

    def test_changed_snapshot_disables_launch(self):
        self.prepare(); target = self.run / 'source/guard-model.py'; target.chmod(0o666)
        target.write_bytes(target.read_bytes() + b'\n# fixture mutation\n')
        self.assertFalse(job.status(self.run)['launchAllowed'])

    def test_permanent_claim_has_one_winner_under_concurrency(self):
        prepared = self.prepare()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: job.claim_launch(self.run, prepared['planSha256']), range(8)))
        self.assertEqual(sum(x is not None for x in results), 1)
        self.assertFalse(job.status(self.run)['launchAllowed'])

    def test_ambiguous_dispatch_can_only_be_reported(self):
        prepared = self.prepare(); job.claim_launch(self.run, prepared['planSha256'])
        result = job.launch(self.run, prepared['planSha256'])
        self.assertEqual(result['status'], 'dispatch-uncertain-report-only')
        self.assertFalse((self.run / 'controller.log').exists())

    def test_residue_without_intent_cannot_be_retried_or_claimed_complete(self):
        self.prepare(); (self.run / 'controller.log').write_text('fixture residue')
        result = job.status(self.run)
        self.assertEqual(result['status'], 'receipt-inconsistent-report-only')
        self.assertFalse(result['launchAllowed'])

    def write_terminal_fixture(self, guard_pid=123):
        prepared = self.prepare(); plan, _, _ = job.verify_package(self.run)
        job.claim_launch(self.run, prepared['planSha256'])
        job.exclusive_json(self.run / 'lease.json', {'status': 'complete', 'guard_command': plan['commands']['guard'],
            'guard_pid': 123, 'guard_exit_code': 0, 'guard_cleanup_verified': True, 'marker_released': True})
        (self.run / 'generation-guard').mkdir()
        job.exclusive_json(self.run / 'generation-guard/guard.json', {'status': 'complete', 'controller_pid': guard_pid,
            'command': plan['commands']['runner'], 'exit_code': 0, 'owned_child_exited': True,
            'owned_processes_remaining': 0, 'owned_survivors': []})
        (self.run / 'cleaned-mesh-pbr.bin').write_bytes(b'FIXTURE ONLY: deliberately unvalidated dump bytes')

    def test_terminal_receipt_requires_matching_owned_guard(self):
        self.write_terminal_fixture(124)
        result = job.status(self.run)
        self.assertFalse(result['guardCleanupVerified'])
        self.assertNotEqual(result['status'], 'generation-complete-awaiting-dump-and-art-review')

    def test_terminal_success_never_claims_art_or_dump_validation(self):
        self.write_terminal_fixture()
        result = job.status(self.run)
        self.assertEqual(result['status'], 'generation-complete-awaiting-dump-and-art-review')
        self.assertFalse(result['acceptedForGame'])
        self.assertFalse(result['dump']['validated'])
        self.assertFalse(result['launchAllowed'])


if __name__ == '__main__':
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Fixtures))
    report = {'schema': 'quality-job-local-fixtures-v1', 'passed': result.wasSuccessful(), 'testsRun': result.testsRun,
              'failures': [(str(case), error) for case, error in result.failures + result.errors],
              'scope': 'Pure local file/validation/idempotency fixtures. Process creation and subprocess.run patched to fail. No network code called, no Mac probes/uploads/inference.',
              'modelTested': False, 'launcherSha256': job.sha(HERE / 'quality-job.py'),
              'profileSha256': job.sha(HERE / 'profile.json'), 'testSha256': job.sha(Path(__file__))}
    (RUN / 'receipt.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'receipt': str(RUN / 'receipt.json'), 'passed': report['passed'], 'testsRun': result.testsRun}))
    raise SystemExit(0 if result.wasSuccessful() else 1)
