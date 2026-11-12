"""Closed event discovery and deterministic corrective recovery regressions."""
import ast
import copy
import json
from pathlib import Path
import tempfile
import unittest

from ssdlc_corrective import (CODES, CorrectiveError, EVENT_KINDS, FORBIDDEN_KINDS,
    TRANSITIONS, TransactionJournal, build_record, derive_substate, effective_limits,
    frozen_manifest, plan_next, txn_id_for, verify_evidence, verify_lineage,
    verify_record, walk_lineage)
from ssdlc_tasks import digest


def collect_event_kinds(source_path):
    path = Path(source_path)
    tree = ast.parse(path.read_text())
    kinds, unresolved = set(), []

    class Collector(ast.NodeVisitor):
        def __init__(self):
            self.parameters = []

        def resolve(self, node):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                kinds.add(node.value)
            elif isinstance(node, ast.IfExp):
                self.resolve(node.body)
                self.resolve(node.orelse)
            elif isinstance(node, ast.Name) and node.id == 'event' and any(self.parameters):
                pass
            else:
                unresolved.append(f'{path}:{node.lineno}')

        def visit_FunctionDef(self, node):
            args = node.args.posonlyargs + node.args.args
            defaults = [None] * (len(args) - len(node.args.defaults)) + node.args.defaults
            self.parameters.append(any(a.arg == 'event' for a in args + node.args.kwonlyargs))
            for arg, default in zip(args + node.args.kwonlyargs, defaults + node.args.kw_defaults):
                if arg.arg == 'event' and default is not None:
                    self.resolve(default)
            self.generic_visit(node)
            self.parameters.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, 'id', None)
            if name in ('_save', 'save') and node.args:
                self.resolve(node.args[0])
            for kw in node.keywords:
                if kw.arg == 'event':
                    self.resolve(kw.value)
            self.generic_visit(node)

    Collector().visit(tree)
    return kinds, unresolved


SHA = 'a' * 64
LIMITS = dict(max_attempts=3, gate_round_ceiling=3, budget_cap_usd=8,
              spend_at_creation_usd=0)
CHECK = dict(log_ref='check.log', sha256=SHA, exit_code=0, empty_suite=False)


def task(status='INVALIDATED'):
    return dict(status=status, attempts=0, checks=[], failed_attempts=[], revalidations=[])


class Trace:
    def __init__(self, ack='INVALIDATION_APPLIED', closure=('A',), roots=('A',)):
        self.closure, self.roots = list(closure), list(roots)
        self.state = dict(tasks={t: task() for t in closure}, expected_head='head',
                          inputs_sha256=SHA, ignored_paths=[], preflights=[],
                          revalidation_runs=[dict(txn_id='txn', status='RUNNING')])
        self.anchor = dict(event=ack, txn_id='txn', state=copy.deepcopy(self.state))
        self.events = []

    def emit(self, kind):
        self.events.append(dict(event=kind, state=copy.deepcopy(self.state)))

    def walk(self):
        return walk_lineage(self.anchor, self.events, self.closure, self.roots, LIMITS)

    def open(self):
        self.emit('REVALIDATION_STARTED')

    def start(self):
        self.state['tasks']['A'].update(status='RUNNING', attempts=self.state['tasks']['A']['attempts'] + 1, checks=[])
        self.emit('TASK_STARTED')

    def verifying(self):
        self.state['tasks']['A']['status'] = 'VERIFYING'
        self.emit('VERIFYING')

    def check(self, failed=False):
        self.state['tasks']['A']['checks'].append(dict(CHECK, exit_code=int(failed)))
        self.emit('CHECK_FINISHED')

    def fail(self, category='CHECK_FAILURE'):
        r = self.state['tasks']['A']
        r['status'] = 'FAILED'
        r['failed_attempts'].append(dict(category=category, checks=copy.deepcopy(r['checks']), diff_ref='failure.diff', diff_sha256=SHA))
        self.emit('ATTEMPT_FAILED')

    def rollback(self):
        self.emit('ATTEMPT_RESTORED')

    def complete(self):
        r = self.state['tasks']['A']
        r.update(status='COMMIT_INTENT', candidate_tree='tree', start_commit='head')
        self.emit('COMMIT_INTENT')
        r.update(status='COMPLETED', repairs=[dict(txn_id='txn')], restored_by=None, superseded=[])
        self.state['expected_head'] = 'new'
        self.emit('TASK_COMPLETED')

    def preflight(self, passed=True):
        self.state['revalidation_runs'][-1]['status'] = 'COMPLETE'
        self.emit('REVALIDATION_FINISHED')
        p = dict(status='RUNNING', head=self.state['expected_head'], checks=[])
        self.state['preflights'].append(p)
        self.emit('PREFLIGHT_STARTED')
        p['checks'].append(dict(CHECK, exit_code=0 if passed else 1))
        self.emit('CHECK_FINISHED')
        p['status'] = 'PASSED' if passed else 'FAILED'
        self.emit('PREFLIGHT_PASSED' if passed else 'PREFLIGHT_BLOCKED')


class DiscoveryTests(unittest.TestCase):
    def collect(self, source):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.py'
            path.write_text(source)
            return collect_event_kinds(path)

    def test_real_source(self):
        kinds, unresolved = collect_event_kinds(Path(__file__).resolve().parents[1] / 'ssdlc_tasks.py')
        self.assertEqual(unresolved, [])
        self.assertEqual(kinds, EVENT_KINDS | FORBIDDEN_KINDS)
        self.assertTrue({'PREFLIGHT_PASSED', 'PREFLIGHT_BLOCKED'} <= kinds)
        self.assertEqual({kind for _, kind in TRANSITIONS}, EVENT_KINDS)
        self.assertFalse({kind for _, kind in TRANSITIONS} & FORBIDDEN_KINDS)

    def test_recursive_conditional(self):
        self.assertEqual(self.collect("self._save('A' if x else ('B' if y else 'C'))"), ({'A', 'B', 'C'}, []))

    def test_parameter_and_keyword(self):
        self.assertEqual(self.collect("def f(event='E1'):\n self._save(event)\nf(event='E2')"), ({'E1', 'E2'}, []))

    def test_unresolved_expressions(self):
        for expression in ('name', "f'X{n}'", 'kinds[0]'):
            kinds, unresolved = self.collect('self._save(' + expression + ')')
            self.assertFalse(kinds)
            self.assertEqual(len(unresolved), 1)
            self.assertTrue(unresolved[0].endswith('fixture.py:1'))

    def test_unregistered_fails_closed(self):
        kinds, _ = self.collect("self._save('UNREGISTERED')")
        self.assertFalse(kinds <= EVENT_KINDS | FORBIDDEN_KINDS)


class LineageTests(unittest.TestCase):
    def assertInvalid(self, trace):
        with self.assertRaises(CorrectiveError) as raised:
            trace.walk()
        self.assertIn(raised.exception.code, ('journal_lineage_invalid', 'txn_record_conflict'))

    def test_retry_uses_immediate_predecessor(self):
        t = Trace()
        t.open(); t.start(); t.verifying(); t.check(True); t.fail(); t.rollback(); t.start()
        result = t.walk()
        self.assertEqual(result['state'], 'R_RUN')
        self.assertEqual(result['starts']['A'], 2)
        self.assertEqual(t.state['tasks']['A']['attempts'], 2)

    def test_blocker_failure_rollback_is_terminal(self):
        for category in ('ENVIRONMENT_BLOCKED', 'DEVELOPER_INVOCATION'):
            t = Trace(); t.open(); t.start(); t.verifying(); t.fail(category); t.rollback()
            result = t.walk()
            sub = derive_substate(result, t.state['tasks']['A'], dict(head='head', expected_head='head', clean=True))
            self.assertEqual(sub, 'FAILED_UNVERIFIED')
            self.assertEqual(plan_next(dict(status='REPAIRING'), result, dict(substate=sub)), 'fail_repair_blocked')
            t.start()
            self.assertInvalid(t)

    def test_last_dependent_pre_ack(self):
        t = Trace('REPAIR_COMMITTED', ('A', 'B'))
        t.anchor['closure_index'] = 0
        t.state['tasks']['A']['status'] = 'COMPLETED'
        t.anchor['state'] = copy.deepcopy(t.state)
        r = t.state['tasks']['B']
        r['revalidations'].append(dict(status='RUNNING', attempt=1, checks=[]))
        t.emit('REVALIDATION_TASK_STARTED')
        t.state['revalidation_runs'][-1]['cursor'] = dict(task_id='B')
        t.emit('REVALIDATION_CHECK_STARTED')
        r['revalidations'][-1]['checks'].append(CHECK)
        t.emit('REVALIDATION_CHECK_FINISHED')
        r.update(status='COMPLETED', restored_by=1)
        r['revalidations'][-1]['status'] = 'PASSED'
        t.emit('REVALIDATION_TASK_PASSED')
        result = t.walk()
        self.assertEqual(result['state'], 'DONE')
        self.assertEqual(plan_next(dict(status='REVALIDATING'), result, dict(unacked='dependent')), 'ack_dependent')
        self.assertEqual(plan_next(dict(status='REVALIDATING'), dict(result, last_event=None), {}), 'finalize_run')

    def test_terminal_preflights(self):
        for passed in (True, False):
            t = Trace(); t.open(); t.start(); t.verifying(); t.check(); t.complete(); t.preflight(passed)
            result = t.walk()
            sub = derive_substate(result, t.state, dict(head='new', expected_head='new', clean=True))
            self.assertEqual(sub, 'PREFLIGHT_PASSED_UNACKED' if passed else 'PREFLIGHT_FAILED')
            self.assertEqual(plan_next(dict(status='REVALIDATED'), result, dict(substate=sub)), 'ack_final_preflight' if passed else 'fail_final_preflight')
            t.emit('PREFLIGHT_STARTED')
            self.assertInvalid(t)

    def test_forbidden_unknown_and_out_of_context_events(self):
        for kind in FORBIDDEN_KINDS | {'UNKNOWN', 'TASKS_INVALIDATED', 'CHECK_FINISHED', 'ATTEMPT_FAILED'}:
            t = Trace(); t.open(); t.emit(kind)
            with self.subTest(kind=kind):
                self.assertInvalid(t)
        t = Trace(); t.open(); t.start(); t.emit('ATTEMPT_FAILED')
        self.assertInvalid(t)

    def test_attempt_counter_attacks(self):
        for attempts in (-1, 2, 4):
            t = Trace(); t.open(); t.start()
            t.events[-1]['state']['tasks']['A']['attempts'] = attempts
            self.assertInvalid(t)
        t = Trace(); t.anchor['state']['tasks']['A']['attempts'] = 3
        t.state['tasks']['A']['attempts'] = 3
        t.open(); t.start()
        self.assertInvalid(t)

    def test_outside_closure_touch(self):
        t = Trace()
        t.anchor['state']['tasks']['X'] = task('COMPLETED')
        t.state['tasks']['X'] = task('COMPLETED')
        t.open()
        t.events[-1]['state']['tasks']['X']['attempts'] = 1
        self.assertInvalid(t)

    def test_no_revalidation_after_finish(self):
        t = Trace(); t.open(); t.start(); t.verifying(); t.check(); t.complete()
        t.state['revalidation_runs'][-1]['status'] = 'COMPLETE'
        t.emit('REVALIDATION_FINISHED'); t.emit('REVALIDATION_STARTED')
        self.assertInvalid(t)

    def test_anchor_must_be_on_chain(self):
        with tempfile.TemporaryDirectory() as directory:
            j = TransactionJournal(Path(directory))
            j.append('TXN_CREATED', dict(status='CREATED'))
            with self.assertRaises(CorrectiveError):
                verify_lineage(j.path, dict(sequence=1, sha256='b'*64), ['A'], ['A'], LIMITS)
            self.assertEqual(j.snapshot.name, 'txn-state.json')


class EvidenceTests(unittest.TestCase):
    def preview(self):
        return dict(phase='review', attempt=1, reviewed_commit='head', preview_revision=SHA,
                    execution_authorized=False, status='candidates', blockers=[],
                    repair_roots=['A'], closure=['A'], revalidation_tasks=[])

    def record(self):
        ref = dict(ref='x', sha256=SHA)
        return build_record(self.preview(), ticket_id='ticket', inputs_sha256=SHA,
            preview=dict(ref, revision=SHA, summary_ref='s', summary_sha256=SHA),
            plan=ref, quality=ref, requirements=ref, roster=ref, completion=ref,
            outcomes=[dict(ref, outcome_class='substantive')],
            gate_outcomes=[dict(gate='review', identity='r', outcome_class='substantive', verdict='reject')],
            evidence_manifest=dict(reader={'x': SHA}, controller_contract=ref),
            journal_head=dict(sequence=1, sha256=SHA), limits=LIMITS, next_gate_attempt=2)

    def test_schema_authority_is_separate_from_preview(self):
        preview = self.preview()
        before = copy.deepcopy(preview)
        record = self.record()
        self.assertEqual(preview, before)
        self.assertIs(preview['execution_authorized'], False)
        self.assertEqual(record['txn_id'], txn_id_for(preview))
        self.assertEqual(record['preview']['revision'], preview['preview_revision'])
        for field in ('authorized', 'execution_authorized'):
            with self.assertRaises(CorrectiveError):
                verify_record(dict(record, **{field: True}))
        with self.assertRaises(CorrectiveError):
            build_record(dict(preview, execution_authorized=True))

    def test_id_excludes_journal_head(self):
        p = self.preview()
        self.assertEqual(txn_id_for(p), txn_id_for(dict(p, journal_head=dict(sequence=99, sha256='b'*64))))

    def test_normalized_inventory_and_mutation(self):
        controller = dict(ref='controller', sha256=SHA)
        p = dict(source_hashes={'report': SHA, 'controller': SHA})
        self.assertEqual(frozen_manifest(p, {'report': SHA}, controller)['reader'], {'report': SHA})
        for changed in ({'report': 'b'*64}, {'report': SHA, 'extra': SHA}):
            with self.assertRaises(CorrectiveError):
                frozen_manifest(p, changed, controller)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'report').write_text('report')
            (root/'controller').write_text('controller')
            from ssdlc_tasks import file_hash
            hashes = {r: file_hash(root/r) for r in ('report', 'controller')}
            r = self.record()
            r['evidence_manifest'] = dict(reader={'report': hashes['report']}, controller_contract=dict(ref='controller', sha256=hashes['controller']))
            calls = []
            verify_evidence(r, root, hashes, inputs_sha256=SHA, controller_verifier=calls.append)
            self.assertEqual(len(calls), 1)
            (root/'report').write_text('swapped')
            with self.assertRaises(CorrectiveError):
                verify_evidence(r, root, hashes, inputs_sha256=SHA, controller_verifier=calls.append)

    def test_limits_only_lower(self):
        for cap in ('0', '100'):
            result = effective_limits(dict(SSDLC_MAX_DEV_RETRIES='10', SSDLC_BUDGET_CAP_USD=cap), [dict(limits=LIMITS)])
            self.assertEqual(result, dict(max_attempts=3, gate_round_ceiling=3, budget_cap_usd=8))
        self.assertEqual(effective_limits(dict(SSDLC_MAX_DEV_RETRIES='2', SSDLC_BUDGET_CAP_USD='4'), [dict(limits=LIMITS)]), dict(max_attempts=2, gate_round_ceiling=2, budget_cap_usd=4))
        self.assertEqual(effective_limits({}, []), dict(max_attempts=3, gate_round_ceiling=3, budget_cap_usd=6))
        self.assertIsNone(effective_limits(dict(SSDLC_BUDGET_CAP_USD='0'), [])['budget_cap_usd'])


if __name__ == '__main__':
    unittest.main()
