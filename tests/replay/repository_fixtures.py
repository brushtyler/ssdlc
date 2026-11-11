"""Pinned semantic replay fixtures, independent of mutable execution-mode tests.

Extracted unchanged for issue #22. Keep these helpers host-owned: production
candidates may adapt ordinary unit fixtures, never their replay oracle.
"""
import json
from pathlib import Path
import tempfile
import unittest

from ssdlc_tasks import TaskPlanValidator, TaskScheduler, file_hash, git


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name) / 'repo'
        self.repo.mkdir()
        git(self.repo, 'init')
        git(self.repo, 'config', 'user.name', 'SSDLC Test')
        git(self.repo, 'config', 'user.email', 'ssdlc@example.invalid')
        (self.repo / '.gitignore').write_text('.ssdlc/\n')
        (self.repo / 'app.py').write_text('value = 0\n')
        git(self.repo, 'add', '.gitignore', 'app.py')
        git(self.repo, 'commit', '-m', 'baseline')
        self.base = git(self.repo, 'rev-parse', 'HEAD')
        self.artifacts = self.repo / '.ssdlc' / 'TKT-1'
        (self.artifacts / '02-design').mkdir(parents=True)
        self.log = self.artifacts / 'baseline.log'
        self.log.write_text('Ran 1 test\nOK')
        self.quality = {
            'schema_version': '1.0', 'contract_revision': 1, 'base_commit': self.base,
            'sources': [], 'standards': [], 'limitations': [], 'exceptions': [],
            'checks': [{'check_id': 'CHECK-GLOBAL', 'argv': ['python3', '-m', 'unittest'], 'cwd': '.', 'timeout_sec': 5, 'kind': 'test', 'required': True}],
            'baseline': [{'check_id': 'CHECK-GLOBAL', 'exit_code': 0, 'log_ref': 'baseline.log', 'sha256': file_hash(self.log)}],
        }
        self.plan = {
            'schema_version': '1.0', 'plan_revision': 1, 'ticket_id': 'TKT-1',
            'execution_strategy': 'sequential_dag', 'base_commit': self.base,
            'quality_contract_ref': '00-recon/quality-contract.json',
            'tasks': [self.task('T1', ['AC-1'], ['SEC-1'], ['app.py'])],
        }

    def task(self, task_id, ac, sec, targets, deps=None):
        return {'task_id': task_id, 'title': 'Implement ' + task_id, 'description': 'Scoped behavior',
                'depends_on': deps or [], 'target_files': targets, 'ac_refs': ac, 'sec_refs': sec,
                'standard_refs': [], 'profile': 'ALGORITHMIC', 'complexity': 'standard',
                'kind': 'task', 'contract_refs': [],
                'verification': {'mode': 'automated', 'quality_check_refs': ['CHECK-GLOBAL'],
                                 'checks': [{'check_id': 'CHECK-' + task_id, 'argv': ['python3', '-m', 'unittest'],
                                             'cwd': '.', 'timeout_sec': 5, 'ac_refs': ac, 'sec_refs': sec}]}}

    def validator(self, requirements='AC-1 SEC-1'):
        return TaskPlanValidator(self.repo, requirements, self.quality, self.artifacts)

    def worktree(self):
        wt = self.artifacts / 'worktree'
        git(self.repo, 'worktree', 'add', '-b', 'ssdlc/test', str(wt), self.base)
        return wt

    def scheduler(self, developer, verifier=None, wt=None, **kwargs):
        return TaskScheduler(wt or self.wt, self.artifacts / '03-dev' / 'tasks', self.plan,
                             self.quality, {'requirements': 'AC-1 SEC-1'}, developer,
                             verifier or (lambda *args: ('Ran 1 test\nOK', '', 0)), **kwargs)


def finding(identifier='D1', **kwargs):
    return {'finding_id': identifier, 'status': 'OPEN', 'severity': 'BLOCKING', 'category': 'PLAN_DEFECT',
            'requirement_refs': ['SEC-2'], 'task_ids': [], 'message': 'Retained evidence inventory is incomplete',
            'evidence': 'ADR omits failed_attempts check logs stored by the scheduler',
            'requested_change': 'Enumerate retained logs, including archived failed attempts, and test tampering',
            'new_issue_reason': 'INITIAL_REVIEW', **kwargs}


def review(findings=()):
    blockers = [f for f in findings if f['status'] == 'OPEN' and f['severity'] == 'BLOCKING']
    return json.dumps({'verdict': 'REVISION_REQUIRED' if blockers else 'APPROVED', 'analysis': 'STRIDE and source-contract assessment',
                       'findings': list(findings)})

