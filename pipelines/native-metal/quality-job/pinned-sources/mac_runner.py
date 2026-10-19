"""Owned process-group watchdog: backend cannot outlive its controller."""
import json,os,signal,subprocess,sys,time

os.umask(0o077)
parent=os.getppid()
stopping=False
def halt(*_):
    global stopping
    stopping=True
signal.signal(signal.SIGTERM,halt)
child=subprocess.Popen(json.loads(sys.argv[1]))
while child.poll() is None and not stopping and os.getppid()==parent:
    time.sleep(.25)
if child.poll() is None:
    # We share the process group; our handler lets us enforce the grace deadline.
    os.killpg(os.getpgrp(),signal.SIGTERM)
    try:child.wait(timeout=4)
    except subprocess.TimeoutExpired:os.killpg(os.getpgrp(),signal.SIGKILL)
sys.exit(child.returncode or 0)
