"""Run isolated offline suites; never invokes model backends or Blender."""
import ast
from pathlib import Path
import subprocess
import sys
ROOT=Path(__file__).resolve().parents[1]

def main():
    for folder in ['pipelines','skills','scripts','tests']:
        for path in (ROOT/folder).rglob('*.py'):
            ast.parse(path.read_text(encoding='utf-8-sig'),filename=str(path))
    suites=[('tests',None),('pipelines/mac-auxiliary',None),('pipelines/photo-mesh',None),
            ('pipelines/blender',None),('pipelines/native-metal/validation',None),
            ('pipelines/native-metal/quality-job','test-quality-job.py')]
    failed=[]
    for directory,script in suites:
        print('\nCHECK '+directory,flush=True)
        argv=[sys.executable,'-B',script] if script else [sys.executable,'-B','-m','unittest','discover','-s','.','-v']
        result=subprocess.run(argv,cwd=ROOT/directory,timeout=180)
        if result.returncode:failed.append(directory)
    print('\n'+('FAILED: '+', '.join(failed) if failed else 'All offline suites passed.'))
    return bool(failed)

if __name__=='__main__':raise SystemExit(main())
