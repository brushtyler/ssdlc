import unittest

from orchestrator import (CostAndTelemetryEntry, parse_review_verdict,
                          parse_qa_verdict, parse_appsec_verdict)


class VerdictTests(unittest.TestCase):
    def test_exact_final_marker_only(self):
        for parser, marker, positive, negative in (
                (parse_review_verdict, 'REVIEW_STATUS', 'APPROVED', 'CHANGES_REQUESTED'),
                (parse_qa_verdict, 'QA_STATUS', 'PASSED', 'FAILED'),
                (parse_appsec_verdict, 'APPSEC_STATUS', 'SECURE', 'VULNERABLE')):
            line = f'{marker}: {positive}'
            with self.subTest(marker=marker):
                self.assertEqual(parser('Human report\n' + line + '\n'), positive)
                self.assertEqual(parser(f'{marker}: {negative}'), negative)
                for response in ('', 'prefix ' + line, line + ' annotation',
                                 line + '\n' + line, f'{marker}: {negative}\n' + line,
                                 line + '\nmore prose', 'X' + line):
                    with self.subTest(response=response):
                        self.assertIsNone(parser(response))

    def test_telemetry_details_are_additive(self):
        entry = CostAndTelemetryEntry('qa', 'qa', 'fake', 'model', 'PASSED', 1)
        self.assertNotIn('details', entry.to_dict())
        entry = CostAndTelemetryEntry('qa', 'qa', 'fake', 'model', 'PASSED', 1,
                                      details={'identity': 'abc'})
        self.assertEqual(entry.to_dict()['details'], {'identity': 'abc'})
