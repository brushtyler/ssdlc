"""Isolated A5 entrypoint: stage only host-pinned test modules, never discovery.

Invoke with python -I. Production modules remain the candidate under test;
new tests and test packages cannot enter this process through tests/sys.path.
This is import isolation, not containment of malicious production code.
"""
import argparse
from pathlib import Path
import shutil
import sys
import tempfile


def main(argv=None):
    if not sys.flags.isolated:
        raise SystemExit('A5 requires python -I ssdlc_replay_gate.py')
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    output = str(Path(args.output).resolve())
    # -I omits CWD, PYTHONPATH and user site. Keep its interpreter/library paths;
    # add the production root and a disposable allowlist of pinned test files.
    sys.path.insert(0, str(root))
    from ssdlc_grader import REQUIRED, verify_baseline
    verify_baseline(root)
    with tempfile.TemporaryDirectory(prefix='ssdlc-a5-') as temporary:
        tests = Path(temporary) / 'tests'
        tests.mkdir()
        for name in sorted(REQUIRED):
            if not name.startswith('tests/'):
                continue
            source = root / name
            target = Path(temporary) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        sys.path.insert(0, str(tests))
        try:
            from replay.benchmark import main as benchmark
            return benchmark(['--output', output])
        finally:
            sys.path.remove(str(tests))


if __name__ == '__main__':
    raise SystemExit(main())
