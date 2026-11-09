import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
toolkit = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(toolkit))
from orchestrator import EngineRunner

parser = argparse.ArgumentParser(description='Verify real Docker isolation using synthetic credentials')
parser.add_argument('--image', default=EngineRunner.IMAGE_NAME)
parser.add_argument('--suite', action='store_true', help='Also run the unit/integration suite in the read-only runner')
args = parser.parse_args()
EngineRunner.IMAGE_NAME = args.image

with tempfile.TemporaryDirectory(prefix='ssdlc-docker-smoke-') as tmp:
    repo = Path(tmp) / 'repo'
    wt = repo / '.ssdlc/TKT-DOCKER/worktree'
    wt.mkdir(parents=True)
    (wt / 'source.py').write_text('value = 1\n')
    credential = repo / '.ssdlc/.runtime/codex_home/auth.json'
    credential.parent.mkdir(parents=True)
    credential.write_text('TEST_SENTINEL_ONLY')
    scratch = Path(tmp) / 'scratch'
    scratch.mkdir()
    home = Path(tmp) / 'empty-home'
    home.mkdir()
    settings = {'TMPDIR': str(scratch), 'XDG_CACHE_HOME': str(scratch / 'cache'), 'HOME': str(scratch / 'home')}
    code = '''
import json, os
from pathlib import Path
source = Path('source.py')
try:
 source.write_text('tampered')
 raise AssertionError('source is writable')
except OSError as e:
 assert e.errno == 30, repr(e)
secret = Path(os.environ['TEST_SECRET_PATH'])
assert not secret.exists(), 'runtime secret is visible'
assert not Path('/opt/ssdlc/.ssdlc').joinpath('project.env').exists(), 'toolkit configuration exposed'
assert 'OPENAI_API_KEY' not in os.environ
assert 'CLAUDE_CODE_OAUTH_TOKEN' not in os.environ
scratch = Path(os.environ['TMPDIR'])
(scratch / 'probe.txt').write_text('writable scratch')
print(json.dumps({'source_read_only': True, 'runtime_hidden': True, 'ai_env_hidden': True, 'scratch_writable': True}))
'''
    with patch('pathlib.Path.home', return_value=home), patch.dict(os.environ, {}, clear=True):
        cmd = EngineRunner._wrap_in_role_container(['python3', '-c', code], wt, pass_ai_keys=False, verification_scratch=scratch, verification_env={**settings, 'TEST_SECRET_PATH': str(credential)})
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    print(proc.stdout)
    if proc.returncode:
        print(proc.stderr)
        sys.exit(proc.returncode)
    assert (wt / 'source.py').read_text() == 'value = 1\n'
    assert (scratch / 'probe.txt').read_text() == 'writable scratch'
    # Exercise the actual verification entry point against real Docker, even when
    # the controller itself runs inside a container.
    with patch.object(EngineRunner, '_should_spawn_role_container', return_value=True), patch('pathlib.Path.home', return_value=home):
        out, err, rc = EngineRunner.run_sandboxed_cmd(['python3', '-c', "import os; from pathlib import Path; p=Path(os.environ['TMPDIR'])/'result'; p.write_text('OK'); print('verification entry point OK')"], wt)
    print(out)
    if rc:
        print(err)
        sys.exit(rc)
    print('DOCKER_SMOKE_PASS')

if args.suite:
    with patch.object(EngineRunner, '_should_spawn_role_container', return_value=True):
        out, err, rc = EngineRunner.run_sandboxed_cmd(
            ['python3', '-m', 'unittest', 'discover', '-s', 'tests', '-q'], toolkit, timeout=180,
        )
    print(out)
    print(err)
    sys.exit(rc)
