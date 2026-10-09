"""The call ledger: one line per live Onshape exchange the test bed makes, and the budget.

Onshape counts every 2xx and 3xx response against a small annual allowance, so each live
exchange (every redirect hop, and every retry attempt urllib3 makes) is a line in
state_dir()/ledger.jsonl. Retry attempts are kept but not counted here: the response that
ends a retry chain is recorded on its own. A bad line refuses rather than undercounts.
"""
import json
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from testbed.settings import budget_settings, state_dir

_ledger_lock = threading.Lock()     # one per process, shared by every CallLedger


class LedgerUnreadable(Exception):
    pass


class BudgetExceeded(Exception):
    pass


class AllowanceExhausted(Exception):
    """Onshape answered 402: the annual allowance is used up. Every record command stops."""


def _one_year_before(day):
    try:
        return day.replace(year=day.year - 1)
    except ValueError:                       # 29 February
        return day.replace(year=day.year - 1, day=28)


class CallLedger:
    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else state_dir() / 'ledger.jsonl'
        self.budget, cycle_start, self.per_run_cap = budget_settings()
        self.cycle_start = cycle_start or _one_year_before(date.today())

    def record_call(self, scenario, method, path, status, kind='response'):
        line = {
            't': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'scenario': scenario, 'method': method, 'path': path, 'status': status,
            'kind': kind, 'counted': 200 <= status < 400 and kind == 'response',
        }
        with _ledger_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open('a') as f:
                f.write(json.dumps(line) + '\n')

    def counted_in_cycle(self) -> int:
        if not self.path.exists():
            return 0
        total = 0
        with _ledger_lock:
            lines = self.path.read_text().splitlines()
        for number, text in enumerate(lines, start=1):
            if not text.strip():
                continue
            try:
                entry = json.loads(text)
                when = datetime.fromisoformat(entry['t']).astimezone(timezone.utc).date()
                counted = entry['counted']
            except (ValueError, KeyError, TypeError) as exc:
                raise LedgerUnreadable(
                    f'{self.path} line {number} is unreadable ({type(exc).__name__}); '
                    'fix or remove it rather than undercount') from None
            if counted and when >= self.cycle_start:
                total += 1
        return total

    def check_budget(self, estimate: int) -> None:
        counted = self.counted_in_cycle()
        if counted + estimate > self.budget or estimate > self.per_run_cap:
            raise BudgetExceeded(
                f'{counted} counted calls since {self.cycle_start} plus an estimate of '
                f'{estimate} would pass the budget of {self.budget} '
                f'or the per-run cap of {self.per_run_cap}')

    def raise_if_allowance_exhausted(self, status: int) -> None:
        if status == 402:
            raise AllowanceExhausted('Onshape answered 402 Payment Required: the annual API '
                                     'allowance is used up; every record command stops')
