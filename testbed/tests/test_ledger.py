"""Tests for testbed/ledger.py: the local count of live Onshape calls and the budget."""
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from testbed.ledger import AllowanceExhausted, BudgetExceeded, CallLedger, LedgerUnreadable


class CallLedgerTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'ledger.jsonl'
        patcher = mock.patch.dict(os.environ, {'XDG_STATE_HOME': tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ('TESTBED_BUDGET', 'TESTBED_CYCLE_START', 'TESTBED_RUN_CAP'):
            os.environ.pop(name, None)

    def test_default_path_and_settings(self):
        ledger = CallLedger()
        self.assertEqual(ledger.path, self.path.parent / 'penguincam-testbed' / 'ledger.jsonl')
        self.assertEqual((ledger.budget, ledger.per_run_cap), (250, 150))
        today = date.today()
        self.assertIn(ledger.cycle_start, (today.replace(year=today.year - 1),
                                           today - timedelta(days=365)))

    def test_missing_file_counts_zero(self):
        self.assertEqual(CallLedger(self.path).counted_in_cycle(), 0)

    def test_only_2xx_3xx_responses_counted(self):
        ledger = CallLedger(self.path)
        for status in (200, 204, 307, 399, 400, 401, 402, 429, 500, 0):
            ledger.record_call('s', 'GET', '/api/v13/x', status)
        self.assertEqual(ledger.counted_in_cycle(), 4)
        lines = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual(len(lines), 10)
        self.assertEqual(set(lines[0]), {'t', 'scenario', 'method', 'path', 'status', 'kind',
                                         'counted'})
        self.assertEqual(lines[0]['kind'], 'response')

    def test_retry_lines_not_counted_but_kept(self):
        ledger = CallLedger(self.path)
        ledger.record_call('s', 'GET', '/api/v13/x', 200, kind='retry')
        ledger.record_call('s', 'GET', '/api/v13/x', 503, kind='retry')
        ledger.record_call('s', 'GET', '/api/v13/x', 200)
        lines = [json.loads(line) for line in self.path.read_text().splitlines()]
        self.assertEqual([(l['kind'], l['counted']) for l in lines],
                         [('retry', False), ('retry', False), ('response', True)])
        self.assertEqual(ledger.counted_in_cycle(), 1)

    def test_cycle_boundary_excludes_older_lines(self):
        os.environ['TESTBED_CYCLE_START'] = '2026-03-01'
        ledger = CallLedger(self.path)
        self.assertEqual(ledger.cycle_start, date(2026, 3, 1))
        old = datetime(2026, 2, 28, 23, 0, tzinfo=timezone.utc).isoformat()
        new = datetime(2026, 3, 1, 0, 30, tzinfo=timezone.utc).isoformat()
        with self.path.open('w') as f:
            for t in (old, new, new):
                f.write(json.dumps({'t': t, 'scenario': 's', 'method': 'GET', 'path': '/x',
                                    'status': 200, 'kind': 'response', 'counted': True}) + '\n')
        self.assertEqual(ledger.counted_in_cycle(), 2)

    def test_check_refuses_over_budget(self):
        os.environ['TESTBED_BUDGET'] = '10'
        ledger = CallLedger(self.path)
        for _ in range(8):
            ledger.record_call('s', 'GET', '/x', 200)
        ledger.check_budget(2)                       # exactly at the budget is allowed
        with self.assertRaises(BudgetExceeded) as caught:
            ledger.check_budget(3)
        message = str(caught.exception)
        for number in ('8', '3', '10', '150'):
            self.assertIn(number, message)

    def test_check_refuses_over_run_cap(self):
        os.environ['TESTBED_RUN_CAP'] = '5'
        ledger = CallLedger(self.path)
        ledger.check_budget(5)
        with self.assertRaises(BudgetExceeded) as caught:
            ledger.check_budget(6)
        self.assertIn('5', str(caught.exception))
        self.assertIn('250', str(caught.exception))

    def test_corrupt_line_refuses_naming_line(self):
        ledger = CallLedger(self.path)
        ledger.record_call('s', 'GET', '/x', 200)
        with self.path.open('a') as f:
            f.write('{"t": "not a time"\n')
        with self.assertRaises(LedgerUnreadable) as caught:
            ledger.counted_in_cycle()
        self.assertIn('line 2', str(caught.exception))
        with self.assertRaises(LedgerUnreadable):
            ledger.check_budget(1)

    def test_402_raises(self):
        ledger = CallLedger(self.path)
        for status in (200, 401, 429):
            ledger.raise_if_allowance_exhausted(status)
        with self.assertRaises(AllowanceExhausted):
            ledger.raise_if_allowance_exhausted(402)


if __name__ == '__main__':
    unittest.main()
