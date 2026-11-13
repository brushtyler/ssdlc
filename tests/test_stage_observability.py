"""Bounded stage logging is visible by default without leaking into JSON stdout."""
import io
import json
import logging
import os
from contextlib import redirect_stderr, redirect_stdout
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import orchestrator as o
import ssdlc_bootstrap as bootstrap


class StageObservabilityTests(unittest.TestCase):
    def test_default_cli_handler_has_utc_timestamps_and_leaves_stdout_untouched(self):
        loggers=[logging.getLogger(name) for name in (o.__name__,'ssdlc_corrective',bootstrap.__name__)]
        snapshots=[(logger,list(logger.handlers),logger.level,logger.propagate) for logger in loggers]
        err,out=io.StringIO(),io.StringIO()
        try:
            for logger in loggers:logger.handlers=[]
            with redirect_stderr(err),redirect_stdout(out):
                o._configure_stage_logging();o._configure_stage_logging()
                o._log_mode_decision('TKT-OBS','run',dict(ok=True,cls='new_run',code='task'))
                print(json.dumps({'status':'ok'}))
            self.assertEqual(json.loads(out.getvalue()),{'status':'ok'})
            lines=err.getvalue().splitlines()
            self.assertEqual(len(lines),1)
            self.assertRegex(lines[0],r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ mode-decision ')
            self.assertIn('run=TKT-OBS txn=- preview=- tasks=-',lines[0])
            self.assertIn('decision=task',lines[0])
        finally:
            for logger,handlers,level,propagate in snapshots:
                for handler in logger.handlers:
                    if handler not in handlers:handler.close()
                logger.handlers=handlers;logger.setLevel(level);logger.propagate=propagate

    def test_obsolete_warning_is_once_bound_to_run_and_never_contains_flag_value(self):
        with patch.dict(os.environ,{'SSDLC_ENABLE_TASK_BREAKDOWN':'untrusted flag value: do not log'}), \
             patch.object(o,'_OBSOLETE_FLAG_WARNED',False),self.assertLogs(o.__name__,level='INFO') as logs:
            o._warn_obsolete_task_flag('TKT-OBS');o._warn_obsolete_task_flag('TKT-OBS')
        self.assertEqual(len(logs.records),1)
        message=logs.records[0].getMessage()
        self.assertIn('action=ignored run=TKT-OBS',message)
        self.assertNotIn('untrusted flag value',message)

    def test_bootstrap_refusal_has_run_and_bounded_reason_without_repository_or_payload(self):
        before=bootstrap.COUNTERS['bootstrap_ineligible']
        with self.assertLogs(bootstrap.__name__,level='INFO') as logs:
            error=bootstrap._ineligible('profile_invalid','TKT-OBS')
        self.assertEqual(error.code,'profile_invalid')
        self.assertEqual(bootstrap.COUNTERS['bootstrap_ineligible'],before+1)
        self.assertIn('reason=profile_invalid run=TKT-OBS txn=- preview=- tasks=-',logs.records[0].getMessage())

    def test_untrusted_and_oversize_stage_fields_are_bounded_and_credentials_scrubbed(self):
        instance=SimpleNamespace(ticket_id='TKT-OBS',corrective_counters={})
        secret='a-real-looking-test-credential'
        with patch.dict(os.environ,{'PROVIDER_SECRET':secret}),self.assertLogs('ssdlc_corrective',level='INFO') as logs:
            o.SSDLCOrchestrator._corrective_stage(instance,'refused',{'revision':'a'*64},'b'*64,
                tasks=['task']*100,reason=secret)
            o._log_mode_decision('ticket\ninjected','run',dict(ok=False,cls='legacy',reason='raw finding '+secret))
        message=logs.records[0].getMessage()
        self.assertNotIn(secret,message)
        self.assertIn('omitted_tasks=68',message)
        self.assertLess(len(message),2400)
        self.assertEqual(instance.corrective_counters['corrective_transactions_refused'],{'-':1})
        self.assertNotIn('\n',o._stage_log_field('ticket\ninjected'))
        self.assertLessEqual(len(o._stage_log_field('x'*10000)),128)
