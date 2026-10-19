"""Small synthetic serialization tests only; no TRELLIS assets or inference."""
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest


TOOL = Path(__file__).resolve().parents[1] / 'inspect-trellis-dump.py'
spec = importlib.util.spec_from_file_location('dump_inspector', TOOL)
inspector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inspector)
FIXTURES = Path(tempfile.mkdtemp(prefix='dump-inspector-validation-', dir=None))


def fixture(name, *, header=(3, 1, 1, 512), vertices=None, faces=(0, 1, 2),
            coords=(0, 200, 511), pbr=(0.1, 0.2, 0.3, 0.0, 0.5, 1.0), suffix=b''):
    vertices = [0., 0., 0., 1., 0., 0., 0., 1., 0.] if vertices is None else vertices
    payload = struct.pack('<4i', *header)
    payload += struct.pack(f'<{len(vertices)}f', *vertices)
    payload += struct.pack(f'<{len(faces)}i', *faces)
    payload += struct.pack(f'<{len(coords)}i', *coords)
    payload += struct.pack(f'<{len(pbr)}f', *pbr)
    path = FIXTURES / name
    path.write_bytes(payload + suffix)
    return path


class DumpInspectorTests(unittest.TestCase):
    def check(self, path):
        return inspector.scan(path, 1024 * 1024, 10)

    def test_valid_ranges_hash_offsets(self):
        path = fixture('valid.bin')
        report = self.check(path)
        self.assertEqual(report['status'], 'passed')
        self.assertTrue(report['input_unchanged'])
        self.assertEqual(report['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(report['sections']['coords']['maximum'], [0, 200, 511])
        self.assertEqual(report['sections']['pbr6']['offset_bytes'], 76)

    def test_geometry_only(self):
        report = self.check(fixture('no-pbr.bin', header=(3, 1, 0, 1024), coords=(), pbr=()))
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(report['sections']['pbr6']['minimum'], [None] * 6)
        self.assertTrue(report['warnings'])

    def test_face_and_coordinate_bounds(self):
        for name, changes in (('negative-face', {'faces': (-1, 1, 2)}),
                              ('upper-face', {'faces': (0, 1, 3)}),
                              ('negative-coord', {'coords': (-1, 2, 3)}),
                              ('upper-coord', {'coords': (0, 2, 512)})):
            with self.subTest(name=name):
                report = self.check(fixture(name + '.bin', **changes))
                self.assertEqual(report['status'], 'failed')
                self.assertIsNotNone(report['sha256'])
                self.assertIn('out-of-bounds', ' '.join(report['errors']))

    def test_nonfinite(self):
        for name, changes in (('nan-vertex', {'vertices': [float('nan')] + [0.] * 8}),
                              ('inf-pbr', {'pbr': [float('inf')] + [0.] * 5})):
            with self.subTest(name=name):
                report = self.check(fixture(name + '.bin', **changes))
                self.assertEqual(report['status'], 'failed')
                self.assertIn('nonfinite', ' '.join(report['errors']))
                json.dumps(report, allow_nan=False)

    def test_invalid_headers_and_lengths(self):
        for name, changes in (('negative', {'header': (-1, 1, 1, 512)}),
                              ('huge', {'header': (2_147_483_647, 1, 1, 512)}),
                              ('res', {'header': (3, 1, 1, 256)}),
                              ('extra', {'suffix': b'garbage'})):
            with self.subTest(name=name):
                report = self.check(fixture(name + '.bin', **changes))
                self.assertEqual(report['status'], 'failed')
                self.assertEqual(report['bytes_hashed'], 16)
        short = FIXTURES / 'short.bin'
        short.write_bytes(b'\0' * 12)
        self.assertEqual(self.check(short)['status'], 'failed')

    def test_cli_exclusive_report_and_failure_receipt(self):
        path = fixture('cli-bad.bin', faces=(0, 1, 3))
        report = FIXTURES / 'cli-report.json'
        command = [sys.executable, str(TOOL), str(path), '--report', str(report)]
        first = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(first.returncode, 1)
        self.assertEqual(json.loads(report.read_text())['status'], 'failed')
        old = report.read_bytes()
        second = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual(report.read_bytes(), old)


if __name__ == '__main__':
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(DumpInspectorTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {'schema': 'dump-inspector-tests-v1', 'status': 'passed' if result.wasSuccessful() else 'failed',
              'tests_run': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
              'script_sha256': hashlib.sha256(TOOL.read_bytes()).hexdigest(), 'fixtures': str(FIXTURES)}
    with (FIXTURES / 'test-receipt.json').open('x') as stream:
        json.dump(report, stream, indent=2)
    print(json.dumps(report))
    raise SystemExit(0 if result.wasSuccessful() else 1)
