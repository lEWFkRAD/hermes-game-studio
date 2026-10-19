"""Build only the project-private replay translation unit; never build live targets."""
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import time



def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work-dir',required=True,type=Path)
    parser.add_argument('--repo',required=True,type=Path)
    parser.add_argument('--ninja',default='ninja')
    args=parser.parse_args()
    ROOT=args.work_dir.resolve()
    REPO=args.repo.resolve()
    BUILD=REPO/'build'
    NINJA=args.ninja
    SOURCE=ROOT/'post-replay-quality.cpp'
    OBJECT=ROOT/'post-replay-quality.o'
    BINARY=ROOT/'post-replay-quality'
    assert ROOT.resolve() == ROOT and SOURCE.is_file()
    assert not BINARY.exists() and not OBJECT.exists()
    assert subprocess.check_output(['git', '-C', str(REPO), 'rev-parse', 'HEAD'], text=True).strip() == '883346efb49bd3b46875ea996cb9f195b954d525'
    commands = subprocess.check_output([NINJA, '-C', str(BUILD), '-t', 'commands', 'post-replay'], text=True).splitlines()
    compile_recipe = next(line for line in commands if line.startswith('/usr/bin/c++ ') and ' -c ' in line and '/src/post_replay.cpp' in line)
    link_recipe = next(line for line in commands if line.startswith(': && /usr/bin/c++ ') and ' -o post-replay ' in line)
    compile_cmd = shlex.split(compile_recipe)
    assert compile_cmd[0] == '/usr/bin/c++'
    for flag, replacement in (('-MT', str(OBJECT)), ('-MF', str(OBJECT) + '.d'), ('-o', str(OBJECT)), ('-c', str(SOURCE))):
        compile_cmd[compile_cmd.index(flag) + 1] = replacement
    link_cmd = shlex.split(link_recipe)
    assert link_cmd[:2] == [':', '&&'] and link_cmd[-2:] == ['&&', ':']
    link_cmd = link_cmd[2:-2]
    link_cmd[link_cmd.index('-o') + 1] = str(BINARY)
    link_cmd = [str(OBJECT) if value == 'CMakeFiles/post-replay.dir/src/post_replay.cpp.o' else
                str(BUILD / value) if value.endswith(('.a', '.dylib')) and not value.startswith('/') else value
                for value in link_cmd]
    assert link_cmd[0] == '/usr/bin/c++'
    protected = [REPO / 'src/post_replay.cpp', BUILD / 'post-replay', BUILD / 'trellis-cli']
    protected += [Path(value) for value in link_cmd if value.endswith(('.a', '.dylib'))]
    before = {str(path): digest(path) for path in protected}
    git_before = subprocess.check_output(['git', '-C', str(REPO), 'status', '--short'], text=True)
    record = {'schema': 'private-replay-quality-build-v1', 'source_sha256': digest(SOURCE),
              'upstream_commit': '883346efb49bd3b46875ea996cb9f195b954d525',
              'original_ninja_compile': compile_recipe, 'original_ninja_link': link_recipe,
              'commands': [compile_cmd, link_cmd], 'jobs': 1, 'protected_before': before,
              'tests': [], 'status': 'building'}
    start = time.monotonic()
    with (ROOT / 'build.log').open('x') as log:
        for command in (compile_cmd, link_cmd):
            subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=180)
    record['elapsed_s'] = time.monotonic() - start
    for args, expected_code, text in (
            (['--help'], 0, '--geometry-res'),
            (['missing.bin', 'unused.glb', '--geometry-res', '1024'], 1, 'cannot open missing.bin'),
            (['missing.bin', 'unused.glb', '--geometry-res', '512'], 1, 'cannot open missing.bin'),
            (['missing.bin', 'unused.glb', '--geometry-res', '1536'], 1, 'cannot open missing.bin'),
            (['missing.bin', 'unused.glb', '--geometry-res', '256'], 1, '--geometry-res requires'),
            (['missing.bin', 'unused.glb', '--geometry-res'], 1, '--geometry-res requires')):
        result = subprocess.run([str(BINARY), *args], cwd=ROOT, capture_output=True, text=True, timeout=5)
        passed = result.returncode == expected_code and text in result.stdout + result.stderr
        record['tests'].append({'argv': args, 'passed': passed, 'returncode': result.returncode,
                                'stdout': result.stdout, 'stderr': result.stderr})
    record['protected_after'] = {str(path): digest(path) for path in protected}
    record['protected_unchanged'] = record['protected_after'] == before
    record['git_status_unchanged'] = subprocess.check_output(['git', '-C', str(REPO), 'status', '--short'], text=True) == git_before
    record['binary_sha256'] = digest(BINARY)
    record['status'] = 'passed' if all(t['passed'] for t in record['tests']) and record['protected_unchanged'] and record['git_status_unchanged'] else 'failed'
    with (ROOT / 'build-receipt.json').open('x') as stream:
        json.dump(record, stream, indent=2)
        stream.write('\n')
    print(json.dumps({k: record[k] for k in ('status', 'elapsed_s', 'source_sha256', 'binary_sha256', 'protected_unchanged', 'git_status_unchanged')}))
    raise SystemExit(0 if record['status'] == 'passed' else 1)

if __name__=='__main__':main()
