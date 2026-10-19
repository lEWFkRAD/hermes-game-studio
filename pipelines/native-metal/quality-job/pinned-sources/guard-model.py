"""Bounded, owned-process-only Mac test controller; never alters host services."""
import argparse
import datetime
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.request
import urllib.error

import psutil


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--output', required=True)
    ap.add_argument('--seconds', type=int, default=1800)
    ap.add_argument('command', nargs=argparse.REMAINDER)
    args = ap.parse_args()
    cmd = args.command
    if cmd and cmd[0] == '--':
        cmd = cmd[1:]
    if not cmd:
        raise SystemExit('Missing command')
    out = Path(args.output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    config = json.loads((Path(os.environ.get('MAC_AUX_ROOT', str(Path.home() / 'auxiliary'))) / 'controller.json').read_text())

    class NoRedirects(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, message, headers, new_url):
            raise urllib.error.HTTPError(request.full_url, code, "Controller redirect refused", headers, fp)

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirects())

    def status():
        request = urllib.request.Request(
            'http://127.0.0.1:18080/status',
            headers={'Authorization': 'Bearer ' + config['api_key']})
        with opener.open(request, timeout=8) as response:
            return json.load(response)

    first = status()
    if first.get('active') or first.get('voice'):
        raise SystemExit('Existing controller work owns the Mac')
    if first['telemetry']['pressure'] != 1 or not first['telemetry']['thermal_ok']:
        raise SystemExit('Mac resource admission failed')
    if first['telemetry']['available_bytes'] < 14 * 1024**3 or first['telemetry']['idle_seconds'] < 300:
        raise SystemExit('Mac memory or foreground admission failed')
    swap_base = first['telemetry']['swapout_bytes']
    start = time.time()
    monotonic_start = time.monotonic()
    record = {'started_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'controller_pid': os.getpid(), 'command': cmd, 'baseline': first,
              'deadline_seconds': args.seconds, 'status': 'running'}
    child = None
    reason = None
    peak_rss = 0
    identities = {}

    def remember(proc):
        for item in [proc, *proc.children(recursive=True)]:
            try:
                identities[item.pid] = item.create_time()
            except psutil.NoSuchProcess:
                pass

    def owned_alive():
        alive = []
        for pid, created in list(identities.items()):
            try:
                proc = psutil.Process(pid)
                if proc.create_time() == created and proc.status() != psutil.STATUS_ZOMBIE:
                    alive.append(proc)
            except psutil.NoSuchProcess:
                pass
        return alive

    def remember_group():
        if child is None or child.pid not in identities:
            return
        try:
            if psutil.Process(child.pid).create_time() != identities[child.pid]:
                return  # A reused leader PID never grants ownership.
        except psutil.NoSuchProcess:
            pass
        for proc in psutil.process_iter(['pid', 'create_time']):
            try:
                if (proc.info['create_time'] >= identities[child.pid]
                        and os.getpgid(proc.pid) == child.pid
                        and os.getsid(proc.pid) == child.pid):
                    identities[proc.pid] = proc.info['create_time']
            except (ProcessLookupError, PermissionError, psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    def interrupted(signum, frame):
        nonlocal reason
        reason = 'controller-signal-' + str(signum)
        raise KeyboardInterrupt(reason)

    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(signum, interrupted)
    try:
        with (out / 'run.log').open('w') as log, (out / 'telemetry.jsonl').open('w') as samples:
            child = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT,
                                     start_new_session=True, env=os.environ.copy())
            remember(psutil.Process(child.pid))
            record['child_pid'] = child.pid
            (out / 'guard.json').write_text(json.dumps(record, indent=2))
            print(json.dumps({'started': child.pid, 'output': str(out)}), flush=True)
            while child.poll() is None:
                try:
                    st = status()
                    proc = psutil.Process(child.pid)
                    remember(proc)
                    rss = 0
                    for owned in owned_alive():
                        try:
                            rss += owned.memory_info().rss
                        except psutil.NoSuchProcess:
                            pass
                    peak_rss = max(peak_rss, rss)
                    telemetry = st['telemetry']
                    sample = {'elapsed_seconds': time.time() - start, 'rss_bytes': rss,
                              'telemetry': telemetry, 'active': st.get('active'),
                              'voice': st.get('voice')}
                    samples.write(json.dumps(sample) + '\n')
                    samples.flush()
                    if st.get('active') or st.get('voice'):
                        reason = 'controller-or-voice-preemption'
                    elif telemetry['pressure'] != 1 or not telemetry['thermal_ok']:
                        reason = 'memory-or-thermal-pressure'
                    elif telemetry['swapout_bytes'] - swap_base > 256 * 1024**2:
                        reason = 'swapout-growth-exceeded-256MiB'
                    elif telemetry['available_bytes'] < 1024**3:
                        reason = 'available-memory-below-1GiB'
                    elif rss > 14 * 1024**3:
                        reason = 'owned-RSS-above-14GiB'
                    elif telemetry['idle_seconds'] < 300:
                        reason = 'foreground-activity'
                    elif time.monotonic() - monotonic_start > args.seconds:
                        reason = 'runtime-deadline'
                    if reason:
                        break
                except psutil.NoSuchProcess:
                    if child.poll() is not None:
                        break
                except Exception as exc:
                    reason = 'monitor-failed:' + type(exc).__name__
                    break
                time.sleep(2)
    except BaseException as exc:
        reason = reason or 'controller-exception-' + type(exc).__name__
    finally:
        for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(signum, signal.SIG_IGN)
        if child:
            try:
                remember(psutil.Process(child.pid))
            except psutil.NoSuchProcess:
                pass
        remember_group()
        for sig, seconds in ((signal.SIGTERM, 12), (signal.SIGKILL, 8)):
            remember_group()
            live = owned_alive()
            if not live:
                break
            # Only signal the group while a previously observed owned identity
            # still belongs to it. Never target a reused PID or unrelated group.
            group_owned = False
            for proc in live:
                try:
                    group_owned |= child is not None and os.getpgid(proc.pid) == child.pid
                except ProcessLookupError:
                    pass
            if child and group_owned:
                try:
                    os.killpg(child.pid, sig)
                except ProcessLookupError:
                    pass
            for proc in owned_alive():
                try:
                    proc.send_signal(sig)
                except psutil.NoSuchProcess:
                    pass
            until = time.monotonic() + seconds
            while owned_alive() and time.monotonic() < until:
                if child:
                    child.poll()
                time.sleep(0.2)
        survivors = [{'pid': p.pid, 'created': p.create_time()} for p in owned_alive()]
        if child:
            child.poll()
        record.update(status='complete' if child and child.returncode == 0 and not reason and not survivors else 'failed',
                      reason=reason, exit_code=child.returncode if child else None,
                      elapsed_seconds=time.time() - start, peak_owned_rss_bytes=peak_rss,
                      owned_child_exited=child is None or child.poll() is not None,
                      owned_survivors=survivors, owned_processes_remaining=len(survivors))
        (out / 'guard.json').write_text(json.dumps(record, indent=2))
        print(json.dumps({k: record[k] for k in ('status', 'reason', 'exit_code', 'elapsed_seconds',
                                                'peak_owned_rss_bytes', 'owned_child_exited')}), flush=True)
    return 0 if record['status'] == 'complete' else 1


if __name__ == '__main__':
    sys.exit(main())
