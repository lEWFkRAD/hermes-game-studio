"""Bounded, dependency-free Godot fixture verification. No network or model calls."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time
import uuid

SKILL = Path(__file__).resolve().parents[1]
FIXTURE = SKILL / "assets" / "signal-garden"
ERROR = re.compile(r"SCRIPT ERROR:|ERROR:|Parse Error|CHECK_FAILED:", re.I)


def hashes(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and ".godot" not in p.parts}


def run(command, timeout=45):
    started = time.monotonic()
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=timeout)
        return {"command": command, "exit_code": result.returncode,
                "seconds": round(time.monotonic() - started, 3),
                "stdout": result.stdout, "stderr": result.stderr, "timeout": False}
    except subprocess.TimeoutExpired as exc:
        def decode(value):
            return value.decode("utf-8", "replace") if isinstance(value, bytes) else (value or "")
        return {"command": command, "exit_code": None,
                "seconds": round(time.monotonic() - started, 3),
                "stdout": decode(exc.stdout), "stderr": decode(exc.stderr), "timeout": True}
    except OSError as exc:
        return {"command": command, "exit_code": None, "seconds": 0,
                "stdout": "", "stderr": str(exc), "timeout": False}


def passed(result, marker=None):
    content = result["stdout"] + "\n" + result["stderr"]
    return (result["exit_code"] == 0 and not result["timeout"]
            and not ERROR.search(content) and (marker is None or marker in content.splitlines()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["doctor", "check"])
    parser.add_argument("--godot", default=os.environ.get("GODOT_BIN", "godot"))
    parser.add_argument("--output", type=Path, default=Path("artifacts"))
    args = parser.parse_args()
    binary = shutil.which(args.godot)
    if not binary:
        print(json.dumps({"status": "blocked", "reason": "Godot binary not found", "requested": args.godot}))
        return 2
    version = run([binary, "--version"], timeout=10)
    if not passed(version) or not re.match(r"4\.", version["stdout"].strip()):
        print(json.dumps({"status": "blocked", "reason": "Godot 4 editor required", "probe": version}))
        return 2
    info = {"python": sys.version.split()[0], "platform": platform.platform(),
            "godot": binary, "godot_version": version["stdout"].strip(),
            "godot_sha256": hashlib.sha256(Path(binary).read_bytes()).hexdigest()}
    if args.action == "doctor":
        print(json.dumps({"status": "available", **info, "scope": "Version/path probe only"}, indent=2))
        return 0
    if args.output.resolve().is_relative_to(FIXTURE.resolve()):
        print(json.dumps({"status": "blocked", "reason": "Evidence output cannot be inside fixture source"}))
        return 2
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    out = args.output.resolve() / stamp
    out.mkdir(parents=True, exist_ok=False)
    project = out / "project"
    before = hashes(FIXTURE)
    shutil.copytree(FIXTURE, project, ignore=shutil.ignore_patterns(".godot"))
    steps = []
    common = [binary, "--headless", "--path", str(project)]
    stages = [("import", common + ["--editor", "--import"], None),
              ("gameplay", common + ["--script", "res://test_game.gd"], "STUDIO_GAMEPLAY_PASS checks=12"),
              ("scene_boot", common + ["--quit-after", "5"], None)]
    for name, command, marker in stages:
        result = run(command)
        (out / (name + ".stdout.log")).write_text(result["stdout"], encoding="utf-8")
        (out / (name + ".stderr.log")).write_text(result["stderr"], encoding="utf-8")
        ok = bool(passed(result, marker))
        steps.append({"name": name, "passed": ok, **result})
        if not ok:
            break
    unchanged = before == hashes(FIXTURE)
    ok = len(steps) == len(stages) and all(s["passed"] for s in steps) and unchanged
    evidence = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in out.glob("*.log")}
    receipt = {"schema_version": 1, "run_id": stamp, "created_utc": datetime.now(timezone.utc).isoformat(),
               "status": "passed" if ok else "failed", "environment": info,
               "source_sha256": before, "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
               "source_unchanged": unchanged, "steps": steps, "evidence_sha256": evidence,
               "unverified": ["rendered appearance", "physical input", "frame times", "native export", "human playtest", "other platforms"],
               "scope": "Bundled fixture only; not another project or end-to-end agent execution"}
    (out / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": receipt["status"], "receipt": str(out / "receipt.json"),
                      "gates": [{"name": s["name"], "passed": s["passed"]} for s in steps]}, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
