"""Protected semantic replay gate. No live models and no runtime artifact export."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.error
from unittest.mock import patch

from replay.test_timeout_recovery import TimeoutRecoveryTests
from test_design_recovery import DesignRecoveryTests
from test_recovery_limits import policy
from ssdlc_recovery import LimitsStore
from ssdlc_tasks import TaskError, git


def task_case(name):
    case = TimeoutRecoveryTests('test_quiescent_timeout_produces_checkpoint_without_another_dispatch')
    case.setUp()
    outcome = None
    try:
        develop, verify = case.develop, None
        if name == 'uncertain_timeout':
            def develop(*args):
                case.calls.append(1)
                raise subprocess.TimeoutExpired(['redacted'], 30)
        elif name == 'unowned_git_lock':
            Path(git(case.wt, 'rev-parse', '--git-path', 'index.lock')).write_text('unowned')
        elif name == 'out_of_scope_timeout':
            def develop(task, attempt, prompt, workspace):
                (workspace / 'unauthorized.py').write_text('out of scope')
                return case.develop(task, attempt, prompt, workspace)
        elif name == 'timeout_repair':
            def develop(task, attempt, prompt, workspace):
                if attempt == 1:
                    return case.develop(task, attempt, prompt, workspace)
                case.calls.append(attempt)
                assert (workspace / 'app.py').read_text() == 'value = 1\n'
                (workspace / 'app.py').write_text('value = 2\n')
                return {'output': 'TASK_DEV_STATUS: COMPLETE', 'exit_code': 0, 'quiescent': True}
            def verify(argv, cwd, timeout, workspace):
                good = (workspace / 'app.py').read_text() in ('value = 0\n', 'value = 2\n')
                return 'Ran 1 test\n' + ('OK' if good else 'FAILED'), '', 0 if good else 1
        scheduler = case.make(developer=develop, verifier=verify, max_attempts=2)
        try:
            result = scheduler.run(case.plan['tasks'])
            outcome = result['tasks']['T1']['status']
        except TaskError as exc:
            outcome = getattr(exc, 'category', type(exc).__name__)
        record = scheduler.journal.peek()['tasks']['T1']
        head = git(case.wt, 'rev-parse', 'HEAD')
        expected_source = 'value = 2\n' if name == 'timeout_repair' else 'value = 1\n'
        checkpoint = (record['status'] == 'COMPLETED' and record.get('commit') == head and head != case.base
                      and git(case.wt, 'status', '--porcelain') == ''
                      and (case.wt / 'app.py').read_text() == expected_source)
        return {'outcome': outcome, 'logical_invocations': len(case.calls), 'attempts': record['attempts'],
                'checkpoint': checkpoint}
    finally:
        case.doCleanups()


def run_case(name):
    if name in ('timeout_checkpoint', 'timeout_repair', 'uncertain_timeout', 'unowned_git_lock', 'out_of_scope_timeout'):
        return task_case(name)
    if name in ('design_repair', 'design_stall'):
        case = DesignRecoveryTests('test_rewording_does_not_reset_stagnation_diagnosis_then_next_round_repairs')
        case.setUp()
        try:
            try:
                case.make(resolve=name == 'design_repair').run()
                outcome = case.state()['rounds'][-1]['status']
            except TaskError as exc:
                outcome = getattr(exc, 'category', type(exc).__name__)
            return {'outcome': outcome, 'logical_invocations': len(case.arch_calls) + len(case.review_calls) + len(case.diagnoses),
                    'diagnoses': len(case.diagnoses), 'checkpoint': False}
        finally:
            case.doCleanups()
    if name == 'cost_limit':
        with tempfile.TemporaryDirectory() as tmp:
            store = LimitsStore(Path(tmp), lambda: 100)
            store.authorize(policy(), {'ticket': 'offline-benchmark'})
            identity = store.reserve('03-dev', '03-dev', 'claude')
            store.finish(identity, 'returned', {'metered_usd': None, 'cli_estimate_usd': 10})
            try:
                store.reserve('03-dev', '03-dev', 'codex')
                outcome = 'unexpected-dispatch'
            except TaskError as exc:
                outcome = getattr(exc, 'category', type(exc).__name__)
            return {'outcome': outcome, 'logical_invocations': len(store._load()[1]['invocations']), 'checkpoint': False}
    if name == 'uncertain_transport':
        from orchestrator import EngineRunner
        with patch.dict('os.environ', {'OPENROUTER_API_KEY': 'offline-sentinel'}), \
             patch('orchestrator.urllib.request.urlopen', side_effect=urllib.error.URLError('lost response')) as request:
            try:
                EngineRunner.call_openrouter('model', 'system', 'user', 'p', 't', '03-dev')
                outcome = 'unexpected-success'
            except TaskError as exc:
                outcome = getattr(exc, 'category', type(exc).__name__)
            return {'outcome': outcome, 'logical_invocations': request.call_count, 'checkpoint': False}
    raise ValueError('Unregistered benchmark case')


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--output', required=True)
    a = p.parse_args(argv)
    manifest = json.loads((Path(__file__).parent / 'fixtures/i2-recovery.json').read_text())
    results = []
    for name, expected in manifest['cases'].items():
        start = time.monotonic()
        observed = run_case(name)
        elapsed = time.monotonic() - start
        passed = observed == expected and elapsed <= manifest['max_case_seconds']
        stop = observed['outcome'] not in ('COMPLETED', 'APPROVED')
        false_block = expected['outcome'] in ('COMPLETED', 'APPROVED') and stop
        results.append({'case': name, 'passed': passed, 'observed': observed, 'elapsed_seconds': round(elapsed, 6),
                        'live_model_calls': 0, 'human_callbacks_executed': 0, 'total_interventions_required': int(stop),
                        'avoidable_interventions': int(false_block),
                        'escaped_seeded_defects': int(name == 'timeout_repair' and observed['outcome'] == 'COMPLETED' and not observed['checkpoint']),
                        'operator_wait_seconds': 0,
                        'ready_pr': False})
    report = {'schema_version': 1, 'corpus': manifest['corpus'], 'cases': results,
              'failed_cases': sum(not c['passed'] for c in results),
              'note': 'Semantic engine/checkpoint and expected-stop benchmark, not ready-PR delivery or the ten-defect C oracle. Expected stops still count required operator interventions. No callbacks were executed; waits were not simulated.'}
    Path(a.output).write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps({'corpus': report['corpus'], 'cases': len(results), 'failed_cases': report['failed_cases']}))
    return int(bool(report['failed_cases']))


if __name__ == '__main__':
    raise SystemExit(main())
