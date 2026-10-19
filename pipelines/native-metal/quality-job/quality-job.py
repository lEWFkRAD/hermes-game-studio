"""Package, inspect, or explicitly dispatch one pinned native Metal quality job.

prepare/status are local and never contact the Mac. launch must be invoked from
the prepared source snapshot on Apple Silicon. Any launch intent is permanent:
an ambiguous dispatch is report-only, with no automatic retry or crash reaper.
"""
from __future__ import annotations
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import struct
import subprocess
import sys
import uuid
import zlib

HERE = Path(__file__).resolve().parent
PROFILE_SHA256 = 'b5f36a1b41e7ec335f4c61f95200e830b82a3aaa560878877df92e970a7d3a27'
MAX_PNG_BYTES = 16 * 1024 * 1024


def utc():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read_json(path):
    if not Path(path).is_file():
        raise ValueError('JSON evidence must be a regular file')
    with Path(path).open('rb') as stream:
        raw = stream.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('JSON evidence exceeds 2 MiB')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('JSON evidence must contain an object')
    return value


def read_receipt(run, relative):
    path = Path(run)
    for part in Path(relative).parts:
        path = path / part
        if path.is_symlink():
            raise ValueError('Execution receipts must not be symlinks')
    return read_json(path)


def exclusive_bytes(path, raw, readonly=False):
    with Path(path).open('xb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    if readonly:
        Path(path).chmod(0o444)
    if os.name == 'posix':
        descriptor = os.open(Path(path).parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def exclusive_json(path, value, readonly=False):
    exclusive_bytes(path, (json.dumps(value, indent=2) + '\n').encode(), readonly)


def load_profile():
    path = HERE / 'profile.json'
    if sha(path) != PROFILE_SHA256:
        raise ValueError('Pinned profile changed')
    return read_json(path)


def run_name(value):
    if not re.fullmatch(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*', value) or len(value) > 64:
        raise ValueError('Run name must be 1..64 lowercase letters/digits with internal hyphens')
    if value in {'con', 'prn', 'aux', 'nul'} or re.fullmatch(r'(com|lpt)[1-9]', value):
        raise ValueError('Reserved run name')
    return value


def mac_path(value):
    path = PurePosixPath(value)
    if (not path.is_absolute() or '..' in path.parts or str(path) != value
            or any(ord(char) < 32 for char in value) or value == '/'):
        raise ValueError('Use a canonical absolute Mac path')
    return str(path)


def validate_png(raw):
    """Bounded PNG integrity and decompression check; never changes image pixels."""
    if len(raw) > MAX_PNG_BYTES or not raw.startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError('Expected PNG up to 16 MiB')
    offset, header, idat, ended = 8, None, bytearray(), False
    seen_idat, left_idat = False, False
    while offset < len(raw):
        if offset + 12 > len(raw):
            raise ValueError('Truncated PNG chunk')
        length, kind = struct.unpack_from('>I4s', raw, offset)
        end = offset + 12 + length
        if end > len(raw):
            raise ValueError('Truncated PNG data')
        payload = raw[offset + 8:offset + 8 + length]
        expected = struct.unpack_from('>I', raw, offset + 8 + length)[0]
        if zlib.crc32(kind + payload) & 0xffffffff != expected:
            raise ValueError('PNG CRC mismatch')
        if header is None:
            if kind != b'IHDR' or length != 13:
                raise ValueError('PNG must begin with IHDR')
            width, height, depth, color, compression, filtering, interlace = struct.unpack('>IIBBBBB', payload)
            if not (1 <= width <= 2048 and 1 <= height <= 2048 and depth == 8
                    and color in (2, 6) and compression == filtering == interlace == 0):
                raise ValueError('Use noninterlaced 8-bit RGB/RGBA PNG, at most 2048x2048')
            header = {'width': width, 'height': height, 'channels': 4 if color == 6 else 3,
                      'alphaChannel': color == 6, 'pixelsModified': False}
        elif kind == b'IHDR':
            raise ValueError('Duplicate PNG header')
        elif kind == b'IDAT':
            if left_idat:
                raise ValueError('Noncontiguous PNG image chunks')
            seen_idat = True
            idat.extend(payload)
        elif kind == b'IEND':
            if length or end != len(raw) or not seen_idat:
                raise ValueError('Invalid PNG end')
            ended = True
        elif kind in (b'acTL', b'fcTL', b'fdAT') or not kind[0] & 32:
            raise ValueError('Animated or unsupported critical PNG chunk')
        if seen_idat and kind != b'IDAT':
            left_idat = True
        offset = end
    if not ended:
        raise ValueError('Missing PNG end')
    stride = header['width'] * header['channels'] + 1
    expected_bytes = stride * header['height']
    decoder = zlib.decompressobj()
    pixels = decoder.decompress(bytes(idat), expected_bytes + 1)
    if (len(pixels) != expected_bytes or not decoder.eof or decoder.unused_data
            or decoder.unconsumed_tail or any(pixels[i] > 4 for i in range(0, len(pixels), stride))):
        raise ValueError('Invalid or oversized PNG image stream')
    return header


def commands(target, seed):
    run = PurePosixPath(target['runDir'])
    source = run / 'source'
    cli = [target['cli'], str(run / 'input.png'), str(run / 'unused.glb'),
           '--models', target['models'], '--require-gpu', '--threads', '4',
           '--seed', str(seed), '--steps', '12', '--res', '1024', '--tex-res', '1024',
           '--dump-bg', '--dump-post', str(run / 'cleaned-mesh-pbr.bin'), '--verbose']
    runner = [target['python'], str(source / 'mac_runner.py'), json.dumps(cli)]
    guard = [target['python'], str(source / 'guard-model.py'), '--output',
             str(run / 'generation-guard'), '--seconds', '1800', '--', *runner]
    lease = [target['python'], str(source / 'manual-queue-lease.py'), '--output',
             str(run / 'lease.json'), '--guard-report', str(run / 'generation-guard/guard.json'), '--', *guard]
    return {'cli': cli, 'runner': runner, 'guard': guard, 'lease': lease}


def prepare(out, name, image, input_sha, seed, target_overrides=None):
    out = Path(out).absolute()
    if out.exists() or out.is_symlink():
        return status(out)  # Existing, even partial, is always report-only.
    name = run_name(name)
    if not re.fullmatch('[0-9a-f]{64}', input_sha):
        raise ValueError('Supply the exact lowercase PNG SHA256')
    if type(seed) is not int or not 0 <= seed <= 2147483647:
        raise ValueError('Seed must be an explicit integer in [0,2147483647]')
    profile = load_profile()
    target = dict(profile['defaults'])
    target.update(target_overrides or {})
    if set(target) != set(profile['defaults']):
        raise ValueError('Unknown target path setting')
    target = {key: mac_path(value) for key, value in target.items()}
    target['runDir'] = str(PurePosixPath(target.pop('macRoot')) / name)
    image = Path(image).resolve(strict=True)
    if image.stat().st_size > MAX_PNG_BYTES:
        raise ValueError('PNG exceeds 16 MiB')
    raw = image.read_bytes()
    if hashlib.sha256(raw).hexdigest() != input_sha:
        raise ValueError('Input SHA256 mismatch')
    dimensions = validate_png(raw)
    pinned = {}
    for filename, expected in profile['sources'].items():
        path = HERE / 'pinned-sources' / filename
        if path.stat().st_size != expected['bytes'] or sha(path) != expected['sha256']:
            raise ValueError('Pinned source mismatch: ' + filename)
        pinned[filename] = path.read_bytes()
    try:
        out.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        return status(out)
    (out / 'source').mkdir()
    (out / 'scratch').mkdir()
    exclusive_bytes(out / 'input.png', raw, True)
    snapshots = {**pinned, 'quality-job.py': Path(__file__).read_bytes(), 'profile.json': (HERE / 'profile.json').read_bytes()}
    rows = []
    for filename, data in snapshots.items():
        exclusive_bytes(out / 'source' / filename, data, True)
        rows.append({'path': 'source/' + filename, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()})
    plan = {'schema': 'mac-quality-job-plan-v1', 'createdAt': utc(), 'runName': name,
            'profileId': profile['id'], 'profileSha256': PROFILE_SHA256, 'seed': seed,
            'input': {'originalPath': str(image), 'path': 'input.png', 'sha256': input_sha, 'bytes': len(raw), **dimensions},
            'target': target, 'commands': commands(target, seed), 'sourceSnapshots': rows,
            'outputContract': 'Native cleaned geometry/PBR dump. Separate inspection, qualified post-replay, editable preparation and artistic review remain required.',
            'launchPolicy': 'Exactly one permanent launch intent. Existing attempts and partial preparation are report-only; never retry or resume automatically.'}
    exclusive_json(out / 'plan.json', plan, True)
    marker = {'schema': 'mac-quality-job-prepared-v1', 'planSha256': sha(out / 'plan.json'),
              'profileSha256': PROFILE_SHA256, 'createdAt': utc(), 'status': 'prepared'}
    exclusive_json(out / 'prepared.json', marker, True)
    return status(out)


def verify_package(run, expected_plan_sha=None):
    run = Path(run).resolve(strict=True)
    if any((run / name).is_symlink() for name in ('plan.json', 'prepared.json', 'input.png', 'source')):
        raise ValueError('Prepared package files must not be symlinks')
    marker = read_json(run / 'prepared.json')
    plan = read_json(run / 'plan.json')
    actual = sha(run / 'plan.json')
    if actual != marker.get('planSha256') or (expected_plan_sha and actual != expected_plan_sha):
        raise ValueError('Prepared plan hash mismatch')
    if (plan.get('schema') != 'mac-quality-job-plan-v1' or plan.get('profileSha256') != PROFILE_SHA256
            or marker.get('profileSha256') != PROFILE_SHA256):
        raise ValueError('Unknown prepared profile')
    expected_sources = {'source/quality-job.py', 'source/profile.json', 'source/guard-model.py',
                        'source/manual-queue-lease.py', 'source/mac_runner.py'}
    rows = plan['sourceSnapshots']
    if len(rows) != len(expected_sources) or {r['path'] for r in rows} != expected_sources:
        raise ValueError('Snapshot source set changed')
    for row in [*rows, plan['input']]:
        path = run / row['path']
        if path.is_symlink() or not path.is_file() or path.stat().st_size != row['bytes'] or sha(path) != row['sha256']:
            raise ValueError('Immutable package binding changed: ' + row['path'])
    if plan['input']['path'] != 'input.png' or sha(run / 'source/profile.json') != PROFILE_SHA256:
        raise ValueError('Input or profile binding changed')
    profile = read_json(run / 'source/profile.json')
    for name, row in profile['sources'].items():
        if sha(run / 'source' / name) != row['sha256']:
            raise ValueError('Qualified source changed: ' + name)
    if plan['commands'] != commands(plan['target'], plan['seed']):
        raise ValueError('Command differs from the pinned quality profile')
    return plan, marker, profile


def status(run):
    run = Path(run).absolute()
    result = {'schema': 'mac-quality-job-status-v1', 'runDir': str(run), 'readOnly': True,
              'networkContacted': False, 'automaticRetryAllowed': False, 'processLivenessChecked': False}
    if not run.exists():
        return {**result, 'status': 'absent', 'launchAllowed': False}
    try:
        plan, marker, profile = verify_package(run)
    except (OSError, ValueError, KeyError, TypeError) as error:
        return {**result, 'status': 'incomplete-or-invalid-package-report-only', 'launchAllowed': False, 'error': str(error)}
    result.update(runName=plan['runName'], planSha256=marker['planSha256'], inputSha256=plan['input']['sha256'])
    attempt = run / 'launch-intent.json'
    residues = any((run / name).exists() or (run / name).is_symlink() for name in
                   ('launch.json', 'lease.json', 'generation-guard', 'controller.log', 'cleaned-mesh-pbr.bin'))
    if not attempt.exists() and not attempt.is_symlink() and not residues:
        return {**result, 'status': 'prepared', 'launchAllowed': True,
                'launchCommand': [plan['target']['python'], str(PurePosixPath(plan['target']['runDir']) / 'source/quality-job.py'),
                                  'launch', '--run-dir', plan['target']['runDir'], '--plan-sha256', marker['planSha256']]}
    result.update(status='dispatch-uncertain-report-only', launchAllowed=False)
    try:
        if any((run / name).is_symlink() for name in
               ('launch-intent.json', 'launch.json', 'lease.json', 'generation-guard', 'generation-guard/guard.json')):
            raise ValueError('Execution receipts must not be symlinks')
        if not attempt.is_file() or attempt.is_symlink():
            raise ValueError('Execution residue without an owned launch intent')
        intent = read_receipt(run, 'launch-intent.json')
        if intent.get('planSha256') != marker['planSha256']:
            raise ValueError('Launch intent binds another plan')
        result['launchIntentRecorded'] = True
        if (run / 'launch.json').exists():
            dispatch = read_receipt(run, 'launch.json')
            if dispatch.get('planSha256') != marker['planSha256']:
                raise ValueError('Dispatch receipt binds another plan')
            if dispatch.get('status') not in {'dispatched', 'preflight-failed-no-process', 'dispatch-uncertain'}:
                raise ValueError('Unknown dispatch receipt status')
            # Never echo arbitrary receipt objects or error strings into status.
            result['launch'] = {'status': dispatch['status']}
            if type(dispatch.get('pid')) is int and dispatch['pid'] > 0:
                result['launch']['pid'] = dispatch['pid']
            if dispatch['status'] == 'dispatched':
                result['status'] = 'dispatch-recorded-awaiting-lease'
            elif dispatch['status'] == 'preflight-failed-no-process':
                result['status'] = 'preflight-failed-report-only'
        if not (run / 'lease.json').exists():
            return result
        lease = read_receipt(run, 'lease.json')
        lease_status = lease.get('status')
        if lease_status not in {'preparing', 'reserved', 'running', 'complete', 'failed', 'failed-marker-preserved'}:
            raise ValueError('Unknown lease receipt status')
        result['leaseReportedStatus'] = lease_status
        if lease.get('guard_command') != plan['commands']['guard']:
            raise ValueError('Lease command does not bind this snapshot')
        guard_path = run / 'generation-guard/guard.json'
        guard = read_receipt(run, 'generation-guard/guard.json') if guard_path.exists() else {}
        if guard.get('status') not in {None, 'starting', 'running', 'complete', 'failed'}:
            raise ValueError('Unknown guard receipt status')
        clean = (guard.get('controller_pid') == lease.get('guard_pid') and type(lease.get('guard_pid')) is int
                 and guard.get('status') in {'complete', 'failed'} and guard.get('owned_child_exited') is True
                 and guard.get('owned_processes_remaining') == 0 and guard.get('owned_survivors') == []
                 and guard.get('command') == plan['commands']['runner'])
        result.update(guardReportedStatus=guard.get('status'), guardCleanupVerified=clean,
                      markerReleased=lease.get('marker_released') is True)
        if (lease.get('status') == 'complete' and lease.get('guard_cleanup_verified') is True
                and lease.get('guard_exit_code') == 0 and result['markerReleased'] and clean
                and guard.get('status') == 'complete' and guard.get('exit_code') == 0):
            dump = run / 'cleaned-mesh-pbr.bin'
            if not dump.is_file() or dump.is_symlink() or dump.stat().st_size == 0:
                raise ValueError('Successful process receipts but expected dump is missing')
            result.update(status='generation-complete-awaiting-dump-and-art-review',
                          dump={'bytes': dump.stat().st_size, 'validated': False}, acceptedForGame=False)
        elif lease.get('status') in {'failed', 'failed-marker-preserved'}:
            result['status'] = 'failed-report-only'
        elif lease.get('status') == 'complete':
            raise ValueError('Complete lease lacks matching successful process and cleanup proof')
        else:
            result['status'] = 'lease-' + str(lease.get('status', 'unknown')) + '-reported'
    except (OSError, ValueError, KeyError, TypeError) as error:
        result.update(status='receipt-inconsistent-report-only', error=str(error))
    return result


def claim_launch(run, plan_sha):
    """Permanent O_EXCL claim survives uncertain Popen/receipt outcomes."""
    intent = {'schema': 'mac-quality-job-launch-intent-v1', 'createdAt': utc(),
              'token': uuid.uuid4().hex, 'planSha256': plan_sha, 'status': 'dispatch-not-yet-confirmed'}
    try:
        exclusive_json(Path(run) / 'launch-intent.json', intent, True)
    except FileExistsError:
        return None
    return intent


def clean_environment(source=None):
    return {key: value for key, value in (os.environ if source is None else source).items()
            if key not in {'PYTHONPATH', 'PYTHONHOME'} and not key.startswith(('DYLD_', 'GGML_'))}


def otool_dependencies(text):
    return [match.group(1) for line in text.splitlines()
            if (match := re.match(r'^\s+(.+?) \(compatibility version [^\n]+\)$', line))]


def otool_rpaths(text):
    return re.findall(r'\bcmd LC_RPATH\s+cmdsize \d+\s+path (.+?) \(offset \d+\)', text)


def expand_loader_path(value, owner, binary, cwd):
    for prefix, parent in [('@loader_path', owner.parent), ('@executable_path', binary.parent)]:
        if value == prefix or value.startswith(prefix + '/'):
            return parent / value[len(prefix):].lstrip('/')
    path = Path(value)
    if value.startswith('@'):
        raise ValueError('Unsupported dynamic loader path: ' + value)
    return path if path.is_absolute() else cwd / path


def resolve_dependency(reference, owner, binary, rpaths, cwd):
    if reference.startswith(('/usr/lib/', '/System/Library/')):
        return None  # Apple's shared cache need not expose an on-disk file.
    if reference.startswith('@rpath/'):
        candidates = [path / reference[7:] for path in rpaths]
    else:
        candidates = [expand_loader_path(reference, owner, binary, cwd)]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ValueError('Cannot resolve qualified dynamic dependency: ' + reference)


def verify_libraries(binary, profile, cwd, env):
    """Verify versioned bytes and each actually resolvable install-name alias.

    Only the known five ggml libraries and Apple system dependencies are allowed.
    The native binary stays in its qualified build directory to retain its rpaths.
    """
    directory = binary.parent.resolve()
    expected_by_hash = {row['sha256']: row['name'] for row in profile['runtimeLibraries']}
    verified = []
    for row in profile['runtimeLibraries']:
        path = directory / row['name']
        if sha(path) != row['sha256']:
            raise ValueError('Pinned runtime library mismatch: ' + row['name'])
        verified.append({**row, 'path': str(path), 'resolvedPath': str(path.resolve())})
    visited, links = set(), []

    def walk(owner, inherited):
        owner = owner.resolve()
        if owner in visited:
            return
        visited.add(owner)
        listed = subprocess.run(['/usr/bin/otool', '-L', str(owner)], check=True, capture_output=True,
                                text=True, timeout=15, env=env).stdout
        loaded = subprocess.run(['/usr/bin/otool', '-l', str(owner)], check=True, capture_output=True,
                                text=True, timeout=15, env=env).stdout
        references = otool_dependencies(listed)
        if not references:
            raise ValueError('No Mach-O dependency data: ' + str(owner))
        rpaths = [expand_loader_path(value, owner, binary, cwd) for value in otool_rpaths(loaded)] + inherited
        for reference in references:
            actual = resolve_dependency(reference, owner, binary, rpaths, cwd)
            if actual is None:
                continue
            resolved = actual.resolve()
            digest = sha(actual)
            if resolved.parent != directory or digest not in expected_by_hash:
                raise ValueError('Unqualified dynamic dependency: ' + str(actual))
            links.append({'owner': str(owner), 'installName': reference, 'path': str(actual),
                          'resolvedPath': str(resolved), 'sha256': digest,
                          'qualifiedName': expected_by_hash[digest]})
            walk(actual, rpaths)

    walk(binary, [])
    if {row['sha256'] for row in links} != set(expected_by_hash):
        raise ValueError('Mach-O dependency closure does not include the five qualified ggml libraries')
    return {'verifiedLibraries': verified, 'resolvedDependencies': links,
            'metalShaderPayload': 'Embedded in the pinned libggml-metal dylib; qualified run logs confirm this.'}


def verify_runtime(plan, profile):
    target = plan['target']
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise ValueError('Generation launch requires the qualified Apple Silicon runtime')
    if Path(sys.executable).resolve() != Path(target['python']).resolve():
        raise ValueError('Use the explicitly recorded Python environment')
    repo = Path(target['repo'])
    env = clean_environment()
    commit = subprocess.run(['/usr/bin/git', '-C', str(repo), 'rev-parse', 'HEAD'], check=True,
                            capture_output=True, text=True, timeout=15, env=env).stdout.strip()
    if commit != profile['sourceCommit']:
        raise ValueError('Native source commit differs from the qualified build')
    subprocess.run(['/usr/bin/git', '-C', str(repo), 'diff', '--quiet', 'HEAD', '--'],
                   check=True, timeout=15, env=env)
    binary = Path(target['cli'])
    if sha(binary) != profile['cliSha256'] or not os.access(binary, os.X_OK):
        raise ValueError('Native CLI SHA256/executable check failed')
    weight_rows = []
    for expected in profile['weights']:
        path = Path(target['models']) / expected['name']
        # Existing profile symlinks are intentional. Hash their actual bytes;
        # do not replace, download, or mutate the shared weight installation.
        if path.stat().st_size != expected['bytes'] or sha(path) != expected['sha256']:
            raise ValueError('Pinned weight mismatch: ' + expected['name'])
        weight_rows.append({**expected, 'resolvedPath': str(path.resolve())})
    if sha(binary) != profile['cliSha256']:
        raise ValueError('Native CLI changed during weight verification')
    libraries = verify_libraries(binary, profile, Path(target['runDir']), env)
    return {'sourceCommit': commit, 'cliSha256': profile['cliSha256'], 'verifiedWeights': weight_rows,
            **libraries, 'loaderEnvironmentOverridesRemoved': True}


def launch(run, expected_plan_sha):
    run = Path(run).resolve(strict=True)
    current = status(run)
    if not current.get('launchAllowed'):
        return current
    plan, marker, profile = verify_package(run, expected_plan_sha)
    if str(run) != plan['target']['runDir'] or Path(__file__).resolve() != run / 'source/quality-job.py':
        raise ValueError('Launch only from the prepared snapshot at its explicit target run directory')
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise ValueError('No launch on this host; prepare/status are the local-only actions')
    if claim_launch(run, marker['planSha256']) is None:
        return status(run)
    attempted, process = False, None
    record = {'schema': 'mac-quality-job-dispatch-v1', 'createdAt': utc(), 'planSha256': marker['planSha256']}
    try:
        record['runtimeVerification'] = verify_runtime(plan, profile)
        verify_package(run, expected_plan_sha)
        env = clean_environment()
        env.update(TMPDIR=str(run / 'scratch'), OMP_NUM_THREADS='4', OPENBLAS_NUM_THREADS='4',
                   VECLIB_MAXIMUM_THREADS='4', PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1')
        with (run / 'controller.log').open('xb') as log:
            attempted = True
            process = subprocess.Popen(plan['commands']['lease'], stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=subprocess.STDOUT, cwd=run,
                                       start_new_session=True, env=env)
        record.update(status='dispatched', pid=process.pid, command=plan['commands']['lease'])
    except BaseException as error:
        record.update(status='dispatch-uncertain' if attempted else 'preflight-failed-no-process',
                      error={'type': type(error).__name__, 'message': str(error)})
    # If this write fails, launch-intent remains the durable no-retry boundary.
    exclusive_json(run / 'launch.json', record, True)
    return status(run)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    prepare_parser = sub.add_parser('prepare', help='Local immutable package; no host contact or generation')
    for flag in ('out-dir', 'run-name', 'image', 'input-sha256'):
        prepare_parser.add_argument('--' + flag, required=True)
    prepare_parser.add_argument('--seed', required=True, type=int)
    for flag in ('mac-root', 'python', 'repo', 'cli', 'models'):
        prepare_parser.add_argument('--' + flag)
    status_parser = sub.add_parser('status', help='Read local receipts only; no PID probing or network')
    status_parser.add_argument('--run-dir', required=True)
    launch_parser = sub.add_parser('launch', help='One explicit Mac dispatch from its source snapshot')
    launch_parser.add_argument('--run-dir', required=True)
    launch_parser.add_argument('--plan-sha256', required=True)
    args = parser.parse_args()
    os.umask(0o077)
    if args.action == 'prepare':
        mapping = {'mac_root': 'macRoot', 'python': 'python', 'repo': 'repo', 'cli': 'cli', 'models': 'models'}
        overrides = {target: getattr(args, key) for key, target in mapping.items() if getattr(args, key) is not None}
        result = prepare(args.out_dir, args.run_name, args.image, args.input_sha256, args.seed, overrides)
    elif args.action == 'status':
        result = status(args.run_dir)
    else:
        result = launch(args.run_dir, args.plan_sha256)
    print(json.dumps(result, indent=2))
    ok = {'prepared', 'generation-complete-awaiting-dump-and-art-review', 'dispatch-recorded-awaiting-lease',
          'lease-running-reported', 'lease-reserved-reported', 'lease-preparing-reported'}
    return 0 if args.action == 'status' or result.get('status') in ok else 1


if __name__ == '__main__':
    sys.exit(main())
