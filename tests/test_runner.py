import importlib.util
from pathlib import Path
import sys
import unittest

spec = importlib.util.spec_from_file_location("studio", Path(__file__).resolve().parents[1] / "skills/game-dev-verify/scripts/studio.py")
studio = importlib.util.module_from_spec(spec)
spec.loader.exec_module(studio)

class AcceptanceTests(unittest.TestCase):
    def result(self, text="", code=0, timeout=False):
        return {"stdout": text, "stderr": "", "exit_code": code, "timeout": timeout}

    def test_zero_exit_script_error_rejected(self):
        self.assertFalse(studio.passed(self.result("SCRIPT ERROR: invalid member")))

    def test_indented_error_rejected(self):
        self.assertFalse(studio.passed(self.result("  ERROR: import failed")))

    def test_marker_must_be_exact_line(self):
        marker = "STUDIO_GAMEPLAY_PASS checks=12"
        self.assertFalse(studio.passed(self.result(marker + "0"), marker))
        self.assertTrue(studio.passed(self.result("header\n" + marker + "\n"), marker))

    def test_missing_marker_rejected(self):
        self.assertFalse(studio.passed(self.result("Godot started"), "STUDIO_GAMEPLAY_PASS checks=12"))

    def test_nonzero_exit_rejected(self):
        self.assertFalse(studio.passed(self.result("", 1)))

    def test_actual_timeout_rejected(self):
        result = studio.run([sys.executable, "-B", "-c", "import time; time.sleep(3)"], timeout=0.1)
        self.assertTrue(result["timeout"])
        self.assertFalse(studio.passed(result))

    def test_missing_executable_rejected(self):
        result = studio.run([str(Path(__file__).parent / "nonexistent-engine-29fb7.exe")])
        self.assertFalse(studio.passed(result))

if __name__ == "__main__":
    unittest.main()
