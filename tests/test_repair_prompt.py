"""Corrective prompts contain only bounded, scrubbed structured evidence."""
import json
from unittest.mock import patch

from replay.repository_fixtures import RepositoryTests
from ssdlc_tasks import TaskError, DevelopmentBlocked, TaskScheduler, file_hash


class RepairPromptTests(RepositoryTests):
    def setUp(self):
        super().setUp()
        self.wt = self.worktree()
        self.prompts = []
        self.engine = self.scheduler(self.developer, max_attempts=4)
        self.engine.run(self.plan['tasks'])
        self.prompts.clear()

    def developer(self, task, attempt, prompt):
        self.prompts.append(prompt)
        (self.wt / 'app.py').write_text('value = %d\n' % attempt)
        return dict(output='developer-output-private sk-outputsecret', exit_code=0)

    def test_full_retry_prompt_projection_and_scrubbing(self):
        secret = 'environment-credential-private'
        finding = dict(id='F1', severity='high', classification='necessary_gap',
                       scenario='scenario-private ' + secret, rationale='rationale-private',
                       task_attribution=['T1', 'unknown'], file_refs=['app.py', '../escape'],
                       obligation_refs=['AC-1', 'AC-999'], evidence_refs=['ev', 'unknown'])
        corrective = dict(findings=[finding], phase='qa', reviewed_commit=self.base,
                          registered_evidence={'ev': 'a' * 64})
        self.engine.invalidate(['T1'], 'corrective:txn')
        count = 0
        def verify(*args):
            nonlocal count
            count += 1
            return ('Ran 1 test\n' + secret + ' sk-logsecret ghp_logsecret Bearer abcdefghijkl\n' + 'x' * 4000,
                    '', 1 if count == 1 else 0)
        self.engine.verifier = verify
        original_prompt = self.engine._developer_prompt
        def assemble(task, record, corrective=None):
            record['failure'] = 'raw-failure-private ' + secret
            return original_prompt(task, record, corrective)
        self.engine._developer_prompt = assemble
        with patch.dict('os.environ', {'PROVIDER_TOKEN': secret}):
            self.engine.repair(self.plan['tasks'], ['T1'], corrective, txn_id='txn', on_step=lambda *a: None)
        self.assertEqual(len(self.prompts), 2)
        for prompt in self.prompts:
            for forbidden in (secret, 'scenario-private', 'rationale-private', 'developer-output-private',
                              'raw-failure-private', 'sk-logsecret', 'ghp_logsecret', 'Bearer abcdefghijkl'):
                self.assertNotIn(forbidden, prompt)
        context = json.loads(self.prompts[-1].split('\nREPAIR CONTEXT:\n')[1])
        self.assertEqual(context['txn_id'], 'txn')
        self.assertEqual(context['findings'][0]['task_attribution'], ['T1'])
        self.assertEqual(context['findings'][0]['file_refs'], ['app.py'])
        self.assertGreaterEqual(context['omitted_count'], 6)
        self.assertLessEqual(len(context['checks'][0]['log_excerpt']), 2000)
        self.assertLessEqual(len(self.prompts[-1].split('\nREPAIR CONTEXT:\n')[1]), 12000)

    def failure(self):
        path = self.engine.directory / 'failed.log'
        path.write_text('whole-log-private ' * 500)
        return dict(failed_attempts=[dict(attempt=2, category='CHECK_FAILURE',
                    developer_output='never-copy', checks=[dict(check_id='CHECK-T1', exit_code=1,
                    empty_suite=False, log_ref='failed.log', sha256=file_hash(path))])])

    def test_registered_reference_suppresses_log_read_and_excerpt(self):
        record = self.failure()
        sha = record['failed_attempts'][0]['checks'][0]['sha256']
        proof = dict(check_id='CHECK-T1', log_ref='failed.log', log_sha256=sha,
                     record_ref='proof/record.json', record_sha256='b' * 64)
        with patch('pathlib.Path.read_text', side_effect=AssertionError('whole log read')):
            prompt = self.engine._developer_prompt(self.plan['tasks'][0], record, {
                'registered_evidence': {'proof/record.json': 'b' * 64}, 'candidate_proof': [proof]})
            result = json.loads(prompt.split('\nREPAIR CONTEXT:\n')[1])
        self.assertNotIn('log_excerpt', result['checks'][0])
        self.assertEqual(result['checks'][0]['sha256'], 'b' * 64)
        self.assertEqual(result['checks'][0]['log_ref'], 'proof/record.json')
        self.assertNotIn('whole-log-private', json.dumps(result))

    def test_generation_and_terminal_failures(self):
        record = self.failure()
        record['failed_floor'] = 1
        self.assertIsNone(self.engine._repair_context(record))
        record['failed_floor'] = 0
        record['failed_attempts'][0]['category'] = 'ENVIRONMENT_BLOCKED'
        with self.assertRaises(DevelopmentBlocked):
            self.engine._repair_context(record)

    def test_scrubber_covers_keys_identifiers_and_pem(self):
        secret = 'credential-value-123'
        with patch.dict('os.environ', {'MY_PASSWORD': secret}):
            result = TaskScheduler._scrub_prompt({secret: [secret, 'AKIAABCDEFGHIJKLMNOP',
                     '-----BEGIN PRIVATE KEY-----\nprivate-material\n-----END PRIVATE KEY-----']})
        self.assertNotIn(secret, json.dumps(result))
        self.assertNotIn('private-material', json.dumps(result))
        self.assertNotIn('AKIA', json.dumps(result))

    def test_oversize_refused_before_provider_and_dispatch_assertion(self):
        self.engine.invalidate(['T1'], 'corrective:large')
        finding = dict(id='F1', severity='high', classification='necessary_gap',
                       task_attribution=['T1'] * 3000)
        with self.assertRaisesRegex(TaskError, '12000'):
            self.engine.repair(self.plan['tasks'], ['T1'], {'findings': [finding]},
                               txn_id='large', on_step=lambda *a: None)
        self.assertEqual(self.prompts, [])
        with self.assertRaisesRegex(TaskError, '12000'):
            self.engine._assert_prompt_bound('prefix\nREPAIR CONTEXT:\n' + 'x' * 12001)

    def test_credentials_in_approved_context_are_scrubbed_from_full_prompt(self):
        with patch.dict('os.environ', {'API_KEY': 'private-env-credential'}):
            self.engine.context['extra'] = 'private-env-credential'
            prompt = self.engine._developer_prompt(self.plan['tasks'][0], {'failure': 'raw-private'})
        self.assertNotIn('private-env-credential', prompt)
        self.assertNotIn('raw-private', prompt)
