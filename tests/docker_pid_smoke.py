import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator import EngineRunner
parser = argparse.ArgumentParser(description='Verify bounded recovery from deliberate PID exhaustion')
parser.add_argument('--image', default=EngineRunner.IMAGE_NAME)
args = parser.parse_args()
EngineRunner.IMAGE_NAME = args.image
code = '''import subprocess
children=[]
try:
 for _ in range(64):
  children.append(subprocess.Popen(['sleep','1']))
except BlockingIOError:
 print('PID limit deliberately reached')
finally:
 for child in children: child.wait()
'''
with tempfile.TemporaryDirectory(prefix='ssdlc-pid-test-') as tmp:
 root=Path(tmp)
 (root/'.ssdlc').mkdir()
 with patch.object(EngineRunner,'_should_spawn_role_container',return_value=True), patch.dict(os.environ,{'SSDLC_CONTAINER_PIDS_LIMIT':'32'}):
  out,err,rc=EngineRunner.run_sandboxed_cmd(['python3','-c',code],root,30)
 evidence=[json.loads(p.read_text()) for p in (root/'.ssdlc/.runtime/resources').glob('*.json')]
 assert rc==125,(rc,out,err)
 assert len(evidence)==2,len(evidence)
 assert all(x['resource_failure'] and int(x['counters']['pids.events.max'])>0 for x in evidence),evidence
 assert all(x['counters']['memory.events.oom_kill']=='0' for x in evidence),evidence
 print('PASS: two fresh containers, PID exhaustion recorded before removal, zero CLI exit rejected, bounded retry returned 125, no OOM.')
