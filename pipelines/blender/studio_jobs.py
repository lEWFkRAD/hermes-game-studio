"""File-specific Blender reviews with persistent receipts and explicit recovery.

No existing Blender process or MCP addon is contacted. Every review opens its
explicit source in a disposable background Blender, with auto-execution disabled.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

HERE = Path(__file__).resolve().parent


def save_json(path, value):
    temporary = path.with_suffix('.next')
    temporary.write_text(json.dumps(value, indent=2), encoding='utf-8')
    temporary.replace(path)


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def process_identity(pid):
    """Read process start identity; never signal or terminate an unowned PID."""
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        handle = kernel.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return None if ctypes.get_last_error() == 87 else 'unknown'
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return 'unknown'
            if code.value != 259:
                return None
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(handle, *(ctypes.byref(item) for item in times)):
                return 'unknown'
            return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
        finally:
            kernel.CloseHandle(handle)
    proc = Path('/proc') / str(pid) / 'stat'
    if Path('/proc').is_dir():
        try:
            fields = proc.read_text().rsplit(') ', 1)[1].split()
            return None if fields[0] == 'Z' else fields[19]
        except FileNotFoundError:
            return None
        except (OSError, IndexError):
            return 'unknown'
    result = subprocess.run(['ps', '-p', str(int(pid)), '-o', 'lstart='], capture_output=True, text=True)
    return result.stdout.strip() or None


def owner_state(path):
    if not path.is_file():
        return 'missing'
    owner = json.loads(path.read_text(encoding='utf-8'))
    current = process_identity(owner['pid'])
    if current == 'unknown' or owner.get('identity') == 'unknown':
        return 'unknown'
    return 'alive' if current is not None and current == owner.get('identity') else 'dead'


class StudioJobs:
    def __init__(self, art_root, blender):
        self.art_root = Path(art_root).resolve(strict=True)
        self.blender = Path(blender).resolve(strict=True)
        self.jobs = self.art_root / 'review-jobs'
        self.jobs.mkdir(exist_ok=True)

    def job_dir(self, job_id):
        if not re.fullmatch(r'[a-f0-9]{32}', job_id):
            raise ValueError('Invalid review job id.')
        return self.jobs / job_id

    def submit(self, source_path, samples=32, size=1024, audit_only=False, wait=True, resume_of=None, job_id=None):
        source = Path(source_path).resolve(strict=True)
        if not source.is_relative_to(self.art_root) or source.suffix.lower() != '.blend':
            raise ValueError('Choose a .blend inside the configured art directory.')
        if not isinstance(samples, int) or not 1 <= samples <= 256:
            raise ValueError('Samples must be an integer from 1 to 256.')
        if not isinstance(size, int) or not 128 <= size <= 2048:
            raise ValueError('Preview size must be an integer from 128 to 2048.')
        job_id = job_id or uuid.uuid4().hex
        output = self.job_dir(job_id)
        output.mkdir()
        job = dict(id=job_id, status='queued', source=str(source),
                   source_sha256=digest(source), blender=str(self.blender),
                   created_at=datetime.now(timezone.utc).isoformat(),
                   samples=samples, size=size, audit_only=bool(audit_only), resume_of=resume_of)
        save_json(output / 'job.json', job)
        kwargs = {'stdin': subprocess.DEVNULL}
        if os.name == 'nt':
            kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
        else:
            kwargs['start_new_session'] = True
        with (output / 'runner.log').open('ab') as log:
            process = subprocess.Popen([sys.executable, str(HERE / 'studio_jobs.py'), '--run', str(output)],
                                       stdout=log, stderr=log, **kwargs)
        save_json(output / 'runner-owner.json', {'pid': process.pid, 'identity': process_identity(process.pid)})
        if wait:
            process.wait()
            return self.status(job_id)
        return {'job_id': job_id, 'status': 'queued', 'runner_pid': process.pid,
                'source': str(source), 'source_sha256': job['source_sha256']}

    def status(self, job_id):
        output = self.job_dir(job_id)
        job = json.loads((output / 'job.json').read_text(encoding='utf-8'))
        if job['status'] in ('queued', 'running'):
            state = owner_state(output / 'runner-owner.json')
            age = time.time() - datetime.fromisoformat(job['created_at']).timestamp()
            if state == 'dead' or (state == 'missing' and age > 10):
                child = owner_state(output / 'blender-owner.json')
                job['status'] = 'interrupted_child_running' if child in ('alive', 'unknown') else 'interrupted'
                job['error'] = 'Review runner exited before its completion receipt. No automatic retry was started.'
                # Observational status: do not race a worker writing its receipt.
                job['can_resume'] = job['status'] == 'interrupted'
        job['artifacts'] = {name: str(output / name) for name in ['audit.json', 'preview.png', 'blender.log']
                            if (output / name).is_file()}
        audit = output / 'audit.json'
        if audit.is_file():
            job['audit'] = json.loads(audit.read_text(encoding='utf-8'))
        job['job_id'] = job_id
        return job

    def resume(self, job_id, wait=True):
        prior = self.status(job_id)
        previous_output = self.job_dir(job_id)
        if owner_state(previous_output / 'blender-owner.json') in ('alive', 'unknown'):
            raise ValueError('The prior review still has a live or unverified Blender child. Wait for it to exit.')
        if prior['status'] not in ('interrupted', 'failed'):
            raise ValueError('Only an interrupted or failed review with no live child can be resumed.')
        if digest(Path(prior['source'])) != prior['source_sha256']:
            raise ValueError('Source changed. Submit the new source revision instead of resuming this job.')
        # One replacement per receipt, even when two MCP clients race to resume.
        link = previous_output / 'resume-link.json'
        new_id = uuid.uuid4().hex
        try:
            with link.open('x', encoding='utf-8') as claim:
                json.dump({'job_id': new_id}, claim)
        except FileExistsError:
            try:
                linked = json.loads(link.read_text(encoding='utf-8'))['job_id']
            except json.JSONDecodeError:
                # Another client has just created its exclusive claim and is
                # still writing it. Never race it by launching another job.
                return {'status': 'resume_starting', 'resume_of': job_id}
            if (self.job_dir(linked) / 'job.json').is_file():
                return self.status(linked)
            return {'job_id': linked, 'status': 'resume_starting', 'resume_of': job_id}
        try:
            return self.submit(prior['source'], prior['samples'], prior['size'], prior['audit_only'], wait, job_id, new_id)
        except Exception:
            if not (self.job_dir(new_id) / 'job.json').is_file():
                link.unlink()
            raise


def run_job(output):
    output = Path(output).resolve(strict=True)
    receipt = output / 'job.json'
    job = json.loads(receipt.read_text(encoding='utf-8'))
    job.update(status='running', runner_pid=os.getpid())
    save_json(receipt, job)
    try:
        source = Path(job['source'])
        if digest(source) != job['source_sha256']:
            raise RuntimeError('Source changed before Blender started; submit the current revision again.')
        command = [job['blender'], '--factory-startup', '--disable-autoexec', '--background', str(source),
                   '--python-exit-code', '1', '--python', str(HERE / 'blender_review_worker.py'), '--',
                   '--output', str(output), '--samples', str(job['samples']), '--size', str(job['size'])]
        if job['audit_only']:
            command.append('--audit-only')
        kwargs = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        with (output / 'blender.log').open('wb') as log:
            child = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=log, **kwargs)
            save_json(output / 'blender-owner.json', {'pid': child.pid, 'identity': process_identity(child.pid)})
            try:
                code = child.wait(timeout=1800)
            except subprocess.TimeoutExpired:
                child.kill()  # This Popen owns this exact process; no PID search.
                child.wait()
                raise RuntimeError('Review exceeded its 30-minute CPU budget.')
        job['exit_code'] = code
        if code:
            raise RuntimeError('Blender failed; inspect blender.log.')
        if not (output / 'audit.json').is_file() or (not job['audit_only'] and not (output / 'preview.png').is_file()):
            raise RuntimeError('Blender exited without the required review artifacts.')
        job['source_unchanged'] = digest(source) == job['source_sha256']
        job['status'] = 'complete' if job['source_unchanged'] else 'source_changed'
    except Exception as error:
        job.update(status='failed', error=str(error))
        job['source_unchanged'] = Path(job['source']).is_file() and digest(Path(job['source'])) == job['source_sha256']
    job['finished_at'] = datetime.now(timezone.utc).isoformat()
    save_json(receipt, job)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--run', required=True)
    run_job(parser.parse_args().run)
