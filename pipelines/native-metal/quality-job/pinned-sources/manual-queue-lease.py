"""Reserve the existing auxiliary BUSY gate for one guarded manual command.

Does not edit controller configuration. Crashes/SIGKILL deliberately leave BUSY
in place: inspect the recorded guard PID/receipt and the marker before recovery.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def controller_origin(value):
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != 'http' or parsed.hostname not in
            {'127.0.0.1', 'localhost'}
            or parsed.port != 18080 or parsed.username or parsed.password
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
        raise ValueError('Controller must be a known Mac/loopback origin on port 18080')
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, '', '', ''))


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        raise urllib.error.HTTPError(request.full_url, code, 'Controller redirect refused', headers, fp)


def require_idle(status, owned_busy=False):
    required = {'active', 'voice', 'busy', 'queue'}
    if not isinstance(status, dict) or not required.issubset(status):
        raise ValueError('Controller status has an unknown admission schema')
    if status['active'] is not None or status['voice'] != 0:
        raise ValueError('Existing controller or voice work owns the Mac')
    if status['busy'] is not owned_busy:
        raise ValueError('BUSY state is not the expected exclusive lease state')
    if not isinstance(status['queue'], list):
        raise ValueError('Controller queue has an unknown schema')
    terminal = {'completed', 'failed', 'cancelled', 'canceled', 'expired', 'interrupted'}
    if any(not isinstance(job, dict) or job.get('state') not in terminal for job in status['queue']):
        raise ValueError('Queued, running, or unrecognized controller jobs block manual admission')


def marker_identity(info):
    return (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)


class OwnedMarker:
    def __init__(self, path, payload):
        self.path = path
        self.raw = (json.dumps(payload, sort_keys=True) + '\n').encode()
        self.identity = None

    def acquire(self):
        # O_EXCL also refuses existing symlinks, including dangling ones.
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(self.raw)
            stream.flush()
            os.fsync(stream.fileno())
            self.identity = marker_identity(os.fstat(stream.fileno()))

    def unchanged(self):
        if self.identity is None:
            return False
        try:
            before = self.path.lstat()
            if not stat.S_ISREG(before.st_mode) or marker_identity(before) != self.identity:
                return False
            descriptor = os.open(self.path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(descriptor, 'rb') as stream:
                if marker_identity(os.fstat(stream.fileno())) != self.identity:
                    return False
                raw = stream.read(len(self.raw) + 1)
                if marker_identity(os.fstat(stream.fileno())) != self.identity:
                    return False
            return raw == self.raw and marker_identity(self.path.lstat()) == self.identity
        except (FileNotFoundError, OSError):
            return False

    def release(self):
        if not self.unchanged():
            return False
        # Recheck directly before unlink. The controller/user do not share an
        # atomic compare-and-unlink primitive; a concurrent edit at this exact
        # boundary remains a documented filesystem limitation.
        try:
            if marker_identity(self.path.lstat()) != self.identity:
                return False
            self.path.unlink()
        except OSError:
            return False
        return True


def verified_guard_cleanup(report_path, pid):
    try:
        report = json.loads(report_path.read_text())
    except (OSError, ValueError):
        return False
    return (report.get('controller_pid') == pid
            and report.get('status') in {'complete', 'failed'}
            and report.get('owned_child_exited') is True
            and report.get('owned_processes_remaining') == 0
            and report.get('owned_survivors') == [])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='Fresh lease lifecycle JSON')
    parser.add_argument('--guard-report', type=Path, required=True, help='Fresh guard output/guard.json')
    parser.add_argument('--controller', default='http://127.0.0.1:18080')
    parser.add_argument('--cleanup-seconds', type=int, default=45)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command and args.command[0] == '--' else args.command
    if not command or not 25 <= args.cleanup_seconds <= 60:
        raise ValueError('Supply the guard command and a cleanup allowance of 25..60 seconds')
    origin = controller_origin(args.controller)
    output = args.output.resolve()
    guard_report = args.guard_report.resolve()
    if guard_report.parent.exists() or output.is_relative_to(guard_report.parent):
        raise ValueError('Use a fresh guard output directory and keep the lease receipt outside it')
    # Bind the cleanup proof to the command actually launched, not an unrelated report.
    prefix = command[:command.index('--')] if '--' in command else command
    if not any(Path(part).name in {'guard_run.py', 'guard-model.py'} for part in prefix) or '--output' not in prefix:
        raise ValueError('Command must invoke a reviewed guard_run.py or guard-model.py with --output')
    index = prefix.index('--output')
    if index + 1 >= len(prefix) or Path(prefix[index + 1]).resolve() / 'guard.json' != guard_report:
        raise ValueError('guard-report must match the supplied guard --output directory')

    os.umask(0o077)
    output.parent.mkdir(parents=True, exist_ok=True)
    record = {'schema': 'golden-sun-manual-queue-lease-v1', 'created_utc': utc(),
              'wrapper_pid': os.getpid(), 'controller': origin, 'guard_command': command,
              'guard_report': str(guard_report), 'token': uuid.uuid4().hex,
              'status': 'preparing', 'events': [],
              'recovery_limits': [
                  'No crash reaper. SIGKILL, power loss, or uncertain guard cleanup preserves BUSY.',
                  'Before manual recovery verify the exact recorded guard/children have exited and marker ownership.',
                  'Ownership is checked by device/inode/mtime/size/content immediately before unlink; POSIX has no atomic conditional unlink.',
                  'The marker prevents queue admission; the existing guard remains responsible for resource limits and active/voice preemption.']}
    with output.open('x') as stream:
        json.dump(record, stream, indent=2)
        stream.write('\n')

    def event(kind, **details):
        record['events'].append({'utc': utc(), 'event': kind, **details})
        temporary = output.with_name(output.name + '.tmp-' + uuid.uuid4().hex)
        with temporary.open('x') as stream:
            json.dump(record, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(output)
        print(json.dumps({'event': kind, **details}), flush=True)

    root = Path(os.environ.get('MAC_AUX_ROOT', str(Path.home() / 'auxiliary')))
    config = json.loads((root / 'controller.json').read_text())
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirects())

    def status():
        request = urllib.request.Request(origin + '/status',
                                         headers={'Authorization': 'Bearer ' + config['api_key']})
        with opener.open(request, timeout=8) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError('Controller status exceeds bounded response size')
        return json.loads(raw)

    interrupted = []

    def on_signal(signum, frame):
        interrupted.append(signum)

    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, on_signal)
    marker = OwnedMarker(root / 'BUSY', {'lease': record['token'], 'pid': os.getpid(),
                                        'created_utc': record['created_utc'], 'receipt': str(output)})
    child = None
    acquired = False
    clean = False
    success = False
    lease_lost = False
    try:
        admission = status()
        require_idle(admission)
        record['admission'] = admission
        if interrupted:
            raise InterruptedError('Signal before lease acquisition')
        marker.acquire()
        acquired = True
        record['status'] = 'reserved'
        record['marker'] = {'path': str(marker.path), 'identity': list(marker.identity),
                            'sha256': hashlib.sha256(marker.raw).hexdigest()}
        event('lease_acquired')
        # Recheck after the dispatcher can observe the marker, then let the
        # guard perform its independent final resource/admission check.
        time.sleep(1)
        confirmed = status()
        require_idle(confirmed, owned_busy=True)
        record['reservation_confirmation'] = confirmed
        if interrupted or not marker.unchanged():
            raise InterruptedError('Signal or changed marker before launch')
        child = subprocess.Popen(command, start_new_session=True)
        record['guard_pid'] = child.pid
        record['status'] = 'running'
        event('guard_started', pid=child.pid)
        while child.poll() is None and not interrupted:
            if not marker.unchanged():
                lease_lost = True
                record['error'] = {'type': 'LeaseLost', 'message': 'BUSY marker changed or disappeared during guard execution'}
                event('lease_lost')
                break
            time.sleep(0.25)
        if interrupted:
            event('signal_received', signal=interrupted[0])
        elif not lease_lost:
            success = child.returncode == 0
    except BaseException as error:
        record['error'] = {'type': type(error).__name__, 'message': str(error)}
        event('lease_exception', error_type=type(error).__name__)
    finally:
        if child is not None:
            if child.poll() is None:
                try:
                    child.terminate()  # The guard owns and cleans its own descendants.
                except ProcessLookupError:
                    pass  # It exited between poll() and terminate().
                event('guard_termination_requested', pid=child.pid)
                try:
                    child.wait(timeout=args.cleanup_seconds)
                except subprocess.TimeoutExpired:
                    event('guard_cleanup_timeout', pid=child.pid)
            clean = child.poll() is not None and verified_guard_cleanup(guard_report, child.pid)
            record['guard_exit_code'] = child.poll()
            record['guard_cleanup_verified'] = clean
        else:
            clean = True  # No subprocess was started by this wrapper.
        released = False
        if acquired and clean:
            released = marker.release()
        record['marker_released'] = released
        record['status'] = ('complete' if success and clean and released else
                            'failed-marker-preserved' if acquired and not released else 'failed')
        event('lease_finished', status=record['status'], marker_released=released,
              guard_cleanup_verified=clean, guard_pid=child.pid if child else None)
    return 0 if record['status'] == 'complete' else 1


if __name__ == '__main__':
    sys.exit(main())
