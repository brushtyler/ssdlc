"""Live lock ownership can be borrowed explicitly, never inferred from a file."""
from dataclasses import replace
import fcntl
from pathlib import Path
import tempfile
import threading
import unittest

from ssdlc_locks import current_lock, transition_lock
from ssdlc_tasks import TaskError, ticket_lock


class LockLeaseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'ticket.lock'

    def test_explicit_borrow_preserves_the_outer_ownership(self):
        self.assertIsNone(current_lock(self.path))
        with ticket_lock(self.path) as lease:
            self.assertIs(current_lock(self.path), lease)
            with transition_lock(self.path, (lease,)) as borrowed:
                self.assertIs(borrowed, lease)
            self.assertIs(current_lock(self.path), lease)
            with self.assertRaises(TaskError):
                with ticket_lock(self.path):
                    self.fail('ordinary locks must remain nonreentrant')
        self.assertIsNone(current_lock(self.path))
        with self.assertRaisesRegex(TaskError, 'no current'):
            with transition_lock(self.path, (lease,)):
                self.fail('released lease cannot authorize a transition')

    def test_recreated_lock_file_does_not_prove_ownership(self):
        with ticket_lock(self.path) as lease:
            self.path.unlink()
            self.path.write_text('replacement')
            with self.assertRaisesRegex(TaskError, 'identity changed'):
                lease.validate(self.path)

    def test_fabricated_or_other_process_receipt_is_refused(self):
        with ticket_lock(self.path) as lease:
            for forged in (replace(lease), replace(lease, pid=lease.pid + 1)):
                with self.subTest(forged=forged), self.assertRaisesRegex(TaskError, 'no current'):
                    with transition_lock(self.path, (forged,)):
                        self.fail('receipt must be the live registered object')

    def test_lease_cannot_be_borrowed_by_another_thread(self):
        errors = []
        with ticket_lock(self.path) as lease:
            def other():
                try:
                    lease.validate(self.path)
                except TaskError:
                    errors.append('refused')
            worker = threading.Thread(target=other)
            worker.start(); worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
        self.assertEqual(errors, ['refused'])

    def test_exception_releases_registration_and_real_lock(self):
        with self.assertRaises(RuntimeError):
            with ticket_lock(self.path):
                raise RuntimeError('interrupted')
        self.assertIsNone(current_lock(self.path))
        with ticket_lock(self.path) as lease:
            self.assertIs(current_lock(self.path), lease)

    def test_open_descriptor_with_released_or_downgraded_lock_is_not_ownership(self):
        for operation in (fcntl.LOCK_UN, fcntl.LOCK_SH):
            with self.subTest(operation=operation), ticket_lock(self.path) as lease:
                fcntl.flock(lease.fd, operation)
                with self.assertRaisesRegex(TaskError, 'Kernel exclusive'):
                    lease.validate(self.path)


if __name__ == '__main__':
    unittest.main()
